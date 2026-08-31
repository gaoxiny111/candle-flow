"""Layer-3 tactical entry: daily candles — wait pullback, confirm, set stop."""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Sequence

from sqlalchemy.orm import Session

from app.core.candle import Candle
from app.core.nison_rules import NEEDS_NEXT_CONFIRM, pattern_bar_count, pattern_stop
from app.core.pattern_engine import PatternEngine, kline_to_candles
from app.core.western_levels import active_retracements, swing_points
from app.services.fundamental_position import position_one
from app.services.fundamental_screen import candidate_out, list_pool
from app.services.kline_service import KlineService
from app.services.valuation import get_valuations

logger = logging.getLogger(__name__)

RECENT_BARS = 5
SUPPORT_TOL = 0.025
VOL_SURGE = 1.5
STOP_PCT = 0.02  # 形态低点下方 2%

# 优选：多根确认；次选：单根需次日确认；避免仅十字星
PRIMARY_ENTRY = frozenset({"启明星", "十字启明星", "看涨吞没", "红三兵"})
SECONDARY_ENTRY = frozenset({"锤子线", "倒锤子线", "刺透", "平头底部", "破低反涨"})
AVOID_SOLO = frozenset({"十字星", "北方十字", "长腿十字线", "黄包车夫", "蜻蜓十字", "墓碑十字"})

IRON_RULES = [
    "基本面恶化时，蜡烛图形态再好也不买（一票否决）",
    "看涨形态需放量确认；无量形态像没有弹药的枪",
    "永远等确认，不预判（锤子等次日阳线）",
    "周期越大形态越可靠：月 > 周 > 日",
    "形态出现在关键支撑位才有效（位置 > 形态）",
    "基本面极强时可小仓分批，不必等教科书级形态",
]


@dataclass
class SupportHit:
    name: str
    price: float
    detail: str

    def to_dict(self) -> dict[str, Any]:
        return {"name": self.name, "price": round(self.price, 4), "detail": self.detail}


@dataclass
class EntryHit:
    name: str
    date: str
    score: float
    confirmed: bool
    tier: str  # primary | secondary | weak
    volume_ok: bool

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "date": self.date,
            "score": round(self.score, 1),
            "confirmed": self.confirmed,
            "tier": self.tier,
            "volume_ok": self.volume_ok,
        }


@dataclass
class TacticsResult:
    symbol: str
    status: str  # ready | wait_pullback | wait_confirm | avoid | not_eligible | no_signal
    label: str
    action: str
    entry_patterns: list[EntryHit] = field(default_factory=list)
    supports: list[SupportHit] = field(default_factory=list)
    near_support: bool = False
    pullback_ok: bool = False
    volume_ratio: float | None = None
    stop_loss: float | None = None
    stop_basis: str = ""
    entry_hint: str = ""
    zone: str | None = None
    warnings: list[str] = field(default_factory=list)
    notes: str = ""
    error: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "label": self.label,
            "action": self.action,
            "entry_patterns": [h.to_dict() for h in self.entry_patterns],
            "supports": [s.to_dict() for s in self.supports],
            "near_support": self.near_support,
            "pullback_ok": self.pullback_ok,
            "volume_ratio": round(self.volume_ratio, 2) if self.volume_ratio is not None else None,
            "stop_loss": round(self.stop_loss, 4) if self.stop_loss is not None else None,
            "stop_basis": self.stop_basis,
            "entry_hint": self.entry_hint,
            "zone": self.zone,
            "warnings": self.warnings,
            "notes": self.notes,
            "error": self.error,
        }


STATUS_LABEL = {
    "ready": "可扣扳机",
    "wait_pullback": "等回调到支撑",
    "wait_confirm": "等次日确认",
    "avoid": "回避追涨",
    "not_eligible": "非底部区，不入场",
    "no_signal": "暂无日线入场点",
}

