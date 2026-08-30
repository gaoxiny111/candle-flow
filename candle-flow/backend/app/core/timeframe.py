"""Daily → weekly aggregation. Nison: weekly sets the trend, daily times entry."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Sequence

from app.core.candle import Candle
from app.core.indicators import is_downtrend, is_uptrend


@dataclass
class WeekBar:
    date: date
    open: float
    high: float
    low: float
    close: float
    volume: float


def _as_date(value) -> date:
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    return date.fromisoformat(str(value)[:10])


def _week_key(d: date) -> tuple[int, int]:
    iso = d.isocalendar()
    return int(iso[0]), int(iso[1])


def _bar_date(bar, fallback_day: int) -> date:
    value = getattr(bar, "date", None) or getattr(bar, "timestamp", None)
    if value is None:
        return date(2020, 1, 6) + timedelta(days=fallback_day)
    return _as_date(value)


def to_weekly(bars: Sequence) -> list[WeekBar]:
    """Fold daily bars into ISO weeks. Date is the last session in that week."""
    weeks: list[WeekBar] = []
    current_key: tuple[int, int] | None = None
    bucket: list = []
    for i, bar in enumerate(bars):
        key = _week_key(_bar_date(bar, i))
        if current_key is None:
            current_key = key
            bucket = [bar]
            continue
        if key != current_key:
            weeks.append(_fold(bucket))
            current_key = key
            bucket = [bar]
        else:
            bucket.append(bar)
    if bucket:
        weeks.append(_fold(bucket))
    return weeks


def _fold(bucket: list) -> WeekBar:
    first, last = bucket[0], bucket[-1]
    return WeekBar(
        date=_as_date(getattr(last, "date", None) or getattr(last, "timestamp", None) or date(2020, 1, 6)),
        open=float(first.open),
        high=max(float(b.high) for b in bucket),
        low=min(float(b.low) for b in bucket),
        close=float(last.close),
        volume=sum(float(getattr(b, "volume", 0) or 0) for b in bucket),
    )


def weekly_candles_as_of(bars: Sequence, end_index: int) -> list[Candle]:
    if end_index < 0 or not bars:
        return []
    weekly = to_weekly(bars[: end_index + 1])
    candles: list[Candle] = []
    for w in weekly:
        candles.append(
            Candle(
                open=w.open,
                high=w.high,
                low=w.low,
                close=w.close,
                volume=w.volume,
                timestamp=datetime.combine(w.date, datetime.min.time()),
            )
        )
    return candles


def weekly_trend_at(bars: Sequence, index: int) -> str:
    """'up' | 'down' | 'sideways' from weekly swings through this daily bar."""
    candles = weekly_candles_as_of(bars, index)
    if len(candles) < 10:
        return "sideways"
    end = len(candles) - 1
    if is_uptrend(candles, end):
        return "up"
    if is_downtrend(candles, end):
        return "down"
    return "sideways"
