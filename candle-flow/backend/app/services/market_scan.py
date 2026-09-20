"""全市场基本面排序读层。

数据来源：`factor_snapshots`（盘后由 `app.services.factor_db.build_all()` 预构建，
内容即 `analyze_symbol_full()` 的完整结果）。本模块**只读，不重算任何分数**。

设计要点（为什么这样做）：

1. **排序依据沿用既有 `composite_score`**，它已经包含模块权重、E 档打折、风险乘数、
   公告事件型扣分与各类 veto（`MODULE_WEIGHTS = profitability .20 / growth .16 /
   solvency .16 / cashflow .32 / valuation .16`）。
   若在扫描层另用一套「五维等权 / 百分位加权」重算总分，同一只股票会在
   「个股分析」与「市场扫描」两处得到两个分——违反「同一判定单一来源」铁律。
2. **分位是展示列，不是分数。** 全市场分位 / 行业内分位仅用于横向定位，
   不参与排序、不影响综合分；缺失值不参与排名，也**不赋 50 分中性值**
   （缺失不得被当作真实读数）。
3. **口径必须能自证。** 返回体强制带上 `coverage`（已覆盖 / SH·SZ 总数）与
   `percentile_base`，因为分位只在已覆盖样本内成立，不能对外宣称「全市场」。
4. **只做有取数链路的过滤。** 现有字段里没有「审计意见」「日均成交额」，
   故不提供这两项过滤（做了也会全通过，属假过滤）。
"""

from __future__ import annotations

import logging
import time
from bisect import bisect_left, bisect_right
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import bindparam, text
from sqlalchemy.orm import Session

from app.database import SessionLocal
from app.models.factor_snapshot import FactorSnapshot
from app.models.stock import StockInfo

logger = logging.getLogger(__name__)

# 五维键（英文）→ dim_scores 中文键。与 engine.py:478 的 dim_scores 一一对应。
DIM_KEYS: dict[str, str] = {
    "profitability": "盈利能力",
    "growth": "成长性",
    "cashflow": "现金流质量",
    "solvency": "偿债能力",
    "valuation": "估值合理性",
}
_CN_TO_DIM = {v: k for k, v in DIM_KEYS.items()}

# 与 engine.py 保持一致的权重口径，仅用于对外声明
MODULE_WEIGHTS_NOTE = (
    "盈利能力0.20 / 成长性0.16 / 偿债能力0.16 / 现金流质量0.32 / 估值合理性0.16"
)

# ST / 退市标识（数据源按证券简称判断，StockInfo 与快照 payload 都有 name）
ST_MARKERS: tuple[str, ...] = ("ST", "退")

# 板块口径：默认榜单只看沪深主板，创业板/科创板按需勾选纳入。
# symbol 形如 600519.SH，取前缀判断；主板白名单与 is_main_board() 同源，
# 创业板(300/301/302) + 科创板(688/689) 默认剔除。
GEM_STAR_PREFIXES: tuple[str, ...] = ("300", "301", "302", "688", "689")

DEFAULT_TOP = 50
MAX_TOP = 500


def _symbol_code(symbol: str) -> str:
    """600519.SH → 600519。"""
    return (symbol or "").split(".")[0]


def _is_gem_star(symbol: str) -> bool:
    return _symbol_code(symbol).startswith(GEM_STAR_PREFIXES)

# ── 技术共振叠加层 ──────────────────────────────────────────
# 复用既有内核（不立第二套口径）：
#   形态：app.core.pattern_engine.PatternEngine（Nison 蜡烛 + 西方指标）
#   共振：app.core.confluence.evaluate_confluence（趋势/动量/波动/量价/结构
#         正交加权 + 周线趋势多周期确认 + 软冲突否决）
#   买点：market_confluence_service._detect_buy_signal（MA20/60 + 量比 + PEG）
# 参数与 market_confluence_service 保持一致（KLINE_LIMIT / MIN_BARS / 信号阈值）。
OVERLAY_WORKERS = 8
OVERLAY_KLINE_LIMIT = 90
OVERLAY_MIN_BARS = 40
OVERLAY_RECENT_BARS = 2
# 技术叠加层逐票分析，覆盖「本页全部标的」——上限与榜单单页上限一致(M=500)，
# 不再截断到 120 只（旧行为会让第 121 行之后的技术列整列空白）。
# 靠 _overlay_item_cache 单票缓存摊薄翻页成本：同票 10 分钟内不重复算。
OVERLAY_PAGE_LIMIT = MAX_TOP
OVERLAY_CACHE_TTL_SEC = 600
_overlay_cache: dict[str, Any] = {"ts": 0.0, "key": None, "payload": None}
# symbol → (ts, 技术面结果)：跨页复用，翻页只算新出现的票
_overlay_item_cache: dict[str, tuple[float, dict[str, Any]]] = {}

# ── 双阈值漏斗（共振过滤法）────────────────────────────────
# 口径：基本面 ≥70 且 技术面 ≥70 → 买入候选；两者均 ≥85 → 核心持仓；
#       任一 <60 → 淘汰。阈值可被请求参数覆盖，默认即上述值。
# 「技术面得分」沿用信号页的**共振组合分**（形态分 + 有效共振数×6，见
# market_confluence_service._combined_score），截断到 0~100——不是新造的
# 加权表。方案里的趋势/动量/量价/形态四项已内含在该组合分中
# （core/confluence.py 的维度映射：trend/momentum/volatility/volume/structure），
# 「大盘环境」因本数据源无取数链路不纳入。
# 注意：系统候选门槛 CANDIDATE_COMBINED=80，所以「有达标形态共振」的票
# 技术面得分天然 ≥80；techn_min=70 在本数据上等价于「有形态共振」。
VERDICT_MIN_FUND = 70.0
VERDICT_MIN_TECH = 70.0
VERDICT_CORE_SCORE = 85.0
VERDICT_VETO_SCORE = 60.0
VERDICT_TECH_MAX = 100.0

