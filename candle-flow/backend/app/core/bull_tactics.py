"""主板战法：黑马跨栏、N字反包、牛股三绝。"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from app.core.candle import Candle
from app.core.indicators import calc_ma
from app.utils.symbol import SymbolError, parse_symbol

HEIMA = "黑马跨栏"
N_FAN = "N字反包"
NIU_SAN = "牛股三绝"

TACTIC_NAMES = (HEIMA, N_FAN, NIU_SAN)

_MAIN_SH = ("600", "601", "603", "605")
_MAIN_SZ = ("000", "001", "002", "003")


@dataclass
class TacticHit:
    tactic: str
    buy_index: int
    buy_date: str
    buy_price: float
    score: float
    setup_date: str
    details: dict[str, Any] = field(default_factory=dict)


def is_main_board(symbol: str) -> bool:
    try:
        code, market = parse_symbol(symbol)
    except SymbolError:
        return False
    if market == "sh":
        return code.startswith(_MAIN_SH)
    if market == "sz":
        return code.startswith(_MAIN_SZ)
    return False


def is_st_name(name: str | None) -> bool:
    if not name:
        return False
    compact = name.upper().replace(" ", "")
    return "ST" in compact


def limit_price(prev_close: float) -> float:
    return round(prev_close * 1.10, 2)


def _avg_volume(candles: list[Candle], end_index: int, period: int = 20) -> float:
    start = max(0, end_index - period + 1)
    vols = [c.volume for c in candles[start : end_index + 1] if c.volume > 0]
    return sum(vols) / len(vols) if vols else 0.0


def is_strict_limit_up(c: Candle, prev_close: float) -> bool:
    if prev_close <= 0:
        return False
    lp = limit_price(prev_close)
    return c.close >= lp - 0.005


def touches_limit_up(c: Candle, prev_close: float) -> bool:
    if prev_close <= 0:
        return False
    lp = limit_price(prev_close)
    return c.high >= lp - 0.005


def is_yizi_limit(c: Candle, prev_close: float) -> bool:
    """一字涨停：全天封板，振幅极小。"""
    if not touches_limit_up(c, prev_close):
        return False
    lp = limit_price(prev_close)
    if c.low < lp - 0.02:
        return False
    rng = c.high - c.low
    return rng <= max(c.close * 0.003, 0.03)


def _fmt_date(c: Candle) -> str:
    ts = c.timestamp
    if isinstance(ts, datetime):
        return ts.strftime("%Y-%m-%d")
    return str(ts)[:10]


def scan_heima_kualan(candles: list[Candle]) -> list[TacticHit]:
    """连续三天涨停（第三天允许炸板未回封），13 个交易日内缩量回踩不破收盘、不破十日线。"""
    hits: list[TacticHit] = []
    if len(candles) < 20:
        return hits

    for i in range(2, len(candles)):
        c0, c1, c2 = candles[i - 2], candles[i - 1], candles[i]
        pc0 = candles[i - 3].close if i >= 3 else 0.0
        pc1 = candles[i - 2].close
        pc2 = candles[i - 1].close
        if pc0 <= 0 or pc1 <= 0 or pc2 <= 0:
            continue
        if not is_strict_limit_up(c0, pc0):
            continue
        if not is_strict_limit_up(c1, pc1):
            continue
        # 第三天触板即可（允许炸板未回封）
        if not touches_limit_up(c2, pc2):
            continue

        # 不破收盘：以第三日收盘为支撑
        floor_close = c2.close
        peak_high = max(c0.high, c1.high, c2.high)
        peak_vol = max(c0.volume, c1.volume, c2.volume)

        for j in range(i + 1, min(i + 14, len(candles))):
            if j < 9:
                continue
            cj = candles[j]
            ma10 = calc_ma(candles, 10, j)

            # 回踩：收盘低于三连板高点
            if cj.close >= peak_high - 0.01:
                continue
            if peak_vol > 0 and cj.volume >= peak_vol * 0.85:
                continue
            if cj.low < floor_close - 0.01:
                continue
            if cj.low < ma10 - 0.01:
                continue

            score = 72.0
            if is_strict_limit_up(c2, pc2):
                score += 8.0
            if cj.volume <= peak_vol * 0.6:
                score += 6.0
            if cj.low >= ma10:
                score += 4.0

            hits.append(
                TacticHit(
                    tactic=HEIMA,
                    buy_index=j,
                    buy_date=_fmt_date(cj),
                    buy_price=cj.close,
                    score=min(100.0, score),
                    setup_date=_fmt_date(c2),
                    details={
                        "floor_close": floor_close,
                        "ma10": round(ma10, 4),
                        "limit_days": [_fmt_date(c0), _fmt_date(c1), _fmt_date(c2)],
                        "day3_zhaban": not is_strict_limit_up(c2, pc2),
                    },
                )
            )
            break
    return hits


def scan_n_fanbao(candles: list[Candle]) -> list[TacticHit]:
    """放量涨停；8 个交易日内缩量回踩不破涨停阳线开盘价。"""
    hits: list[TacticHit] = []
    if len(candles) < 20:
        return hits

    for i in range(1, len(candles)):
        c = candles[i]
        pc = candles[i - 1].close
        if pc <= 0:
            continue
        if not is_strict_limit_up(c, pc):
            continue
        # 一字板通常无实质放量换手，排除
        if is_yizi_limit(c, pc):
            continue
        avg_vol = _avg_volume(candles, i - 1, 20)
        if avg_vol <= 0 or c.volume < avg_vol * 1.3:
            continue

        ref_open = c.open
        for j in range(i + 1, min(i + 9, len(candles))):
            cj = candles[j]
            if cj.low < ref_open - 0.01:
                break
            if cj.volume >= c.volume * 0.75:
                continue
            if cj.close >= c.close:
                continue

            score = 68.0
            vol_ratio = c.volume / avg_vol if avg_vol else 0
            if vol_ratio >= 2.0:
                score += 10.0
            if cj.volume <= c.volume * 0.55:
                score += 6.0

            hits.append(
                TacticHit(
                    tactic=N_FAN,
                    buy_index=j,
                    buy_date=_fmt_date(cj),
                    buy_price=cj.close,
                    score=min(100.0, score),
                    setup_date=_fmt_date(c),
                    details={
                        "limit_open": ref_open,
                        "limit_close": c.close,
                        "limit_volume_ratio": round(vol_ratio, 2),
                    },
                )
            )
            break
    return hits


def scan_niu_sanjue(candles: list[Candle]) -> list[TacticHit]:
    """跳空高开大阳线或涨停；缩量回踩不破该阳线开盘价，且不跌破 39 日均线。"""
    hits: list[TacticHit] = []
    if len(candles) < 45:
        return hits

    for i in range(39, len(candles)):
        c = candles[i]
        prev = candles[i - 1]
        pc = prev.close
        if pc <= 0 or prev.high <= 0:
            continue

        # 跳空高开
        if c.open <= prev.high + 0.01:
            continue

        yang_pct = (c.close - c.open) / max(c.open, 0.01)
        big_yang = yang_pct >= 0.03 and c.is_bullish
        limit_up = is_strict_limit_up(c, pc)
        if not (big_yang or limit_up):
            continue

        signal_open = c.open
        avg_vol = _avg_volume(candles, i - 1, 20)

        for j in range(i + 1, len(candles)):
            cj = candles[j]
            ma39 = calc_ma(candles, 39, j)
            # 不破高开阳线开盘价
            if cj.low < signal_open - 0.01:
                break
            # 不跌破 39 日均线
            if cj.low < ma39 - 0.01:
                continue
            if cj.close >= c.close:
                continue
            if c.volume > 0 and cj.volume >= c.volume * 0.75:
                continue

            score = 68.0
            vol_ratio = (c.volume / avg_vol) if avg_vol else 0.0
            if limit_up:
                score += 8.0
            if cj.volume <= c.volume * 0.55:
                score += 6.0
            if cj.low >= ma39:
                score += 4.0

            hits.append(
                TacticHit(
                    tactic=NIU_SAN,
                    buy_index=j,
                    buy_date=_fmt_date(cj),
                    buy_price=cj.close,
                    score=min(100.0, score),
                    setup_date=_fmt_date(c),
                    details={
                        "signal_open": signal_open,
                        "signal_close": c.close,
                        "ma39": round(ma39, 4),
                        "volume_ratio": round(vol_ratio, 2),
                        "limit_up": limit_up,
                    },
                )
            )
            break
    return hits


TACTIC_SCANNERS = {
    HEIMA: scan_heima_kualan,
    N_FAN: scan_n_fanbao,
    NIU_SAN: scan_niu_sanjue,
}

# Minimum bars needed per tactic (+ buffer for MA / lookback windows).
TACTIC_KLINE_LIMITS: dict[str, int] = {
    HEIMA: 80,
    N_FAN: 80,
    NIU_SAN: 120,
}

MIN_SCAN_BARS = 45


def normalize_tactics(tactics: list[str] | None) -> list[str]:
    if not tactics:
        return list(TACTIC_NAMES)
    out: list[str] = []
    for raw in tactics:
        name = (raw or "").strip()
        if name in TACTIC_SCANNERS and name not in out:
            out.append(name)
    return out


def kline_limit_for_tactics(tactics: list[str] | None) -> int:
    """How many recent daily bars to load for the selected tactic(s)."""
    selected = normalize_tactics(tactics)
    if not selected:
        return max(TACTIC_KLINE_LIMITS.values())
    return max(TACTIC_KLINE_LIMITS[name] for name in selected)


def scan_tactics(
    candles: list[Candle],
    recent_bars: int = 30,
    tactics: list[str] | None = None,
) -> list[TacticHit]:
    """Scan selected tactics; default all three."""
    if not candles:
        return []
    selected = normalize_tactics(tactics)
    if not selected:
        return []
    cutoff = len(candles) - recent_bars - 1
    all_hits: list[TacticHit] = []
    for name in selected:
        all_hits.extend(TACTIC_SCANNERS[name](candles))
    by_key: dict[tuple[str, int], TacticHit] = {}
    for hit in all_hits:
        if hit.buy_index < cutoff:
            continue
        key = (hit.tactic, hit.buy_index)
        prev = by_key.get(key)
        if not prev or hit.score > prev.score:
            by_key[key] = hit
    out = list(by_key.values())
    out.sort(key=lambda h: h.buy_index, reverse=True)
    return out


def scan_all_tactics(candles: list[Candle], recent_bars: int = 30) -> list[TacticHit]:
    return scan_tactics(candles, recent_bars=recent_bars, tactics=None)
