"""MA golden / death cross — Western tools for confluence, not candlestick patterns."""

from __future__ import annotations

from typing import List

from app.core.candle import Candle


def _sma(candles: List[Candle], period: int, end: int) -> float | None:
    if end < period - 1:
        return None
    return sum(float(c.close) for c in candles[end - period + 1 : end + 1]) / period


def ma_cross_at(candles: List[Candle], index: int) -> str | None:
    """Return 'golden' or 'death' if MA5/MA10 or MA10/MA20 crossed on this close."""
    kind, _ = ma_cross_kind(candles, index)
    return kind


def ma_cross_kind(candles: List[Candle], index: int) -> tuple[str | None, str]:
    """(golden|death|None, detail). Nison: use as Western confirmation, not a trigger."""
    if index < 20:
        return None, ""
    ma5 = _sma(candles, 5, index)
    ma10 = _sma(candles, 10, index)
    ma20 = _sma(candles, 20, index)
    p5 = _sma(candles, 5, index - 1)
    p10 = _sma(candles, 10, index - 1)
    p20 = _sma(candles, 20, index - 1)
    if None in (ma5, ma10, ma20, p5, p10, p20):
        return None, ""
    short_up = p5 <= p10 and ma5 > ma10
    mid_up = p10 <= p20 and ma10 > ma20
    short_dn = p5 >= p10 and ma5 < ma10
    mid_dn = p10 >= p20 and ma10 < ma20
    bits = []
    if short_up:
        bits.append(f"MA5 {ma5:.4g} 上穿 MA10 {ma10:.4g}")
    if mid_up:
        bits.append(f"MA10 {ma10:.4g} 上穿 MA20 {ma20:.4g}")
    if short_dn:
        bits.append(f"MA5 {ma5:.4g} 下穿 MA10 {ma10:.4g}")
    if mid_dn:
        bits.append(f"MA10 {ma10:.4g} 下穿 MA20 {ma20:.4g}")
    if (short_up or mid_up) and not (short_dn or mid_dn):
        return "golden", "；".join(bits)
    if (short_dn or mid_dn) and not (short_up or mid_up):
        return "death", "；".join(bits)
    return None, ""
