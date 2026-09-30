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
import threading
import time
from bisect import bisect_left, bisect_right
from collections import Counter
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from statistics import median
from typing import Any

from sqlalchemy import bindparam, text
from sqlalchemy.orm import Session

from app.database import SessionLocal
from app.models.factor_snapshot import FactorSnapshot
from app.models.stock import StockInfo
from app.services import dividend_store
from app.services.market_snapshot import market_snapshot as _market_snapshot
# 强周期行业（精确匹配，28 行业 / 919 只）。**不能用子串关键词表**：
# 线上行业名来自申万三级（特钢Ⅱ/化学原料/工业金属…），「特钢Ⅱ」里根本没有
# 「钢铁」二字，子串表只能命中 1.6%。见 engine.STRONG_CYCLICAL_INDUSTRIES。
from app.analysis.engine import STRONG_CYCLICAL_INDUSTRIES

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

# 行情字段排序键（读层展示口径，与五维互斥）：键 = _base_items 条目上的字段名。
# 仅这些字段支持 sort_order 双向（升/降）；综合分与五维固定降序。
# 股息率降序 = 高息在前（红利视角）；PE/PB 升序 = 便宜在前（价值视角）。
EXTRA_SORT_KEYS: dict[str, str] = {
    "dividend_yield": "股息率",
    "pe_ttm": "PE",
    "pb": "PB",
    "market_cap_yi": "市值",
    # 真高股息综合评分（池内分位归一，见 apply_hd_scores）：只对高股息命中集
    # 有值。按它排序 = 只看命中集，故 sort_by=hd_score 会**隐含** high_dividend
    # 过滤（未筛的票没有分，硬按分排会把整张榜单打乱）。
    "hd_score": "真高股息评分",
}

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
# K 线窗口不在此处另设常量：统一取 market_confluence_service.KLINE_LIMIT（180），
# 避免「榜单用 90、信号页用 180」这类同源不同值的分叉。
OVERLAY_WORKERS = 8
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
           json_extract(payload, '$.dim_scores."估值合理性"') AS dim_valuation,
           -- 分类准入门槛（_admission_gate）所需字段：四类资产各用各的门槛，
           -- 不能一套绝对阈值一刀切（见 ADMISSION_* 说明）。
           json_extract(payload, '$.valuation.is_dividend_asset')             AS is_dividend_asset,
           json_extract(payload, '$.valuation.growth_stock_profile.is_growth_stock') AS is_growth_stock,
           json_extract(payload, '$.market.pe_percentile')                    AS pe_percentile,
           json_extract(payload, '$.valuation.dividend_profile.payout_ratio_pct') AS payout_ratio_pct,
           -- ROE 恒为盈利能力指标数组第 0 项、总资产周转率恒为效率模块第 0 项
           -- （实测 5254 只快照位置稳定；见 601006/600722 抽查）。
           json_extract(payload, '$.modules.profitability.indicators[0].value') AS roe_pct,
           json_extract(payload, '$.modules.efficiency.indicators[0].value')    AS asset_turnover,
           -- 毛利率：盈利能力数组第 1 项（ROE/毛利率/ROIC/净利率 固定顺序）
           json_extract(payload, '$.modules.profitability.indicators[1].value') AS gross_margin_pct,
           json_extract(payload, '$.revenue_yoy')                             AS revenue_yoy,
           json_extract(payload, '$.profit_yoy')                              AS profit_yoy,
           -- 大股东减持窗口期：供买点信号封顶（强买入→观察）使用。
           -- 只改档位不改分数，因此不参与任何重算，仅随快照透传。
           json_extract(payload, '$.major_risks.reduce_window')               AS reduce_window,
           -- ── 质量×价值视图（quality_value.py）所需字段 ──────────────
           -- 铁律 17：新增读层字段必须同时改 _LIGHT_SQL + _from_payload +
           -- 使用处（漏 SQL 恒 None → 判据形同虚设；漏回退 → 旧路径炸）。
           json_extract(payload, '$.market.pb_percentile')                    AS pb_percentile,
           -- ROIC 固定为盈利能力指标数组第 2 项（ROE/毛利率/ROIC/净利率/股息率）
           json_extract(payload, '$.modules.profitability.indicators[2].value') AS roic_pct,
           -- 资产负债率固定为偿债能力数组第 0 项
           json_extract(payload, '$.modules.solvency.indicators[0].value')    AS debt_ratio_pct,
           -- 经营现金流/净利润：取【5 年均值】(第 0 项)，**不是**第 1 项的年报值。
           -- 年报单期值行业间极性相反（江西铜业 -0.97 / 中国神华 1.42），
           -- 用单年会误杀正常经营的周期股；5 年均值口径更稳（见 quality_value）。
           json_extract(payload, '$.modules.cashflow.indicators[0].value')    AS ocf_np_5y,
           -- PEG：valuation.relative.PEG.value（红利资产被口径置空 → 恒 None）
           json_extract(payload, '$.valuation.relative.PEG.value')            AS peg,
           -- 展示分成长维（qv）：compute_qv 读 profit_yoy_pct，快照顶层键是
           -- profit_yoy —— 旧实现漏了这条映射 → growth_pct 恒中性 50（铁律 17）。
           json_extract(payload, '$.profit_yoy')                              AS profit_yoy_pct,
           -- ── 高股息筛选（读层过滤，不改分数）所需字段 ──────────────
           -- 铁律 17：与 _from_payload 一一对应，漏一处 → 判据静默失效。
           -- 扣非归母净利在 growth 模块 metadata 下（不是顶层）。
           json_extract(payload, '$.modules.growth.metadata.deducted_net_profit') AS deducted_net_profit,
           json_extract(payload, '$.modules.solvency.metadata.bank_leverage_only') AS bank_leverage_only,
           json_extract(payload, '$.modules.solvency.metadata.deposit_loan_dual_high') AS deposit_loan_dual_high,
           json_extract(payload, '$.major_risks.pledge_ratio')               AS pledge_ratio,
           json_extract(payload, '$.major_risks.in_reduce_window')           AS in_reduce_window,
           -- 存贷双高「读层精判」所需的资产负债原值（见 _refined_dual_high）：
           -- 评分侧 metadata.deposit_loan_dual_high 判据过宽（现金>10亿 ∧ 有息>10亿 ∧
           -- 有息/现金≥0.4），全库 193 个标记里 186 个是误标（万科A/中兴/潍柴…），
           -- 读层用「现金占总资产 + 有息占总资产 + 有息/现金」三条件重判。
           json_extract(payload, '$.modules.solvency.metadata.balance_sheet.monetary_funds') AS monetary_funds,
           json_extract(payload, '$.modules.solvency.metadata.balance_sheet.interest_bearing_debt') AS interest_bearing_debt,
           json_extract(payload, '$.modules.solvency.metadata.balance_sheet.total_assets') AS total_assets,
           json_extract(payload, '$.modules.solvency.metadata.balance_sheet.interest_bearing_explicit') AS ibd_explicit
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


def _snap_value(row: dict[str, Any], key: str) -> Any:
    """风格判据专用：取**行情覆盖前**的快照原值（由 attach_realtime_quotes 留档）。

    为什么风格判据不看行情覆盖后的值：覆盖按日内价缩放 pe/pb/市值/股息率，会让贴着
    阈值（股息率 3.95% / PB 2.0 / 市值 1000 亿）的标的**盘中来回翻转**；而「风格」是
    结构性属性，应与因子分一样锚定披露报告期的快照口径（用户报的股息率也与快照值一致：
    新和成快照 3.96% / 盘中覆盖后 3.95%）。展示字段仍用行情覆盖后的值。
    """
    snap = row.get("_snap")
    if isinstance(snap, dict) and snap.get(key) is not None:
        return snap[key]
    return row.get(key)


# ── 轻量读层缓存（杜绝「每请求全表 json_extract」）─────────────────
# 为什么需要：_LIGHT_SQL 要对全表跑 25 个 json_extract，线上实测 **0.335s/次**
# （3218 只已覆盖快照，且随覆盖率线性增长），而翻页 / 换筛选 / 切视图每次都
# 重跑一遍 —— 纯重复劳动。
#
# 失效策略用**内容指纹**而非固定 TTL：
#   键 = (已覆盖条数, max(built_at))。
#   build_all 逐只写快照会持续推高 built_at → 指纹一变立刻失效，于是构建期间
#   读到的永远是当刻真实内容（若用固定 TTL，盘后构建过程中会返回滞后十几分钟
#   的旧数据，属于口径问题而不只是性能问题）；构建完成后指纹稳定，此后所有
#   请求直接命中。另加软 TTL 兜底，防「内容变而指纹不变」的极端情形。
#
# 命中时返回**浅拷贝**（列表 + 每行各复制一层），调用方可以放心就地写展示
# 字段（如 _overlay 叠加列），不会污染缓存里的原始行。
_BASE_CACHE_MAX_AGE = 900.0  # 软兜底：最长 15 分钟强制重建一次
_BASE_ROWS_CACHE: dict[str, Any] = {
    "fp": None,
    "ts": 0.0,
    "rows": None,
    "fallback": False,
}
_BASE_LOCK = threading.Lock()
_BASE_COUNTERS: dict[str, int] = {"hits": 0, "misses": 0}


def _snapshot_fingerprint(db: Session) -> tuple[int, str] | None:
    """快照内容指纹 = (已覆盖条数, 最新 built_at)。

    读不到返回 ``None`` —— 此时**不启用缓存**（宁可每次真读，也不能拿无法
    判定的缓存当新鲜数据）。
    """
    try:
        row = (
            db.execute(
                text(
                    "SELECT COUNT(*) AS n, MAX(built_at) AS latest "
                    "FROM factor_snapshots WHERE composite_score IS NOT NULL"
                )
            )
            .mappings()
            .first()
        )
    except Exception as exc:  # pragma: no cover - 指纹失败只损失命中率
        logger.warning("快照指纹读取失败，本次不启用读层缓存：%s", exc)
        try:
            db.rollback()
        except Exception:  # noqa: BLE001
            pass
        return None
    if row is None:
        return None
    return int(row["n"] or 0), str(row["latest"] or "")


def light_cache_stats() -> dict[str, Any]:
    """读层缓存自证（供诊断 / 健康检查）：命中数、指纹、缓存行数与年龄。"""
    with _BASE_LOCK:
        c = _BASE_ROWS_CACHE
        fp = c["fp"]
        return {
            "cached": c["rows"] is not None,
            "fingerprint": list(fp) if fp else None,
            "rows": len(c["rows"]) if c["rows"] is not None else 0,
            "age_sec": (round(time.time() - float(c["ts"]), 1) if c["ts"] else None),
            "max_age_sec": _BASE_CACHE_MAX_AGE,
            "hits": _BASE_COUNTERS["hits"],
            "misses": _BASE_COUNTERS["misses"],
        }


def invalidate_light_cache() -> None:
    """手动失效（供 build_all 收尾 / 诊断强制刷新用）。"""
    with _BASE_LOCK:
        _BASE_ROWS_CACHE.update(fp=None, ts=0.0, rows=None, fallback=False)


def _from_payload(row: FactorSnapshot) -> dict[str, Any]:
    """json_extract 不可用时的回退：Python 侧解析 payload。"""
    import json

    try:
        d = json.loads(row.payload or "{}")
    except (json.JSONDecodeError, TypeError):
        d = {}
    m = d.get("market") or {}
    dims = d.get("dim_scores") or {}
    val = d.get("valuation") or {}
    modules = d.get("modules") or {}

    def _ind_value(module: str, idx: int) -> Any:
        items = ((modules.get(module) or {}).get("indicators") or [])
        if idx < len(items):
            return (items[idx] or {}).get("value")
        return None

    # 存贷双高读层精判所需的资产负债原值（评分侧 solvency metadata.balance_sheet）
    _bs: dict[str, Any] = ((modules.get("solvency") or {}).get("metadata") or {}).get(
        "balance_sheet"
    ) or {}

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
        # 分类准入门槛所需字段（与 _LIGHT_SQL 一一对应，两条读取路径口径必须一致）
        "is_dividend_asset": val.get("is_dividend_asset"),
        "is_growth_stock": (val.get("growth_stock_profile") or {}).get("is_growth_stock"),
        "pe_percentile": m.get("pe_percentile"),
        "payout_ratio_pct": (val.get("dividend_profile") or {}).get("payout_ratio_pct"),
        "roe_pct": _ind_value("profitability", 0),
        "asset_turnover": _ind_value("efficiency", 0),
        "gross_margin_pct": _ind_value("profitability", 1),
        "revenue_yoy": d.get("revenue_yoy"),
        "profit_yoy": d.get("profit_yoy"),
        # 减持窗口期嵌在 major_risks 下（**不是**顶层），路径写错会静默取到
        # None → 封顶逻辑形同虚设且零报错。旧快照（本版之前构建）该字段为
        # None，_detect_buy_signal 会自然放行，不会炸。
        "reduce_window": (d.get("major_risks") or {}).get("reduce_window"),
        # 质量×价值视图所需字段（与 _LIGHT_SQL 一一对应，口径必须一致）
        "pb_percentile": m.get("pb_percentile"),
        "roic_pct": _ind_value("profitability", 2),
        "debt_ratio_pct": _ind_value("solvency", 0),
        "ocf_np_5y": _ind_value("cashflow", 0),
        "peg": ((val.get("relative") or {}).get("PEG") or {}).get("value"),
        # 展示分成长维（qv）：快照顶层 profit_yoy → compute_qv 的 profit_yoy_pct
        "profit_yoy_pct": d.get("profit_yoy"),
        # ── 高股息筛选所需字段（与 _LIGHT_SQL 一一对应，两条读取路径口径必须一致）──
        "deducted_net_profit": ((modules.get("growth") or {}).get("metadata") or {}).get(
            "deducted_net_profit"
        ),
        "bank_leverage_only": (((modules.get("solvency") or {}).get("metadata")) or {}).get(
            "bank_leverage_only"
        ),
        "deposit_loan_dual_high": (((modules.get("solvency") or {}).get("metadata")) or {}).get(
            "deposit_loan_dual_high"
        ),
        "pledge_ratio": (d.get("major_risks") or {}).get("pledge_ratio"),
        "in_reduce_window": (d.get("major_risks") or {}).get("in_reduce_window"),
        # 存贷双高读层精判所需的资产负债原值（与 _LIGHT_SQL 一一对应，见 _refined_dual_high）
        "monetary_funds": _bs.get("monetary_funds"),
        "interest_bearing_debt": _bs.get("interest_bearing_debt"),
        "total_assets": _bs.get("total_assets"),
        "ibd_explicit": _bs.get("interest_bearing_explicit"),
    }
    for eng, cn in DIM_KEYS.items():
        out[f"dim_{eng}"] = dims.get(cn)
    return out


# 最近一次行情覆盖的自证（供 market-scan meta 读取）。单键整体替换，GIL 下原子。
_QUOTE_LAST: dict[str, Any] = {"data": {"applied": False, "reason": "never_ran"}}


def _sym6(symbol: str) -> str:
    """``000504.SZ`` → ``000504``（快照 key 归一兜底，铁律 7.9）。"""
    return str(symbol or "").split(".")[0]


def attach_realtime_quotes(rows: list[dict[str, Any]]) -> dict[str, Any]:
    """把行情类展示字段按腾讯最新快照缩放覆盖（在 load_covered 的浅拷贝副本上）。

    为什么需要：因子快照锚定披露报告期（非披露期不重建，分数冻结是**设计**），
    但 payload 里的 股价/市值/PE/PB/股息率 是行情衍生量，冻结在构建时刻会随
    行情失真 —— 实测东鹏饮料 605499 快照价 115.45 vs 2026-09-28 收盘 111.12。

    口径：报告期内 总股本/EPS/BVPS 不变 → PE/PB/市值按 price 比例缩放、
    股息率按比例反缩放，等价于用现价重算；**不改任何分数与分位**
    （composite_score / pe_percentile / pb_percentile 仍锚定报告期口径）。

    降级：行情源不可用（ok=False，如腾讯被限流）→ 整体跳过并留痕，
    展示退回快照构建时价，**绝不让榜单挂掉**。停牌/异常价（≤0）单条跳过。
    """
    meta: dict[str, Any]
    try:
        snap = _market_snapshot()
    except Exception as exc:  # noqa: BLE001 - 覆盖层失败不得影响榜单可用性
        logger.warning("realtime quote overlay skipped: market_snapshot failed: %s", exc)
        meta = {"applied": False, "reason": "snapshot_error", "error": str(exc)[:200]}
        _QUOTE_LAST["data"] = meta
        return meta
    if not snap.get("ok"):
        meta = {
            "applied": False,
            "reason": "snapshot_unavailable",
            "errors": (snap.get("errors") or [])[:5],
        }
        _QUOTE_LAST["data"] = meta
        return meta

    items = snap.get("items") or {}
    applied = 0
    missed_no_quote = 0
    missed_bad_price = 0
    for r in rows:
        sym = str(r.get("symbol") or "")
        q = items.get(sym)
        if q is None:  # key 归一兜底（快照 key 均带市场后缀，理论上一致）
            q = next((v for k, v in items.items() if _sym6(k) == _sym6(sym)), None)
        if q is None:
            missed_no_quote += 1
            continue
        px_new = _to_float(q.get("price"))
        px_old = _to_float(r.get("price"))
        if not px_new or px_new <= 0 or not px_old or px_old <= 0:
            missed_bad_price += 1  # 停牌/异常价：保留快照价
            continue
        ratio = px_new / px_old
        # 风格/高股息判据锚定快照原值：把覆盖前的 pe/pb/市值/股息率/股价都留档
        # （见 _snap_value）。price 也要留：近三年平均股息率 = 年均每股分红 ÷ 快照价，
        # 与 TTM 股息率同源，不随盘中价翻转。
        # 只在真正要覆盖时留档，无覆盖/异常价的行不带此键 → 回落当前值。
        if "_snap" not in r:
            r["_snap"] = {
                "price": r.get("price"),
                "pe_ttm": r.get("pe_ttm"),
                "pb": r.get("pb"),
                "market_cap": r.get("market_cap"),
                "dividend_yield": r.get("dividend_yield"),
            }
        r["price"] = px_new
        pe = _to_float(r.get("pe_ttm"))
        if pe is not None:
            r["pe_ttm"] = round(pe * ratio, 4)
        pb = _to_float(r.get("pb"))
        if pb is not None:
            r["pb"] = round(pb * ratio, 4)
        cap = _to_float(r.get("market_cap"))
        if cap is not None:
            r["market_cap"] = round(cap * ratio, 4)
        dy = _to_float(r.get("dividend_yield"))
        if dy is not None:
            r["dividend_yield"] = round(dy / ratio, 4)
        applied += 1

    meta = {
        "applied": applied > 0,
        "reason": "ok" if applied else "no_symbol_matched",
        "as_of": snap.get("ts") or snap.get("date"),
        "quote_source": snap.get("source"),
        "applied_count": applied,
        "missed_no_quote": missed_no_quote,
        "missed_bad_price": missed_bad_price,
    }
    _QUOTE_LAST["data"] = meta
    return meta


