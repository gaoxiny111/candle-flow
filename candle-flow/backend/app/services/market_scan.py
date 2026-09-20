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

DEFAULT_TOP = 50
MAX_TOP = 500

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
OVERLAY_TOP_LIMIT = 120
OVERLAY_CACHE_TTL_SEC = 600
_overlay_cache: dict[str, Any] = {"ts": 0.0, "key": None, "payload": None}

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


def scan_market(
    db: Session | None = None,
    *,
    top: int = DEFAULT_TOP,
    min_composite: float | None = None,
    min_market_cap_yi: float | None = None,
    exclude_st: bool = True,
    industry: str | None = None,
    sort_by: str = "composite_score",
) -> dict[str, Any]:
    """全市场基本面排序。

    Args:
        top: 返回条数（上限 500）。
        min_composite: 综合分下限（沿用个股分析口径的 0~100 分）。
        min_market_cap_yi: 总市值下限，单位**亿元**。
        exclude_st: 剔除证券简称含 ST / 退 的标的（数据源唯一可得的风险剔除项）。
        industry: 行业名包含匹配（如「银行」「煤炭」）。
        sort_by: `composite_score`（默认）或五个维度键之一
            （profitability/growth/cashflow/solvency/valuation），
            维度排序只是展示排序，不合成新总分。
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

    # 分位基准 = 全部已覆盖样本（先算分位，再过滤），保证「全市场分位」语义稳定
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

    ind_filter = (industry or "").strip()
    selected: list[dict[str, Any]] = []
    for r in rows:
        name = str(r.get("name") or "")
        if exclude_st and any(mk in name for mk in ST_MARKERS):
            continue
        comp = _to_float(r.get("composite_score"))
        if min_composite is not None and (comp is None or comp < min_composite):
            continue
        mcap = _to_float(r.get("market_cap"))
        if min_market_cap_yi is not None:
            if mcap is None or mcap < float(min_market_cap_yi) * 1e8:
                continue
        ind = str(r.get("industry") or "")
        if ind_filter and ind_filter not in ind:
            continue

        item: dict[str, Any] = {
            "symbol": r["symbol"],
            "name": name,
            "industry": ind,
            "composite_score": None if comp is None else round(comp, 2),
            "final_rating": r.get("final_rating"),
            "risk_level_label": r.get("risk_level_label"),
            "pe_ttm": _to_float(r.get("pe_ttm")),
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
        selected.append(item)

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
    coverage = market_coverage(db)
    notes = [
        f"排序依据为个股分析的综合分（单一权威口径，权重 {MODULE_WEIGHTS_NOTE}），"
        "扫描层不重算分数，不新增第二套权重。",
        "market_pct / industry_pct 为展示用横截面分位（0~100，越高越好），"
        "不参与排序；缺失值不参与排名、也不赋中性分。",
        f"分位基准为已覆盖样本 {len(market_pct)} 只"
        f"（占 SH·SZ {coverage['universe']} 只的 {coverage['coverage_pct']}%），"
        "缺口源于单次因子构建预算截断，未经覆盖的标的不在榜内。",
        "本数据源无「审计意见」「日均成交额」字段，故未提供这两项过滤。",
    ]
    if fallback:
        notes.append("本次读取走了 Python 解析回退路径（SQLite JSON1 不可用）。")

    return {
        "coverage": coverage,
        "percentile_base": len(market_pct),
        "count": len(selected[:limit]),
        "matched": len(selected),
        "sort_by": key,
        "filters": {
            "min_composite": min_composite,
            "min_market_cap_yi": min_market_cap_yi,
            "exclude_st": exclude_st,
            "industry": ind_filter or None,
        },
        "items": selected[:limit],
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
            "pattern_date": (best or {}).get("candle_date"),
        }
    except Exception:
        logger.debug("technical overlay failed for %s", symbol, exc_info=True)
        empty["buy_label"] = "分析失败"
        return empty
    finally:
        sess.close()


def technical_overlay(
    db: Session | None = None,
    *,
    top: int = 60,
    min_composite: float | None = None,
    min_market_cap_yi: float | None = None,
    exclude_st: bool = True,
    industry: str | None = None,
    sort_by: str = "composite_score",
    force: bool = False,
) -> dict[str, Any]:
    """榜单技术共振叠加（三层漏斗的第二、三层）。

    第一层 = scan_market 的基本面榜单（同一口径，本函数内部复用）；
    第二层 = K线买点信号（趋势 MA20/60 + 量价 + PEG，_detect_buy_signal）；
    第三层 = 形态共振（bullish 形态 × 趋势/动量/波动/量价/结构正交共振 +
    周线趋势多周期确认，evaluate_confluence；无达标形态即为「无共振」）。

    共振得分沿用 `_combined_score = 形态分 + 有效共振数×6`（与信号页同源），
    不采用「趋势×1.5+动量×1.2」式第二套权重。
    """
    from concurrent.futures import ThreadPoolExecutor, as_completed

    base = scan_market(
        db,
        top=OVERLAY_TOP_LIMIT,
        min_composite=min_composite,
        min_market_cap_yi=min_market_cap_yi,
        exclude_st=exclude_st,
        industry=industry,
        sort_by=sort_by,
    )
    pool = base["items"][: max(1, min(int(top), OVERLAY_TOP_LIMIT))]

    cache_key = (
        tuple(sorted((base.get("filters") or {}).items())),
        base.get("sort_by"),
        len(pool),
        tuple(it["symbol"] for it in pool),
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

    def _job(it: dict[str, Any]) -> dict[str, Any]:
        sym = it["symbol"]
        return _overlay_one(
            sym,
            it.get("name") or "",
            it.get("composite_score"),
            it.get("pe_ttm"),
            py_map.get(sym),
        )

    results: dict[str, dict[str, Any]] = {}
    with ThreadPoolExecutor(max_workers=OVERLAY_WORKERS) as ex:
        futures = {ex.submit(_job, it): it["symbol"] for it in pool}
        for fut in as_completed(futures):
            sym = futures[fut]
            try:
                results[sym] = fut.result(timeout=30.0)
            except Exception:
                logger.debug("overlay job failed for %s", sym, exc_info=True)
                results[sym] = {
                    "symbol": sym, "name": "", "kline_bars": 0,
                    "buy_signal": "insufficient_data", "buy_label": "分析失败",
                    "buy_reasons": [], "pattern_name": None,
                }

    items: list[dict[str, Any]] = []
    for it in pool:
        merged = dict(it)
        merged.update(results.get(it["symbol"]) or {})
        items.append(merged)

    # 行业共振：同一板块多只出现买点信号 → 板块效应统计（仅展示，不改个股信号）
    industry_stats: dict[str, dict[str, int]] = {}
    for it in items:
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
    for it in items:
        s = str(it.get("buy_signal") or "unknown")
        sig_counts[s] = sig_counts.get(s, 0) + 1
    peg_available = sum(1 for it in items if it.get("peg") is not None)

    payload = {
        "count": len(items),
        "items": items,
        "signal_counts": sig_counts,
        "industry_confluence": industry_confluence,
        "stats": {
            "kline_ok": sum(1 for it in items if (it.get("kline_bars") or 0) >= OVERLAY_MIN_BARS),
            "pattern_hits": sum(1 for it in items if it.get("pattern_name")),
            "peg_available": peg_available,
            "peg_note": (
                "快照均携带净利同比，PEG 完整"
                if peg_available == len(items)
                else f"仅 {peg_available}/{len(items)} 只快照携带净利同比（旧快照未落库该字段），"
                "PEG 缺失的票按保守口径不触发强买入；明日盘后增量批跑后补齐"
            ),
        },
        "filters": base.get("filters"),
        "sort_by": base.get("sort_by"),
        "coverage": base.get("coverage"),
        "cached": False,
        "notes": [
            "买点信号口径：强买入=基本面≥80 + PEG<1.5 + 站上MA20 + 近5日量能较20日均量放大20%+；"
            "观察=基本面≥80 + PEG>2 + 回踩MA60(±3%)缩量；短线博弈=基本面<60 但突破MA20且放量。",
            "形态共振口径：Nison 蜡烛+西方指标 bullish 形态（PatternEngine≥60 分），"
            "叠加趋势/动量/波动/量价/结构正交共振（含周线趋势多周期确认），"
            "组合分 = 形态分 + 有效共振数×6，与「主板战法 / 信号页」同源。",
            "行业共振为板块效应统计（同板块≥2 只出现信号才列出），不改变个股信号。",
        ],
    }
    _overlay_cache["ts"] = now
    _overlay_cache["key"] = cache_key
    _overlay_cache["payload"] = payload
    return payload
