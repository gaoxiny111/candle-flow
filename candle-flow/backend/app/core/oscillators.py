"""Oscillators for Nison Ch.14: Stochastic + simple divergences."""

from __future__ import annotations

from typing import Optional, Sequence


def _close(k) -> float:
    return float(k.close)


def _high(k) -> float:
    return float(k.high)


def _low(k) -> float:
    return float(k.low)


def stochastic_at(
    klines: Sequence,
    index: int,
    k_period: int = 14,
    d_period: int = 3,
) -> tuple[float, float] | None:
    """Slow stochastic %K / %D (SMA of %K)."""
    if index < k_period + d_period - 2:
        return None
    ks: list[float] = []
    for i in range(index - d_period + 1, index + 1):
        start = i - k_period + 1
        if start < 0:
            return None
        window = klines[start : i + 1]
        hh = max(_high(k) for k in window)
        ll = min(_low(k) for k in window)
        if hh <= ll:
            ks.append(50.0)
        else:
            ks.append(100.0 * (_close(klines[i]) - ll) / (hh - ll))
    k_val = ks[-1]
    d_val = sum(ks) / len(ks)
    return k_val, d_val


def _local_extrema(values: list[float], order: int = 2) -> tuple[list[int], list[int]]:
    highs: list[int] = []
    lows: list[int] = []
    for i in range(order, len(values) - order):
        v = values[i]
        if all(v >= values[j] for j in range(i - order, i + order + 1) if j != i):
            highs.append(i)
        if all(v <= values[j] for j in range(i - order, i + order + 1) if j != i):
            lows.append(i)
    return highs, lows


def bullish_divergence(
    prices: list[float],
    indicator: list[float | None],
    index: int,
    lookback: int = 35,
) -> bool:
    """Price lower low, indicator higher low."""
    start = max(0, index - lookback)
    p = prices[start : index + 1]
    ind_raw = indicator[start : index + 1]
    if len(p) < 10:
        return False
    # fill None with neighbor for extrema scan
    ind: list[float] = []
    last = 50.0
    for x in ind_raw:
        if x is None:
            ind.append(last)
        else:
            last = x
            ind.append(x)
    _, plows = _local_extrema(p)
    _, ilows = _local_extrema(ind)
    if len(plows) < 2:
        return False
    a, b = plows[-2], plows[-1]
    if p[b] >= p[a] * 0.998:
        return False
    # nearest indicator lows around those bars
    ia = min(ilows, key=lambda i: abs(i - a)) if ilows else a
    ib = min(ilows, key=lambda i: abs(i - b)) if ilows else b
    if abs(ia - a) > 3 or abs(ib - b) > 3:
        ia, ib = a, b
    return ind[ib] > ind[ia] * 1.01


def bearish_divergence(
    prices: list[float],
    indicator: list[float | None],
    index: int,
    lookback: int = 35,
) -> bool:
    """Price higher high, indicator lower high."""
    start = max(0, index - lookback)
    p = prices[start : index + 1]
    ind_raw = indicator[start : index + 1]
    if len(p) < 10:
        return False
    ind: list[float] = []
    last = 50.0
    for x in ind_raw:
        if x is None:
            ind.append(last)
        else:
            last = x
            ind.append(x)
    phighs, _ = _local_extrema(p)
    ihighs, _ = _local_extrema(ind)
    if len(phighs) < 2:
        return False
    a, b = phighs[-2], phighs[-1]
    if p[b] <= p[a] * 1.002:
        return False
    ia = min(ihighs, key=lambda i: abs(i - a)) if ihighs else a
    ib = min(ihighs, key=lambda i: abs(i - b)) if ihighs else b
    if abs(ia - a) > 3 or abs(ib - b) > 3:
        ia, ib = a, b
    return ind[ib] < ind[ia] * 0.99


def rsi_series(closes: list[float], period: int = 14) -> list[float | None]:
    out: list[float | None] = [None] * len(closes)
    if len(closes) <= period:
        return out
    gains = 0.0
    losses = 0.0
    for i in range(1, period + 1):
        ch = closes[i] - closes[i - 1]
        if ch >= 0:
            gains += ch
        else:
            losses -= ch
    avg_gain = gains / period
    avg_loss = losses / period
    if avg_loss == 0:
        out[period] = 100.0
    else:
        out[period] = 100.0 - 100.0 / (1.0 + avg_gain / avg_loss)
    for i in range(period + 1, len(closes)):
        ch = closes[i] - closes[i - 1]
        gain = ch if ch > 0 else 0.0
        loss = -ch if ch < 0 else 0.0
        avg_gain = (avg_gain * (period - 1) + gain) / period
        avg_loss = (avg_loss * (period - 1) + loss) / period
        if avg_loss == 0:
            out[i] = 100.0
        else:
            out[i] = 100.0 - 100.0 / (1.0 + avg_gain / avg_loss)
    return out