def load_covered(db: Session | None = None) -> tuple[list[dict[str, Any]], bool]:
    """读取全部已构建快照的轻量字段。返回 (rows, 是否走了回退路径)。

    带内容指纹缓存（见 ``_BASE_ROWS_CACHE`` 说明）：命中时返回**浅拷贝**，
    调用方可就地写展示字段而不污染缓存。
    """
    own = db is None
    db = db or SessionLocal()
    try:
        fp = _snapshot_fingerprint(db)
        if fp is not None:
            with _BASE_LOCK:
                c = _BASE_ROWS_CACHE
                if (
                    c["fp"] == fp
                    and c["rows"] is not None
                    and time.time() - float(c["ts"]) < _BASE_CACHE_MAX_AGE
                ):
                    _BASE_COUNTERS["hits"] += 1
                    out = [{**r} for r in c["rows"]]
                    attach_realtime_quotes(out)  # 行情覆盖在副本上，不污染缓存
                    dividend_store.attach_profiles(out, db)  # 分红档案同样只写副本
                    return out, bool(c["fallback"])
        try:
            res = db.execute(_LIGHT_SQL).mappings().all()
            rows: list[dict[str, Any]] = [dict(r) for r in res]
            fallback = False
        except Exception as exc:  # pragma: no cover - 依赖 SQLite JSON1 版本
            logger.warning("json_extract 读取失败，回退 Python 解析：%s", exc)
            db.rollback()
            raw = (
                db.query(FactorSnapshot)
                .filter(FactorSnapshot.composite_score.isnot(None))
                .all()
            )
            rows = [_from_payload(r) for r in raw]
            fallback = True
        if fp is not None:
            with _BASE_LOCK:
                _BASE_COUNTERS["misses"] += 1
                _BASE_ROWS_CACHE.update(
                    fp=fp, ts=time.time(), rows=rows, fallback=fallback
                )
        # 与命中分支一致：**必须返回浅拷贝**。否则「首次请求」拿到的就是缓存里
        # 那批 dict 本身，下游（如技术叠加列）就地写字段会把缓存写脏。
        out = [{**r} for r in rows]
        attach_realtime_quotes(out)  # 行情覆盖在副本上，不污染缓存
        dividend_store.attach_profiles(out, db)  # 分红档案同样只写副本
        return out, fallback
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
    """覆盖率自证：已构建快照数 / 沪深**主板**股票数（与 build_all 同一 universe）。

    build_all 的 universe = SH/SZ 主板（``is_main_board`` 过滤，见
    ``factor_db._get_all_symbols``：默认榜单 ``include_gem=False`` 只消费主板，
    构建双创是浪费预算）。coverage 的 universe **必须与它同源**，否则会把
    刻意不建的科创板/创业板显示成「待构建缺口」（实测误导：3218/5390=59.7%、
    「2172 待构建」，实际主板 universe 已 100% 覆盖、每日例行检查正常）。

    非披露期 `build_all` 只补缺失条目（单次受批处理预算截断），
    因此覆盖率天然是渐进的——对外必须显式给出，不得默认已覆盖全市场。
    """
    from app.core.bull_tactics import is_main_board
    from app.services import factor_db as _factor_db

    own = db is None
    db = db or SessionLocal()
    try:
        sym_rows = (
            db.query(StockInfo.symbol)
            .filter(StockInfo.market.in_(("SH", "SZ")))
            .all()
        )
        all_syms = [r[0] for r in sym_rows]
        main_syms = [s for s in all_syms if is_main_board(s)]
        universe = len(main_syms)
        excluded_non_main = len(all_syms) - universe
        snap_syms = {
            r[0]
            for r in db.query(FactorSnapshot.symbol)
            .filter(FactorSnapshot.composite_score.isnot(None))
            .all()
        }
        covered = sum(1 for s in main_syms if s in snap_syms)
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
        # 刻意不构建的 SH/SZ 非主板票（创业板/科创板）：不在默认榜单样本内，
        # 不是「待构建缺口」。前端据此把口径说明清楚。
        "excluded_non_main": excluded_non_main,
        # 披露窗口期（1-4/7-8/10 月）才要求快照紧随财报重建；
        # 非披露期「快照滞后 N 天」是设计行为，前端不得按告警渲染。
        "disclosure_window": bool(_factor_db._is_in_disclosure_window()),
    }


# ── 风格分类（读层展示口径，2026-09-29 修订版）───────────────────
# ── 风格标签（**两维叠加**，2026-09-29 二次修订）────────────────────
# **只做展示与筛选，不改任何分数**（铁律 3/10：不得造第二个总分）。
#
# 结构（用户口径：不要 if-elif 互斥单选，每条规则独立判定后取并集）：
#   维度 A（价值 / 成长轴，二选一）：价值 = 便宜（低 PB/PE）+ 盈利质量；
#                                   成长 = 成长分高 且 不满足价值条件。
#   维度 B（属性轴，命中即加）：周期、红利、蓝筹。
#   **红利命中时不再显示价值** —— 红利 = 价值 + 股息 ≥4% + 分红稳定，价值是其母项，
#   重复列等于标签泛滥（用户口径）。周期/蓝筹可与任意维度叠加。
# 展示顺序固定 价值 → 成长 → 周期 → 红利 → 蓝筹，对应用户示例：
#   「价值+周期」（券商）、「周期+红利」、「成长+红利」、「价值+蓝筹」。
#
# 三条硬规则（用户 2026-09-29 二次口径）：
#   ① 周期 + 红利叠加：行业判周期、但股息率 ≥4% 的**必须**输出「周期+红利」
#      （天山铝业 / 芭田股份 / 鄂尔多斯 / 新和成），不能停在「周期」。
#   ② 券商走「价值+周期」：低 PB 给价值、行业给周期，两个都加；**周期标的判价值
#      只认 PB、不看 PE**（周期顶 PE 最低，PE 是反向指标）。
#   ③ 收租型资产（港口、写字楼收租）从周期闸摘出改判红利，不与油运/集运混为一谈
#      （唐山港 / 中国国贸 = 红利；锦江航运 / 招商南油仍是周期）。
#
# 与上一版（单一主风格 + 蓝筹副标签）的差异：价值/成长从「互斥主风格」变成各自
# 独立判定，周期/红利/蓝筹作为属性叠加 —— 因此条目可能带多个标签（style_tags）。
STYLE_KEYS: tuple[str, ...] = (
    "value", "growth", "cyclical", "dividend", "blue_chip", "other",
)
STYLE_LABELS: dict[str, str] = {
    "value": "价值股",
    "growth": "成长股",
    "cyclical": "周期股",
    "dividend": "红利股",
    "blue_chip": "蓝筹股",
    "other": "其他",
}
# 组合名的短名（用户口径写法：「价值+周期」「周期+红利」「成长+红利」「价值+蓝筹」）。
# 单标签仍用完整名（「红利股」），多标签用短名拼接 —— 避免「红利股+蓝筹股」这种叠字。
STYLE_SHORT: dict[str, str] = {
    "value": "价值",
    "growth": "成长",
    "cyclical": "周期",
    "dividend": "红利",
    "blue_chip": "蓝筹",
    "other": "其他",
}
STYLE_CRITERIA: dict[str, str] = {
    "value": "低估值（PB <2，且 PE <15 或低于行业中位）+ 盈利质量分 ≥70；周期标的只认 PB",
    "growth": "成长分 ≥75 且不满足价值条件（周期/券商行业不判成长，防「周期顶当成长」）",
    "cyclical": "强周期行业（有色/化工/煤炭/油气/运价/地产等）或券商；港口收租除外",
    "dividend": "股息率 ≥3.95%（≈4.0%）且分红率已知、≤200%；或弱周期/收租行业 + 可持续分红证据",
    "blue_chip": "市值 ≥1000 亿 且 ROE ≥10%（行业龙头，可与任意风格并存）",
    "other": "以上标签均未命中",
}
# ── 行业集合（**精确匹配申万三级名**，铁律 5：子串会误命中）────────────
# 券商 = 行情β（用户口径「券商靠行情吃饭，不是稳定成长」）→ 归周期。
RATE_CYCLE_INDUSTRIES: frozenset[str] = frozenset({"证券Ⅱ"})
# 🔴 **煤炭红利豁免已废除**（2026-09-29）：原 COAL_DIVIDEND_INDUSTRIES 把「煤炭开采」
#   从周期闸摘出、改走红利闸，与评分侧新增的「强周期前置拦截」口径正好相反
#   （`dividend_profile.classify_dividend_asset` 已把煤炭判为非红利资产）。
#   煤炭利润由煤价主导、景气高点的股息来自峰值利润，与其余强周期行业同源 → 统一归周期；
#   真实的现金分红能力由周期框架的股息利差锚承接，不再靠标签豁免保护。
# 红利行业 = 需求刚性/弱周期 + 有连续分红习惯。
# 🔴 **保险Ⅱ 已移除**（2026-09-29 用户口径：保险分红无承诺、确定性差，定价核心是
#    PB 资产折价 → 中国人寿这类应判价值）。保险只有在股息率真正够高时才拿红利标签
#    （中国平安息 5.15% 仍走「红利」高股息路径，不受影响）。
# 注意：**饮料乳品 / 休闲食品 不在内** —— 那类靠收入扩张，单年利润撑出的 4% 股息
# 不算红利风格（它们的成长分普遍 ≥70，走成长分支）。
DIVIDEND_INDUSTRIES: frozenset[str] = frozenset({
    "银行Ⅱ", "铁路公路", "电力", "燃气Ⅱ",
    "白酒Ⅱ", "非白酒", "食品加工", "调味发酵品Ⅱ",
    "中药Ⅱ", "化学制药", "医药商业",
})
# ── 收租型基础设施（用户口径③：从周期闸摘出、改判红利）──────────────
# 数据源把「港口」和「航运」并成同一个「航运港口」，无法按行业名拆分，只能按
# 证券简称区分：含「港」= 港口（吞吐费/租金驱动，弱周期收租资产）；不含「港」=
# 航运（运价驱动，强周期）。实测切分干净 —— 上港/宁波港/青岛港/唐山港/招商港口…
# 全含「港」；中远海控/中远海能/招商南油/锦江航运/中谷物流/渤海轮渡… 全不含「港」。
RENTAL_PORT_INDUSTRIES: frozenset[str] = frozenset({"航运港口"})
RENTAL_PORT_NAME_KEY = "港"
# 收租型地产：行业里没有「写字楼收租」细分，按标的白名单兜底（用户点名中国国贸）。
# 新增同类标的（陆家嘴/浦东金桥/张江高科…）只需在此登记。
RENTAL_SYMBOLS: dict[str, str] = {"600007.SH": "中国国贸（写字楼收租）"}

# ── 阈值 ────────────────────────────────────────────────────────
VALUE_MAX_PB = 2.0                # 价值：PB 上限（「便宜」的硬条件）
VALUE_MAX_PE = 15.0               # 价值：PE 绝对上限（周期标的不用此条）
VALUE_MIN_PROFIT_SCORE = 70.0     # 价值：盈利质量分下限（盈利能力维度分）
# 成长：成长分下限。用户口径「≥75 或处于上半区」，并亲口把星宇股份的 71.3 判为
# 「下半区」→ 该系统的「上半区」分界即 ≈75，故取 75（不是全市场中位数 57.9）。
# 硬过滤价值：成长分 <75 一律不得进成长分支（天有为 47.2 = 全表最低，绝不标成长）。
GROWTH_MIN_SCORE = 75.0
DIVIDEND_MIN_YIELD_IN_IND = 2.0   # 分红证据（行业路径的股息率下限）
DIVIDEND_MIN_PAYOUT = 30.0        # 分红证据（分红率下限）
DIVIDEND_MAX_PAYOUT = 200.0       # 分红证据（分红率上限：分红远超利润 = 一次性/不可持续）
DIVIDEND_MIN_YIELD = 3.95         # 高股息属性线：≈用户口径 4.0%（其数据四舍五入到 1 位）
DIVIDEND_MAX_YIELD = 12.0         # 高股息上限：>12% 多半是股价崩了、不是真高分红
DIVIDEND_ONE_OFF_PAYOUT = 120.0   # 超过此分红率时在依据里提示「可能含一次性特别分红」
BLUE_CHIP_MIN_MCAP_YI = 1000.0    # 蓝筹门槛：市值（用户口径「千亿级行业龙头」）
BLUE_CHIP_MIN_ROE = 10.0


def is_blue_chip(
    *, market_cap_yi: float | None, roe_pct: float | None, pe_ttm: float | None
) -> bool:
    """蓝筹（行业龙头）判据：市值 ≥1000 亿 且 ROE ≥10% 且 PE>0。

    与主风格判定共用同一判据（铁律 10：同一判定不两处各算）：主风格落蓝筹、
    以及给红利/周期/成长股挂「蓝筹」副标签，都走这里。
    """
    return (
        market_cap_yi is not None
        and float(market_cap_yi) >= BLUE_CHIP_MIN_MCAP_YI
        and roe_pct is not None
        and float(roe_pct) >= BLUE_CHIP_MIN_ROE
        and pe_ttm is not None
        and float(pe_ttm) > 0
    )


def dividend_evidence(
    dividend_yield: float | None, payout_ratio_pct: float | None
) -> str | None:
    """可持续分红证据：分红被利润覆盖（分红率 30%~200%），或股息率 2%~12%。

    双向边界都是为了挡**伪影**：分红率 >200%（分红远超利润）与股息率 >12%
    （多半是股价崩了、而非真高分红）都不算「可复制的红利风格」。实测全库存在
    分红率 3394%（东方雨虹）、股息率 39%（皓宸医疗）、股息率 25.9% 且分红率
    832%（山子高科）、以及若干 *ST 票的高息伪影 —— 只设下限会把它们全打成
    红利股。缺失值不猜（铁律 8）；行业路径与高股息兜底共用本函数（铁律 10）。
    """
    if (
        payout_ratio_pct is not None
        and DIVIDEND_MIN_PAYOUT <= payout_ratio_pct <= DIVIDEND_MAX_PAYOUT
    ):
        return f"分红率 {payout_ratio_pct:.0f}%"
    if (
        dividend_yield is not None
        and DIVIDEND_MIN_YIELD_IN_IND <= dividend_yield <= DIVIDEND_MAX_YIELD
    ):
        return f"股息率 {dividend_yield:.1f}%"
    return None


def is_rental_infra(industry: str, name: str, symbol: str) -> bool:
    """收租型基础设施（港口 / 写字楼收租）：从周期闸摘出、改走红利闸。

    数据源把「港口」与「航运」并成同一个「航运港口」，无法按行业名拆分，故港口
    按证券简称含「港」判定；收租型地产没有行业细分，按标的白名单兜底（见 RENTAL_*）。
    """
    if symbol and symbol in RENTAL_SYMBOLS:
        return True
    return industry in RENTAL_PORT_INDUSTRIES and RENTAL_PORT_NAME_KEY in (name or "")