# verdict → (中文标签, 排序权重)；顺序即优先级
VERDICT_ORDER: dict[str, int] = {"core": 0, "candidate": 1, "watch": 2, "eliminated": 3}
VERDICT_LABELS: dict[str, str] = {
    "core": "核心持仓",
    "candidate": "买入候选",
    "watch": "观察",
    "eliminated": "淘汰",
}

# 「仅看某档」→ 允许的 verdict 集合（「候选及以上」= 核心 + 候选）
VERDICT_FILTERS: dict[str, tuple[str, ...]] = {
    "core": ("core",),
    "candidate_up": ("core", "candidate"),
    "eliminated": ("eliminated",),
}

# ── 共振视图（基本面×技术面，按档位全局排序）────────────────
# 与 technical_overlay（本页叠加）的区别：共振视图要给**全榜单一个全局的
# 档位排序**，因此必须先对「基本面 ≥ 淘汰线」的全部存量快照算完技术面
# （约 2000 只 × ~0.3s，首次需后台 job 构建索引），之后读层只做
# 过滤 / 判档 / 排序 / 分页（毫秒级），换筛选条件不用重算技术面。
# 技术面只依赖 K线与快照字段，不依赖任何筛选条件——这是可以预构建的前提。
RESO_TTL_SEC = 600
_reso_cache: dict[str, Any] = {"ts": 0.0, "items": None, "coverage": None, "stats": None}
_reso_lock = None  # 惰性初始化，避免 import 时创建锁

# 轻量列读取：payload 平均 ≈12KB，全市场约 60MB，只取排序/展示必需字段
_LIGHT_SQL = text(
    """
    SELECT symbol,
           composite_score,
           pe_ttm,
           built_at,
           json_extract(payload, '$.name')                    AS name,
           json_extract(payload, '$.industry')                AS industry,
           json_extract(payload, '$.final_rating')            AS final_rating,
           json_extract(payload, '$.risk_level_label')        AS risk_level_label,
           json_extract(payload, '$.market.market_cap')       AS market_cap,
           json_extract(payload, '$.market.price')            AS price,
           json_extract(payload, '$.market.pb')               AS pb,
           json_extract(payload, '$.market.dividend_yield')   AS dividend_yield,
           json_extract(payload, '$.dim_scores."盈利能力"')   AS dim_profitability,
           json_extract(payload, '$.dim_scores."成长性"')     AS dim_growth,
           json_extract(payload, '$.dim_scores."现金流质量"') AS dim_cashflow,
           json_extract(payload, '$.dim_scores."偿债能力"')   AS dim_solvency,
           json_extract(payload, '$.dim_scores."估值合理性"') AS dim_valuation
    FROM factor_snapshots
    WHERE composite_score IS NOT NULL
    """
)


def _to_float(v: Any) -> float | None:
    if v is None:
        return None
    try:
        x = float(v)
    except (TypeError, ValueError):
        return None
    if x != x:  # NaN
        return None
    return x


def _from_payload(row: FactorSnapshot) -> dict[str, Any]:
    """json_extract 不可用时的回退：Python 侧解析 payload。"""
    import json

    try:
        d = json.loads(row.payload or "{}")
    except (json.JSONDecodeError, TypeError):
        d = {}
    m = d.get("market") or {}
    dims = d.get("dim_scores") or {}
    out: dict[str, Any] = {
        "symbol": row.symbol,
        "composite_score": row.composite_score,
        "pe_ttm": row.pe_ttm,
        "built_at": row.built_at,
        "name": d.get("name"),
        "industry": d.get("industry"),
        "final_rating": d.get("final_rating"),
        "risk_level_label": d.get("risk_level_label"),
        "market_cap": m.get("market_cap"),
        "price": m.get("price"),
        "pb": m.get("pb"),
        "dividend_yield": m.get("dividend_yield"),
    }
    for eng, cn in DIM_KEYS.items():
        out[f"dim_{eng}"] = dims.get(cn)
    return out


def load_covered(db: Session | None = None) -> tuple[list[dict[str, Any]], bool]:
    """读取全部已构建快照的轻量字段。返回 (rows, 是否走了回退路径)。"""
    own = db is None
    db = db or SessionLocal()
    try:
        try:
            res = db.execute(_LIGHT_SQL).mappings().all()
            return [dict(r) for r in res], False
        except Exception as exc:  # pragma: no cover - 依赖 SQLite JSON1 版本
            logger.warning("json_extract 读取失败，回退 Python 解析：%s", exc)
            db.rollback()
            rows = (
                db.query(FactorSnapshot)
                .filter(FactorSnapshot.composite_score.isnot(None))
                .all()
            )
            return [_from_payload(r) for r in rows], True
    finally:
        if own:
            db.close()


def percentile_rank(values: dict[str, float]) -> dict[str, float]:
    """横截面百分位（0~100，越大越好），平均秩处理并列。

    缺失值由调用方排除，不进入本函数——**不赋中性值**。
    """
    if not values:
        return {}
    xs = sorted(values.values())
    n = len(xs)
    out: dict[str, float] = {}
    for key, v in values.items():
        lo = bisect_left(xs, v)
        hi = bisect_right(xs, v)
        out[key] = round((lo + hi) / 2.0 / n * 100.0, 1)
    return out