STATUS_ACTION = {
    "ready": "日线收盘前可小仓买入，止损已给出且不可下移",
    "wait_pullback": "上升趋势中不追高，等回踩支撑再找形态",
    "wait_confirm": "已有潜在形态，等次日阳线/放量确认",
    "avoid": "信号过弱（如仅十字星）或位置不佳，观望",
    "not_eligible": "第二层未确认大方向可买，日线不找买点",
    "no_signal": "继续观察日线回调与确认形态",
}


def _candle_date(c: Candle) -> str:
    ts = c.timestamp
    if isinstance(ts, datetime):
        return ts.date().isoformat()
    return str(ts)[:10]


def _sma(values: Sequence[float], period: int, end: int) -> float | None:
    if end < period - 1 or period <= 0:
        return None
    window = values[end - period + 1 : end + 1]
    if len(window) < period:
        return None
    return sum(window) / period


def _avg_vol(candles: list[Candle], end: int, period: int = 20) -> float:
    start = max(0, end - period + 1)
    vols = [c.volume for c in candles[start : end + 1] if c.volume > 0]
    return sum(vols) / len(vols) if vols else 0.0


def _volume_ratio(candles: list[Candle], index: int) -> float | None:
    avg = _avg_vol(candles, index - 1 if index > 0 else index, 20)
    if avg <= 0:
        return None
    return candles[index].volume / avg


def _find_supports(candles: list[Candle], index: int) -> list[SupportHit]:
    if index < 20:
        return []
    closes = [c.close for c in candles]
    lows = [c.low for c in candles]
    hits: list[SupportHit] = []
    close = candles[index].close
    low = candles[index].low

    ma60 = _sma(closes, 60, index)
    if ma60 and ma60 > 0:
        dist = min(abs(close - ma60), abs(low - ma60)) / ma60
        if dist <= SUPPORT_TOL:
            hits.append(SupportHit("60日均线", ma60, f"贴近 MA60 {ma60:.3f}"))

    ma20 = _sma(closes, 20, index)
    if ma20 and ma20 > 0:
        dist = min(abs(close - ma20), abs(low - ma20)) / ma20
        if dist <= SUPPORT_TOL:
            hits.append(SupportHit("20日均线", ma20, f"贴近 MA20 {ma20:.3f}"))

    _, swing_lows = swing_points(candles, index - 1, lookback=40)
    if swing_lows:
        _, pl = swing_lows[-1]
        if pl > 0 and min(abs(close - pl), abs(low - pl)) / pl <= SUPPORT_TOL:
            hits.append(SupportHit("前低", pl, f"贴近近期摆动低点 {pl:.3f}"))

    for level, ratio, kind, hi, lo in active_retracements(candles, index):
        if kind != "up_retrace" or level <= 0:
            continue
        dist = min(abs(close - level), abs(low - level)) / level
        if dist <= SUPPORT_TOL:
            pct = f"{ratio * 100:.1f}".rstrip("0").rstrip(".")
            hits.append(
                SupportHit(
                    f"斐波那契{pct}%",
                    level,
                    f"升浪 {lo:.3f}→{hi:.3f} 的 {pct}% 回撤 {level:.3f}",
                )
            )
            break

    # de-dupe by name
    best: dict[str, SupportHit] = {}
    for h in hits:
        prev = best.get(h.name)
        if prev is None or abs(close - h.price) < abs(close - prev.price):
            best[h.name] = h
    return list(best.values())


def _dry_then_surge(candles: list[Candle], index: int) -> bool:
    """缩量回调后突然放量阳线（量比 > 1.5）。"""
    if index < 5:
        return False
    c = candles[index]
    if c.close <= c.open:
        return False
    ratio = _volume_ratio(candles, index)
    if ratio is None or ratio < VOL_SURGE:
        return False
    avg = _avg_vol(candles, index - 1, 20)
    if avg <= 0:
        return False
    dry = sum(1 for j in range(index - 3, index) if candles[j].volume <= avg * 0.85)
    return dry >= 2