def classify_style(
    *,
    industry: str,
    pe_ttm: float | None,
    pb: float | None,
    dividend_yield: float | None,
    payout_ratio_pct: float | None,
    roe_pct: float | None,
    market_cap_yi: float | None,
    growth_score: float | None,
    profit_score: float | None,
    symbol: str = "",
    name: str = "",
    industry_median_pe: float | None = None,
) -> tuple[list[str], str]:
    """单票风格标签（**两维叠加**），返回 ``(标签列表, 依据)``。纯函数、只读、不改分数。

    标签列表已按展示顺序排好（价值 → 成长 → 周期 → 红利 → 蓝筹）；空列表即「其他」。
    缺失字段的判据直接不命中（铁律 8：不赋中性值、不猜）。

    维度 A（价值 / 成长，二选一）与维度 B（周期 / 红利 / 蓝筹，命中即加）**各自独立判定
    后取并集**；红利命中时价值不再显示（红利 ⊃ 价值，避免标签泛滥）。
    """
    ind = (industry or "").strip()
    tags: list[str] = []
    reasons: dict[str, str] = {}

    rental = is_rental_infra(ind, name, symbol)
    broker = ind in RATE_CYCLE_INDUSTRIES
    # 商品周期 = 强周期行业；仅收租型基础设施（港口/写字楼）按租金属性摘出。
    commodity_cyclical = (
        ind in STRONG_CYCLICAL_INDUSTRIES
        and not rental
    )

    # ── 维度 B-1：周期（属性能与其余标签并存）────────────────────
    if commodity_cyclical:
        tags.append("cyclical")
        reasons["cyclical"] = f"{ind}：强周期行业，盈利随商品价格/运价波动"
    elif broker:
        tags.append("cyclical")
        reasons["cyclical"] = f"{ind}：行情β，盈利随市场行情波动"

    # ── 维度 A 前置：便宜（价值的前提）────────────────────────
    pb_ok = pb is not None and 0.0 < float(pb) < VALUE_MAX_PB
    if commodity_cyclical:
        # 商品周期股**不判价值**：周期顶 PE 最低，低 PE 是顶点特征而非便宜（用户口径）。
        cheap = False
    elif broker:
        # 券商判价值**只认 PB**（低 PB 才是真安全垫），不看 PE。
        cheap = pb_ok
    else:
        pe_ok = pe_ttm is not None and float(pe_ttm) > 0 and (
            float(pe_ttm) < VALUE_MAX_PE
            or (
                industry_median_pe is not None
                and float(pe_ttm) < float(industry_median_pe)
            )
        )
        cheap = pb_ok and pe_ok
    quality_ok = (
        profit_score is not None and float(profit_score) >= VALUE_MIN_PROFIT_SCORE
    )
    value_ok = cheap and quality_ok

    # ── 维度 A 二选一：成长 优先于 价值 ───────────────────────
    # 周期行业（含煤炭 / 券商）一律不判成长：那类利润暴增是周期弹性而不是成长，
    # 否则「周期顶」会被奖励成成长股（沿用旧口径的硬过滤）。
    growth_ok = (
        ind not in STRONG_CYCLICAL_INDUSTRIES
        and ind not in RATE_CYCLE_INDUSTRIES
        and growth_score is not None
        and float(growth_score) >= GROWTH_MIN_SCORE
        and not value_ok
    )
    if growth_ok:
        tags.append("growth")
        reasons["growth"] = f"成长分 {float(growth_score):.0f} ≥ {GROWTH_MIN_SCORE:.0f}"
    elif value_ok:
        tags.append("value")
        if broker:
            reasons["value"] = (
                f"低 PB（{pb}）+ 盈利质量分 {float(profit_score):.0f}（券商只认 PB）"
            )
        else:
            reasons["value"] = (
                f"低估值（PE {pe_ttm} / PB {pb}）+ 盈利质量分 {float(profit_score):.0f}"
            )

    # ── 维度 B-2：红利 ────────────────────────────────────────
    dividend_reason: str | None = None
    # 高股息路径（不限行业）：股息率 3.95%~12%；有分红率时必须 ≤200%（挡一次性高息）。
    # 🔴 分红率**必须已知**：只知道股息率高、拿不出分红率，就无法验证「分红是否被
    #    利润覆盖」——那正是「一次性特别分红 / 股价崩了」的典型特征（铁律 8：缺失
    #    不猜）。写成 `payout is None or ...` 会把这类票放进红利池（第一版实现曾因
    #    删掉上限而误标 104 只，见验收留痕）。
    if (
        dividend_yield is not None
        and DIVIDEND_MIN_YIELD <= float(dividend_yield) <= DIVIDEND_MAX_YIELD
        and payout_ratio_pct is not None
        and float(payout_ratio_pct) <= DIVIDEND_MAX_PAYOUT
    ):
        dividend_reason = (
            f"股息率 {float(dividend_yield):.2f}% ≥ {DIVIDEND_MIN_YIELD:.2f}%"
        )
    # 弱周期红利行业 / 收租型基础设施 + 可持续分红证据（与行业路径共用 dividend_evidence）。
    if dividend_reason is None and (ind in DIVIDEND_INDUSTRIES or rental):
        ev = dividend_evidence(dividend_yield, payout_ratio_pct)
        if ev:
            src = RENTAL_SYMBOLS.get(symbol) or ("港口收租" if rental else ind)
            dividend_reason = f"{src}：弱周期 + {ev}"
    if dividend_reason:
        tags.append("dividend")
        reasons["dividend"] = dividend_reason

    # ── 维度 B-3：蓝筹（千亿龙头，可与任意风格并存）──────────────
    if is_blue_chip(market_cap_yi=market_cap_yi, roe_pct=roe_pct, pe_ttm=pe_ttm):
        tags.append("blue_chip")
        reasons["blue_chip"] = (
            f"市值 {float(market_cap_yi):.0f} 亿 · ROE {float(roe_pct):.1f}%"
        )

    # 红利命中则不再显示价值（红利 = 价值 + 股息 ≥4% + 分红稳定，价值是其母项）。
    if "dividend" in tags and "value" in tags:
        tags.remove("value")
        reasons.pop("value", None)

    order = {k: i for i, k in enumerate(STYLE_KEYS)}
    tags.sort(key=lambda t: order.get(t, len(order)))
    return tags, "；".join(reasons[t] for t in tags)


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

    # 行业中位 PE：价值判据的「低于行业中位」分支。样本 <5 的行业置空 ——
    # 单只股票的中位数等于它自己，会退化成「永远便宜」（铁律 7.4）。
    _ind_pe: dict[str, list[float]] = {}
    for r in rows:
        v = _to_float(_snap_value(r, "pe_ttm"))
        ind = str(r.get("industry") or "").strip()
        if v is not None and v > 0 and ind:
            _ind_pe.setdefault(ind, []).append(v)
    industry_median_pe: dict[str, float] = {
        k: median(v) for k, v in _ind_pe.items() if len(v) >= 5
    }

    items: list[dict[str, Any]] = []
    for r in rows:
        name = str(r.get("name") or "")
        comp = _to_float(r.get("composite_score"))
        mcap = _to_float(r.get("market_cap"))
        mcap_yi = None if mcap is None else round(mcap / 1e8, 2)
        ind = str(r.get("industry") or "")
        _pe = _to_float(r.get("pe_ttm"))
        _roe = _to_float(r.get("roe_pct"))
        # 风格判据的行情类输入改取**快照原值**（见 _snap_value）；展示字段仍用行情值。
        _style_pe = _to_float(_snap_value(r, "pe_ttm"))
        _style_pb = _to_float(_snap_value(r, "pb"))
        _style_dy = _to_float(_snap_value(r, "dividend_yield"))
        _snap_cap = _to_float(_snap_value(r, "market_cap"))
        _style_mcap_yi = mcap_yi if _snap_cap is None else round(_snap_cap / 1e8, 2)
        # 风格标签（读层展示口径，不改分数）：维度 A（价值/成长）+ 维度 B（周期/红利/蓝筹）
        # 独立判定后取并集，故可能多标签；红利命中时价值被吸收（见 classify_style）。
        _style_tags, _style_why = classify_style(
            symbol=r["symbol"],
            name=name,
            industry=ind,
            pe_ttm=_style_pe,
            pb=_style_pb,
            dividend_yield=_style_dy,
            payout_ratio_pct=_to_float(r.get("payout_ratio_pct")),
            roe_pct=_roe,
            market_cap_yi=_style_mcap_yi,
            # 维度分：成长「成长分」、盈利质量「盈利能力分」（_LIGHT_SQL 已取，
            # 铁律 17 三处同步：SQL + _from_payload + 此处）。
            growth_score=_to_float(r.get("dim_growth")),
            profit_score=_to_float(r.get("dim_profitability")),
            industry_median_pe=industry_median_pe.get(ind),
        )
        # 高股息标记（读层过滤，不改分数）：唯一判定处 hd_verdict，
        # 筛选（_item_passes/scan_market）与计数都消费这里的结论。
        # 一次调用取全结论（pool / hit / review / 隐含分红率），**不重复判**。
        _hd = hd_verdict(r)
        _hd_ok = _hd.hit
        _hd_detail = _hd.detail
        # 命中者的评分分量：池内分位归一需要**整个池**（见 apply_hd_scores），
        # 故这里只把三维原始分量挂上，由 scan_market 在池内统一赋 hd_score。
        _hd_parts = hd_score_parts(r) if _hd_ok else None
        _yield_3y = yield_3y_avg_of(r)
        # 判据取值台账（hd_metrics）：六条判据**各自实际吃进去的数字**，结构化透出。
        # 为什么要透出：判据⑤⑥ 的输入（偿债分 / PE·PB 历史分位 / 支付率）原先只
        # 存在于 hd_detail 的**文案**里，接口不吐结构化值 —— 外部想复核这份名单，
        # 结构化的「分位」只剩列表里那一列（那是 market_pct 横截面分位，不是估值
        # 分位），于是把「估值分位」误算成了综合分分位（2026-09-30 真实踩过：用户
        # 体检表报「估值分位≥95% 有 44 只」，实测那 44 只是 market_pct≥95，
        # 而判据⑥的取值池内上限只有 94，≥95 的全被熔断在池外）。
        # 「判据吃什么数就吐什么数」是这类口径唯一能自证的方式。
        _hd_metrics = {
            # ② 股息率：判据实际取值（周期=近三年均息 / 非周期=快照 TTM）。
            #    yield_display 是列表里展示的那个值（按最新行情缩放）——两者口径
            #    不同，展示值盘中会漂过阈值线（实测有 4 只展示 <3.5% 而判据 ≥3.5%），
            #    筛选用判据值、展示用展示值是**刻意**的，不是过滤失效。
            "yield_used_pct": hd_yield_used(r),
            "yield_display_pct": _to_float(r.get("dividend_yield")),
            "yield_basis": "3y_avg" if ind in HD_CYCLICAL_INDUSTRIES else "ttm",
            # 快照锚定的 TTM 股息率 = **判据⑦⑧ 的实际输入**；与 yield_display_pct
            # （列表那一列，按最新行情缩放）是两个口径 —— 本池实测两者**同集**
            # （剔除集合完全相同 68=68），但边界票的数值会差零点几个百分点。
            # 双吐是为了让外部复核能区分「行情漂移」与「口径差异」，不必去猜。
            "yield_ttm_snap_pct": _to_float(_snap_value(r, "dividend_yield")),
            # v5 判据⑧ 隐含分红率的实际取值（= 快照 TTM 息 × 快照 PE；None = 未判定）
            "implied_payout_pct": _hd.implied_payout_pct,
            # ③ 档案最新完整会计年度 / ④ 连续年限与支付率
            "div_latest_fy": r.get("div_latest_fy"),
            "div_years": r.get("div_years"),
            "payout_pct": _to_float(r.get("payout_ratio_pct")),
            # ⑤ 财务底线（软组合）的两个输入
            "solvency": _to_float(r.get("dim_solvency")),
            "ocf_div_cover": ocf_dividend_cover(r),
            # ⑥ 估值拥挤度：max(PE 分位, PB 分位) = 熔断判据的实际取值
            "pe_percentile": _to_float(r.get("pe_percentile")),
            "pb_percentile": _to_float(r.get("pb_percentile")),
            "crowd_pct": hd_crowding_pct(r),
            # ① 市值（亿元，当前行情口径）
            "mcap_yi": hd_mcap_yi(r),
        }
        # 分类准入门槛：四类资产各用各的门槛（见 ADMISSION_* 说明）。
        # 命中即记 profile_gate，由 _verdict 封顶「观察」——不改任何分数。
        _gate = classify_admission_gate(
            is_dividend_asset=bool(r.get("is_dividend_asset")),
            is_growth_stock=bool(r.get("is_growth_stock")),
            is_strong_cyclical=ind in STRONG_CYCLICAL_INDUSTRIES,
            cycle_trap=False,  # 周期陷阱需 PB 分位，见 _overlay 层补充
            dividend_yield=_to_float(r.get("dividend_yield")),
            payout_ratio=_to_float(r.get("payout_ratio_pct")),
            ocf_positive=None,  # 快照未直出 OCF 符号，缺省放行（不猜）
            revenue_yoy=_to_float(r.get("revenue_yoy")),
            gross_margin=_to_float(r.get("gross_margin_pct")),
            roe=_to_float(r.get("roe_pct")),
            asset_turnover=_to_float(r.get("asset_turnover")),
            pe_percentile=_to_float(r.get("pe_percentile")),
            industry=ind,
        )
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
                "market_cap_yi": mcap_yi,
                "dividend_yield": _to_float(r.get("dividend_yield")),
                # 风格（读层展示口径，见 classify_style 说明）。
                #   style      = 首个标签（无标签时 "other"），供排序/兼容旧前端读取；
                #   style_tags = 完整标签列表（两维叠加，可为多项）；
                #   style_label= 中文组合名，如「周期+红利」「价值+周期」「成长+红利」。
                #   style_label= 中文组合名：单标签用完整名（红利股），多标签用短名
                #                拼接（周期+红利 / 价值+周期 / 成长+红利 / 价值+蓝筹）。
                "style": (_style_tags[0] if _style_tags else "other"),
                "style_tags": _style_tags,
                "style_label": (
                    "+".join(STYLE_SHORT[t] for t in _style_tags)
                    if len(_style_tags) > 1
                    else STYLE_LABELS[_style_tags[0] if _style_tags else "other"]
                ),
                "style_reason": _style_why,
                "dim_scores": {
                    cn: _to_float(r.get(f"dim_{eng}")) for eng, cn in DIM_KEYS.items()
                },
                "market_pct": market_pct.get(r["symbol"]),
                "industry_pct": (industry_pct.get(ind) or {}).get(r["symbol"]),
                "profile_gate": list(_gate) if _gate else None,
                # 减持窗口期：转交 _compute_tech_map → _overlay_one →
                # _detect_buy_signal，窗口开启时把强买入封顶为观察（不改分数）。
                "reduce_window": r.get("reduce_window"),
                # 高股息（读层过滤，不改分数）：判定结论 + 各维留痕（含缺数据标记）
                "high_dividend": _hd_ok,
                "hd_detail": _hd_detail,
                # v5 池归属与自洽标记：
                #   hd_pool   = "main" 主池 / "watch" 观察池（亏损分红，PE<0）/ None 出局
                #   hd_review = 主池内标黄待复核（隐含分红率 100~150%）
                #   hd_implied_payout_pct = 隐含分红率（TTM 息 × PE），None = 未判定
                "hd_pool": _hd.pool,
                "hd_review": _hd.review,
                "hd_implied_payout_pct": _hd.implied_payout_pct,
                # 分红历史派生量（来源 stock_dividend_profile 表，非快照 payload）：
                #   档案最新会计年度 / 连续分红年限 / 近三年年均每股分红 /
                #   近三年平均股息率(=均分红÷快照价)。
                # div_latest_fy 是判据③「档案新鲜度闸门」的输入，一并透出供前端
                # 展示档案年份、自证名单为何不含「档案停更」标的。
                "div_years": r.get("div_years"),
                "div_latest_fy": r.get("div_latest_fy"),
                "dps_3y_avg": _to_float(r.get("dps_3y_avg")),
                "yield_3y_avg": _yield_3y,
                # 评分三维原始分量（仅命中者带；hd_score 由 scan_market 在池内赋）
                "hd_parts": _hd_parts,
                # 六条判据的实际取值台账（见上方 _hd_metrics 说明）：命中的自证、
                # 落选的也能看出卡在哪一维的**哪个数**上，不依赖解析中文留痕。
                "hd_metrics": _hd_metrics,
            }
        )
    return items


def style_matches(it: dict[str, Any], style_key: str) -> bool:
    """条目是否命中某风格标签：**标签列表中任一命中即算**（两维叠加，可多标签）。

    唯一实现处（铁律 10：同一判定不两处各算）——`_item_passes` 的服务端筛选
    与 `scan_market` 的 selected 过滤、以及 style_counts 计数都走这里。
    ``other`` 的语义是「无任何标签」（不是标签列表里的一项）。
    """
    tags = it.get("style_tags") or []
    if style_key == "other":
        return not tags
    return style_key in tags


