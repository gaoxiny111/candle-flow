"""Layer-2 strategic positioning: PE percentile + weekly/monthly candle zones."""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Any, Optional

from sqlalchemy.orm import Session

from app.core.candle import Candle, PatternResult
from app.core.pattern_engine import PatternEngine
from app.core.timeframe import bars_to_candles, to_monthly, to_weekly
from app.models.fundamental import FundamentalCandidate
from app.services.fundamental_screen import candidate_out, list_pool
from app.services.kline_service import KlineService
from app.services.valuation import get_valuations

logger = logging.getLogger(__name__)

PE_CHEAP = 20.0
PE_RICH = 80.0
PE_MID_LOW = 30.0
PE_MID_HIGH = 70.0
RECENT_BARS = 3  # pattern must land in last N HTF bars
FLAT_LOOKBACK = 8
FLAT_TOL = 0.025  # lows within 2.5%

BOTTOM_PATTERNS = frozenset(
    {
        "锤子线",
        "倒锤子线",
        "看涨吞没",
        "启明星",
        "十字启明星",
        "红三兵",
        "平头底部",
        "塔形底部",
        "平底",
        "平底锅底部",
    }
)
TOP_PATTERNS = frozenset(
    {
        "上吊线",
        "流星线",
        "看跌吞没",
        "黄昏星",
        "十字黄昏星",
        "三只乌鸦",
        "乌云盖顶",
        "平头顶部",
        "塔形顶部",
        "圆形顶部",
    }
)

ZONE_LABEL = {
    "bottom": "底部区域（重点买入）",
    "top": "顶部区域（卖出/回避）",
    "mid": "中间区域（持有/观望）",
    "conflict": "冲突观望",
}
ZONE_ACTION = {
    "bottom": "买入观察（等确认，不抢跑）",
    "top": "减仓或回避",
    "mid": "持有不动，不加不减",
    "conflict": "信号冲突，观望为主",
}


@dataclass
class HitPattern:
    name: str
    date: str
    score: float
    confirmed: bool
    timeframe: str  # weekly | monthly

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "date": self.date,
            "score": round(self.score, 1),
            "confirmed": self.confirmed,
            "timeframe": self.timeframe,
        }


@dataclass
class PositionResult:
    symbol: str
    zone: str
    label: str
    action: str
    valuation_bias: str
    pe_percentile: float | None
    weekly_patterns: list[HitPattern] = field(default_factory=list)
    monthly_patterns: list[HitPattern] = field(default_factory=list)
    notes: str = ""
    error: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "zone": self.zone,
            "label": self.label,
            "action": self.action,
            "valuation_bias": self.valuation_bias,
            "pe_percentile": self.pe_percentile,
            "weekly_patterns": [h.to_dict() for h in self.weekly_patterns],
            "monthly_patterns": [h.to_dict() for h in self.monthly_patterns],
            "notes": self.notes,
            "error": self.error,
        }


def _candle_date(c: Candle) -> str:
    ts = c.timestamp
    if isinstance(ts, datetime):
        return ts.date().isoformat()
    return str(ts)[:10]


def _detect_flat_bottom(candles: list[Candle], lookback: int = FLAT_LOOKBACK) -> PatternResult | None:
    """Approximate 平底: recent lows cluster without a decisive break."""
    if len(candles) < lookback:
        return None
    window = candles[-lookback:]
    lows = [c.low for c in window]
    low_min = min(lows)
    if low_min <= 0:
        return None
    near = [x for x in lows if abs(x - low_min) / low_min <= FLAT_TOL]
    if len(near) < 3:
        return None
    # last close should not crush the floor
    if window[-1].close < low_min * (1 - FLAT_TOL):
        return None
    # prefer after some decline into the base
    early = sum(c.close for c in window[:3]) / 3
    late = sum(c.close for c in window[-3:]) / 3
    if late > early * 1.15:
        return None
    idx = len(candles) - 1
    return PatternResult("平底", "bullish", 62.0, idx, "MEDIUM", {"touches": len(near)})


def _scan_htf(candles: list[Candle], timeframe: str, engine: PatternEngine) -> list[HitPattern]:
    if len(candles) < 8:
        return []
    results = engine.scan(candles)
    flat = _detect_flat_bottom(candles)
    if flat:
        results.append(flat)
    n = len(candles)
    hits: list[HitPattern] = []
    for r in results:
        # recent window only
        if r.candle_index < n - RECENT_BARS:
            continue
        name = r.pattern_name
        if name not in BOTTOM_PATTERNS and name not in TOP_PATTERNS:
            continue
        confirmed = bool(r.score >= 60) or (r.candle_index < n - 1)
        # next-bar soft confirmation: if pattern not on last bar, treat as confirmed
        if r.candle_index == n - 1 and r.score < 60:
            confirmed = False
        hits.append(
            HitPattern(
                name=name,
                date=_candle_date(candles[r.candle_index]),
                score=float(r.score),
                confirmed=confirmed,
                timeframe=timeframe,
            )
        )
    # keep strongest per name
    best: dict[str, HitPattern] = {}
    for h in hits:
        prev = best.get(h.name)
        if prev is None or h.score > prev.score:
            best[h.name] = h
    return sorted(best.values(), key=lambda x: -x.score)


def _valuation_bias(pe_pct: float | None) -> str:
    if pe_pct is None:
        return "neutral"
    if pe_pct < PE_CHEAP:
        return "cheap"
    if pe_pct > PE_RICH:
        return "rich"
    if PE_MID_LOW <= pe_pct <= PE_MID_HIGH:
        return "neutral"
    if pe_pct < PE_MID_LOW:
        return "cheap"
    return "rich"