def _is_chase(candles: list[Candle], index: int) -> bool:
    """近端高位追涨：收盘贴近 20 日高点且远离均线支撑。"""
    if index < 20:
        return False
    window = candles[index - 19 : index + 1]
    period_high = max(c.high for c in window)
    close = candles[index].close
    if period_high <= 0:
        return False
    at_high = close >= period_high * 0.985
    closes = [c.close for c in candles]
    ma60 = _sma(closes, 60, index)
    far_from_ma = ma60 is not None and close > ma60 * 1.08
    return at_high and (far_from_ma or close >= period_high * 0.995)


def _tier_for(name: str) -> str:
    if name in PRIMARY_ENTRY:
        return "primary"
    if name in SECONDARY_ENTRY:
        return "secondary"
    return "weak"


def _next_confirmed(candles: list[Candle], pattern_index: int, name: str) -> bool:
    """次日同向确认：看涨形态后一根收阳且收盘高于形态实体上沿。"""
    if pattern_index >= len(candles) - 1:
        # 形态落在最后一根：尚未有次日 → 未确认（除非不在 NEEDS_NEXT）
        return name not in NEEDS_NEXT_CONFIRM and name in PRIMARY_ENTRY
    p = candles[pattern_index]
    n = candles[pattern_index + 1]
    body_top = max(p.open, p.close)
    return n.close > n.open and n.close >= body_top


def _scan_entry_patterns(candles: list[Candle], engine: PatternEngine) -> list[EntryHit]:
    if len(candles) < 10:
        return []
    results = engine.scan(candles)
    n = len(candles)
    hits: list[EntryHit] = []
    for r in results:
        if r.candle_index < n - RECENT_BARS:
            continue
        name = r.pattern_name
        if r.direction != "bullish":
            continue
        if name in AVOID_SOLO:
            continue
        if name not in PRIMARY_ENTRY and name not in SECONDARY_ENTRY:
            # still allow other bullish if score high and multi-bar
            if pattern_bar_count(name) < 2 or r.score < 65:
                continue
        confirmed = _next_confirmed(candles, r.candle_index, name)
        if name in PRIMARY_ENTRY and r.candle_index < n - 1:
            confirmed = True
        elif name in PRIMARY_ENTRY and pattern_bar_count(name) >= 3:
            confirmed = True  # 多根组合本身即确认
        vol_r = _volume_ratio(candles, r.candle_index)
        volume_ok = vol_r is not None and vol_r >= 1.0
        # 看涨吞没/启明星更要求放量
        if name in {"看涨吞没", "启明星", "十字启明星"} and (vol_r is None or vol_r < 1.1):
            volume_ok = False
        hits.append(
            EntryHit(
                name=name,
                date=_candle_date(candles[r.candle_index]),
                score=float(r.score),
                confirmed=confirmed,
                tier=_tier_for(name),
                volume_ok=volume_ok,
            )
        )
    best: dict[str, EntryHit] = {}
    for h in hits:
        prev = best.get(h.name)
        if prev is None or h.score > prev.score:
            best[h.name] = h
    return sorted(best.values(), key=lambda x: (-({"primary": 2, "secondary": 1}.get(x.tier, 0)), -x.score))


def _compute_stop(
    candles: list[Candle],
    pattern_name: str | None,
    pattern_index: int | None,
    supports: list[SupportHit],
) -> tuple[float | None, str]:
    stop_candidates: list[tuple[float, str]] = []
    if pattern_name and pattern_index is not None:
        nison = pattern_stop(candles, pattern_index, "bullish", pattern_name)
        n = pattern_bar_count(pattern_name)
        start = max(0, pattern_index - n + 1)
        extreme = min(c.low for c in candles[start : pattern_index + 1])
        stop_candidates.append((extreme * (1 - STOP_PCT), f"{pattern_name}最低点下方{int(STOP_PCT * 100)}%"))
        if nison:
            stop_candidates.append((float(nison), f"{pattern_name}形态止损（尼森）"))
    if supports:
        s = min(supports, key=lambda x: x.price)
        stop_candidates.append((s.price * (1 - 0.01), f"{s.name}下方1%"))
    if not stop_candidates:
        return None, ""
    # 取更紧但合理的止损（较高者，避免过宽）；仍须低于现价
    close = candles[-1].close
    valid = [(p, b) for p, b in stop_candidates if 0 < p < close]
    if not valid:
        p, b = max(stop_candidates, key=lambda x: x[0])
        return p, b
    p, b = max(valid, key=lambda x: x[0])
    return p, b + "（设定后不可下移）"