# ── 高股息筛选「真高股息防御型」（读层过滤，不改分数）──────────────────
# 口径演进（2026-09-30 同日多次裁决，v4 为定稿；用户提供框架原文）：
#   v1 严格量化 7 维（息≥4% ∧ 支付率30~70% ∧ OCF覆盖 ∧ ROE≥8% ∧ 扣非>0 ∧
#      负债率≤70% ∧ 非双高 ∧ PB<1.5 ∧ PE分位≤50 ∧ 质押<50% ∧ 非减持 ∧ 非ST）
#      → 全库 23 只，28 只代表性高股息标的仅 2 只入选。
#   v2 放宽两处（支付率上限 70→100、PE 分位 50→80）→ 36 只，代表性名单 3/28。
#   v3「高股息板块名单」（撤估值维度 + 放宽质量下限）→ 310 只、21/28，但
#      放过了支付率越界（苏泊尔 100.3%）、现金流覆盖为负（中国建筑 -4.32）等真问题。
#   v4「真高股息防御型」（**本口径**，替换 v3）—— 六条判据：
#     ① 基础门槛：剔 ST/退市 ∧ 市值 ≥ 30 亿
#     ② 动态股息率门槛：周期股看 **近三年平均股息率 ≥ 5%**（三年均值平滑单年虚高，
#        周期股景气高点的峰值分红不可持续）；非周期股看 TTM 股息率 ≥ 3.5%
#     ③ 分红档案新鲜度：档案最新完整会计年度 ≥ 最近**应已披露完毕**的年度
#        （随日期滚动，见 HD_PROFILE_DISCLOSURE_MONTH / hd_required_fy）
#     ④ 分红连续性 ∧ 支付率健康度：连续现金分红 ≥ 3 年 ∧ 支付率 30%~90%
#     ⑤ 财务底线（**软组合**，非全否）：偿债分 ≥ 40 ∨ 经营现金流覆盖分红 ≥ 1.0
#        —— 两条都差才否，避免单指标误杀
#     ⑥ 估值拥挤度熔断：PE/PB 分位取较高者 ≥ 95 → 剔除（防高位接盘）
#   与 v3 的差异：撤掉 ROE / 扣非净利 / 资产负债率 / 存贷双高 / 大股东质押 /
#     减持窗口六个维度（用户框架未包含），改为上述条款。存贷双高精判
#     （_refined_dual_high）**保留但只作 hd_detail 提示项，不参与判定**。
#   ③ 是首版上线验收后补的补丁（2026-09-30）：验收发现名单混入 21 只「档案停更」
#     标的（金科股份 latest_fy=2020 却显示 36.58% / 正邦科技 2020→24.14% /
#     万科A 2022→17.26% / 新城控股 2020→16.67%）。它们的高息是股价崩塌后冻结的
#     历史每股分红被缩小的分母放大出来的，不代表防御属性，恰是「基本面已崩」信号。
#     用户裁决「档案过期即不入选」，语义对齐铁律 7.5「缺失不免检」。
#   v5（2026-09-30 用户第二次迭代，**本口径**）：在 v4 六条之上加三条 ——（用户
#      口径编号①②③ = 本实现的判据⑦⑧⑨，避免与 v4 的①②③ 混淆）
#      ⑦ 当前股息率下限：TTM 股息率 < 4.0% 一律移出主池（含周期股；取**快照锚定值**，
#         展示值只用于展示 —— 与判据②③④ 同口径）。靶点：冀中能源
#         近三年均息 11.47% 但**当年 TTM 仅 2.29%** —— 防御型要求「当下也有息」，
#         三年均值是判据②的事，两条并列。
#      ⑧ 隐含分红率自洽：隐含分红率 = 快照 TTM 息 × PE(TTM)（≡ 每股分红÷每股收益）。
#         >150% 直接剔除（息与盈利不自洽，多来自分红口径错位/一次性特别分红）；
#         100~150% 入池但**标黄待复核**。靶点：长虹美菱 422.8% / 力生制药 182.9% /
#         东鹏控股 179.0%（全池仅此 3 只越线），标黄 14 只。
#      ⑨ 亏损分红观察池：PE < 0 的（六条判据仍成立）**单列观察池、不计主池**。
#         靶点：北大荒（PE −63.3）/ 万和电气（PE −53.1）恰好 2 只。
#         ⑨ **优先于 ⑦⑧**：万和电气 TTM 息 3.58% < 4.0%，若 ⑦ 先判就会被直接剔除、
#         而不是「移入观察池」—— 用户要的是后者（亏损 ≠ 骗息，故单列而非剔除）。
#      实测（2026-09-30 收盘口径）：v4 池 232 只 → ⑦ 剔 68（其中 1 只已被⑨接走）
#         → ⑧ 剔 3 → 主池 160 只、标黄 14 只、观察池 2 只。
#   数据底座：②③④三维依赖分红历史 —— stock_dividend_profile 表
#     （见 app/services/dividend_store.py），回补后读层才有值；
#     **无档案的标的按「缺数据」不入选**，绝不默认通过。
#   缺失语义（铁律 7.5 死法 1 反向）：主动筛选，必判维度缺数据 → 不入选；
#     唯独「估值拥挤度」属熔断类判据，缺失 = 未判定（不熔断），与风险排除同语义。
HD_MIN_MCAP_YI = 30.0  # 市值门槛（亿元，取行情覆盖后的当前市值）
HD_CYCLICAL_YIELD_3Y_MIN = 5.0  # 周期股：近三年平均股息率 ≥ 5%
HD_MIN_YIELD = 3.5  # 非周期股：TTM 股息率（快照口径）≥ 3.5%
HD_MIN_DIV_YEARS = 3  # 连续现金分红年数 ≥ 3
HD_PAYOUT_MIN = 30.0  # 股利支付率下限 30%（防象征性分红）
HD_PAYOUT_MAX = 90.0  # 股利支付率上限 90%（防透支分红）
HD_SOLVENCY_FLOOR = 40.0  # 偿债能力分底线（与现金流覆盖构成软组合）
HD_OCF_DIV_COVER_MIN = 1.0  # 经营现金流覆盖分红倍数 ≥ 1.0
HD_CROWD_PERCENTILE = 95.0  # 估值拥挤度熔断线（分位 ≥ 95 剔除）
# ── v5 三条补丁（2026-09-30 用户裁决，口径演进见上方 v5 段）──────────────
# ① 当前股息率下限 4.0%（**一律适用，含周期股**）：语义是「**当下**还值不值得为息
#    持有」，取**快照锚定的 TTM 股息率**（= 快照每股分红 ÷ 快照价）—— 与判据②③④
#    同口径，服从既有的「判据锚定快照价，展示值只用于展示」约定（见
#    test_high_dividend_uses_snapshot_price_not_intraday_overlay：行情覆盖后展示息会
#    跌破阈值线而命中结论不翻转，那是**有意**的，不是过滤失效）。
#    为什么不用展示值（用户肉眼所见的那一列）：展示值按最新行情缩放，盘中会漂过
#    4.0% 线（实测阈值附近还有 3.7~4.3 之间的一批票）→ 名单随分时价进出，
#    外部按截图复核必然对不上。实测两种口径在本池的剔除集合**完全相同**（68 = 68），
#    故取可复现的锚定值；展示值仍透出在 hd_metrics.yield_display_pct 供对账。
#    与判据② 是**并列**的两道门槛，不互相替代：周期股过得了「近三年均息≥5%」，
#    仍可能因当年 TTM 只有 2.29%（冀中能源）被这条挡在池外。
HD_MIN_YIELD_FLOOR = 4.0  # ① 当前 TTM 股息率下限（%，快照锚定）
# ② 隐含分红率自洽校验：隐含分红率 = TTM 股息率 × PE(TTM) ≡ 每股分红 ÷ 每股收益
#    （价格项在乘式中约掉 → 该值本身对价格不敏感，是天然可复现的口径）。
#    两个输入都取**快照锚定值**，与①同源。
#    用 TTM 息而非周期股的三年均息：派息率是**当期**比率，拿三年均值乘当期 PE
#    会得出「三年均分红 ÷ 当年利润」这种无意义的数。
#    >150% 视为「息与盈利不自洽」→ 剔除；100~150% → 入池但标黄待复核；
#    缺失（无 PE 或无息）→ **未判定**（不剔除、不标黄），与判据⑥ 同属熔断类语义。
HD_IMPLIED_PAYOUT_MAX = 150.0  # ② 隐含分红率上限（%），超过即剔除
HD_IMPLIED_PAYOUT_REVIEW = 100.0  # ② 隐含分红率关注线（%），超过即标黄待复核
# 分红档案新鲜度闸门（2026-09-30 新增）：分红档案的「最新完整会计年度」必须达到
# 最近一个**应已披露完毕**的会计年度，否则按缺数据处理（不入选）。
#
# 为什么必须加：档案停更的标的会给出**假的超高股息率**。典型如金科股份
# （latest_fy=2020、三年均息 33.33%、展示息 36.58%）、正邦科技（2020、24.14%）、
# 万科A（2022、17.26%）、新城控股（2020、16.67%）—— 它们的高息是股价从十几元
# 跌到一两元后，冻结的历史每股分红被缩小的分母放大出来的，不代表「防御型高股息」，
# 恰恰是「基本面已崩」的信号。全池实测 21/247 属此类，且展示息前 4 名全是它们。
# 语义对齐铁律 7.5「缺失不免检」：档案过期 ≡ 拿不到当下的分红数据 → 不入选。
#
# 阈值**随日期滚动**，刻意不写死年份：年报与分红方案集中在 3~6 月披露，故
#   5 月起 → 要求上一完整会计年度（today.year - 1）
#   1~4 月 → 放宽一年（today.year - 2），否则每年年初整张名单会被误判为
#            「档案过期」而清空（FY N 的方案尚未披露）。见 hd_required_fy。
HD_PROFILE_DISCLOSURE_MONTH = 5  # 该月起，上一完整会计年度的分红方案应已披露完毕
# 综合评分权重（池内分位归一，见 apply_hd_scores）：
#   股息率 .4 / 偿债分 .3 / 支付率接近度 .3 —— 权重取自用户原式。
HD_SCORE_W_YIELD = 0.40
HD_SCORE_W_SOLVENCY = 0.30
HD_SCORE_W_PAYOUT = 0.30
HD_SCORE_PAYOUT_TARGET = 0.60  # 支付率越接近 60% 得分越高（用户原式 1-|p-0.6|/2）
# 周期行业名册（**申万精确名**）。用户给定 8 类按申万口径展开：
#   航运港口 / 煤炭开采 / 普钢 / 特钢（→「特钢Ⅱ」）/ 有色金属（一级行业，
#   库中不存在，展开为 工业金属·小金属·能源金属·贵金属·金属新材料）/
#   化学原料 / 农化制品 / 水泥。
# 刻意用精确匹配而非子串：「特钢Ⅱ」里没有「钢铁」二字（见 engine 同款注释）。
# 本名册是 engine.STRONG_CYCLICAL_INDUSTRIES 的**子集**（用户只要最典型一批，
# 不含 化学制品/化学纤维/塑料/焦炭Ⅱ/房地产开发 等），测试锁住子集关系防漂移。
HD_CYCLICAL_INDUSTRIES = frozenset(
    {
        "航运港口",
        "煤炭开采",
        "普钢",
        "特钢Ⅱ",
        "工业金属",
        "小金属",
        "能源金属",
        "贵金属",
        "金属新材料",
        "化学原料",
        "农化制品",
        "水泥",
    }
)


# 存贷双高精判阈值（见 _refined_dual_high：现金占比 + 借款占比 + 借款/现金 三条件）。
# 注意：v4 口径下存贷双高**不参与筛选判定**，只作 hd_detail 提示项；
# 保留这套精判是因为它修掉了评分侧 186/193 的误标（用户 2026-09-30 报告的疑点），
# 一旦未来重新启用该维度，必须用精判而非评分侧原标记。
HD_DUAL_MIN_CASH_ASSET = 0.25  # 货币资金/总资产 ≥ 25%（现金占比畸高）
HD_DUAL_MIN_IBD_ASSET = 0.20  # 有息负债/总资产 ≥ 20%（借款规模够大）
HD_DUAL_MIN_IBD_CASH = 0.60  # 有息负债/货币资金 ≥ 0.60（借的钱接近或超过账上现金）
# 提示项阈值（**不参与判定**，只在 hd_detail 里标出来）：
# 大股东质押 ≥ 50% 与「减持窗口」对齐 major_risk_events.PLEDGE_OBSERVE_RATIO 的观察线。
HD_PLEDGE_OBSERVE = 0.5
# v4 不再需要「金融口径豁免」：本口径已无「资产负债率 / 经营现金流覆盖」两个硬
# 维度，金融企业靠判据④的**软组合**自然过关（银行偿债分普遍 50~67 ≥ 40，
# 实测农行 50 / 工行 56 / 建行 58 / 兴业 60 / 招行 67）。
# v3 的 _is_finance_industry + HD_FINANCE_IND_PREFIXES 随之删除，避免留死代码。


def _refined_dual_high(row: dict[str, Any]) -> bool | None:
    """存贷双高读层精判（三态）：True=双高 / False=非双高 / None=数据不可评估。

    为什么读层要重判（2026-09-30 用户报告的疑点）：评分侧
    metadata.deposit_loan_dual_high 的判据是「货币资金>10亿 ∧ 有息负债>10亿 ∧
    有息/现金≥0.40」——对大公司几乎恒成立。全库实测：193 个标记里 186 个是误标
    （万科A 现金/总资产仅 6.2%、中兴通讯、潍柴动力、中联重科、申万宏源…），
    用户点名的格力/美的/苏泊尔/中石油/神华也在其中 —— 大集团「账上有现金 + 有借款」
    本就是常态（财务公司、海外低息债、供应链占款），不构成存疑信号。

    精判补上结构性条件，只留经典双高画像（康得新/康美型）：
      ① 有息负债为**显式值**（interest_bearing_explicit；残差估计值不可当分母）
      ② 货币资金/总资产 ≥ 25%（现金占比畸高，才谈得上「账上百亿却借钱」）
      ③ 有息负债/总资产 ≥ 20%（借款规模本身够大）
      ④ 有息负债/货币资金 ≥ 60%（借的钱接近或超过账上现金）
    实测：原判 193 → 精判 8（深科技/京粮控股/万向钱潮/格力电器/顺鑫农业/光华股份/
    智微智能/惠科股份），全是经典画像（格力：现金/资产 32%、有息/现金 0.65）。

    评分口径本轮不动（改它要 bump SCORING_VERSION + 全库重建约 3 天），**只修读层**：
    筛选判据用精判；原始资产负债数据缺失时才退回评分侧原标记（三态：缺数据=未判定=放行）。
    """
    cash = _to_float(row.get("monetary_funds"))
    ibd = _to_float(row.get("interest_bearing_debt"))
    ta = _to_float(row.get("total_assets"))
    if cash is None or ta is None or ta <= 0:
        return None  # 无原始资产负债数据 → 不可评估
    if not row.get("ibd_explicit"):
        return None  # 有息负债为残差估计值 → 不可评估
    if ibd is None or cash <= 0:
        return None
    return bool(
        cash / ta >= HD_DUAL_MIN_CASH_ASSET
        and ibd / ta >= HD_DUAL_MIN_IBD_ASSET
        and ibd / cash >= HD_DUAL_MIN_IBD_CASH
    )


def hd_required_fy(today: date | None = None) -> int:
    """真高股息判据要求的「分红档案最新会计年度」下限（随日期滚动）。

    见上方 HD_PROFILE_DISCLOSURE_MONTH 说明。刻意不写死年份：写死则每年需人工
    改常量，忘了就让名单逐年畸变（年初误清空 / 年中漏掉新披露的标的）。
    唯一实现处（铁律 10）——判据与留痕文案都消费本函数。
    """
    d = today or date.today()
    if d.month >= HD_PROFILE_DISCLOSURE_MONTH:
        return d.year - 1
    return d.year - 2


def yield_3y_avg_of(row: dict[str, Any]) -> float | None:
    """近三年平均股息率(%) ＝ 近三年年均每股分红 ÷ 快照价 × 100。

    分子 ``dps_3y_avg`` 来自分红档案表（最近 3 个**完整会计年度**的年均每股
    分红），平滑单年虚高 —— 周期股景气高点的峰值分红会被摊薄，这正是用户
    要求「周期股看三年均值」的原因。分母取 ``_snap_value(row,"price")``：
    与 TTM 股息率同源锚定**快照价**，判据不随盘中价翻转（与风格标签同款铁律）。
    任一缺失 → None（调用方按「缺数据」处理，不免检）。
    """
    dps = _to_float(row.get("dps_3y_avg"))
    px = _to_float(_snap_value(row, "price"))
    if dps is None or px is None or px <= 0:
        return None
    return round(dps / px * 100.0, 4)


def ocf_dividend_cover(row: dict[str, Any]) -> float | None:
    """经营现金流覆盖分红倍数 ＝ (5年均值OCF/净利) ÷ 支付率。

    恒等式：OCF/分红额 = (OCF/净利) ÷ (分红额/净利)。快照没有「OCF/分红额」
    直接字段，但两个分量都有（cashflow 指标第 0 项 + dividend_profile.payout），
    故能精确还原而非近似。支付率缺失/非正 → None。
    """
    ocf = _to_float(row.get("ocf_np_5y"))
    payout = _to_float(row.get("payout_ratio_pct"))
    if ocf is None or payout is None or payout <= 0:
        return None
    return round(ocf / (payout / 100.0), 4)


