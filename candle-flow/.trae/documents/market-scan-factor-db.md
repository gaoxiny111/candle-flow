# 市场扫描优化：盘后预构建因子库

## Context

当前市场扫描在 `_compute_fundamental_score` 中对每只股票调用 `analyze_symbol_full`（含 `run_full_analysis`），需要实时拉取财报数据 + 估值数据 + 风险事件 + 运行 7 个分析模块 + 估值模型。冷启动 728 只股票需 ~84s（8 workers 并行），依赖 6h TTL 进程内缓存缓解二次扫描。

**核心问题**：财报数据每季度才变，但每次扫描都重新拉取 + 计算全部因子。用户提出"盘后预构建因子库 + 盘中零 API 扫描"架构，将全量分析结果预存 SQLite，扫描时直接读取。

**预期效果**：扫描 fundamentals 阶段从 84s → <1s（纯 DB 读），且不再依赖进程内缓存。

## 方案

### 1. 新建 SQLAlchemy 模型 `FactorSnapshot`

文件：`backend/app/models/factor_snapshot.py`

```python
class FactorSnapshot(Base):
    __tablename__ = "factor_snapshots"
    symbol: Mapped[str] = mapped_column(String(20), primary_key=True)
    composite_score: Mapped[float | None] = mapped_column(Float, index=True)
    pe_ttm: Mapped[float | None] = mapped_column(Float, nullable=True)
    payload: Mapped[str] = mapped_column(Text, nullable=False)  # run_full_analysis 全量 JSON
    built_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now(), index=True)
```

- JSON Blob 存储 `run_full_analysis` 完整返回 dict — 对字段演进零成本
- `composite_score` / `pe_ttm` 提为索引列，供 prescreen 快速过滤
- 在 `app/models/__init__.py` 注册，`init_db()` 自动 `create_all`

### 2. 新建 `app/services/factor_db.py`

核心函数：

```python
def is_stale(symbol: str, *, ttl_days: int = 30) -> bool:
    """查 built_at 是否超过 TTL 且处于披露窗口期"""

def _is_in_disclosure_window(now=None) -> bool:
    """1-4月年报/一季报、7-10月半年报/三季报窗口返回 True"""

def get(symbol: str) -> dict | None:
    """从 DB 读取并反序列化 payload；缺失返回 None"""

def get_many(symbols: list[str]) -> dict[str, dict]:
    """单次 SELECT IN 批量读取"""

def upsert(symbol: str, report: dict) -> None:
    """写入/更新单条因子快照"""

def build_all(*, force: bool = False) -> dict:
    """批量构建全市场因子库"""
    # 1. 从 StockInfo 拉 SH/SZ 主板非 ST 全集
    # 2. 若 not force and not _is_in_disclosure_window()：只补缺失/过期条目
    # 3. ThreadPoolExecutor(max_workers=8, per_stock_timeout=20s, batch_deadline=1800s)
    # 4. 每只调 analyze_symbol_full(use_cache=True) → upsert
    # 5. 返回 {built, failed, skipped, duration_sec}
```

- 非披露期：仅补建缺失的条目（新上市股、之前失败的）
- 披露窗口期：全量重建（财报数据已更新）
- 复用 `analyze_symbol_full` 获取完整 report，直接 JSON 存入

### 3. APScheduler 集成

文件：`backend/app/services/main_board_kline_sync.py` L267-278

在 `_scheduled_sync` 末尾（bull tactics 之后）追加：

```python
try:
    from app.services.factor_db import build_all
    stats = build_all()
    logger.info("scheduled factor build: %s", stats)
except Exception:
    logger.exception("scheduled factor build failed")
```

利用已有 16:35 周一至五 cron（盘后），K 线同步 + 战法扫描后紧接着构建因子库。

### 4. 改造 `_compute_fundamental_score`

文件：`backend/app/services/market_confluence_service.py` L228-250

```python
def _compute_fundamental_score(symbol: str) -> dict[str, Any]:
    # 优先读因子库（毫秒级）
    try:
        from app.services.factor_db import get, is_stale
        if not is_stale(symbol):
            cached = get(symbol)
            if cached:
                return _extract_fundamental_fields(cached)
    except Exception:
        pass
    # 回退：现有 analyze_symbol_full 路径
    try:
        from app.analysis.engine import analyze_symbol_full
        result = analyze_symbol_full(db=None, symbol=symbol, use_cache=True)
        return _extract_fundamental_fields(result)
    except Exception as exc:
        logger.debug("fundamental analysis failed for %s: %s", symbol, exc)
        return {"score": None, "error": str(exc)}

def _extract_fundamental_fields(result: dict) -> dict[str, Any]:
    """从 run_full_analysis 返回 dict 中提取扫描所需字段"""
    composite = result.get("composite_score")
    if composite is None:
        return {"score": None, "error": result.get("error", "no_composite_score")}
    modules = result.get("modules") or {}
    market = result.get("market") or {}
    return {
        "score": float(composite),
        "profitability": (modules.get("profitability") or {}).get("score"),
        "growth": (modules.get("growth") or {}).get("score"),
        "cashflow": (modules.get("cashflow") or {}).get("score"),
        "valuation_score": (modules.get("valuation") or {}).get("score"),
        "pe_ttm": market.get("pe_ttm"),
    }
```

- 提取逻辑抽成 `_extract_fundamental_fields`，两条路径共用
- 因子库 miss/stale 时无缝回退到 `analyze_symbol_full`
- 24 个 pytest 不受影响（测试环境中 factor_snapshots 表为空 → `is_stale` 返回 True → 走回退路径）

### 5. 手动重建 API

文件：`backend/app/api/v1/fundamentals.py`

```python
@router.post("/fundamentals/factors/rebuild")
def rebuild_factors(force: bool = False, db: Session = Depends(get_db)):
    from app.services.factor_db import build_all
    stats = build_all(force=force)
    return ApiResponse(data=stats)
```

- ops 调试用，可强制全量重建
- 复用现有 `fundamentals.py` router 和 `ApiResponse` schema

## 改动文件清单

| 文件 | 操作 | 说明 |
|---|---|---|
| `backend/app/models/factor_snapshot.py` | 新建 | FactorSnapshot ORM 模型 |
| `backend/app/models/__init__.py` | 改 | 注册 FactorSnapshot |
| `backend/app/services/factor_db.py` | 新建 | 因子库核心服务 |
| `backend/app/services/market_confluence_service.py` L228-250 | 改 | _compute_fundamental_score 加因子库优先路径 |
| `backend/app/services/main_board_kline_sync.py` L267-278 | 改 | _scheduled_sync 末尾加 build_all |
| `backend/app/api/v1/fundamentals.py` | 改 | 加 rebuild API endpoint |

## 验证步骤

1. **单元测试**：`pytest tests/test_market_confluence.py -v`（24 用例全绿，回退路径保证兼容）
2. **因子库构建**：`POST /api/v1/fundamentals/factors/rebuild?force=true` → 返回 `{built: N, failed: M, duration_sec: X}`
3. **扫描对比**：
   - 清空因子库 → `POST /signals/scan/market` → 记录 fundamentals 阶段耗时（回退路径 ~84s）
   - 构建因子库 → `POST /signals/scan/market` → fundamentals 阶段应 < 1s
4. **部署到服务器**：`push.ps1 -Server 47.100.175.214` → SSH 触发 rebuild → 验证扫描秒级完成