def tactics_from_candles(
    candles: list[Candle],
    *,
    symbol: str,
    zone: str | None = None,
    pe_percentile: float | None = None,
) -> TacticsResult:
    """Pure logic on daily candles (for tests + service)."""
    warnings: list[str] = []
    if zone and zone != "bottom":
        return TacticsResult(
            symbol=symbol,
            status="not_eligible",
            label=STATUS_LABEL["not_eligible"],
            action=STATUS_ACTION["not_eligible"],
            zone=zone,
            notes="第二层未标为底部区",
        )

    if len(candles) < 40:
        return TacticsResult(
            symbol=symbol,
            status="no_signal",
            label=STATUS_LABEL["no_signal"],
            action=STATUS_ACTION["no_signal"],
            zone=zone,
            notes="日线不足",
            error="insufficient_klines",
        )

    # 铁律：基本面恶化（此处用净利/估值由上层传；仅 PE 极高时警告）
    if pe_percentile is not None and pe_percentile > 70:
        warnings.append("PE分位偏高，基本面侧不支持新开仓（铁律1）")

    engine = PatternEngine(min_score=50.0)
    idx = len(candles) - 1
    supports = _find_supports(candles, idx)
    near_support = bool(supports)
    surge = _dry_then_surge(candles, idx)
    pullback_ok = near_support or surge
    vol_r = _volume_ratio(candles, idx)
    chasing = _is_chase(candles, idx)
    patterns = _scan_entry_patterns(candles, engine)

    # 仅十字类且无多根确认 → avoid
    if not patterns and chasing:
        return TacticsResult(
            symbol=symbol,
            status="avoid",
            label=STATUS_LABEL["avoid"],
            action=STATUS_ACTION["avoid"],
            supports=supports,
            near_support=near_support,
            pullback_ok=pullback_ok,
            volume_ratio=vol_r,
            zone=zone,
            warnings=warnings + ["疑似追涨，等待回调"],
            notes="价格贴近近期高点，不符合「等回调再买」",
        )

    confirmed = [p for p in patterns if p.confirmed]

    if not patterns:
        if not pullback_ok:
            return TacticsResult(
                symbol=symbol,
                status="wait_pullback",
                label=STATUS_LABEL["wait_pullback"],
                action=STATUS_ACTION["wait_pullback"],
                supports=supports,
                near_support=near_support,
                pullback_ok=False,
                volume_ratio=vol_r,
                zone=zone,
                warnings=warnings,
                notes="尚未回到关键支撑，也无缩量回踩放量阳线",
            )
        return TacticsResult(
            symbol=symbol,
            status="no_signal",
            label=STATUS_LABEL["no_signal"],
            action=STATUS_ACTION["no_signal"],
            supports=supports,
            near_support=near_support,
            pullback_ok=pullback_ok,
            volume_ratio=vol_r,
            zone=zone,
            warnings=warnings,
            notes="已近支撑或放量，但尚无合格日线形态",
        )

    best = patterns[0]
    # 定位形态 index 用于止损
    pat_idx = None
    for i, c in enumerate(candles):
        if _candle_date(c) == best.date:
            pat_idx = i
    if pat_idx is None:
        pat_idx = idx

    stop, stop_basis = _compute_stop(candles, best.name, pat_idx, supports)

    if not pullback_ok and best.name in {"红三兵"}:
        warnings.append("红三兵若处高位不可追涨（尼森）")
        return TacticsResult(
            symbol=symbol,
            status="wait_pullback",
            label=STATUS_LABEL["wait_pullback"],
            action=STATUS_ACTION["wait_pullback"],
            entry_patterns=patterns,
            supports=supports,
            near_support=near_support,
            pullback_ok=False,
            volume_ratio=vol_r,
            stop_loss=stop,
            stop_basis=stop_basis,
            zone=zone,
            warnings=warnings,
            notes="有红三兵但未确认回调支撑",
        )

    if best.tier == "secondary" and not best.confirmed:
        return TacticsResult(
            symbol=symbol,
            status="wait_confirm",
            label=STATUS_LABEL["wait_confirm"],
            action=STATUS_ACTION["wait_confirm"],
            entry_patterns=patterns,
            supports=supports,
            near_support=near_support,
            pullback_ok=pullback_ok,
            volume_ratio=vol_r,
            stop_loss=stop,
            stop_basis=stop_basis,
            zone=zone,
            warnings=warnings + ["单根形态需次日阳线确认（铁律3）"],
            notes=f"见{best.name}，等待确认",
        )

    if not best.volume_ok and not surge:
        warnings.append("形态缺少放量确认（铁律2）")
        return TacticsResult(
            symbol=symbol,
            status="wait_confirm",
            label=STATUS_LABEL["wait_confirm"],
            action="等待放量确认后再考虑入场",
            entry_patterns=patterns,
            supports=supports,
            near_support=near_support,
            pullback_ok=pullback_ok,
            volume_ratio=vol_r,
            stop_loss=stop,
            stop_basis=stop_basis,
            zone=zone,
            warnings=warnings,
            notes="有形态但量能不足",
        )

    if not pullback_ok and best.tier != "primary":
        return TacticsResult(
            symbol=symbol,
            status="wait_pullback",
            label=STATUS_LABEL["wait_pullback"],
            action=STATUS_ACTION["wait_pullback"],
            entry_patterns=patterns,
            supports=supports,
            near_support=near_support,
            pullback_ok=False,
            volume_ratio=vol_r,
            stop_loss=stop,
            stop_basis=stop_basis,
            zone=zone,
            warnings=warnings,
            notes="形态未落在关键支撑附近",
        )

    # ready: 优选已确认，或次选已确认+支撑/放量
    ready_ok = (
        (best.tier == "primary" and (best.confirmed or best.name in PRIMARY_ENTRY))
        or (best.confirmed and pullback_ok and (best.volume_ok or surge))
    )
    if pe_percentile is not None and pe_percentile > 70:
        return TacticsResult(
            symbol=symbol,
            status="avoid",
            label=STATUS_LABEL["avoid"],
            action=STATUS_ACTION["avoid"],
            entry_patterns=patterns,
            supports=supports,
            near_support=near_support,
            pullback_ok=pullback_ok,
            volume_ratio=vol_r,
            stop_loss=stop,
            stop_basis=stop_basis,
            zone=zone,
            warnings=warnings,
            notes="高估值一票否决新开仓",
        )

    if ready_ok:
        hint = (
            f"支撑：{', '.join(s.name for s in supports) or '缩量回踩放量'}；"
            f"形态：{best.name}；止损：{stop}"
        )
        return TacticsResult(
            symbol=symbol,
            status="ready",
            label=STATUS_LABEL["ready"],
            action=STATUS_ACTION["ready"],
            entry_patterns=patterns,
            supports=supports,
            near_support=near_support,
            pullback_ok=pullback_ok,
            volume_ratio=vol_r,
            stop_loss=stop,
            stop_basis=stop_basis,
            entry_hint=hint,
            zone=zone,
            warnings=warnings,
            notes="回调+确认+量能满足，可扣扳机",
        )

    if not confirmed:
        return TacticsResult(
            symbol=symbol,
            status="wait_confirm",
            label=STATUS_LABEL["wait_confirm"],
            action=STATUS_ACTION["wait_confirm"],
            entry_patterns=patterns,
            supports=supports,
            near_support=near_support,
            pullback_ok=pullback_ok,
            volume_ratio=vol_r,
            stop_loss=stop,
            stop_basis=stop_basis,
            zone=zone,
            warnings=warnings,
            notes="等待确认棒",
        )

    return TacticsResult(
        symbol=symbol,
        status="no_signal",
        label=STATUS_LABEL["no_signal"],
        action=STATUS_ACTION["no_signal"],
        entry_patterns=patterns,
        supports=supports,
        near_support=near_support,
        pullback_ok=pullback_ok,
        volume_ratio=vol_r,
        stop_loss=stop,
        stop_basis=stop_basis,
        zone=zone,
        warnings=warnings,
        notes="条件未齐",
    )