def hd_yield_used(row: dict[str, Any]) -> float | None:
    """命中判定实际使用的股息率(%)：周期股取近三年均息，其余取 TTM。

    唯一实现处（铁律 10）——判据与综合评分都消费本函数，保证「按什么判的
    就按什么排」：周期股的三年均息既是门槛，评分就不能改用被景气摊薄的
    TTM，否则周期股会在排序里被系统性压低。
    """
    if str(row.get("industry") or "") in HD_CYCLICAL_INDUSTRIES:
        return yield_3y_avg_of(row)
    return _to_float(_snap_value(row, "dividend_yield"))


def hd_score_parts(row: dict[str, Any]) -> dict[str, float] | None:
    """综合评分三维原始分量（未归一）：股息率 / 偿债分 / 支付率接近度。

    支付率接近度用用户原式 ``1 - |payout - 0.6| / 2``（payout 取小数）。
    用户原式直接加权存在**量纲不一致**：股息率是小数(0.045)、偿债分是百分数
    (0~100)、接近度是 0~1 —— 直接加权会让偿债分独占 99% 权重。故三维先在
    命中池内取百分位再按 0.4/0.3/0.3 加权（见 apply_hd_scores）。
    任一维缺失 → None（该只不参与评分，排序时排末尾）。
    """
    y = hd_yield_used(row)
    solv = _to_float(row.get("dim_solvency"))
    payout = _to_float(row.get("payout_ratio_pct"))
    if y is None or solv is None or payout is None:
        return None
    return {
        "yield": y,
        "solvency": solv,
        "payout_stab": 1.0 - abs(payout / 100.0 - HD_SCORE_PAYOUT_TARGET) / 2.0,
    }


def apply_hd_scores(pool: list[dict[str, Any]]) -> int:
    """给命中池就地写 ``hd_score``（0~100，越大越好）：池内分位归一后加权。

    为什么要归一：见 hd_score_parts 的量纲说明。分位复用 ``percentile_rank``
    （横截面 0~100，平均秩处理并列）。赋分范围只限命中池 —— 池外条目没有
    hd_score，按 EXTRA_SORT_KEYS 的缺失语义排到末尾，不参与。
    """
    if not pool:
        return 0
    parts = [it.get("hd_parts") or {} for it in pool]
    for key, weight in (
        ("yield", HD_SCORE_W_YIELD),
        ("solvency", HD_SCORE_W_SOLVENCY),
        ("payout_stab", HD_SCORE_W_PAYOUT),
    ):
        vals = {
            it["symbol"]: float(p[key])
            for it, p in zip(pool, parts)
            if p.get(key) is not None
        }
        ranks = percentile_rank(vals)
        for it in pool:
            p = it.get("hd_parts")
            if p is None or p.get(key) is None:
                continue
            # 该维缺失的条目本维权重按 0 计入（不赋中性 50，缺失不得伪装成读数）
            it["hd_score"] = round(it.get("hd_score", 0.0) + ranks[it["symbol"]] * weight, 2)
    for it in pool:
        if "hd_score" not in it:
            it["hd_score"] = None
    return sum(1 for it in pool if it.get("hd_score") is not None)


def hd_mcap_yi(row: dict[str, Any]) -> float | None:
    """判据①的市值(亿元)：取**行情覆盖后的当前市值**（不是快照值）。

    市值不是结构性属性，故不锚定快照（与用户可见的「市值筛选」同口径）。
    唯一实现处（铁律 10）——判据①与 hd_metrics 都消费本函数，避免两处各算一次
    导致「留痕写 88 亿、体检表算出 87 亿」这类对不上的账。
    """
    cap = _to_float(row.get("market_cap"))
    return None if cap is None else round(cap / 1e8, 2)


def hd_crowding_pct(row: dict[str, Any]) -> float | None:
    """判据⑥的估值拥挤度取值 ＝ max(PE 历史分位, PB 历史分位)。

    取**较高者**：任一维处于历史极端即视为拥挤（保守侧）。
    两者都缺 → None（熔断类判据的缺失语义 = 未判定，不熔断）。
    唯一实现处（铁律 10）——判据⑥与 hd_metrics 共用。
    """
    vals = [
        v
        for v in (_to_float(row.get("pe_percentile")), _to_float(row.get("pb_percentile")))
        if v is not None
    ]
    return max(vals) if vals else None


@dataclass(frozen=True)
class HDVerdict:
    """高股息判定的**完整结论**（唯一实现处的返回类型，铁律 10）。

    - ``hit``：是否计入主池（= 对外的 ``high_dividend`` 标记）
    - ``pool``：``"main"`` 主池 / ``"watch"`` 观察池（亏损分红）/ ``None`` 剔除
    - ``detail``：逐维留痕（✓ / ✗ / 缺 / 提示项）
    - ``implied_payout_pct``：隐含分红率（TTM 息 × PE），``None`` = 未判定
    - ``review``：主池内**标黄待复核**（隐含分红率 100~150%）
    """

    hit: bool
    pool: str | None
    detail: list[str] = field(default_factory=list)
    implied_payout_pct: float | None = None
    review: bool = False


def hd_verdict(row: dict[str, Any]) -> HDVerdict:
    """「真高股息防御型」判据（**唯一实现处**，铁律 10）：v5 = v4 六条 + ⑦⑧⑨。

    判据清单（口径与先后顺序见上方 v4/v5 块注释；顺序即口径）：
      ① 基础门槛（剔 ST/退市 ∧ 市值≥30亿）
      ② 动态股息率门槛（周期股 近三年均息≥5% / 其余 TTM≥3.5%）
      ③ 分红档案新鲜度闸门（随日期滚动）
      ④ 连续现金分红≥3 年 ∧ 支付率 30%~90%
      ⑤ 财务底线（软组合：偿债分≥40 ∨ 现金流覆盖分红≥1.0）
      ⑥ 估值拥挤度熔断（PE/PB 分位取高者 ≥95 剔除）
      ⑦ 当前股息率下限 4.0%（v5，一律适用，含周期股）
      ⑧ 隐含分红率自洽（v5，>150% 剔除 / 100~150% 标黄）
      ⑨ 亏损分红观察池（v5，PE<0 且六条成立 → 单列观察池，**优先于⑦⑧**）

    输入是轻量行（``load_covered`` 产物：行情覆盖后的展示值 + ``_snap`` 留档的快照
    原值 + 分红档案合并进来的 div_years / dps_3y_avg / div_latest_fy）。
    必判维度（①~⑤、⑦）缺数据 → 不命中（缺失不免检），留痕标「缺」；
    熔断类判据（⑥ 估值拥挤度、⑧ 隐含分红率）缺失 = 未判定，与风险排除同语义。
    """
    ok = True
    detail: list[str] = []

    # ① 基础门槛：剔除 ST/退市 + 微盘（市值取行情覆盖后的当前值，与用户可见的
    #    「市值筛选」同口径；它不是结构性属性，不锚定快照）
    _name = str(row.get("name") or "")
    if any(mk in _name for mk in ST_MARKERS):
        ok = False
        detail.append("ST✗")
    _cap_yi = hd_mcap_yi(row)
    if _cap_yi is None:
        ok = False
        detail.append("市值缺")
    elif _cap_yi < HD_MIN_MCAP_YI:
        ok = False
        detail.append(f"市值{_cap_yi:.0f}亿✗")
    else:
        detail.append(f"市值{_cap_yi:.0f}亿✓")

    # ② 动态股息率门槛：周期股用近三年均息（平滑景气高点的单年虚高），其余用 TTM
    _ind = str(row.get("industry") or "")
    if _ind in HD_CYCLICAL_INDUSTRIES:
        _y3 = yield_3y_avg_of(row)
        if _y3 is None:
            ok = False
            detail.append("周期·三年均息缺")
        elif _y3 < HD_CYCLICAL_YIELD_3Y_MIN:
            ok = False
            detail.append(f"周期·三年均息{_y3:.2f}%✗")
        else:
            detail.append(f"周期·三年均息{_y3:.2f}%✓")
    else:
        _dy = _to_float(_snap_value(row, "dividend_yield"))
        if _dy is None:
            ok = False
            detail.append("股息率缺")
        elif _dy < HD_MIN_YIELD:
            ok = False
            detail.append(f"股息率{_dy:.2f}%✗")
        else:
            detail.append(f"股息率{_dy:.2f}%✓")

    # ③ 分红档案新鲜度闸门（见 HD_PROFILE_DISCLOSURE_MONTH / hd_required_fy）：
    #    记录停更 ≡ 拿不到当下的分红数据 → 不入选（铁律 7.5 缺失不免检）。
    #    不加这条会把「股价崩塌型」标的顶到名单最前（金科股份 latest_fy=2020 却
    #    显示 36.58%、正邦科技 24.14%），与「防御型」立意正相反。
    #    三种缺法分开留痕（整条无档案 / 有年限但缺年度 / 年度过期），便于穿透定位。
    _req_fy = hd_required_fy()
    _years = row.get("div_years")
    _latest_fy = row.get("div_latest_fy")
    _latest_fy = None if _latest_fy is None else int(_latest_fy)
    if _latest_fy is None:
        ok = False
        detail.append("分红档案缺" if _years is None else "档案年度缺")
    elif _latest_fy < _req_fy:
        ok = False
        detail.append(f"档案停更FY{_latest_fy}✗")  # 需 ≥ FY{_req_fy}
    else:
        detail.append(f"档案FY{_latest_fy}✓")
    _archive_ok = _latest_fy is not None and _latest_fy >= _req_fy

    # ④ 分红连续性（档案表 dt 值）+ 支付率健康度。
    #    档案不可用时连续性**不可采信**（年限是在过期口径下累积的），故不标 ✓ ——
    #    否则留痕会出现「档案停更✗ · 连续分红10年✓」这种自相矛盾的组合。
    #    整条无档案时不重复吐这句（上一条「分红档案缺」已说明）。
    if not _archive_ok:
        if _years is not None:
            detail.append("连续分红不采信")
    elif _years is None:
        ok = False
        detail.append("连续分红缺")
    elif int(_years) < HD_MIN_DIV_YEARS:
        ok = False
        detail.append(f"连续分红{int(_years)}年✗")
    else:
        detail.append(f"连续分红{int(_years)}年✓")
    _payout = _to_float(row.get("payout_ratio_pct"))
    if _payout is None:
        ok = False
        detail.append("支付率缺")
    elif not (HD_PAYOUT_MIN <= _payout <= HD_PAYOUT_MAX):
        ok = False
        detail.append(f"支付率{_payout:.1f}%✗")
    else:
        detail.append(f"支付率{_payout:.1f}%✓")

    # ⑤ 财务底线（软组合）：偿债分达标 ∨ 现金流覆盖分红达标 —— 两条都差才否，
    #    避免单指标误杀（银行 OCF 是贷款投放镜像，但银行偿债分普遍 ≥40 自然过关）
    _solv = _to_float(row.get("dim_solvency"))
    _cover = ocf_dividend_cover(row)
    if _solv is not None and _solv >= HD_SOLVENCY_FLOOR:
        detail.append(f"偿债{_solv:.0f}✓")
    elif _cover is not None and _cover >= HD_OCF_DIV_COVER_MIN:
        detail.append(f"现金流覆盖分红{_cover:.2f}✓")
    else:
        ok = False
        _s_txt = "缺" if _solv is None else f"{_solv:.0f}"
        _c_txt = "缺" if _cover is None else f"{_cover:.2f}"
        detail.append(f"偿债{_s_txt}且现金流覆盖分红{_c_txt}✗")

    # ⑥ 估值拥挤度熔断：PE/PB 分位取**较高者**（任一维处于极高即视为拥挤）。
    #    为什么不用较低者：用户原意是「行业分位极高时，即使股息率高也不买」，
    #    取高者是保守侧；代价是景气底部 PE 分位虚高的周期股会被误熔断（已知取舍）。
    _worst = hd_crowding_pct(row)
    if _worst is None:
        detail.append("估值分位未判定")
    elif _worst >= HD_CROWD_PERCENTILE:
        ok = False
        detail.append(f"估值拥挤{_worst:.0f}%分位✗")
    else:
        detail.append(f"估值分位{_worst:.0f}%✓")

    # 提示项（**不参与判定**）：存贷双高精判 / 大股东质押 / 减持窗口。
    # v4 口径不含这三条（用户框架未列），但它们是真风险信号，写进留痕让用户
    # 自己看得到，不静默丢弃、也不偷偷改名单。
    _flags: list[str] = []
    _dual = _refined_dual_high(row)
    if _dual is None:
        _stored = row.get("deposit_loan_dual_high")
        # json_extract 把 JSON true 变成整数 1（`1 is True` == False）→ 必须 bool()
        _dual = None if _stored is None else bool(_stored)
    if _dual:
        _flags.append("存贷双高")
    _pledge = _to_float(row.get("pledge_ratio"))
    if _pledge is not None and _pledge >= HD_PLEDGE_OBSERVE:
        _flags.append(f"质押{_pledge:.0%}")
    if row.get("in_reduce_window"):
        _flags.append("减持窗口")
    if _flags:
        detail.append("提示·不参与判定：" + "、".join(_flags))

    # ── v5 三条：⑦ 当前息下限 / ⑧ 隐含分红率 / ⑨ 亏损分红观察池 ──────────
    # 取数一律走 `_snap_value`（**快照锚定**，与判据②③④ 同源）：展示值按最新行情
    # 缩放，拿它判会让名单随分时价进出（阈值附近的票最容易翻转）。
    # 两者缺其一 → ⑧ 未判定（熔断类语义：不剔除、不标黄）。
    _dy_now = _to_float(_snap_value(row, "dividend_yield"))
    _pe = _to_float(_snap_value(row, "pe_ttm"))
    _implied = None if (_dy_now is None or _pe is None) else _dy_now * _pe

    # ⑨ 观察池（**优先级最高**）：六条判据成立且 PE<0 → 单列观察池，而非剔除。
    #    为什么必须优先于⑦：万和电气 TTM 息 3.58%（<4.0）且 PE=−53.1 —— 若⑦ 先判
    #    就会被当成「息不够」直接剔除，而用户要的是「亏损分红单列」（亏损 ≠ 骗息，
    #    故保留观察而非清出）。实测该优先级下观察池恰为北大荒 + 万和电气 2 只。
    if _pe is not None and _pe < 0 and ok:
        detail.append(f"亏损分红·观察池(PE{_pe:.1f})")
        detail.append(
            "当前息" + ("缺" if _dy_now is None else f"{_dy_now:.2f}%") + "·观察池不做4%下限判定"
        )
        return HDVerdict(False, "watch", detail, _implied, False)

    # ⑦ 当前 TTM 股息率下限（一律适用，含周期股）。与判据② 并列：② 的「近三年均息」
    #    用来平滑周期股景气高点的单年虚高，但不能反过来掩盖「当年已经没息」——
    #    冀中能源三年均息 11.47% 而当年 TTM 只剩 2.29%，防御型不认这种票。
    if _dy_now is None:
        ok = False
        detail.append("当前息缺")
    elif _dy_now < HD_MIN_YIELD_FLOOR:
        ok = False
        detail.append(f"当前息{_dy_now:.2f}%✗")
    else:
        detail.append(f"当前息{_dy_now:.2f}%✓")

    # ⑧ 隐含分红率自洽校验（熔断类）：>150% 剔除，100~150% 入池标黄待复核。
    _review = False
    if _implied is None:
        detail.append("隐含分红率未判定")
    elif _implied > HD_IMPLIED_PAYOUT_MAX:
        ok = False
        detail.append(f"隐含分红率{_implied:.1f}%✗")
    elif _implied > HD_IMPLIED_PAYOUT_REVIEW:
        _review = True
        detail.append(f"隐含分红率{_implied:.1f}%⚠待复核")
    else:
        detail.append(f"隐含分红率{_implied:.1f}%✓")

    return HDVerdict(ok, "main" if ok else None, detail, _implied, _review and ok)


def high_dividend_check(row: dict[str, Any]) -> tuple[bool, list[str]]:
    """兼容入口：结论仍在 `hd_verdict` 一处计算（此处只做投影，不重算）。"""
    v = hd_verdict(row)
    return v.hit, v.detail


