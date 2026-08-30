"""Nison Ch.16 — measuring price targets (Western tools used with candles).

Candles give direction and risk; they do not give a price objective.
Targets use box breakouts, measured moves, and flag/pennant (incl. three methods).
Nison's conservative flag rule: project the pole from the flag's near edge
(bullish: from flag bottom; bearish: from flag top).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional, Sequence

from app.core.nison_rules import MIN_RISK_REWARD, pattern_bar_count


@dataclass(frozen=True)
class MeasuredTarget:
    price: float
    method: str
    detail: str


def _f(k, attr: str) -> float:
    return float(getattr(k, attr))


def _risk_targets(entry: float, stop: float, bullish: bool) -> tuple[float, float]:
    risk = abs(entry - stop)
    if bullish:
        return entry + risk * 2, entry + risk * 3
    return entry - risk * 2, entry - risk * 3


def _ok_direction(price: float, entry: float, bullish: bool, stop: float) -> bool:
    if price <= 0:
        return False
    if bullish and price <= entry:
        return False
    if not bullish and price >= entry:
        return False
    risk = abs(entry - stop)
    if risk <= 0:
        return False
    reward = abs(price - entry)
    return reward / risk >= MIN_RISK_REWARD


def _swing_low(klines: Sequence, end: int, lookback: int = 20) -> tuple[int, float]:
    start = max(0, end - lookback + 1)
    best_i, best = start, _f(klines[start], "low")
    for i in range(start, end + 1):
        v = _f(klines[i], "low")
        if v <= best:
            best_i, best = i, v
    return best_i, best


def _swing_high(klines: Sequence, end: int, lookback: int = 20) -> tuple[int, float]:
    start = max(0, end - lookback + 1)
    best_i, best = start, _f(klines[start], "high")
    for i in range(start, end + 1):
        v = _f(klines[i], "high")
        if v >= best:
            best_i, best = i, v
    return best_i, best


def measure_three_methods(klines: Sequence, index: int, direction: str) -> Optional[MeasuredTarget]:
    """Rising/falling three methods ≈ flag: pole into first long bar, project from flag edge."""
    if index < 4:
        return None
    c0 = klines[index - 4]
    mids = klines[index - 3 : index]
    bullish = direction == "bullish"
    if bullish:
        pole_end = _f(c0, "high")
        _, pole_start = _swing_low(klines, index - 4, lookback=15)
        pole = pole_end - pole_start
        if pole <= 0:
            return None
        flag_bottom = min(_f(c0, "low"), min(_f(m, "low") for m in mids))
        price = flag_bottom + pole
        return MeasuredTarget(
            round(price, 4),
            "旗形测幅(三法)",
            f"旗杆 {_px(pole)} 叠加旗底 {_px(flag_bottom)}（尼森保守算法）",
        )
    pole_end = _f(c0, "low")
    _, pole_start = _swing_high(klines, index - 4, lookback=15)
    pole = pole_start - pole_end
    if pole <= 0:
        return None
    flag_top = max(_f(c0, "high"), max(_f(m, "high") for m in mids))
    price = flag_top - pole
    return MeasuredTarget(
        round(price, 4),
        "旗形测幅(三法)",
        f"旗杆 {_px(pole)} 从旗顶 {_px(flag_top)} 下减（尼森保守算法）",
    )


def measure_tower(klines: Sequence, index: int, direction: str) -> Optional[MeasuredTarget]:
    """Tower: project the vertical span of the tower structure from the breakout bar."""
    n = pattern_bar_count("塔形底部" if direction == "bullish" else "塔形顶部")
    start = max(0, index - n + 1)
    window = klines[start : index + 1]
    if len(window) < 3:
        return None
    hi = max(_f(k, "high") for k in window)
    lo = min(_f(k, "low") for k in window)
    height = hi - lo
    if height <= 0:
        return None
    close = _f(klines[index], "close")
    if direction == "bullish":
        price = close + height
        return MeasuredTarget(
            round(price, 4),
            "塔形测幅",
            f"塔形高度 {_px(height)} 自确认收盘 {_px(close)} 上加",
        )
    price = close - height
    return MeasuredTarget(
        round(price, 4),
        "塔形测幅",
        f"塔形高度 {_px(height)} 自确认收盘 {_px(close)} 下减",
    )


def measure_box_breakout(klines: Sequence, index: int, direction: str) -> Optional[MeasuredTarget]:
    """Horizontal box: after close breaks the edge, project the box height from that edge."""
    if index < 12:
        return None
    # Prefer a compact box ending just before the breakout bar.
    for width in (8, 12, 16, 20):
        end = index - 1
        start = end - width + 1
        if start < 0:
            continue
        box = klines[start : end + 1]
        tops = [_f(k, "high") for k in box]
        bots = [_f(k, "low") for k in box]
        box_top, box_bot = max(tops), min(bots)
        height = box_top - box_bot
        if height <= 0:
            continue
        mid = (box_top + box_bot) / 2
        # Require a real range and that most closes stay inside (consolidation).
        closes = [_f(k, "close") for k in box]
        inside = sum(1 for c in closes if box_bot <= c <= box_top)
        if inside < len(box) * 0.85:
            continue
        # Relative tightness: avoid huge expanding trends mistaken as boxes.
        if height / mid > 0.18:
            continue
        close = _f(klines[index], "close")
        if direction == "bullish":
            if close <= box_top:
                continue
            # Breakout should not be a tiny pierce.
            if close < box_top + height * 0.02:
                continue
            price = box_top + height
            return MeasuredTarget(
                round(price, 4),
                "箱体突破",
                f"箱体 {_px(box_bot)}–{_px(box_top)} 高度 {_px(height)}，自顶边叠加",
            )
        if close >= box_bot:
            continue
        if close > box_bot - height * 0.02:
            continue
        price = box_bot - height
        return MeasuredTarget(
            round(price, 4),
            "箱体突破",
            f"箱体 {_px(box_bot)}–{_px(box_top)} 高度 {_px(height)}，自底边下减",
        )
    return None


def measure_equal_move(klines: Sequence, index: int, direction: str) -> Optional[MeasuredTarget]:
    """Measured / equal move: first leg A→B, pullback to C, project from C."""
    if index < 10:
        return None
    look = min(35, index)
    start = index - look
    bullish = direction == "bullish"
    if bullish:
        b_i = max(range(start, index + 1), key=lambda i: _f(klines[i], "high"))
        b = _f(klines[b_i], "high")
        if b_i <= start + 2 or b_i >= index:
            return None
        c_i = min(range(b_i, index + 1), key=lambda i: _f(klines[i], "low"))
        c = _f(klines[c_i], "low")
        if c_i <= b_i or c >= b * 0.995:
            return None
        a_i = min(range(start, b_i + 1), key=lambda i: _f(klines[i], "low"))
        a = _f(klines[a_i], "low")
        if b <= a:
            return None
        leg = b - a
        if leg / max(a, 1e-9) < 0.02:
            return None
        # Need a real pullback (at least ~30% of the impulse) and recovery off the low.
        if (b - c) < leg * 0.25:
            return None
        if _f(klines[index], "close") <= c:
            return None
        price = c + leg
        return MeasuredTarget(
            round(price, 4),
            "对等运动",
            f"第一波 {_px(a)}→{_px(b)} 高度 {_px(leg)}，叠加调整低点 {_px(c)}",
        )
    b_i = min(range(start, index + 1), key=lambda i: _f(klines[i], "low"))
    b = _f(klines[b_i], "low")
    if b_i <= start + 2 or b_i >= index:
        return None
    c_i = max(range(b_i, index + 1), key=lambda i: _f(klines[i], "high"))
    c = _f(klines[c_i], "high")
    if c_i <= b_i or c <= b * 1.005:
        return None
    a_i = max(range(start, b_i + 1), key=lambda i: _f(klines[i], "high"))
    a = _f(klines[a_i], "high")
    if a <= b:
        return None
    leg = a - b
    if leg / max(b, 1e-9) < 0.02:
        return None
    if (c - b) < leg * 0.25:
        return None
    if _f(klines[index], "close") >= c:
        return None
    price = c - leg
    return MeasuredTarget(
        round(price, 4),
        "对等运动",
        f"第一波 {_px(a)}→{_px(b)} 高度 {_px(leg)}，自调整高点 {_px(c)} 下减",
    )


def _px(v: float) -> str:
    return f"{v:.3f}".rstrip("0").rstrip(".")


def collect_measured_targets(
    klines: Sequence,
    index: int,
    direction: str,
    pattern_name: str,
) -> list[MeasuredTarget]:
    out: list[MeasuredTarget] = []
    if pattern_name in {"上升三法", "下降三法"}:
        t = measure_three_methods(klines, index, direction)
        if t:
            out.append(t)
    if pattern_name in {"塔形底部", "塔形顶部"}:
        t = measure_tower(klines, index, direction)
        if t:
            out.append(t)
    for fn in (measure_box_breakout, measure_equal_move):
        t = fn(klines, index, direction)
        if t:
            out.append(t)
    # de-dupe by rounded price
    seen: set[float] = set()
    uniq: list[MeasuredTarget] = []
    for t in out:
        key = round(t.price, 3)
        if key in seen:
            continue
        seen.add(key)
        uniq.append(t)
    return uniq


def resolve_take_profits(
    klines: Sequence,
    index: int,
    direction: str,
    pattern_name: str,
    entry: float,
    stop: float,
) -> tuple[float, float, str]:
    """Return (tp1, tp2, notes). Prefer Ch.16 measure; keep 2R/3R as risk fallback."""
    bullish = direction == "bullish"
    rr2, rr3 = _risk_targets(entry, stop, bullish)
    measured = [
        t
        for t in collect_measured_targets(klines, index, direction, pattern_name)
        if _ok_direction(t.price, entry, bullish, stop)
    ]

    if not measured:
        return (
            round(rr2, 4),
            round(rr3, 4),
            "蜡烛图不提供目标价；暂无清晰箱体/对等/旗形测幅，止盈按风险回报 2R/3R。",
        )

    # Prefer nearer conservative objective as tp1 (Nison: exit early rather than chase last tick).
    measured.sort(key=lambda t: abs(t.price - entry))
    primary = measured[0]
    secondary_price = rr3
    if len(measured) > 1:
        farther = measured[-1]
        if abs(farther.price - entry) > abs(primary.price - entry) * 1.05:
            secondary_price = farther.price
    elif abs(rr3 - entry) > abs(primary.price - entry):
        secondary_price = rr3

    # If measured is beyond 2R, still use it as main target; put 2R as nearer scale-out if closer.
    tp1, tp2 = primary.price, secondary_price
    if abs(rr2 - entry) < abs(primary.price - entry) * 0.95 and _ok_direction(rr2, entry, bullish, stop):
        # Keep measured as the chapter objective on tp2 when 2R is the nearer scale-out.
        tp1, tp2 = rr2, primary.price

    if bullish and tp2 < tp1:
        tp1, tp2 = tp2, tp1
    if not bullish and tp2 > tp1:
        tp1, tp2 = tp2, tp1

    notes = (
        f"第十六章测幅：{primary.method} → {_px(primary.price)}（{primary.detail}）。"
        f"目标用于减仓/平仓，不单独据此开反向仓；未达目标也可能提前反转。"
    )
    return round(tp1, 4), round(tp2, 4), notes