def _load_daily(db: Session, symbol: str, lookback: int = 400) -> list[Candle]:
    kline_svc = KlineService(db)
    if kline_svc.get_latest(symbol) is None or kline_svc.is_contaminated(symbol):
        kline_svc.sync(symbol, force=True)
    klines, _ = kline_svc.get_recent_klines(symbol, limit=lookback)
    if len(klines) < 60:
        kline_svc.sync(symbol, force=True)
        klines, _ = kline_svc.get_recent_klines(symbol, limit=lookback)
    return kline_to_candles(klines)


def tactics_one(
    db: Session,
    *,
    symbol: str,
    pe_percentile: float | None = None,
    profit_yoy: float | None = None,
    zone: str | None = None,
    require_bottom: bool = True,
) -> TacticsResult:
    try:
        if zone is None and require_bottom:
            pos = position_one(db, symbol=symbol, pe_percentile=pe_percentile, profit_yoy=profit_yoy)
            zone = pos.zone
        if require_bottom and zone != "bottom":
            return TacticsResult(
                symbol=symbol,
                status="not_eligible",
                label=STATUS_LABEL["not_eligible"],
                action=STATUS_ACTION["not_eligible"],
                zone=zone,
                notes="第二层非底部区",
            )
        candles = _load_daily(db, symbol)
        return tactics_from_candles(
            candles, symbol=symbol, zone=zone or "bottom", pe_percentile=pe_percentile
        )
    except Exception as e:
        logger.warning("tactics_one %s failed: %s", symbol, e)
        return TacticsResult(
            symbol=symbol,
            status="no_signal",
            label=STATUS_LABEL["no_signal"],
            action=STATUS_ACTION["no_signal"],
            zone=zone,
            notes=f"战术扫描失败：{e}",
            error=str(e),
        )