def market_coverage(db: Session | None = None) -> dict[str, Any]:
    """覆盖率自证：已构建快照数 / SH·SZ 股票总数。

    非披露期 `build_all` 只补缺失条目，且单次受批处理预算截断，
    因此覆盖率天然是渐进的——对外必须显式给出，不得默认已覆盖全市场。
    """
    own = db is None
    db = db or SessionLocal()
    try:
        universe = int(
            db.query(StockInfo).filter(StockInfo.market.in_(("SH", "SZ"))).count()
        )
        covered = int(
            db.query(FactorSnapshot)
            .filter(FactorSnapshot.composite_score.isnot(None))
            .count()
        )
        latest = (
            db.query(FactorSnapshot.built_at)
            .order_by(FactorSnapshot.built_at.desc())
            .limit(1)
            .scalar()
        )
    finally:
        if own:
            db.close()

    age_days: float | None = None
    if latest is not None:
        ref = latest
        if ref.tzinfo is not None:
            ref = ref.astimezone(timezone.utc).replace(tzinfo=None)
        age_days = round(
            (datetime.now(timezone.utc).replace(tzinfo=None) - ref).total_seconds()
            / 86400.0,
            2,
        )

    return {
        "covered": covered,
        "universe": universe,
        "remaining": max(0, universe - covered),
        "coverage_pct": round(covered / universe * 100.0, 1) if universe else 0.0,
        "latest_built_at": latest.isoformat() if latest else None,
        "stale_days": age_days,
        "complete": bool(universe) and covered >= universe,
    }