def _growth_inflection(profit_yoy: float | None) -> bool:
    """Without prior-period field on pool row, treat strong positive growth as soft inflection."""
    return profit_yoy is not None and profit_yoy >= 15


def _growth_slowing(profit_yoy: float | None) -> bool:
    return profit_yoy is not None and profit_yoy < 10


def _classify(
    pe_pct: float | None,
    profit_yoy: float | None,
    bull_hits: list[HitPattern],
    bear_hits: list[HitPattern],
) -> tuple[str, str]:
    bias = _valuation_bias(pe_pct)
    fund_bottom = bias == "cheap" or (pe_pct is not None and pe_pct < PE_CHEAP) or _growth_inflection(profit_yoy)
    fund_top = bias == "rich" or (pe_pct is not None and pe_pct > PE_RICH) or _growth_slowing(profit_yoy)
    # Prefer confirmed hits
    bull_ok = any(h.confirmed for h in bull_hits) or (bull_hits and not bear_hits)
    bear_ok = any(h.confirmed for h in bear_hits) or (bear_hits and not bull_hits)

    notes: list[str] = []
    if fund_bottom and bull_ok and not bear_ok:
        return "bottom", "估值偏低且周/月线见底部形态"
    if fund_top and bear_ok and not bull_ok:
        return "top", "估值偏高且周/月线见顶部形态"
    if fund_bottom and bear_ok:
        return "conflict", "低估但出现顶部形态，冲突观望"
    if fund_top and bull_ok:
        return "conflict", "高估但出现底部形态，冲突观望"
    if bull_ok and not fund_bottom and not fund_top:
        notes.append("有底部形态但估值未到极端低位")
        return "mid", "；".join(notes) or "形态偏多但估值中性"
    if bear_ok and not fund_top and not fund_bottom:
        return "mid", "有顶部形态但估值未到极端高位"
    return "mid", "估值与形态均未给出明确大位置信号"


def position_one(
    db: Session,
    *,
    symbol: str,
    pe_percentile: float | None = None,
    profit_yoy: float | None = None,
    lookback_days: int = 1300,
) -> PositionResult:
    """Position a single symbol on weekly + monthly candles."""
    engine = PatternEngine(min_score=50.0)
    try:
        kline_svc = KlineService(db)
        if kline_svc.get_latest(symbol) is None or kline_svc.is_contaminated(symbol):
            kline_svc.sync(symbol, force=True)
        klines, _ = kline_svc.get_recent_klines(symbol, limit=lookback_days)
        if len(klines) < 60:
            kline_svc.sync(symbol, force=True)
            klines, _ = kline_svc.get_recent_klines(symbol, limit=lookback_days)
        if len(klines) < 40:
            return PositionResult(
                symbol=symbol,
                zone="mid",
                label=ZONE_LABEL["mid"],
                action=ZONE_ACTION["mid"],
                valuation_bias=_valuation_bias(pe_percentile),
                pe_percentile=pe_percentile,
                notes="K线不足，无法做周/月线定位",
                error="insufficient_klines",
            )

        weekly = bars_to_candles(to_weekly(klines))
        monthly = bars_to_candles(to_monthly(klines))
        w_hits = _scan_htf(weekly, "weekly", engine)
        m_hits = _scan_htf(monthly, "monthly", engine)
        all_hits = w_hits + m_hits
        bull = [h for h in all_hits if h.name in BOTTOM_PATTERNS]
        bear = [h for h in all_hits if h.name in TOP_PATTERNS]
        zone, note = _classify(pe_percentile, profit_yoy, bull, bear)
        return PositionResult(
            symbol=symbol,
            zone=zone,
            label=ZONE_LABEL[zone],
            action=ZONE_ACTION[zone],
            valuation_bias=_valuation_bias(pe_percentile),
            pe_percentile=pe_percentile,
            weekly_patterns=w_hits,
            monthly_patterns=m_hits,
            notes=note,
        )
    except Exception as e:
        logger.warning("position_one %s failed: %s", symbol, e)
        return PositionResult(
            symbol=symbol,
            zone="mid",
            label=ZONE_LABEL["mid"],
            action=ZONE_ACTION["mid"],
            valuation_bias=_valuation_bias(pe_percentile),
            pe_percentile=pe_percentile,
            notes=f"定位失败：{e}",
            error=str(e),
        )


def position_pool(db: Session) -> dict[str, Any]:
    rows = list_pool(db)
    quotes = {
        v["symbol"]: v
        for v in get_valuations([r.symbol for r in rows], db=db)
    } if rows else {}
    items: list[dict[str, Any]] = []
    counts = {"bottom": 0, "top": 0, "mid": 0, "conflict": 0}
    for row in rows:
        base = candidate_out(row, quotes.get(row.symbol))
        pe = base.get("pe_percentile")
        if pe is None and quotes.get(row.symbol):
            pe = quotes[row.symbol].get("pe_percentile")
        pos = position_one(
            db,
            symbol=row.symbol,
            pe_percentile=float(pe) if pe is not None else None,
            profit_yoy=row.profit_yoy,
        )
        counts[pos.zone] = counts.get(pos.zone, 0) + 1
        base["position"] = pos.to_dict()
        items.append(base)
    # Sort: bottom first, then conflict, mid, top
    order = {"bottom": 0, "conflict": 1, "mid": 2, "top": 3}
    items.sort(key=lambda x: (order.get((x.get("position") or {}).get("zone"), 9), -(x.get("score") or 0)))
    return {
        "count": len(items),
        "counts": counts,
        "items": items,
        "note": "第二层战略定位：PE分位 + 周线/月线蜡烛形态。形态需确认，月/周级别不抢跑。",
    }