def tactics_pool(db: Session, *, require_bottom: bool = True) -> dict[str, Any]:
    rows = list_pool(db)
    quotes = {v["symbol"]: v for v in get_valuations([r.symbol for r in rows], db=db)} if rows else {}
    items: list[dict[str, Any]] = []
    counts: dict[str, int] = {
        "ready": 0,
        "wait_pullback": 0,
        "wait_confirm": 0,
        "avoid": 0,
        "not_eligible": 0,
        "no_signal": 0,
    }
    for row in rows:
        base = candidate_out(row, quotes.get(row.symbol))
        pe = base.get("pe_percentile")
        if pe is None and quotes.get(row.symbol):
            pe = quotes[row.symbol].get("pe_percentile")
        tac = tactics_one(
            db,
            symbol=row.symbol,
            pe_percentile=float(pe) if pe is not None else None,
            profit_yoy=row.profit_yoy,
            require_bottom=require_bottom,
        )
        counts[tac.status] = counts.get(tac.status, 0) + 1
        base["tactics"] = tac.to_dict()
        items.append(base)

    order = {
        "ready": 0,
        "wait_confirm": 1,
        "wait_pullback": 2,
        "no_signal": 3,
        "avoid": 4,
        "not_eligible": 5,
    }
    items.sort(
        key=lambda x: (
            order.get((x.get("tactics") or {}).get("status"), 9),
            -(x.get("score") or 0),
        )
    )
    return {
        "count": len(items),
        "counts": counts,
        "items": items,
        "iron_rules": IRON_RULES,
        "note": "第三层战术入场：仅底部区找日线买点——等回调、等确认、设止损（不可下移）。",
    }