def _base_items(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """全量轻量行 → 展示条目（含全市场/行业内分位展示列）。

    分位基准 = 全部已覆盖样本（先算分位，再过滤），保证「全市场分位」语义稳定。
    """
    market_pct = percentile_rank(
        {
            r["symbol"]: v
            for r in rows
            if (v := _to_float(r.get("composite_score"))) is not None
        }
    )
    industry_pct: dict[str, dict[str, float]] = {}
    by_industry: dict[str, dict[str, float]] = {}
    for r in rows:
        v = _to_float(r.get("composite_score"))
        ind = str(r.get("industry") or "").strip()
        if v is None or not ind:
            continue
        by_industry.setdefault(ind, {})[r["symbol"]] = v
    for ind, vals in by_industry.items():
        industry_pct[ind] = percentile_rank(vals)

    items: list[dict[str, Any]] = []
    for r in rows:
        name = str(r.get("name") or "")
        comp = _to_float(r.get("composite_score"))
        mcap = _to_float(r.get("market_cap"))
        ind = str(r.get("industry") or "")
        items.append(
            {
                "symbol": r["symbol"],
                "name": name,
                "industry": ind,
                "composite_score": None if comp is None else round(comp, 2),
                "final_rating": r.get("final_rating"),
                "risk_level_label": r.get("risk_level_label"),
                "pe_ttm": _to_float(r.get("pe_ttm")),
                "price": _to_float(r.get("price")),
                "pb": _to_float(r.get("pb")),
                "market_cap_yi": (
                    None if mcap is None else round(mcap / 1e8, 2)
                ),
                "dividend_yield": _to_float(r.get("dividend_yield")),
                "dim_scores": {
                    cn: _to_float(r.get(f"dim_{eng}")) for eng, cn in DIM_KEYS.items()
                },
                "market_pct": market_pct.get(r["symbol"]),
                "industry_pct": (industry_pct.get(ind) or {}).get(r["symbol"]),
            }
        )
    return items


def _item_passes(
    it: dict[str, Any],
    *,
    exclude_st: bool,
    include_gem: bool,
    keyword: str,
    industry: str,
    min_composite: float | None,
    min_market_cap_yi: float | None,
) -> bool:
    """单条目过滤（与 scan_market 的筛选口径一致，共振读层复用）。"""
    name = str(it.get("name") or "")
    if exclude_st and any(mk in name for mk in ST_MARKERS):
        return False
    if not include_gem and _is_gem_star(it["symbol"]):
        return False
    if keyword and keyword not in name.lower() and keyword not in _symbol_code(it["symbol"]).lower():
        return False
    comp = _to_float(it.get("composite_score"))
    if min_composite is not None and (comp is None or comp < min_composite):
        return False
    mcap = _to_float(it.get("market_cap_yi"))
    if min_market_cap_yi is not None:
        if mcap is None or mcap < float(min_market_cap_yi):
            return False
    ind = str(it.get("industry") or "")
    if industry and industry not in ind:
        return False
    return True


def scan_market(
    db: Session | None = None,
    *,
    top: int = DEFAULT_TOP,
    min_composite: float | None = None,
    min_market_cap_yi: float | None = None,
    exclude_st: bool = True,
    industry: str | None = None,
    sort_by: str = "composite_score",
    keyword: str | None = None,
    include_gem: bool = False,
    offset: int = 0,
) -> dict[str, Any]:
    """全市场基本面排序。

    Args:
        top: 单页返回条数（上限 500）。
        min_composite: 综合分下限（沿用个股分析口径的 0~100 分）。
        min_market_cap_yi: 总市值下限，单位**亿元**。
        exclude_st: 剔除证券简称含 ST / 退 的标的（数据源唯一可得的风险剔除项）。
        industry: 行业名包含匹配（如「银行」「煤炭」）。
        sort_by: `composite_score`（默认）或五个维度键之一
            （profitability/growth/cashflow/solvency/valuation），
            维度排序只是展示排序，不合成新总分。
        keyword: 名称/代码包含匹配（如「茅台」「600519」），大小写不敏感。
        include_gem: 是否纳入创业板(300/301/302)与科创板(688/689)，默认剔除。
        offset: 分页起始下标（在**过滤并排序后**的命中集合上偏移），负值按 0 处理。
    """
    rows, fallback = load_covered(db)

    key = (sort_by or "composite_score").strip()
    dim = None
    if key in _CN_TO_DIM:
        dim = _CN_TO_DIM[key]
    elif key in DIM_KEYS:
        dim = key
    elif key != "composite_score":
        raise ValueError(f"不支持的排序字段：{sort_by}")

    base = _base_items(rows)
    ind_filter = (industry or "").strip()
    kw = (keyword or "").strip().lower()
    selected = [
        it
        for it in base
        if _item_passes(
            it,
            exclude_st=exclude_st,
            include_gem=include_gem,
            keyword=kw,
            industry=ind_filter,
            min_composite=min_composite,
            min_market_cap_yi=min_market_cap_yi,
        )
    ]

    if dim is not None:
        cn_key = DIM_KEYS[dim]
        selected.sort(
            key=lambda x: (
                x["dim_scores"][cn_key]
                if x["dim_scores"].get(cn_key) is not None
                else float("-inf")
            ),
            reverse=True,
        )
    else:
        selected.sort(key=lambda x: x["composite_score"] or 0.0, reverse=True)

    limit = max(1, min(int(top), MAX_TOP))
    start = max(0, int(offset or 0))
    window = selected[start : start + limit]
    coverage = market_coverage(db)
    pct_base = sum(
        1 for r in rows if _to_float(r.get("composite_score")) is not None
    )
    notes = [
        f"排序依据为个股分析的综合分（单一权威口径，权重 {MODULE_WEIGHTS_NOTE}），"
        "扫描层不重算分数，不新增第二套权重。",
        "market_pct / industry_pct 为展示用横截面分位（0~100，越高越好），"
        "不参与排序；缺失值不参与排名、也不赋中性分。",
        f"分位基准为已覆盖样本 {pct_base} 只"
        f"（占 SH·SZ {coverage['universe']} 只的 {coverage['coverage_pct']}%），"
        "缺口源于单次因子构建预算截断，未经覆盖的标的不在榜内。",
        "默认仅沪深主板（剔除创业板 300/301/302 与科创板 688/689），勾选「含创业板/科创板」后纳入。",
        "「股价」取自因子快照构建时刻的行情（非实时报价），与快照新鲜度同源。",
        "本数据源无「审计意见」「日均成交额」字段，故未提供这两项过滤。",
    ]
    if fallback:
        notes.append("本次读取走了 Python 解析回退路径（SQLite JSON1 不可用）。")

    return {
        "coverage": coverage,
        "percentile_base": pct_base,
        "count": len(window),
        "matched": len(selected),
        "offset": start,
        "limit": limit,
        "has_more": start + len(window) < len(selected),
        "sort_by": key,
        "filters": {
            "min_composite": min_composite,
            "min_market_cap_yi": min_market_cap_yi,
            "exclude_st": exclude_st,
            "industry": ind_filter or None,
            "keyword": kw or None,
            "include_gem": include_gem,
        },
        "items": window,
        "notes": notes,
    }


def _profit_yoy_map(db: Session | None, symbols: list[str]) -> dict[str, float | None]:
    """从因子快照取净利同比（engine 返回自 2026-09-20 起携带 profit_yoy）。

    旧快照无此键 → 返回 None → PEG 缺失 → 强买入信号保守降级（不虚构增速）。
    """
    if not symbols:
        return {}
    out: dict[str, float | None] = {}
    sess = db or SessionLocal()
    try:
        sql = text(
            "SELECT symbol, json_extract(payload, '$.profit_yoy') FROM factor_snapshots "
            "WHERE symbol IN :syms"
        ).bindparams(bindparam("syms", expanding=True))
        for sym, val in sess.execute(sql, {"syms": list(symbols)}):
            out[sym] = None if val is None else float(val)
    except Exception:
        logger.debug("profit_yoy map query failed", exc_info=True)
    finally:
        if db is None:
            sess.close()
    return out


def _tech_score(combined: float | None) -> int | None:
    """共振组合分 → 0~100 技术面得分。

    只做上限截断，不重标定（避免造第二套技术面权重表）。
    无达标注形态共振 → None（**不可评估**，不赋 0、不赋中性 50）。
    """
    if combined is None:
        return None
    return int(round(max(0.0, min(float(combined), VERDICT_TECH_MAX))))


def _verdict(
    fund: float | None,
    tech: int | None,
    *,
    min_fund: float,
    min_tech: float,
    core: float,
    veto: float,
) -> tuple[str, list[str]]:
    """双阈值漏斗判定：淘汰 → 核心持仓 → 买入候选 → 观察。

    缺失值不当作最差：技术面不可评估（无形态共振）时归入「观察」并说明，
    而不是按 <veto 淘汰。
    """
    if fund is not None and fund < veto:
        return "eliminated", [f"基本面 {fund:.1f} < {veto:g}"]
    if tech is not None and tech < veto:
        return "eliminated", [f"技术面 {tech} < {veto:g}"]
    if fund is not None and tech is not None and fund >= core and tech >= core:
        return "core", [f"基本面 {fund:.1f} ≥ {core:g}", f"技术面 {tech} ≥ {core:g}"]
    if fund is not None and tech is not None and fund >= min_fund and tech >= min_tech:
        return "candidate", [
            f"基本面 {fund:.1f} ≥ {min_fund:g}",
            f"技术面 {tech} ≥ {min_tech:g}",
        ]
    reasons: list[str] = []
    if fund is None:
        reasons.append("基本面分缺失")
    elif fund < min_fund:
        reasons.append(f"基本面 {fund:.1f} < {min_fund:g}")
    if tech is None:
        reasons.append("技术面不可评估（近 2 根K线无达标注形态共振）")
    elif tech < min_tech:
        reasons.append(f"技术面 {tech} < {min_tech:g}")
    return "watch", reasons or ["未同时满足双阈值"]


def _overlay_one(
    symbol: str,
    name: str,
    fund_score: float | None,
    pe: float | None,
    profit_yoy: float | None,
) -> dict[str, Any]:
    """单票技术面叠加：K线买点信号（全票）+ 形态共振（达标才有）。

    复用 MarketConfluenceService._scan_job（PatternEngine + evaluate_confluence +
    候选门槛），保证「主板战法 / 信号页」与本榜单的形态共振口径逐位一致。
    """
    from app.services.kline_service import KlineService
    from app.services.market_confluence_service import (
        KLINE_LIMIT,
        _calc_peg,
        _detect_buy_signal,
        MarketConfluenceService,
    )
    from app.services.market_confluence_service import _Job

    empty = {
        "symbol": symbol,
        "name": name,
        "kline_bars": 0,
        "buy_signal": "insufficient_data",
        "buy_label": "无K线数据",
        "buy_reasons": [],
        "pattern_name": None,
        "pattern_score": None,
        "confluence_effective": None,
        "confluence_hits": None,
        "combined_score": None,
        "tech_score": None,
    }
    sess = SessionLocal()
    try:
        klines, _kmeta = KlineService(sess).get_recent_klines(symbol, limit=KLINE_LIMIT)
        if len(klines) < OVERLAY_MIN_BARS:
            empty["buy_label"] = "K线不足40根"
            return empty
        # 买点信号：MA20/MA60 + 量比 + PEG（_detect_buy_signal 对 peg=None 已保守处理）
        peg = _calc_peg(pe, profit_yoy)
        buy = _detect_buy_signal(klines, float(fund_score or 0), peg, pe)
        # 形态共振：bullish 形态 + 共振达标 + 近 2 根K线（_scan_job 内部自会过滤）
        svc = MarketConfluenceService(sess)
        best, _outcome = svc._scan_job(_Job(symbol=symbol, name=name, recent_bars=OVERLAY_RECENT_BARS))
        return {
            "symbol": symbol,
            "name": name,
            "kline_bars": len(klines),
            "buy_signal": buy.get("signal"),
            "buy_label": buy.get("label"),
            "buy_reasons": buy.get("reasons") or [],
            "buy_note": buy.get("note"),
            "peg": peg,
            "pattern_name": (best or {}).get("pattern_name"),
            "pattern_score": (best or {}).get("pattern_score"),
            "confluence_effective": (best or {}).get("confluence_effective"),
            "confluence_hits": (best or {}).get("confluence_hits"),
            "confluence_detail": (best or {}).get("confluence_detail"),
            "combined_score": (best or {}).get("combined_score"),
            "tech_score": _tech_score((best or {}).get("combined_score")),
            "pattern_date": (best or {}).get("candle_date"),
        }
    except Exception:
        logger.debug("technical overlay failed for %s", symbol, exc_info=True)
        empty["buy_label"] = "分析失败"
        # 临时性失败（多为 SQLite 写锁竞争）不得进入 10 分钟缓存，
        # 否则一张票整个缓存周期都会显示「分析失败」；由调用方重试。
        empty["failed"] = True
        return empty
    finally:
        sess.close()


def _compute_tech_map(
    pool: list[dict[str, Any]],
    py_map: dict[str, float | None],
    *,
    force: bool = False,
    progress: Any = None,
) -> dict[str, dict[str, Any]]:
    """对给定标的池计算技术面（带单票缓存 + 失败串行重试）。

    返回 ({symbol: overlay_fields}, 重试票数, 本次新算票数)。失败的票带
    ``failed=True`` 且**不写缓存**（多为盘后批跑期间的 SQLite 写锁竞争，属瞬时状态）。
    """
    from concurrent.futures import ThreadPoolExecutor, as_completed

    now = time.time()
    results: dict[str, dict[str, Any]] = {}
    todo: list[dict[str, Any]] = []
    retry_count = 0
    for it in pool:
        hit = _overlay_item_cache.get(it["symbol"])
        if hit is not None and now - hit[0] < OVERLAY_CACHE_TTL_SEC and not force:
            results[it["symbol"]] = hit[1]
        else:
            todo.append(it)

    def _job(it: dict[str, Any]) -> dict[str, Any]:
        sym = it["symbol"]
        return _overlay_one(
            sym,
            it.get("name") or "",
            it.get("composite_score"),
            it.get("pe_ttm"),
            py_map.get(sym),
        )

    if todo:
        done = 0
        with ThreadPoolExecutor(max_workers=OVERLAY_WORKERS) as ex:
            futures = {ex.submit(_job, it): it["symbol"] for it in todo}
            for fut in as_completed(futures):
                sym = futures[fut]
                try:
                    res = fut.result(timeout=60.0)
                except Exception:
                    logger.debug("overlay job failed for %s", sym, exc_info=True)
                    res = {
                        "symbol": sym, "name": "", "kline_bars": 0,
                        "buy_signal": "insufficient_data", "buy_label": "分析失败",
                        "buy_reasons": [], "pattern_name": None, "failed": True,
                    }
                results[sym] = res
                if not res.get("failed"):
                    _overlay_item_cache[sym] = (now, res)
                done += 1
                if progress:
                    progress(done, len(todo), "tech")

        # 失败票串行补一次：并发争锁是瞬时状态，重试通常即可成功；
        # 仍失败则如实保留「分析失败」，但不缓存。
        retried = [it for it in todo if (results.get(it["symbol"]) or {}).get("failed")]
        retry_count = len(retried)
        if retried:
            logger.info("technical overlay: retrying %d symbols after failure", len(retried))
            for it in retried:
                res = _job(it)
                results[it["symbol"]] = res
                if not res.get("failed"):
                    _overlay_item_cache[it["symbol"]] = (now, res)

        # 防止缓存无限增长（全市场也就 5k 量级，留一倍余量足够）
        if len(_overlay_item_cache) > 12000:
            cutoff = now - OVERLAY_CACHE_TTL_SEC
            for k in [k for k, v in _overlay_item_cache.items() if v[0] < cutoff]:
                _overlay_item_cache.pop(k, None)

    return results, retry_count, len(todo)


def technical_overlay(
    db: Session | None = None,
    *,
    top: int = 60,
    min_composite: float | None = None,
    min_market_cap_yi: float | None = None,
    exclude_st: bool = True,
    industry: str | None = None,
    sort_by: str = "composite_score",
    keyword: str | None = None,
    include_gem: bool = False,
    offset: int = 0,
    min_fund: float = VERDICT_MIN_FUND,
    min_tech: float = VERDICT_MIN_TECH,
    core_score: float = VERDICT_CORE_SCORE,
    veto_score: float = VERDICT_VETO_SCORE,
    force: bool = False,
) -> dict[str, Any]:
    """榜单技术共振叠加（三层漏斗的第二、三层 + 双阈值决策标签）。

    第一层 = scan_market 的基本面榜单（同一口径，本函数内部复用）；
    第二层 = K线买点信号（趋势 MA20/60 + 量价 + PEG，_detect_buy_signal）；
    第三层 = 形态共振（bullish 形态 × 趋势/动量/波动/量价/结构正交共振 +
    周线趋势多周期确认，evaluate_confluence；无达标形态即为「无共振」）。

    共振得分沿用 `_combined_score = 形态分 + 有效共振数×6`（与信号页同源），
    不采用「趋势×1.5+动量×1.2」式第二套权重。

    **双阈值决策（共振过滤法，不改任何分数）**：
    基本面 ≥min_fund 且 技术面 ≥min_tech → 买入候选；
    两者均 ≥core_score → 核心持仓；任一 <veto_score → 淘汰；其余「观察」。
    判定只写 `verdict` 字段，`composite_score` 与技术面各原始读数均不改写。

    **覆盖范围 = 当前榜单页的全部标的**（offset/top 与榜单请求一致），
    不再截断到前若干名；逐票结果进 `_overlay_item_cache`，翻页时已算过的
    标的直接复用，因此「全榜单都要技术分析」只付一次成本。
    """
    base = scan_market(
        db,
        top=min(max(1, int(top)), OVERLAY_PAGE_LIMIT),
        min_composite=min_composite,
        min_market_cap_yi=min_market_cap_yi,
        exclude_st=exclude_st,
        industry=industry,
        sort_by=sort_by,
        keyword=keyword,
        include_gem=include_gem,
        offset=offset,
    )
    pool = base["items"]

    cache_key = (
        tuple(sorted((base.get("filters") or {}).items())),
        base.get("sort_by"),
        base.get("offset"),
        len(pool),
        tuple(it["symbol"] for it in pool),
        (min_fund, min_tech, core_score, veto_score),
    )
    now = time.time()
    if (
        not force
        and _overlay_cache["payload"] is not None
        and _overlay_cache["key"] == cache_key
        and now - float(_overlay_cache["ts"]) < OVERLAY_CACHE_TTL_SEC
    ):
        cached = dict(_overlay_cache["payload"])
        cached["cached"] = True
        return cached

    py_map = _profit_yoy_map(db, [it["symbol"] for it in pool])
    results, retry_count, computed_count = _compute_tech_map(pool, py_map, force=force)

    items: list[dict[str, Any]] = []
    for it in pool:
        merged = dict(it)
        merged.update(results.get(it["symbol"]) or {})
        v, v_reasons = _verdict(
            merged.get("composite_score"),
            merged.get("tech_score"),
            min_fund=min_fund,
            min_tech=min_tech,
            core=core_score,
            veto=veto_score,
        )
        merged["verdict"] = v
        merged["verdict_label"] = VERDICT_LABELS.get(v, v)
        merged["verdict_reasons"] = v_reasons
        items.append(merged)

    # 本页技术分析结果（供 stats 自证「覆盖了多少」）
    page_items = items

    # 决策分布（基于本页全部标的；前端「仅看某档」在本页内过滤，不再二次请求）
    verdict_counts: dict[str, int] = {k: 0 for k in VERDICT_ORDER}
    for it in page_items:
        key = str(it.get("verdict"))
        verdict_counts[key] = verdict_counts.get(key, 0) + 1

    # 行业共振：同一板块多只出现买点信号 → 板块效应统计（仅展示，不改个股信号）
    industry_stats: dict[str, dict[str, int]] = {}
    for it in page_items:
        ind = str(it.get("industry") or "").strip()
        sig = it.get("buy_signal")
        if not ind or sig not in ("strong_buy", "watch", "short_term"):
            continue
        bucket = industry_stats.setdefault(ind, {"total": 0, "strong_buy": 0, "signaled": 0})
        bucket["total"] += 1
        bucket["signaled"] += 1
        if sig == "strong_buy":
            bucket["strong_buy"] += 1
    industry_confluence = [
        {"industry": k, **v}
        for k, v in sorted(industry_stats.items(), key=lambda kv: -kv[1]["signaled"])
        if v["signaled"] >= 2
    ]

    sig_counts: dict[str, int] = {}
    for it in page_items:
        s = str(it.get("buy_signal") or "unknown")
        sig_counts[s] = sig_counts.get(s, 0) + 1
    peg_available = sum(1 for it in page_items if it.get("peg") is not None)

    payload = {
        "count": len(items),
        "items": items,
        "offset": base.get("offset"),
        "matched": base.get("matched"),
        "has_more": base.get("has_more"),
        "page_size": len(page_items),
        "verdict_counts": verdict_counts,
        "verdict_thresholds": {
            "min_fund": min_fund,
            "min_tech": min_tech,
            "core": core_score,
            "veto": veto_score,
        },
        "signal_counts": sig_counts,
        "industry_confluence": industry_confluence,
        "stats": {
                "analyzed": len(page_items),
                "computed": computed_count,
            "retried": retry_count,
            "failed": sum(1 for it in page_items if it.get("failed")),
            "kline_ok": sum(1 for it in page_items if (it.get("kline_bars") or 0) >= OVERLAY_MIN_BARS),
            "pattern_hits": sum(1 for it in page_items if it.get("pattern_name")),
            "peg_available": peg_available,
            "peg_note": (
                "快照均携带净利同比，PEG 完整"
                if peg_available == len(page_items)
                else f"仅 {peg_available}/{len(page_items)} 只快照携带净利同比（旧快照未落库该字段），"
                "PEG 缺失的票按保守口径不触发强买入；明日盘后增量批跑后补齐"
            ),
        },
        "filters": base.get("filters"),
        "sort_by": base.get("sort_by"),
        "coverage": base.get("coverage"),
        "cached": False,
        "notes": [
            f"技术分析覆盖本页全部 {len(page_items)} 只（含无K线者的「数据不足」判定），"
            "不按名次截断；同票 10 分钟内结果复用，翻页只新增计算未出现过的标的。",
            "分析失败（多为盘后批跑期间的数据库写锁竞争）会串行重试一次，且不写入缓存，"
            "下一次请求即可恢复，不会在缓存周期内固定在「分析失败」。",
            "买点信号口径：强买入=基本面≥80 + PEG<1.5 + 站上MA20 + 近5日量能较20日均量放大20%+；"
            "观察=基本面≥80 + PEG>2 + 回踩MA60(±3%)缩量；短线博弈=基本面<60 但突破MA20且放量。",
            "形态共振口径：Nison 蜡烛+西方指标 bullish 形态（PatternEngine≥60 分），"
            "叠加趋势/动量/波动/量价/结构正交共振（含周线趋势多周期确认），"
            "组合分 = 形态分 + 有效共振数×6，与「主板战法 / 信号页」同源。",
            "行业共振为板块效应统计（同板块≥2 只出现信号才列出），不改变个股信号。",
            f"决策标签（共振过滤法）：基本面 ≥{min_fund:g} 且 技术面 ≥{min_tech:g} → 买入候选；"
            f"两者均 ≥{core_score:g} → 核心持仓；任一 <{veto_score:g} → 淘汰；其余为观察。"
            "标签只做分类，`composite_score` 与技术面原始读数均不改写，不产生第二个综合分。",
            f"技术面得分 = 信号页共振组合分（形态分 + 有效共振数×6）截断到 0~100，"
            "无达标形态共振时为缺失（不可评估，不赋 0、不赋中性）；"
            "系统候选门槛为 80，故有形态共振者天然 ≥80，"
            f"因此 min_tech={min_tech:g} 在本数据上等价于「有达标形态共振」，真正的区分线是 {core_score:g}。"
            "「大盘环境」与「资金面」因本数据源无取数链路，未纳入技术面得分。",
        ],
    }
    _overlay_cache["ts"] = now
    _overlay_cache["key"] = cache_key
    _overlay_cache["payload"] = payload
    return payload


# ── 共振视图：索引构建（后台 job）+ 读层（过滤/判档/排序/分页）──


def resonance_index_age() -> float | None:
    """索引年龄（秒）；未构建返回 None。"""
    if _reso_cache["items"] is None:
        return None
    return max(0.0, time.time() - float(_reso_cache["ts"]))


def resonance_index_build(
    progress: Any = None,
    *,
    force: bool = False,
) -> dict[str, Any]:
    """构建共振索引：对「基本面 ≥ 淘汰线」的全部存量快照算技术面。

    技术面只依赖 K线与快照自身字段，与任何筛选条件无关，因此可以一次构建、
    读层任意组合过滤（keyword/行业/市值/板块）都不必重算。fund < 淘汰线的
    标的无须技术面即可判「淘汰」（双阈值规则决定），不进计算池。

    构建结果整体缓存 RESO_TTL_SEC；单票结果另进 `_overlay_item_cache`，
    过期重建时只重算真正过期的票。
    """
    import threading

    global _reso_lock
    if _reso_lock is None:
        _reso_lock = threading.Lock()

    with _reso_lock:
        now = time.time()
        if (
            not force
            and _reso_cache["items"] is not None
            and now - float(_reso_cache["ts"]) < RESO_TTL_SEC
        ):
            return {"cached": True, **(_reso_cache["stats"] or {})}

        rows, _fallback = load_covered()
        coverage = market_coverage()
        base = _base_items(rows)
        # 只给 fund ≥ 淘汰线的票算技术面；更低的票由规则直接淘汰
        pool = [
            it
            for it in base
            if (it.get("composite_score") or 0.0) >= VERDICT_VETO_SCORE
        ]
        if progress:
            progress(0, len(pool), "tech")
        py_map = _profit_yoy_map(None, [it["symbol"] for it in pool])
        results, retry_count, _computed = _compute_tech_map(
            pool, py_map, force=force, progress=progress
        )

        items: list[dict[str, Any]] = []
        for it in base:
            merged = dict(it)
            res = results.get(it["symbol"])
            if res is not None:
                merged.update(res)
            items.append(merged)

        failed = sum(1 for it in items if it.get("failed"))
        computed = len(results)
        stats = {
            "total": len(items),
            "tech_needed": len(pool),
            "tech_computed": computed,
            "tech_failed": failed,
            "retried": retry_count,
            "duration_sec": round(time.time() - now, 1),
        }
        _reso_cache.update(
            {"ts": time.time(), "items": items, "coverage": coverage, "stats": stats}
        )
        logger.info(
            "resonance index built: %s", stats,
        )
        return {"cached": False, **stats}


def resonance_view(
    db: Session | None = None,
    *,
    top: int = DEFAULT_TOP,
    offset: int = 0,
    min_composite: float | None = None,
    min_market_cap_yi: float | None = None,
    exclude_st: bool = True,
    industry: str | None = None,
    keyword: str | None = None,
    include_gem: bool = False,
    min_fund: float = VERDICT_MIN_FUND,
    min_tech: float = VERDICT_MIN_TECH,
    core_score: float = VERDICT_CORE_SCORE,
    veto_score: float = VERDICT_VETO_SCORE,
    verdict_filter: str = "",
) -> dict[str, Any]:
    """共振视图读层：基本面×技术面双阈值判档 + **按档位全局排序**。

    排序不再以基本面（综合分）为主：核心持仓 → 买入候选 → 观察 → 淘汰；
    同档内按技术面得分降序（缺失排后）、再按综合分降序。
    档位判定沿用 `_verdict`（与 technical_overlay 同一函数，不出现第二套规则），
    分数一律不改写。

    索引未构建时抛 RuntimeError（由路由层转成 ``{"empty": True}`` 提示前端
    触发后台构建）。索引技术面覆盖 fund ≥ 默认淘汰线(60) 的全部标的；
    若请求把 veto 放宽到 60 以下，fund ∈ [veto, 60) 的票无技术面、按缺失
    归入「观察」并在 notes 里说明。
    """
    if verdict_filter and verdict_filter not in VERDICT_FILTERS:
        raise ValueError(f"不支持的 verdict_filter：{verdict_filter}")
    items = _reso_cache["items"]
    if items is None:
        raise RuntimeError("共振索引未构建，请先触发后台构建")

    age = resonance_index_age() or 0.0
    kw = (keyword or "").strip().lower()
    ind_filter = (industry or "").strip()

    counts: dict[str, int] = {k: 0 for k in VERDICT_ORDER}
    matched: list[dict[str, Any]] = []
    for raw in items:
        if not _item_passes(
            raw,
            exclude_st=exclude_st,
            include_gem=include_gem,
            keyword=kw,
            industry=ind_filter,
            min_composite=min_composite,
            min_market_cap_yi=min_market_cap_yi,
        ):
            continue
        v, reasons = _verdict(
            raw.get("composite_score"),
            raw.get("tech_score"),
            min_fund=min_fund,
            min_tech=min_tech,
            core=core_score,
            veto=veto_score,
        )
        counts[v] += 1
        if verdict_filter and v not in VERDICT_FILTERS[verdict_filter]:
            continue
        row = dict(raw)
        row["verdict"] = v
        row["verdict_label"] = VERDICT_LABELS.get(v, v)
        row["verdict_reasons"] = reasons
        matched.append(row)

    def _sort_key(r: dict[str, Any]) -> tuple:
        tech = r.get("tech_score")
        comp = _to_float(r.get("composite_score")) or 0.0
        return (
            VERDICT_ORDER.get(r["verdict"], 99),
            -(tech if tech is not None else -1),
            -comp,
        )

    matched.sort(key=_sort_key)

    limit = max(1, min(int(top), MAX_TOP))
    start = max(0, int(offset or 0))
    window = matched[start : start + limit]
    coverage = dict(_reso_cache["coverage"] or {})
    notes = [
        "排序依据为「基本面×技术面共振档位」：核心持仓 → 买入候选 → 观察 → 淘汰，"
        "不再按基本面综合分排序；同档内按技术分降序、综合分降序。",
        f"决策标签（共振过滤法）：基本面 ≥{min_fund:g} 且 技术面 ≥{min_tech:g} → 买入候选；"
        f"两者均 ≥{core_score:g} → 核心持仓；任一 <{veto_score:g} → 淘汰；其余为观察。"
        "标签只做分类，综合分与技术面原始读数均不改写，不产生第二个综合分。",
        "技术面得分 = 信号页共振组合分（形态分 + 有效共振数×6）截断 0~100，与信号页同源；"
        "无达标形态共振为缺失（不可评估，不赋 0），缺失归入「观察」。",
        "档位分布统计基于当前筛选条件命中的全部标的（非仅本页）；"
        "「仅看某档」为服务端过滤，翻页 / 计数口径一致。",
    ]
    stats = dict(_reso_cache["stats"] or {})
    if veto_score < VERDICT_VETO_SCORE:
        notes.append(
            f"索引技术面覆盖范围为基本面 ≥{VERDICT_VETO_SCORE:g} 的标的；"
            f"本次淘汰线放宽到 {veto_score:g}，基本面 ∈ [{veto_score:g}, {VERDICT_VETO_SCORE:g}) "
            "的票无技术面读数，按缺失归入「观察」（不会误淘汰）。"
        )
    if stats.get("tech_failed"):
        notes.append(
            f"索引中有 {stats['tech_failed']} 只技术面分析失败（瞬时写锁竞争），"
            "按缺失归入「观察」；重建索引即可补齐。"
        )
    return {
        "count": len(window),
        "matched": len(matched),
        "offset": start,
        "limit": limit,
        "has_more": start + len(window) < len(matched),
        "items": window,
        "verdict_counts": counts,
        "verdict_thresholds": {
            "min_fund": min_fund,
            "min_tech": min_tech,
            "core": core_score,
            "veto": veto_score,
        },
        "verdict_filter": verdict_filter or None,
        "index_age_sec": round(age, 1),
        "index_stale": age > RESO_TTL_SEC,
        "index_stats": stats,
        "coverage": coverage,
        "filters": {
            "min_composite": min_composite,
            "min_market_cap_yi": min_market_cap_yi,
            "exclude_st": exclude_st,
            "industry": ind_filter or None,
            "keyword": kw or None,
            "include_gem": include_gem,
        },
        "notes": notes,
    }
