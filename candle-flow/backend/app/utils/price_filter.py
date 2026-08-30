"""Drop mock/outlier bars that would smash the chart price scale."""

from typing import Callable, Sequence, TypeVar

T = TypeVar("T")


def median(values: Sequence[float]) -> float:
    if not values:
        raise ValueError("median of empty sequence")
    ordered = sorted(values)
    return ordered[len(ordered) // 2]


def price_anchor(closes: Sequence[float], tail: int = 10) -> float | None:
    """Anchor on the most recent cluster of closes (usually live quotes)."""
    if not closes:
        return None
    recent = list(closes[-max(tail, 1) :])
    med = median(recent)
    clustered = [c for c in recent if med * 0.5 <= c <= med * 1.8]
    if clustered:
        return median(clustered)
    return med


def is_price_outlier(close: float, anchor: float, low_ratio: float = 0.45, high_ratio: float = 2.2) -> bool:
    if anchor <= 0:
        return False
    return close < anchor * low_ratio or close > anchor * high_ratio


def filter_inliers(items: Sequence[T], get_close: Callable[[T], float]) -> list[T]:
    closes = [get_close(i) for i in items]
    anchor = price_anchor(closes)
    if anchor is None:
        return list(items)
    return [i for i in items if not is_price_outlier(get_close(i), anchor)]
