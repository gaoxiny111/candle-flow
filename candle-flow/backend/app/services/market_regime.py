"""大盘环境（指数状态）—— 只读展示层，**不参与任何打分**。

设计约束：

1. **不入分**：不作为技术面得分的构成项，也不自动改写任何阈值。
   口径铁律要求「不造第二套权重表」，因此本模块只输出市场环境提示，
   是否据此调整门槛由用户决定（前端给可点的建议，不静默生效）。
2. **可自证**：判定规则随响应返回（``rule`` 字段），并给出每个指数的
   MA20/MA60/20 日涨跌/周线趋势等原始读数，便于复核。
3. **数据源**：沪深300(000300.SH) 与上证指数(000001.SH) 日线，与个股同一 K 线源
   （库内不足 60 根时按需同步一次，失败则该指数标记为「数据不足」而不影响其他）。
4. **缺失不冒充**：指数行情取不到时返回 ``unknown``，不使用「中性 50」之类
   的伪造读数（与分位缺失值处理同口径）。
"""

from __future__ import annotations

import logging
import time
from datetime import date, datetime
from typing import Any

from sqlalchemy.orm import Session

from app.core.timeframe import weekly_trend_at
from app.database import SessionLocal

logger = logging.getLogger(__name__)

# 主要指数：(symbol, 中文名)。沪深300 权重更高，用于合成时优先。
REGIME_INDEXES: tuple[tuple[str, str], ...] = (
    ("000300.SH", "沪深300"),
    ("000001.SH", "上证指数"),
)

REGIME_TTL_SEC = 600
REGIME_KLINE_LIMIT = 180
REGIME_MIN_BARS = 60

REGIME_LABELS: dict[str, str] = {
    "risk_on": "偏多",
    "neutral": "震荡",
    "risk_off": "偏空",
    "unknown": "数据不足",
}

REGIME_ADVICE: dict[str, str] = {
    "risk_on": "大盘趋势向上，共振信号可按既定门槛解读，核心持仓/买入候选可正常跟踪。",
    "neutral": "大盘区间震荡，共振信号需要基本面支撑，不宜单靠技术面追高。",
    "risk_off": "大盘处于空头趋势，建议提高技术面门槛（把「技术面≥」从 70 提到 85）"
    "或只看核心持仓，并主动压缩仓位。",
    "unknown": "指数行情暂不可得，本条提示不参与任何判定。",
}

# 合成规则（写进返回体供复核）
REGIME_RULE = (
    "单指数：收盘 > MA20 > MA60 且（周线向上 或 20 日涨幅>0）→ 偏多；"
    "收盘 < MA20 < MA60 且（周线向下 或 20 日涨幅<0）→ 偏空；其余为震荡。"
    "两指数合成：全部偏空 → 偏空；全部偏多 → 偏多；不一致 → 震荡。"
    "取数时若指数未更新到最新交易日会先按需同步；仍落后的指数排除在合成之外"
    "（不同交易日的读数不能混算），并在 stale_indexes 中列出。"
)

_cache: dict[str, Any] = {"ts": 0.0, "payload": None}


def _sma(values: list[float], period: int) -> float | None:
    if len(values) < period:
        return None
    return sum(values[-period:]) / period


def _latest_date(klines: list) -> date | None:
    if not klines:
        return None
    raw = getattr(klines[-1], "date", None)
    if isinstance(raw, datetime):
        return raw.date()
    if isinstance(raw, date):
        return raw
    if raw is None:
        return None
    try:
        return date.fromisoformat(str(raw)[:10])
    except ValueError:
        return None


def _is_stale(klines: list) -> bool:
    """最新一根是否落后于目标交易日。

    指数不在主板定时同步范围内（该任务只覆盖主板非 ST 个股），所以指数数据
    会随时间变旧。两个指数若停在**不同交易日**，用它们的读数合成「大盘环境」
    是伪精度——所以只做基准对齐的合成。
    """
    d = _latest_date(klines)
    if d is None:
        return True
    try:
        from app.services.main_board_kline_sync import target_trade_date

        return d < target_trade_date()
    except Exception:
        return False


def _load_klines(symbol: str, db: Session | None) -> list:
    """读指数日线；不足或落后于目标交易日时按需同步一次（失败即返回已有数据）。"""
    from app.services.kline_service import KlineService

    own = db is None
    sess = db or SessionLocal()
    try:
        svc = KlineService(sess)
        klines, _meta = svc.get_recent_klines(symbol, limit=REGIME_KLINE_LIMIT)
        if len(klines) < REGIME_MIN_BARS or _is_stale(klines):
            try:
                svc.sync(symbol)
            except Exception as exc:  # 数据源不可用时不阻断，交给上层标 stale
                logger.debug("market regime sync failed for %s: %s", symbol, exc)
            klines, _meta = svc.get_recent_klines(symbol, limit=REGIME_KLINE_LIMIT)
        return klines
    finally:
        if own:
            sess.close()


