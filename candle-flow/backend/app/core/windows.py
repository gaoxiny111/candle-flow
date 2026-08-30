"""Window (gap) zones: Nison treats them as support/resistance *areas*.

A window is filled only when a later *close* re-enters the gap — wicks do not count.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional, Sequence

from app.core.indicators import is_downtrend, is_uptrend


@dataclass
class WindowZone:
    kind: str  # rising | falling
    start_index: int
    top: float
    bottom: float
    filled_index: Optional[int] = None

    @property
    def filled(self) -> bool:
        return self.filled_index is not None

    @property
    def key_edge(self) -> float:
        """Rising: lower rim (support). Falling: upper rim (resistance)."""
        return self.bottom if self.kind == "rising" else self.top


def collect_windows(klines: Sequence, min_gap_pct: float = 0.003) -> list[WindowZone]:
    """Find rising/falling windows; mark fill by later close."""
    if len(klines) < 10:
        return []
    zones: list[WindowZone] = []
    for i in range(1, len(klines)):
        prev, cur = klines[i - 1], klines[i]
        prev_high, prev_low = float(prev.high), float(prev.low)
        cur_high, cur_low = float(cur.high), float(cur.low)
        px = max(float(prev.close), 0.01)
        if cur_low > prev_high and (cur_low - prev_high) / px >= min_gap_pct and is_uptrend(klines, i):
            zones.append(WindowZone("rising", i, top=cur_low, bottom=prev_high))
        elif cur_high < prev_low and (prev_low - cur_high) / px >= min_gap_pct and is_downtrend(klines, i):
            zones.append(WindowZone("falling", i, top=prev_low, bottom=cur_high))

    for z in zones:
        for j in range(z.start_index + 1, len(klines)):
            close = float(klines[j].close)
            if z.kind == "rising" and close <= z.bottom:
                z.filled_index = j
                break
            if z.kind == "falling" and close >= z.top:
                z.filled_index = j
                break
    return zones


def active_windows(klines: Sequence, index: int) -> list[WindowZone]:
    """Windows still open as of `index` (fill on or before index excludes them)."""
    out = []
    for z in collect_windows(klines):
        if z.start_index >= index:
            continue
        if z.filled_index is not None and z.filled_index <= index:
            continue
        out.append(z)
    return out


def window_still_open(klines: Sequence, start_index: int, kind: str) -> bool:
    for z in collect_windows(klines):
        if z.start_index == start_index and z.kind == kind:
            return not z.filled
    return False
