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


def _near_ma7(c: Candle, ma7: float) -> bool:
    if ma7 <= 0:
        return False
    tol = ma7 * 0.025
    return c.low - tol <= ma7 <= c.high + tol or abs(c.close - ma7) <= tol


def scan_heima_kualan(candles: list[Candle]) -> list[TacticHit]:
    """连续三天涨停（第三天允许炸板），13 日内缩量回踩不破收盘/十日线，MA7 附近买点。"""
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
        if not touches_limit_up(c2, pc2):
            continue

        ref_close = c2.close
        floor_close = min(c0.close, c1.close, c2.close)
        peak_vol = max(c0.volume, c1.volume, c2.volume)

        for j in range(i + 1, min(i + 14, len(candles))):
            if j < 9:
                continue
            cj = candles[j]
            ma7 = calc_ma(candles, 7, j)
            ma10 = calc_ma(candles, 10, j)

            if cj.close >= ref_close:
                continue
            if peak_vol > 0 and cj.volume >= peak_vol * 0.85:
                continue
            if cj.low < floor_close - 0.01:
                continue
            if cj.low < ma10 - 0.01:
                continue
            if not _near_ma7(cj, ma7):
                continue

            score = 70.0
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
                        "ref_close": ref_close,
                        "floor_close": floor_close,
                        "ma7": round(ma7, 4),
                        "ma10": round(ma10, 4),
                        "limit_days": [_fmt_date(c0), _fmt_date(c1), _fmt_date(c2)],
                        "day3_zhaban": not is_strict_limit_up(c2, pc2),
                    },
                )
            )
            break
    return hits


def scan_n_fanbao(candles: list[Candle]) -> list[TacticHit]:
    """放量涨停（非一字），8 日内缩量回踩不破涨停阳线开盘价。"""
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
    """年内倍量跳空大阳线/涨停，缩量回踩不补缺口。"""
    hits: list[TacticHit] = []
    if len(candles) < 32:
        return hits

    start = max(1, len(candles) - 250)
    for i in range(start, len(candles)):
        c = candles[i]
        prev = candles[i - 1]
        pc = prev.close
        if pc <= 0 or prev.high <= 0:
            continue

        gap_low = prev.high
        if c.open <= gap_low + 0.01:
            continue

        yang_pct = (c.close - c.open) / max(c.open, 0.01)
        big_yang = yang_pct >= 0.03 and c.is_bullish
        limit_up = is_strict_limit_up(c, pc)
        if not (big_yang or limit_up):
            continue

        avg_vol = _avg_volume(candles, i - 1, 20)
        if avg_vol <= 0 or c.volume < avg_vol * 1.95:
            continue

        for j in range(i + 1, len(candles)):
            cj = candles[j]
            if cj.low < gap_low - 0.01:
                break
            if cj.close >= c.close:
                continue
            if cj.volume >= c.volume * 0.75:
                continue

            score = 66.0
            vol_ratio = c.volume / avg_vol
            if vol_ratio >= 2.5:
                score += 8.0
            if limit_up:
                score += 6.0
            if cj.volume <= c.volume * 0.55:
                score += 5.0

            hits.append(
                TacticHit(
                    tactic=NIU_SAN,
                    buy_index=j,
                    buy_date=_fmt_date(cj),
                    buy_price=cj.close,
                    score=min(100.0, score),
                    setup_date=_fmt_date(c),
                    details={
                        "gap_low": gap_low,
                        "signal_close": c.close,
                        "volume_ratio": round(vol_ratio, 2),
                        "limit_up": limit_up,
                    },
                )
            )
            break
    return hits


def scan_all_tactics(candles: list[Candle], recent_bars: int = 30) -> list[TacticHit]:
    """合并三种战法，同一买点去重保留最高分，仅保留近期买点。"""
    if not candles:
        return []
    cutoff = len(candles) - recent_bars - 1
    all_hits = [
        *scan_heima_kualan(candles),
        *scan_n_fanbao(candles),
        *scan_niu_sanjue(candles),
    ]
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