def _index_regime(symbol: str, name: str, klines: list) -> dict[str, Any]:
    bars = len(klines)
    as_of = _latest_date(klines)
    stale = _is_stale(klines) if bars else True
    base: dict[str, Any] = {
        "symbol": symbol,
        "name": name,
        "bars": bars,
        "as_of": as_of.isoformat() if as_of else None,
        "stale": stale,
    }
    if bars < REGIME_MIN_BARS:
        return {**base, "regime": "unknown", "reason": f"K线不足 {REGIME_MIN_BARS} 根（{bars}）"}

    closes = [float(k.close) for k in klines]
    close = closes[-1]
    ma20 = _sma(closes, 20)
    ma60 = _sma(closes, 60)
    chg20 = round((close / closes[-21] - 1) * 100, 2) if bars >= 21 and closes[-21] else None
    weekly = weekly_trend_at(klines, bars - 1)

    if ma20 is not None and ma60 is not None and close > ma20 > ma60:
        trend = "bull"
    elif ma20 is not None and ma60 is not None and close < ma20 < ma60:
        trend = "bear"
    else:
        trend = "range"

    if trend == "bear" and (weekly == "down" or (chg20 is not None and chg20 < 0)):
        regime = "risk_off"
    elif trend == "bull" and (weekly == "up" or (chg20 is not None and chg20 > 0)):
        regime = "risk_on"
    else:
        regime = "neutral"

    trend_cn = {"bull": "多头排列", "bear": "空头排列", "range": "均线纠缠"}[trend]
    weekly_cn = {"up": "向上", "down": "向下", "sideways": "震荡"}.get(weekly, weekly)
    reason = (
        f"收盘 {close:.2f}，MA20 {ma20:.2f}，MA60 {ma60:.2f}（{trend_cn}）；"
        f"周线{weekly_cn}；近 20 日 {chg20:+.2f}%"
        if ma20 is not None and ma60 is not None and chg20 is not None
        else f"收盘 {close:.2f}，均线数据不足"
    )
    return {
        **base,
        "regime": regime,
        "label": REGIME_LABELS[regime],
        "trend": trend,
        "weekly_trend": weekly,
        "close": round(close, 2),
        "ma20": None if ma20 is None else round(ma20, 2),
        "ma60": None if ma60 is None else round(ma60, 2),
        "change_20d_pct": chg20,
        "reason": reason,
    }


def market_regime(db: Session | None = None, *, force: bool = False) -> dict[str, Any]:
    """大盘环境提示（只读）。

    返回体明确带 ``scoring_impact: "none"``：本结果不进入 tech_score、
    不改变任何阈值，只用于前端提示。
    """
    now = time.time()
    if (
        not force
        and _cache["payload"] is not None
        and now - float(_cache["ts"]) < REGIME_TTL_SEC
    ):
        cached = dict(_cache["payload"])
        cached["cached"] = True
        cached["cache_age_sec"] = int(now - float(_cache["ts"]))
        return cached

    items: list[dict[str, Any]] = []
    for symbol, name in REGIME_INDEXES:
        try:
            klines = _load_klines(symbol, db)
        except Exception as exc:
            logger.warning("market regime load failed for %s: %s", symbol, exc)
            klines = []
        items.append(_index_regime(symbol, name, klines))

    usable = [it for it in items if it["regime"] != "unknown"]
    fresh = [it for it in usable if not it.get("stale")]
    pool = fresh or usable  # 全部陈旧时退回全部（有读数胜于 unknown），但如实标注
    known = [it["regime"] for it in pool]
    if not known:
        regime = "unknown"
    elif all(r == "risk_off" for r in known):
        regime = "risk_off"
    elif all(r == "risk_on" for r in known):
        regime = "risk_on"
    else:
        regime = "neutral"

    stale_names = [it["name"] for it in usable if it.get("stale")]
    basis = "、".join(f"{it['name']}（截至 {it['as_of'] or '—'}）" for it in pool)
    note = (
        "大盘环境**不参与**技术面得分与任何阈值，仅作展示提示；"
        "是否按其建议调整门槛（min_tech）由使用者决定，系统不会自动改写。"
    )
    if stale_names:
        if fresh:
            note += (
                f"　注意：{'、'.join(stale_names)} 行情未更新到最新交易日，"
                "已排除在本次合成之外（不同交易日的读数合成属伪精度）。"
            )
        else:
            note += (
                f"　注意：{'、'.join(stale_names)} 行情均未更新到最新交易日，"
                "本次读数按现有数据给出并标记 stale，请谨慎参考。"
            )

    payload = {
        "regime": regime,
        "label": REGIME_LABELS[regime],
        "advice": REGIME_ADVICE[regime],
        "items": items,
        "rule": REGIME_RULE,
        "basis": basis,
        "stale_indexes": stale_names,
        "scoring_impact": "none",
        "note": note,
        "cached": False,
        "cache_age_sec": 0,
    }
    _cache["ts"] = now
    _cache["payload"] = payload
    return payload