def _item_passes(
    it: dict[str, Any],
    *,
    exclude_st: bool,
    include_gem: bool,
    keyword: str,
    industry: str,
    min_composite: float | None,
    min_market_cap_yi: float | None,
    style: str = "",
    high_dividend: bool = False,
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
    # 风格筛选：服务端过滤（分页口径一致，翻页不会错位）。
    # 标签列表中任一命中即算（两维叠加）—— 千亿红利股（茅台/神华）按「蓝筹」筛也命中。
    if style and not style_matches(it, style):
        return False
    # 高股息筛选：条目上的 high_dividend 标记由 _base_items 经 hd_verdict
    # 统一判定（唯一实现处），此处只消费不重算。
    if high_dividend and not it.get("high_dividend"):
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
    style: str | None = None,
    sort_order: str = "desc",
    high_dividend: bool = False,
    hd_watch: bool = False,
) -> dict[str, Any]:
    """全市场基本面排序。

    Args:
        top: 单页返回条数（上限 500）。
        min_composite: 综合分下限（沿用个股分析口径的 0~100 分）。
        min_market_cap_yi: 总市值下限，单位**亿元**。
        exclude_st: 剔除证券简称含 ST / 退 的标的（数据源唯一可得的风险剔除项）。
        industry: 行业名包含匹配（如「银行」「煤炭」）。
        sort_by: `composite_score`（默认）、五个维度键之一
            （profitability/growth/cashflow/solvency/valuation），或
            _EXTRA_SORT_KEYS 中的行情字段（dividend_yield/pe_ttm/pb/market_cap_yi）。
            维度/行情排序只是展示排序，不合成新总分。
        sort_order: `desc`（默认）或 `asc`。仅对 _EXTRA_SORT_KEYS 行情字段生效——
            综合分与五维固定降序（高分在前是唯一自然方向）；行情字段双向可调，
            缺失值恒排末尾（不因方向翻转跑到队首）。
        keyword: 名称/代码包含匹配（如「茅台」「600519」），大小写不敏感。
        include_gem: 是否纳入创业板(300/301/302)与科创板(688/689)，默认剔除。
        offset: 分页起始下标（在**过滤并排序后**的命中集合上偏移），负值按 0 处理。
        style: 风格筛选（价值/成长/周期/红利/蓝筹/其他），读层展示口径不改分数；
            两维叠加，标签列表中任一命中即算（「其他」= 无任何标签）。
        high_dividend: 高股息筛选（读层过滤，不改分数）。**真高股息防御型口径**
            （2026-09-30 v5，在 v4 六条之上加三条；用户口径的①②③ = 判据⑦⑧⑨）：
            v4 六条 —— ① 剔 ST/退市 ∧ 市值≥30亿；② 动态股息率门槛（周期股看
            **近三年平均股息率≥5%**，非周期股看 TTM≥3.5%）；③ 分红档案新鲜度
            （档案最新完整会计年度 ≥ 最近应已披露完毕的年度，随日期滚动）——档案
            停更的「股价崩塌型」标的不入选；④ 连续分红≥3年 ∧ 支付率30%~90%；
            ⑤ 财务底线（**软组合**）：偿债分≥40 ∨ 经营现金流覆盖分红≥1.0；
            ⑥ 估值拥挤度熔断：PE/PB 分位取较高者 ≥95 剔除。
            v5 三条 —— ⑦ **当前 TTM 股息率 < 4.0% 一律移出主池**（含周期股：三年
            均息是判据②的事，⑦ 看的是「当年还有多少息」；判据取**快照锚定值**，
            不随盘中价漂移）；⑧ **隐含分红率
            = 快照 TTM 息 × PE**（≡ 每股分红÷每股收益）：>150% 剔除、100~150% 入池但
            标黄待复核；⑨ **亏损分红单列观察池**：PE<0 且六条成立 → 移入观察池、
            不计主池（**优先于⑦⑧**，故 TTM 息 3.58% 的万和电气进观察池而非被剔除）。
            必判维度（①~⑤、⑦）缺数据即不入选（缺失不免检）；熔断类判据
            （⑥ 估值拥挤度、⑧ 隐含分红率）缺失=未判定，既不剔除也不标黄。
            命中集按池内分位归一算 hd_score（0.4×股息率分位 + 0.3×偿债分分位 +
            0.3×支付率接近度分位），**未经排序参数指定时默认按股息率降序**展示；
            想按该评分排需显式传 sort_by=hd_score。每条另带 hd_metrics（各判据的
            实际取值台账，含隐含分红率与快照锚定 TTM 息）供逐条自证。
            判据唯一实现在 `hd_verdict`；2026-09-30 前的 v3 维度
            （ROE/扣非/负债率/存贷双高/质押/减持）已按用户框架撤除，存贷双高
            精判保留但只作 hd_detail 提示项。数据底座见 dividend_store。
        hd_watch: 只看**高股息观察池**（v5 判据⑨：六条成立但 PE<0 的亏损分红票），
            与 high_dividend 互斥使用；命中集的 high_dividend 为 False、hd_pool
            为 "watch"。计数见返回体的 hd_watch_count。
    """
    rows, fallback = load_covered(db)

    key = (sort_by or "composite_score").strip()
    dim = None
    extra_field: str | None = None
    if key in _CN_TO_DIM:
        dim = _CN_TO_DIM[key]
    elif key in DIM_KEYS:
        dim = key
    elif key in EXTRA_SORT_KEYS:
        extra_field = key
    elif key != "composite_score":
        raise ValueError(f"不支持的排序字段：{sort_by}")

    hd_forced = False
    if extra_field == "hd_score" and not high_dividend:
        # 按真高股息评分排序 = 只看命中集（池外的票没有该分数）
        high_dividend = True
        hd_forced = True
    if hd_watch and high_dividend:
        # 主池与观察池互斥：同时传两个 = 调用方搞错了意图，显式失败而不是
        # 静默返回空集（否则前端会显示「0 条」被误读成「观察池里没有票」）。
        raise ValueError("high_dividend 与 hd_watch 互斥：观察池请只传 hd_watch=true")
    if hd_watch and key == "composite_score":
        # 观察池排序口径与主池一致：默认按股息率降序（谁息高谁在前）
        key = "dividend_yield"
        extra_field = "dividend_yield"
    if high_dividend and key == "composite_score":
        # 「仅高股息」自带主排序：**默认按股息率降序**（红利视角最直觉 —— 谁息高
        # 谁在前）。2026-09-30 二次裁决：原先默认成 hd_score（池内分位归一评分），
        # 用户看到的是「钱江摩托 10.29% → 力生制药 6.42% → 广日股份 6.54%」这种
        # 息率无序的榜，误判成「过滤/排序失效」。
        # 显式传其它排序（含 hd_score）仍被尊重 —— 想按综合评分看，选那一项即可。
        key = "dividend_yield"
        extra_field = "dividend_yield"

    order = (sort_order or "desc").strip().lower()
    if order not in ("asc", "desc"):
        raise ValueError(f"不支持的排序方向：{sort_order}（可选 asc / desc）")

    style_key = (style or "").strip()
    if style_key and style_key not in STYLE_KEYS:
        raise ValueError(f"不支持的风格：{style}（可选：{', '.join(STYLE_KEYS)}）")

    base = _base_items(rows)
    ind_filter = (industry or "").strip()
    kw = (keyword or "").strip().lower()
    # 先做「风格以外」的全部过滤 → 得到风格分布基准（chip 计数与筛选口径同源）；
    # 再叠加风格过滤得到最终命中集。
    pre_style = [
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
    # 风格分布：**每个标签都计入**，与下拉筛选口径同源 —— 例如「蓝筹」chip 的计数
    # 必须等于按蓝筹筛选命中的条数（含千亿红利股）。两维叠加下一条目可带多标签，
    # 因此 sum(style_counts) ≥ 命中总数（= 总数 + 多标签的额外项），不是等式。
    # 计数复用 style_matches，保证「chip 数 == 筛出数」不被两套口径岔开。
    _style_counter: Counter[str] = Counter()
    for _it in pre_style:
        for _k in STYLE_KEYS:
            if style_matches(_it, _k):
                _style_counter[_k] += 1
    style_counts = dict(sorted(_style_counter.items(), key=lambda kv: -kv[1]))
    selected = [it for it in pre_style if not style_key or style_matches(it, style_key)]
    # 高股息筛选：在风格过滤之上再取交集。计数基于 pre_style（与 style_counts
    # 同源口径：先做风格以外全部过滤的基准集），保证「计数 == 无风格筛选时
    # 按高股息筛出的条数」。
    high_dividend_count = sum(1 for it in pre_style if it.get("high_dividend"))
    # v5 两个附加计数（同一处口径，与上面的筛选消费同一份 hd_pool 结论）：
    #   hd_review_count = 主池内**标黄待复核**数（隐含分红率 100~150%）
    #   hd_watch_count  = 观察池（六条成立但 PE<0 的亏损分红票）
    hd_review_count = sum(
        1 for it in pre_style if it.get("high_dividend") and it.get("hd_review")
    )
    hd_watch_count = sum(1 for it in pre_style if it.get("hd_pool") == "watch")
    # 池内分位归一评分：口径基准与计数同源（pre_style 命中集），这样切换风格
    # 不会让同一只票的 hd_score 变化；赋分必须在排序之前（默认排序键就是它）。
    apply_hd_scores([it for it in pre_style if it.get("high_dividend")])
    if high_dividend:
        selected = [it for it in selected if it.get("high_dividend")]
    if hd_watch:
        # 观察池视图：只看 hd_pool == "watch"（主池 filter 已互斥拒绝）。
        # 池内分位归一评分对观察池不适用（它们是「暂不计主池」的票，不参与排名），
        # 故 hd_score 恒为 None —— 前端按股息率降序展示即可。
        selected = [it for it in selected if it.get("hd_pool") == "watch"]

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
    elif extra_field is not None:
        # 行情字段双向排序；缺失值恒排末尾（asc 用 +inf、desc 用 -inf 兜底），
        # 不因方向翻转把「无数据」顶到队首。
        if order == "asc":
            selected.sort(
                key=lambda x: (
                    x[extra_field] if x.get(extra_field) is not None else float("inf")
                )
            )
        else:
            selected.sort(
                key=lambda x: (
                    x[extra_field]
                    if x.get(extra_field) is not None
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
        f"分位基准为已覆盖样本 {pct_base} 只（沪深主板 universe 的 "
        f"{coverage['coverage_pct']}%；科创板/创业板 {coverage['excluded_non_main']} 只"
        "不在默认榜单样本，不参与构建）。",
        "默认仅沪深主板（剔除创业板 300/301/302 与科创板 688/689），勾选「含创业板/科创板」后纳入。",
        "风格为读层展示口径（**两维叠加**：维度A 价值/成长 二选一，维度B 周期/红利/蓝筹 "
        "命中即加，红利命中时不再显示价值）："
        + "；".join(f"{STYLE_LABELS[k]}={STYLE_CRITERIA[k]}" for k in STYLE_KEYS if k != "other")
        + "。条目可带多个标签（如 价值+周期 / 周期+红利 / 成长+红利 / 红利+蓝筹），"
        "因此各标签计数之和会大于命中总数；只影响筛选与标注，不改任何分数。",
        "股价/市值/PE/PB/股息率已按最新行情缩放（腾讯快照），因子分与估值分位仍锚定快照报告期；"
        "快照按最新披露报告期冻结，非披露期不重建属正常（每交易日 16:35 例行检查）。",
        "本数据源无「审计意见」「日均成交额」字段，故未提供这两项过滤。",
        "高股息筛选（读层过滤，不改分数；真高股息防御型口径 v5）＝"
        "① 剔 ST/退市 ∧ 市值≥30亿；"
        "② 动态股息率门槛：周期股（航运港口/煤炭开采/普钢/特钢Ⅱ/工业金属/小金属/"
        "能源金属/贵金属/金属新材料/化学原料/农化制品/水泥）看近三年平均股息率≥5%，"
        "非周期股看 TTM 股息率≥3.5%；"
        "③ 分红档案新鲜度：档案最新完整会计年度 ≥ 最近**应已披露完毕**的年度"
        "（随日期滚动：5 月起要求上一完整年度、1~4 月放宽一年）；"
        "档案停更的标的（股价崩塌型，如金科股份 latest_fy=2020 却显示 36.58%）"
        "按缺数据不入选；"
        "④ 连续现金分红≥3年 ∧ 支付率30%~90%；"
        "⑤ 财务底线（软组合）：偿债分≥40 ∨ 经营现金流覆盖分红≥1.0；"
        "⑥ 估值拥挤度熔断：PE/PB 分位取较高者 ≥95 剔除；"
        "⑦ 【v5】当前 TTM 股息率 <4.0% 一律移出主池（**含周期股**：三年均息是②的事，"
        "⑦ 看当下这一列，判据取**快照锚定值**、不随盘中价漂移）。"
        "靶点：冀中能源三年均息 11.47% 但当年 TTM 仅 2.29%；"
        "⑧ 【v5】隐含分红率自洽校验 = 快照 TTM 股息率 × PE(TTM)（≡ 每股分红÷每股收益）："
        ">150% 视为「息与盈利不自洽」直接剔除（靶点：长虹美菱 422.8% / 力生制药 182.9% / "
        "东鹏控股 179.0%），100~150% 入池但**标黄待复核**（hd_review=true）；"
        "⑨ 【v5】亏损分红**单列观察池**：PE<0 且①②③④⑤⑥ 成立 → 移入观察池、不计主池"
        "（靶点：北大荒 / 万和电气）。⑨ 优先于 ⑦⑧ —— 万和电气 TTM 息 3.58%<4.0%，"
        "若⑦ 先判会被当「息不够」剔除，而用户要的是「亏损分红单列」（亏损≠骗息）。"
        "必判维度（①②③④⑤、⑦）缺数据即不入选（缺失不免检）；熔断类判据"
        "（⑥ 估值拥挤度、⑧ 隐含分红率）缺失=未判定，既不剔除也不标黄。"
        "命中集按池内分位归一算 hd_score："
        "0.4×股息率分位 + 0.3×偿债分分位 + 0.3×支付率接近度分位"
        "（原式三维量纲不一致会使偿债分独占权重，故先归一再加权）；未显式指定排序时"
        "默认按**股息率降序**展示（想按该评分排，排序选「真高股息评分」）。"
        "每条都带 hd_metrics —— 各判据实际取用的数字（判据股息率/展示股息率/"
        "快照锚定 TTM 息/隐含分红率、偿债分、现金流覆盖、PE·PB 历史分位、支付率、"
        "档案年度、市值），"
        "用于逐条自证；注意列表里的「分位」列是 **market_pct 横截面综合分分位**，"
        "不是判据⑥用的估值历史分位，两者不可混用。"
        "分红连续年限/近三年均息/档案年度来自 "
        "stock_dividend_profile 档案表（东财分红历史落地），无档案的标的按缺数据不入选。"
        "本口径不含 ROE/扣非/负债率/存贷双高/质押/减持六个维度（用户框架未列），"
        "其中存贷双高精判、大股东质押≥50%、减持窗口会在 hd_detail 里作**提示项**标出，"
        "但不参与判定；池归属见条目 hd_pool（main/watch），标黄见 hd_review。",
    ]
    if hd_forced:
        notes.append(
            "排序字段 hd_score 隐含「仅高股息」过滤：该分数只对命中集有意义，"
            "池外标的没有分值。"
        )
    if hd_watch:
        notes.append(
            "当前为**高股息观察池视图**（hd_watch=true）：只列「①②③④⑤⑥ 成立但 PE<0」"
            "的亏损分红票，按 v5 判据⑨ 单列、**不计入主池**（主池请用 high_dividend=true）。"
            "这些票不做 ⑦ 4%股息率下限与 ⑧ 隐含分红率判定（亏损状态下两者都失去意义），"
            "故不带 hd_score（池内分位归一评分只在主池内计算）。"
        )
    if high_dividend or hd_watch:
        _dp = dividend_store.stats()
        notes.append(
            f"真高股息判据依赖分红档案 stock_dividend_profile：已覆盖 {_dp['total']} 只，"
            f"其中连续现金分红≥3 年 {_dp['continuous_ge3']} 只；无档案、或档案停更"
            f"（最新完整会计年度 < FY{hd_required_fy()}）的标的按「缺数据」不入选"
            "（缺失不免检，绝不默认通过）。"
        )
    else:
        _dp = None
    if fallback:
        notes.append("本次读取走了 Python 解析回退路径（SQLite JSON1 不可用）。")
    if not _QUOTE_LAST["data"].get("applied"):
        notes.append("本次行情覆盖未生效（行情源暂不可用），股价等为快照构建时价。")

    return {
        "coverage": coverage,
        "quote_overlay": dict(_QUOTE_LAST["data"]),
        "percentile_base": pct_base,
        "count": len(window),
        "matched": len(selected),
        "offset": start,
        "limit": limit,
        "has_more": start + len(window) < len(selected),
        "sort_by": key,
        # 仅行情字段排序（EXTRA_SORT_KEYS）时 asc 有意义；综合分/五维恒 desc
        "sort_order": order if extra_field is not None else "desc",
        # 风格分布（基于风格以外的全部筛选命中集，与下拉筛选口径同源）
        "style_counts": style_counts,
        # 高股息命中数（基准同 style_counts：风格以外的全部筛选命中集）
        "high_dividend_count": high_dividend_count,
        # v5 附加计数（同一基准集）：标黄待复核 / 亏损分红观察池
        "hd_review_count": hd_review_count,
        "hd_watch_count": hd_watch_count,
        # 分红档案覆盖自证（真高股息判据的数据底座；仅高股息/观察池筛选时给出）
        "dividend_profile": _dp,
        "filters": {
            "min_composite": min_composite,
            "min_market_cap_yi": min_market_cap_yi,
            "exclude_st": exclude_st,
            "industry": ind_filter or None,
            "keyword": kw or None,
            "include_gem": include_gem,
            "style": style_key or None,
            "sort_order": order if extra_field is not None else "desc",
            "high_dividend": bool(high_dividend),
            "hd_watch": bool(hd_watch),
        },
        "items": window,
        "notes": notes,
    }


def _profit_yoy_map(db: Session | None, symbols: list[str]) -> dict[str, float | None]:
    """从因子快照取净利同比（engine 返回自 2026-09-20 起携带 profit_yoy）。

    仅用于展示与诊断；**买点信号的 PEG 不再由此重算**（见 _snapshot_peg_map）。
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


def _snapshot_peg_map(db: Session | None, symbols: list[str]) -> dict[str, float | None]:
    """从因子快照取**个股分析口径**的 PEG（`valuation.relative.PEG.value`）。

    这是 engine 算好的值：优先 3 年净利 CAGR，周期底部负 CAGR 与红利资产
    会被置 None 并附 note。扫描层直接复用，不另立「PE_TTM ÷ 单期同比」的算法——
    否则同一只票在个股分析与榜单买点信号里会得到两个 PEG，且周期股在景气
    高点（低 PE + 极高增速）会被严重低估。

    ⚠ 路径必须是 `valuation.relative.PEG.value`：快照 payload 没有顶层 `relative`，
    相对估值块整体挂在 `valuation` 下。2026-09-21 曾误写为 `relative.PEG.value`，
    导致**全库 PEG 恒为 None**，而 buy_signal 的 strong_buy/watch 两档都硬性要求
    `peg is not None` —— 结果整张榜单买点信号 100% 落在 neutral，
    档位静默失效且无任何报错。改动此路径务必同步 test_market_scan 的路径回归用例。
    """
    if not symbols:
        return {}
    out: dict[str, float | None] = {}
    sess = db or SessionLocal()
    try:
        sql = text(
            "SELECT symbol, json_extract(payload, '$.valuation.relative.PEG.value') "
            "FROM factor_snapshots WHERE symbol IN :syms"
        ).bindparams(bindparam("syms", expanding=True))
        for sym, val in sess.execute(sql, {"syms": list(symbols)}):
            try:
                out[sym] = None if val is None else float(val)
            except (TypeError, ValueError):
                out[sym] = None
    except Exception:
        logger.debug("snapshot peg map query failed", exc_info=True)
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


# ── 分类准入门槛（候选池熔断，只封档位不改分）─────────────────────────────
# 背景：综合分是「加权平均」，单一维度极差会被其他维度的高分掩盖。实测
# 600722 金牛化工：PE_TTM 193.5 / PE 分位 89.8 / PB 8.49 / ROE 3.92% /
# 每股经营现金流 0.078，却因偿债 93.9、现金流 75.8 拉到综合 71.5 → 进「买入候选」。
#
# **为什么不用单一绝对阈值**（实测 2026-09-21，5254 只，均被否决）：
#   周转率 < 0.5 → 命中 55.8%（铁路/高速/水电/银行等重资产本就在此区间）
#   ROE < 5%     → 命中 54.2%（大秦铁路 3.64%，最经典的红利压舱石）
#   PE 分位 > 85% → 命中 18.7%（科创板 688 成长期硬科技几乎全中）
#   「PE分位≥90 且 PB分位≥90」→ 拦掉中国神华(87.2/A)、生益科技、深南电路、
#     三环集团等评分最高的优质股——**分位高 ≠ 泡沫**：优质公司盈利持续增长
#     会抬高估值中枢，长期停在高分位是常态而非异常。
#   反证：PE分位 95.6% 拦中国神华，却放走 PE分位 89.8% 的金牛化工
#     —— 单指标阈值对本问题完全无效。
#
# **有效判据 = 指标间的矛盾，而非单指标绝对值**：
#   金牛化工的病是「估值极高(PE分位89.8/PB分位98.8) 却盈利极弱(ROE 3.92)」
#   这种矛盾组合才是真泡沫；而中国神华「估值分位高 但 ROE 12.76」有盈利支撑，
#   不构成矛盾。实测「PE分位≥80 且 ROE<6%」命中 15.0%，且：
#     中国神华/生益科技/深南电路/三环集团 均不命中（ROE 12~21）
#     大秦铁路命中 ROE 3.64 —— 但它是红利型，走股息率/分红比例分支，不受此限。
#
# 四类门槛（命中即封顶「观察」，不改写 composite_score / 技术面读数）：
#   红利型：股息率 ≥3.5% 且 分红比例 ≥50%（不看 ROE/周转——重资产低周转是行业属性）
#   成长型：营收增速 ≥15% 或 毛利率 ≥30%（不看 PE 绝对值——高 PE 是成长特征）
#   周期型：仅 cycle_trap（PE低分位+PB高分位+高ROE）才熔断，即景气高点
#   传统价值：估值分位与盈利质量的**矛盾组合**（PE分位≥80 且 ROE<6%）
#             或 极低周转（<0.15，真僵尸资产）或 PE 分位极端（≥97）
# 任一类**关键字段缺失则放行**（不猜、不因缺数据而改变判定），与全系统口径一致。
ADMISSION_DIV_YIELD_MIN = 3.5
ADMISSION_DIV_PAYOUT_MIN = 50.0
ADMISSION_GROWTH_REV_YOY_MIN = 15.0
ADMISSION_GROWTH_GROSS_MARGIN_MIN = 30.0
# 传统价值型：估值与盈利的「矛盾」判据（核心）
ADMISSION_VALUE_PE_PCTL_MIN = 80.0
ADMISSION_VALUE_ROE_MAX = 6.0
# 传统价值型：极端兜底（单独触发）
ADMISSION_VALUE_TURNOVER_MIN = 0.15
ADMISSION_VALUE_PE_PCTL_EXTREME = 97.0
# 周转率门槛对**金融业结构性豁免**：银行赚息差、券商赚佣金，资产负债表以金融资产
# 为主，总资产周转率天然在 0.02~0.08（实测 42 家银行 / 49 家券商 / 20 家多元金融
# 均 <0.15）。用制造业的周转尺子量金融=必然误杀，与「同一指标在四类资产上含义不同
# 不可同尺」的原则一致。豁免后仍有「估值↔盈利矛盾」判据兜底，不会变成免检通道。
ADMISSION_FINANCIAL_INDUSTRY_KW = (
    "银行", "保险", "证券", "多元金融", "信托", "期货",
)


def classify_admission_gate(
    *,
    is_dividend_asset: bool = False,
    is_growth_stock: bool = False,
    is_strong_cyclical: bool = False,
    cycle_trap: bool = False,
    dividend_yield: float | None = None,
    payout_ratio: float | None = None,
    ocf_positive: bool | None = None,
    revenue_yoy: float | None = None,
    gross_margin: float | None = None,
    roe: float | None = None,
    asset_turnover: float | None = None,
    pe_percentile: float | None = None,
    industry: str | None = None,
) -> tuple[str, str] | None:
    """分类准入门槛：返回 ``(类型标签, 未通过原因)``，通过则返回 None。

    判定顺序按「分类互斥性」排列：红利 → 周期 → 成长 → 传统价值，每类只适用
    自己的门槛（同一指标在四类资产上含义不同，不可同尺）。字段缺失一律放行。
    """
    # ── 红利型：不看 ROE/周转率（重资产低周转是行业属性），看分红能力 ──
    if is_dividend_asset:
        fails: list[str] = []
        if dividend_yield is not None and float(dividend_yield) < ADMISSION_DIV_YIELD_MIN:
            fails.append(f"股息率 {float(dividend_yield):.2f}% < {ADMISSION_DIV_YIELD_MIN:g}%")
        if payout_ratio is not None and float(payout_ratio) < ADMISSION_DIV_PAYOUT_MIN:
            fails.append(f"分红比例 {float(payout_ratio):.0f}% < {ADMISSION_DIV_PAYOUT_MIN:g}%")
        if ocf_positive is False:
            fails.append("经营现金流为负")
        if fails:
            return "红利型", "；".join(fails)
        return None

    # ── 周期型置于成长之前：周期股常因毛利率高被判成长，但门槛完全不同 ──
    # 周期型**不是免检通道**：早先此处只判 cycle_trap 且该参数恒为 False，
    # 导致 894 只强周期股全部无条件放行，混入 143 只「PE分位≥80 且 ROE<6」
    # （泸天化 PE分位99.3/ROE0.5、招商蛇口100.0/0.73、华菱钢铁99.2/4.77），
    # 与金牛化工同病。周期股同样须过「估值↔盈利矛盾」这一关。
    if is_strong_cyclical:
        if cycle_trap:
            return (
                "周期型",
                "估值陷阱：PE 处低分位而 PB 处高分位且 ROE 高位，"
                "便宜来自盈利高点而非资产便宜，杀估值风险高",
            )
        # 与「传统价值型」同尺的矛盾判据：高估值分位 + 弱盈利 → 周期也不例外。
        # 但周期股的 ROE 天然波动大（谷底为负、峰值为高），故：
        #   ① 必须 PE 分位高 且 ROE 弱 —— 单看 ROE 弱会误杀真·底部反转（ROE 为负但
        #      估值已在低位），那正是该买的时点；
        #   ② 只在 PE 分位 ≥ 阈值时触发，谷底低估值不受影响。
        if (
            pe_percentile is not None
            and float(pe_percentile) >= ADMISSION_VALUE_PE_PCTL_MIN
            and roe is not None
            and float(roe) < ADMISSION_VALUE_ROE_MAX
        ):
            return (
                "周期型",
                "估值与盈利背离：PE 分位 "
                f"{float(pe_percentile):.1f}%（市场已给高预期）而 ROE 仅 "
                f"{float(roe):.2f}% < {ADMISSION_VALUE_ROE_MAX:g}%，"
                "周期股在盈利未兑现时拿到高估值，缺乏支撑",
            )
        return None

    # ── 成长型：不看 PE 绝对值（高 PE 本身是成长特征），看成长质量 ──
    if is_growth_stock:
        rev_ok = revenue_yoy is not None and float(revenue_yoy) >= ADMISSION_GROWTH_REV_YOY_MIN
        gm_ok = gross_margin is not None and float(gross_margin) >= ADMISSION_GROWTH_GROSS_MARGIN_MIN
        # 两者均缺失 → 放行；任一已知且达标 → 放行
        if (revenue_yoy is None and gross_margin is None) or rev_ok or gm_ok:
            return None
        # 走到这里说明「两者都有值且都不达标」（缺失已在上面放行），
        # 格式化时任一为 None 都会抛 TypeError（曾导致共振索引构建整体失败），
        # 故用 None 安全的自证文案。
        _rev_txt = f"{float(revenue_yoy):.1f}%" if revenue_yoy is not None else "缺失"
        _gm_txt = f"{float(gross_margin):.1f}%" if gross_margin is not None else "缺失"
        return (
            "成长型",
            f"营收增速 {_rev_txt} < {ADMISSION_GROWTH_REV_YOY_MIN:g}% "
            f"且毛利率 {_gm_txt} < {ADMISSION_GROWTH_GROSS_MARGIN_MIN:g}%，"
            "不具成长质量特征",
        )

    # ── 传统价值型：估值与盈利的矛盾组合（核心）+ 极端兜底 ──
    # ① 矛盾组合：估值分位高（市场已给高预期）而 ROE 低（盈利跟不上）
    if (
        pe_percentile is not None
        and roe is not None
        and float(pe_percentile) >= ADMISSION_VALUE_PE_PCTL_MIN
        and float(roe) < ADMISSION_VALUE_ROE_MAX
    ):
        return (
            "传统价值型",
            f"估值与盈利背离：PE 分位 {float(pe_percentile):.1f}%（市场已给高预期）"
            f"而 ROE 仅 {float(roe):.2f}% < {ADMISSION_VALUE_ROE_MAX:g}%，"
            "高估值缺盈利支撑",
        )
    # ② 极端兜底：周转率过低（真僵尸资产）
    #    金融业结构性豁免——银行/券商的总资产周转率天然在 0.02~0.08，
    #    那不是「资产产出效率过低」而是业务模型本身。
    _is_financial = bool(industry) and any(
        k in industry for k in ADMISSION_FINANCIAL_INDUSTRY_KW
    )
    if (
        not _is_financial
        and asset_turnover is not None
        and float(asset_turnover) < ADMISSION_VALUE_TURNOVER_MIN
    ):
        return (
            "传统价值型",
            f"总资产周转率 {float(asset_turnover):.2f} < {ADMISSION_VALUE_TURNOVER_MIN:g}，"
            "资产产出效率过低",
        )
    # ③ 极端兜底：PE 分位到顶（历史最贵区间）
    if (
        pe_percentile is not None
        and float(pe_percentile) >= ADMISSION_VALUE_PE_PCTL_EXTREME
        and (roe is None or float(roe) < 12.0)  # 高 ROE 者（如中国神华12.76）不拦
    ):
        return (
            "传统价值型",
            f"PE 分位 {float(pe_percentile):.1f}% 已达历史极端区间"
            f"{'且 ROE 偏低' if roe is not None else ''}",
        )
    return None


def _verdict(
    fund: float | None,
    tech: int | None,
    *,
    min_fund: float,
    min_tech: float,
    core: float,
    veto: float,
    tech_blocker: str | None = None,
    profile_gate: tuple[str, str] | None = None,
) -> tuple[str, list[str]]:
    """双阈值漏斗判定：淘汰 → 核心持仓 → 买入候选 → 观察。

    缺失值不当作最差：技术面不可评估（无形态共振）时归入「观察」并说明，
    而不是按 <veto 淘汰。

    ``tech_blocker`` 为技术面为 None 时的**否决原因**（目前只有「左侧超跌
    形态防守」会填）。不传时按「本来就没有形态」描述——两者对用户是不同
    的信息：前者是「形态有、但不让买」，后者是「没有形态」。

    ``profile_gate`` 为**分类准入门槛**的否决结果 ``(类型标签, 原因)``，由
    ``classify_admission_gate`` 产出。命中时**只封顶档位为「观察」**，不改写
    ``composite_score`` 与技术面读数（与双阈值同一处置强度，不产生第二个总分）。
    分类适用不同门槛而非用一套绝对阈值一刀切：红利看股息/分红、成长看营收/毛利、
    周期看 PE-PB-ROE 组合陷阱、传统价值看 ROE/周转率——同一指标在四类资产上
    的含义不同（铁路的 0.37 周转率与消费股的 0.37 不是同一回事）。
    """
    if profile_gate is not None:
        _label, _why = profile_gate
        # 分类准入先于档位判定：不满足该类型的最低质量要求，不进候选池。
        # 但已被双阈值判「淘汰」的仍报淘汰（更差），避免理由互相覆盖。
        if fund is not None and fund < veto:
            return "eliminated", [f"基本面 {fund:.1f} < {veto:g}"]
        return "watch", [f"分类准入未通过（{_label}）：{_why}"]
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
        if tech_blocker:
            reasons.append(f"技术面已否决：{tech_blocker}")
        else:
            reasons.append("技术面不可评估（近 2 根K线无达标注形态共振）")
    elif tech < min_tech:
        reasons.append(f"技术面 {tech} < {min_tech:g}")
    return "watch", reasons or ["未同时满足双阈值"]


def _overlay_one(
    symbol: str,
    name: str,
    fund_score: float | None,
    peg: float | None,
    reduce_window: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """单票技术面叠加：K线买点信号（全票）+ 形态共振（达标才有）。

    复用 MarketConfluenceService._scan_job（PatternEngine + evaluate_confluence +
    候选门槛），保证「主板战法 / 信号页」与本榜单的形态共振口径逐位一致。

    ``peg`` 由调用方从因子快照读取（个股分析 engine 口径：3 年净利 CAGR 优先，
    周期底部负 CAGR / 红利资产置 None），**本函数不再自行计算 PEG**。

    ``reduce_window`` 由调用方从因子快照读取（大股东减持窗口期）。窗口开启时
    ``_detect_buy_signal`` 会把强买入封顶为观察（只改档位、不改分数）。
    """
    from app.services.kline_service import KlineService
    from app.services.market_confluence_service import (
        KLINE_LIMIT,
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
        "tech_blocker": None,
    }
    sess = SessionLocal()
    try:
        klines, _kmeta = KlineService(sess).get_recent_klines(symbol, limit=KLINE_LIMIT)
        if len(klines) < OVERLAY_MIN_BARS:
            empty["buy_label"] = "K线不足40根"
            return empty
        # 形态共振先算：买点信号是决策的下游，必须知道「形态是否达标」「是否
        # 被左侧超跌防守否决」，否则会出现「决策=买入候选 / 买点=中性」的悖论。
        diag: dict[str, Any] = {}
        svc = MarketConfluenceService(sess)
        best, _outcome = svc._scan_job(
            _Job(symbol=symbol, name=name, recent_bars=OVERLAY_RECENT_BARS),
            diag,
        )
        pattern_name = (best or {}).get("pattern_name")
        left_side_blocked = bool(best is None and diag.get("left_side_blocked"))
        # 买点信号：MA20/MA60 + 量比 + PEG（PEG 缺失时 _detect_buy_signal 保守不触发）
        buy = _detect_buy_signal(
            klines,
            float(fund_score or 0),
            peg,
            None,
            pattern_name=pattern_name,
            pattern_ready=best is not None,
            left_side_blocked=left_side_blocked,
            reduce_window=reduce_window,
        )
        return {
            "symbol": symbol,
            "name": name,
            "kline_bars": len(klines),
            "buy_signal": buy.get("signal"),
            "buy_label": buy.get("label"),
            "buy_reasons": buy.get("reasons") or [],
            "buy_note": buy.get("note"),
            "peg": peg,
            # 口径自证必须与真实取值一致：原先无论 peg 是否为 None 都硬编码
            # 「3年净利CAGR口径，周期/红利已豁免」，会让人误以为值已读到。
            "peg_source": (
                "个股分析口径（3年净利CAGR优先，周期/红利已豁免）"
                if peg is not None
                else "个股分析未给出 PEG（负/缺失 CAGR 或红利豁免）→ 强买入/观察档保守不触发"
            ),
            "pattern_name": pattern_name,
            "pattern_score": (best or {}).get("pattern_score"),
            "confluence_effective": (best or {}).get("confluence_effective"),
            "confluence_hits": (best or {}).get("confluence_hits"),
            "confluence_detail": (best or {}).get("confluence_detail"),
            "combined_score": (best or {}).get("combined_score"),
            "tech_score": _tech_score((best or {}).get("combined_score")),
            "pattern_date": (best or {}).get("candle_date"),
            # 技术面为 None 时说明「为什么不可评估」：被左侧超跌防守否决的票
            # 与「本来就没有形态」的票，用户看到的文案必须不同。
            # 第三类：有达标形态（组合分≥80）但无核心共振（量能/趋势）被
            # 白名单拦下 —— 属「凑单式共振」，必须与「形态逻辑坏了」区分。
            "tech_blocker": (
                diag.get("left_side_blocked")
                if best is None and diag.get("left_side_blocked")
                else diag.get("weak_resonance_blocked")
                if best is None and diag.get("weak_resonance_blocked")
                else None
            ),
            # 减持窗口期闸门留痕：signal 被门控时 _detect_buy_signal 会带 gate 字段
            "signal_gate": buy.get("gate"),
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
    peg_map: dict[str, float | None],
    *,
    force: bool = False,
    progress: Any = None,
) -> dict[str, dict[str, Any]]:
    """对给定标的池计算技术面（带单票缓存 + 失败串行重试）。

    ``peg_map`` 为 **个股分析口径** 的 PEG（``_snapshot_peg_map``），逐票透传给
    买点信号判定；缺失即按保守口径不触发强买入。

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
            peg_map.get(sym),
            # 减持窗口期来自因子快照（同一次 payload 读取，不额外发起请求）
            it.get("reduce_window"),
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
    sort_order: str = "desc",
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
        sort_order=sort_order,
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

    peg_map = _snapshot_peg_map(db, [it["symbol"] for it in pool])
    results, retry_count, computed_count = _compute_tech_map(pool, peg_map, force=force)

    items: list[dict[str, Any]] = []
    for it in pool:
        merged = dict(it)
        merged.update(results.get(it["symbol"]) or {})
        _gate = merged.get("profile_gate")
        v, v_reasons = _verdict(
            merged.get("composite_score"),
            merged.get("tech_score"),
            min_fund=min_fund,
            min_tech=min_tech,
            core=core_score,
            veto=veto_score,
            tech_blocker=merged.get("tech_blocker"),
            profile_gate=(tuple(_gate) if _gate else None),
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
                "PEG 取自个股分析（3 年净利 CAGR 口径，周期底部/红利资产已豁免），PEG 完整"
                if peg_available == len(page_items)
                else f"{peg_available}/{len(page_items)} 只可取到 PEG"
                "（其余为缺失：负增长/周期底部/红利资产按口径置空，或旧快照未含相对估值）；"
                "PEG 缺失的票按保守口径不触发强买入——这不是数据缺口，多数是口径豁免"
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
            "观察=基本面≥80 + PEG>2 + 回踩MA60(±3%)缩量；短线博弈=基本面<60 但突破MA20且放量；"
            "左侧超跌观察=左侧反转形态达标但收盘仍在MA60下方且当日无量，等放量站上MA60；"
            "右侧底部企稳=左侧形态 + 站上MA20 + 放量；形态达标待确认=通过候选门槛但形态属延续类，等回踩。"
            "买点信号是决策的下游：凡通过形态共振候选门槛的票，信号不会停在「趋势未确认」，"
            "避免出现「决策=买入候选 / 买点=中性」的自相矛盾。"
            "PEG 取个股分析口径（3 年净利 CAGR 优先；周期底部负增长与红利资产按口径置空，"
            "不适用 PEG 者不触发该信号，也不另用 PB-ROE 造第二套估值尺子）。",
            "左侧抄底防守：左侧反转形态（平底锅底部/破低反涨/看涨吞没/启明星/锤子线等）"
            "若收盘在 MA60 下方且当日量比 <1.2，按「无量确认的下跌中继」处理，"
            "记 structure_flaw 并让技术面不可评估（与「均线空头排列」同一处置强度）。"
            "即在 MA60 上方、或当日放量者不受此限。",
            "形态共振口径：Nison 蜡烛+西方指标 bullish 形态（PatternEngine≥60 分），"
            "叠加趋势/动量/波动/量价/结构正交共振（含周线趋势多周期确认），"
            "组合分 = 形态分 + 有效共振数×6，与「主板战法 / 信号页」同源。"
            "K 线窗口 180 根（≈36 周），与「技术面成文分析」同源——同一只票的周线趋势在两处必须一致。",
            "量能阈值随量能波动率自适应：k=clip(近20日量能变异系数/0.35, 0.85, 1.25)，"
            "放量线 1.5k、温和放量线 1.2k、缩量线 0.78/k。量能基线平稳的缩量行情里 "
            "1.3~1.4 倍即可算有效突破，高波动票则需更大倍数才算「异常」。",
            "行业共振为板块效应统计（同板块≥2 只出现信号才列出），不改变个股信号。",
            f"决策标签（共振过滤法）：基本面 ≥{min_fund:g} 且 技术面 ≥{min_tech:g} → 买入候选；"
            f"两者均 ≥{core_score:g} → 核心持仓；任一 <{veto_score:g} → 淘汰；其余为观察。"
            "标签只做分类，`composite_score` 与技术面原始读数均不改写，不产生第二个综合分。",
            f"技术面得分 = 信号页共振组合分（形态分 + 有效共振数×6）截断到 0~100，"
            "无达标形态共振时为缺失（不可评估，不赋 0、不赋中性）；"
            "系统候选门槛为 80，故有形态共振者天然 ≥80，"
            f"因此 min_tech={min_tech:g} 在本数据上等价于「有达标形态共振」，真正的区分线是 {core_score:g}。"
            "大盘环境不改分：指数状态另由 /fundamentals/market-scan/market-regime 以"
            "展示提示提供（scoring_impact=none），是否据此抬高门槛由使用者决定；"
            "个股资金面亦不进入技术面得分。",
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
        peg_map = _snapshot_peg_map(None, [it["symbol"] for it in pool])
        results, retry_count, _computed = _compute_tech_map(
            pool, peg_map, force=force, progress=progress
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
        _gate = raw.get("profile_gate")
        v, reasons = _verdict(
            raw.get("composite_score"),
            raw.get("tech_score"),
            min_fund=min_fund,
            min_tech=min_tech,
            core=core_score,
            veto=veto_score,
            tech_blocker=raw.get("tech_blocker"),
            profile_gate=(tuple(_gate) if _gate else None),
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
        "技术面口径（2026-09-21 修订）：K 线窗口 180 根（≈36 周，周线趋势判定与"
        "「技术面成文分析」同源）；RSI 命中区间放宽至 28~60 / 40~72 并对弱势区降权 0.6；"
        "量能阈值随近 20 日量能波动率自适应（k=clip(CV/0.35, 0.85, 1.25)）。",
        "PEG 取个股分析口径（3 年净利 CAGR 优先；周期底部负增长与红利资产按口径置空），"
        "扫描层不另算 PEG，也不引入 PB-ROE 等第二套估值尺子。",
        "档位分布统计基于当前筛选条件命中的全部标的（非仅本页）；"
        "「仅看某档」为服务端过滤，翻页 / 计数口径一致。",
        "大盘环境不参与本视图的分数与阈值；指数状态见"
        " /fundamentals/market-scan/market-regime（scoring_impact=none）。",
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
        "quote_overlay": dict(_QUOTE_LAST["data"]),
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


def quality_value_view(
    db: Session | None = None,
    *,
    top: int = DEFAULT_TOP,
    offset: int = 0,
    exclude_st: bool = True,
    include_gem: bool = False,
    industry: str | None = None,
    keyword: str | None = None,
    max_pe: float | None = None,
    max_pb: float | None = None,
    min_roe: float | None = None,
    min_roic: float | None = None,
    max_debt_ratio: float | None = None,
    min_gross_margin: float | None = None,
    min_ocf_np: float | None = None,
    min_dividend_yield: float | None = None,
    max_pe_pctile: float | None = None,
    max_pb_pctile: float | None = None,
    max_peg: float | None = None,
    min_market_cap_yi: float | None = None,
    sort_by: str = "qv_score",
    with_cycle_price: bool = False,
    exclude_cycle_warnings: bool = False,
) -> dict[str, Any]:
    """「质量 × 价值」选股视图（读层，**不重算任何分数**）。

    判定链路的完整说明与**四处偏离通用模板的理由**见 ``quality_value.py``
    模块 docstring（ROIC 阈值下调、毛利率行业中性、现金流用 5 年均值、不建第二总分）。

    本函数只负责：① 读轻量行 ② 交 ``compute_qv`` 判定打分 ③ 排序分页 ④ 组装响应。
    评分口径集中在 quality_value.py，此处不重复实现（铁律 5：同一判定不两处各算）。

    ``exclude_cycle_warnings=True`` 时，周期品价格预警命中「周期陷阱(trap) /
    景气高位(peak)」的条目**不进结果集**（用户口径：选股结果只展示达标票）。
    剔除只发生在展示层（不回写任何分数），剔除计数按档位留在
    ``cycle_price.excluded`` 里自证；被剔除的票不会参与分页，故
    ``matched`` / ``has_more`` 与返回条目始终一致。
    """
    from app.services.quality_value import (
        QV_SORT_KEYS,
        compute_qv,
        sort_qv,
    )

    if sort_by not in QV_SORT_KEYS:
        raise ValueError(f"不支持的排序字段：{sort_by}")

    rows, fallback = load_covered(db)
    # 与榜单/共振视图同一套基础过滤（ST、板块、名称/行业），保证三处口径一致
    kw = (keyword or "").strip().lower()
    ind_filter = (industry or "").strip()
    base = [
        r
        for r in rows
        if _item_passes(
            r,
            exclude_st=exclude_st,
            include_gem=include_gem,
            keyword=kw,
            industry=ind_filter,
            min_composite=None,
            min_market_cap_yi=None,
        )
    ]

    # 只把显式传入的阈值交给 compute_qv，未传的走模块默认值（集中定义，便于调参）
    overrides: dict[str, Any] = {}
    for k, v in (
        ("max_pe", max_pe),
        ("max_pb", max_pb),
        ("min_roe", min_roe),
        ("min_roic", min_roic),
        ("max_debt_ratio", max_debt_ratio),
        ("min_gross_margin", min_gross_margin),
        ("min_ocf_np", min_ocf_np),
        ("min_dividend_yield", min_dividend_yield),
        ("max_pe_pctile", max_pe_pctile),
        ("max_pb_pctile", max_pb_pctile),
        ("max_peg", max_peg),
        ("min_market_cap_yi", min_market_cap_yi),
    ):
        if v is not None:
            overrides[k] = v

    passed, rejected = compute_qv(base, **overrides)
    passed = sort_qv(passed, sort_by)

    cycle_price_attached = 0
    cycle_excluded: dict[str, int] = {}
    if with_cycle_price:
        # 周期品「产品价格拐点」预警：**只读增强**，不改写 qv_score / composite_score，
        # 也不参与任何硬门槛（理由见 quality_value/cycle_price 模块 docstring）。
        # 默认关闭：单元测试必须零外网依赖，由 API 层显式开启。
        from app.services.cycle_price import attach_cycle_price

        cycle_price_attached = attach_cycle_price(passed)

        # 展示口径（用户要求）：命中「周期陷阱/景气高位」的票不进结果集。
        # 只切分列表、不回写任何分数；剔除计数在 cycle_price_meta.excluded 留痕。
        if exclude_cycle_warnings:
            from app.services.cycle_price import split_cycle_warnings

            passed, cycle_excluded = split_cycle_warnings(passed)

    limit = max(1, min(int(top), MAX_TOP))
    start = max(0, int(offset or 0))
    window = passed[start : start + limit]

    notes = [
        "本视图为**读层筛选**：输入全部取自已有因子快照，不重算任何分数，"
        "也不产生第二个综合分（准入选股用硬门槛 + 行业内分位；qv_score 仅用于排序展示）。",
        "质量门槛：ROE ≥ 10、ROIC ≥ 8、毛利率行业中位以上、资产负债率 ≤ 60、"
        "经营现金流/净利润（**5 年均值**）≥ 0.8。",
        "价值门槛：PE 或 PB **任一**处于行业内低分位（PE ≤ 40% 或 PB ≤ 50%），"
        "绝对 PE / PB 上限只作兜底 —— 跨行业直接比 PE 会把银行与科技股放在同一把尺子上。",
        "毛利率门槛对结构性低毛利行业（工业金属、贸易、建筑、电力等）只认行业内分位，"
        "绝对阈值不适用（江西铜业毛利率 4.4%，用绝对门槛会误杀整条产业链）。",
        "PEG 优先级最低且**缺失不淘汰**：红利资产被估值口径置空 PEG（如贵州茅台），"
        "把 PEG 当硬门槛会整体误杀这类票。缺失项在 qv_missing 里逐票留痕。",
        "行业分位基准 = 当前筛选命中集（与榜单 industry_pct 同源），"
        "故叠加地域/行业筛选后分位不会因样本变小而失真。",
        "「资产负债率 ≤ 60%」对银行/保险/券商**不适用**（负债经营是本业）："
        "金融股请勿叠加本视图，或自行放宽该阈值。",
    ]
    if fallback:
        notes.append("本次读取走了 Python 解析回退路径（SQLite JSON1 不可用）。")
    if with_cycle_price:
        notes.append(
            "已附加**周期品产品价格拐点预警**（items[].cycle_price，含 products/costs 明细）："
            "只看产品价（原油等**成本项**不计入预警，故油价回落不会把煤化工/乙烷裂解标成利空）；"
            "该预警**只读**，不改写任何分数、不参与硬门槛。"
        )
        if exclude_cycle_warnings:
            n_exc = sum(cycle_excluded.values())
            detail = "、".join(f"{k} {v} 只" for k, v in sorted(cycle_excluded.items())) or "0 只"
            notes.append(
                f"已剔除周期预警票 {n_exc} 只（{detail}）：命中「周期陷阱(trap) / 景气高位(peak)」"
                "的条目不进结果集（用户口径：选股结果只展示达标票）；剔除只发生在展示层，"
                "不改写任何分数，计数在本 cycle_price.excluded 留痕。"
            )

    # 被硬门槛拦下的样本摘要：让「为什么这只票没进」可自证（按首个原因归并计数）
    reject_summary: dict[str, int] = {}
    for r in rejected:
        head = (r.get("qv_reasons") or ["未达标"])[0].split(":")[0]
        reject_summary[head] = reject_summary.get(head, 0) + 1

    # 周期品预警的覆盖率自检 + 命中分布（让「为什么这票没预警」可自证）
    cycle_price_meta: dict[str, Any] | None = None
    if with_cycle_price:
        from app.services.cycle_price import coverage_report, grade_of

        grades: dict[str, int] = {}
        for r in passed:
            g = grade_of(r)
            grades[g] = grades.get(g, 0) + 1
        mapped = sum(v for k, v in grades.items() if k != "na")
        cycle_price_meta = {
            "enabled": True,
            "attached": cycle_price_attached,
            "mapped": mapped,
            "grade_summary": grades,
            "excluded": cycle_excluded,
            "coverage": coverage_report([r.get("industry") for r in passed]),
        }

    return {
        "count": len(window),
        "matched": len(passed),
        "rejected": len(rejected),
        "reject_summary": reject_summary,
        "offset": start,
        "limit": limit,
        "has_more": start + len(window) < len(passed),
        "sort_by": sort_by,
        "thresholds": {
            "min_roe": overrides.get("min_roe", 10.0),
            "min_roic": overrides.get("min_roic", 8.0),
            "min_gross_margin": overrides.get("min_gross_margin", 15.0),
            "max_debt_ratio": overrides.get("max_debt_ratio", 60.0),
            "min_ocf_np": overrides.get("min_ocf_np", 0.8),
            "max_pe": overrides.get("max_pe", 50.0),
            "max_pb": overrides.get("max_pb", 8.0),
            "max_pe_pctile": overrides.get("max_pe_pctile", 40.0),
            "max_pb_pctile": overrides.get("max_pb_pctile", 50.0),
            "max_peg": overrides.get("max_peg", 1.5),
            "min_dividend_yield": overrides.get("min_dividend_yield", 0.0),
            "min_market_cap_yi": overrides.get("min_market_cap_yi", 50.0),
        },
        "filters": {
            "exclude_st": exclude_st,
            "include_gem": include_gem,
            "industry": ind_filter or None,
            "keyword": kw or None,
        },
        "items": window,
        # 被筛掉的票只回传前 200 只（UI 折叠展示用），避免响应体膨胀
        "rejected_sample": rejected[:200],
        "coverage": market_coverage(db),
        "quote_overlay": dict(_QUOTE_LAST["data"]),
        "cycle_price": cycle_price_meta,
        "notes": notes,
    }
