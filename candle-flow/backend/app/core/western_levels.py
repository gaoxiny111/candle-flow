"""Nison Ch.11–12 helpers: polarity, trend proximity, percentage retracements, springboards."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional, Sequence


def _f(k, attr: str) -> float:
    return float(getattr(k, attr))


@dataclass(frozen=True)
class LevelHit:
    name: str
    detail: str
    price: float


def _px(v: float) -> str:
    if abs(v) >= 100:
        return f"{v:.2f}"
    if abs(v) >= 1:
        return f"{v:.3f}".rstrip("0").rstrip(".")
    return f"{v:.4f}"


def swing_points(klines: Sequence, end: int, lookback: int = 40, left: int = 2, right: int = 2) -> tuple[list[tuple[int, float]], list[tuple[int, float]]]:
    """Local swing highs / lows up to `end` (inclusive)."""
    start = max(0, end - lookback + 1)
    highs: list[tuple[int, float]] = []
    lows: list[tuple[int, float]] = []
    for i in range(start + left, end - right + 1):
        h = _f(klines[i], "high")
        l = _f(klines[i], "low")
        if all(_f(klines[j], "high") <= h for j in range(i - left, i + right + 1) if j != i):
            highs.append((i, h))
        if all(_f(klines[j], "low") >= l for j in range(i - left, i + right + 1) if j != i):
            lows.append((i, l))
    return highs, lows


def detect_springboard(klines: Sequence, index: int) -> Optional[tuple[str, str, float]]:
    """破低反涨 / 破高反跌 — pierce S/R then close back (Ch.11).

    Returns (pattern_name, direction, level) or None.
    """
    if index < 10:
        return None
    look = min(25, index)
    prior = klines[index - look : index]
    close = _f(klines[index], "close")
    high = _f(klines[index], "high")
    low = _f(klines[index], "low")
    support = min(_f(k, "low") for k in prior)
    resistance = max(_f(k, "high") for k in prior)
    # 破低反涨：影线刺破近期低点，收盘重新站上
    if low < support * 0.998 and close > support and close > _f(klines[index], "open"):
        pierce = (support - low) / max(support, 1e-9)
        if 0.001 <= pierce <= 0.04:
            return "破低反涨", "bullish", support
    # 破高反跌：影线刺破近期高点，收盘重新压回
    if high > resistance * 1.002 and close < resistance and close < _f(klines[index], "open"):
        pierce = (high - resistance) / max(resistance, 1e-9)
        if 0.001 <= pierce <= 0.04:
            return "破高反跌", "bearish", resistance
    return None


def polarity_levels(klines: Sequence, index: int, lookback: int = 40) -> list[tuple[str, float, str]]:
    """Broken swing levels that flipped role (Ch.11 polarity conversion).

    Returns list of (role, price, detail) where role is 'support' or 'resistance'.
    """
    if index < 15:
        return []
    highs, lows = swing_points(klines, index - 1, lookback=lookback)
    close = _f(klines[index], "close")
    out: list[tuple[str, float, str]] = []
    # Former resistance broken by close → support
    for i, price in highs[-6:]:
        # Find first close that broke above after the swing
        broken = False
        for j in range(i + 1, index + 1):
            if _f(klines[j], "close") > price * 1.002:
                broken = True
                break
        if not broken:
            continue
        # Still respected as support if recent closes stay mostly above
        if close >= price * 0.99:
            out.append(
                (
                    "support",
                    price,
                    f"原阻力 {_px(price)} 被收盘突破后转支撑（极性转换）",
                )
            )
    # Former support broken by close → resistance
    for i, price in lows[-6:]:
        broken = False
        for j in range(i + 1, index + 1):
            if _f(klines[j], "close") < price * 0.998:
                broken = True
                break
        if not broken:
            continue
        if close <= price * 1.01:
            out.append(
                (
                    "resistance",
                    price,
                    f"原支撑 {_px(price)} 被收盘跌破后转阻力（极性转换）",
                )
            )
    # de-dupe near prices
    uniq: list[tuple[str, float, str]] = []
    for role, price, detail in out:
        if any(abs(price - p) / max(p, 1e-9) < 0.008 and role == r for r, p, _ in uniq):
            continue
        uniq.append((role, price, detail))
    return uniq[:4]


def trendline_proximity(klines: Sequence, index: int, direction: str) -> Optional[LevelHit]:
    """Approximate rising/falling trendline from last two swings; near-touch confluence."""
    if index < 20:
        return None
    highs, lows = swing_points(klines, index, lookback=45)
    close = _f(klines[index], "close")
    if direction == "bullish" and len(lows) >= 2:
        (i1, y1), (i2, y2) = lows[-2], lows[-1]
        if i2 <= i1 or y2 < y1 * 0.995:
            return None  # need rising lows
        slope = (y2 - y1) / (i2 - i1)
        proj = y2 + slope * (index - i2)
        if proj <= 0:
            return None
        dist = abs(close - proj) / proj
        if dist <= 0.015 and close >= proj * 0.985:
            return LevelHit(
                "上升趋势线",
                f"收盘 {_px(close)} 贴近上升趋势线 {_px(proj)}（近两低点连线）",
                proj,
            )
    if direction == "bearish" and len(highs) >= 2:
        (i1, y1), (i2, y2) = highs[-2], highs[-1]
        if i2 <= i1 or y2 > y1 * 1.005:
            return None  # need falling highs
        slope = (y2 - y1) / (i2 - i1)
        proj = y2 + slope * (index - i2)
        if proj <= 0:
            return None
        dist = abs(close - proj) / proj
        if dist <= 0.015 and close <= proj * 1.015:
            return LevelHit(
                "下降趋势线",
                f"收盘 {_px(close)} 贴近下降趋势线 {_px(proj)}（近两高点连线）",
                proj,
            )
    return None


RETRACE_RATIOS = (0.382, 0.5, 0.618)


def active_retracements(klines: Sequence, index: int) -> list[tuple[float, float, str, float, float]]:
    """Last major swing's 38.2/50/61.8 levels.

    Returns (level, ratio, kind, swing_high, swing_low) where kind is
    'up_retrace' (pullback in uptrend) or 'down_retrace' (bounce in downtrend).
    """
    if index < 15:
        return []
    look = min(50, index + 1)
    start = index - look + 1
    hi_i, hi = start, _f(klines[start], "high")
    lo_i, lo = start, _f(klines[start], "low")
    for i in range(start, index + 1):
        h, l = _f(klines[i], "high"), _f(klines[i], "low")
        if h >= hi:
            hi_i, hi = i, h
        if l <= lo:
            lo_i, lo = i, l
    out: list[tuple[float, float, str, float, float]] = []
    if hi_i > lo_i and hi > lo:
        span = hi - lo
        if span / hi < 0.03:
            return []
        for r in RETRACE_RATIOS:
            level = hi - span * r
            out.append((level, r, "up_retrace", hi, lo))
    elif lo_i > hi_i and hi > lo:
        span = hi - lo
        if span / max(lo, 1e-9) < 0.03:
            return []
        for r in RETRACE_RATIOS:
            level = lo + span * r
            out.append((level, r, "down_retrace", hi, lo))
    return out


def retracement_hit(klines: Sequence, index: int, direction: str) -> Optional[LevelHit]:
    """Ch.12 — price near percentage retracement of prior swing."""
    close = _f(klines[index], "close")
    levels = active_retracements(klines, index)
    best: Optional[LevelHit] = None
    best_dist = 1.0
    for level, ratio, kind, hi, lo in levels:
        dist = abs(close - level) / max(level, 1e-9)
        if dist > 0.012:
            continue
        pct = f"{ratio * 100:.1f}".rstrip("0").rstrip(".")
        if direction == "bullish" and kind == "up_retrace":
            hit = LevelHit(
                "回撤支撑",
                f"收盘 {_px(close)} 贴近升浪 {_px(lo)}→{_px(hi)} 的 {pct}% 回撤 {_px(level)}",
                level,
            )
        elif direction == "bearish" and kind == "down_retrace":
            hit = LevelHit(
                "回撤阻力",
                f"收盘 {_px(close)} 贴近降浪 {_px(hi)}→{_px(lo)} 的 {pct}% 反弹 {_px(level)}",
                level,
            )
        else:
            continue
        if dist < best_dist:
            best_dist = dist
            best = hit
    return best
