from datetime import date, datetime, timedelta
from types import SimpleNamespace

from app.core.candle import Candle, PatternResult
from app.core.timeframe import bars_to_candles, to_monthly, to_weekly
from app.services.fundamental_position import (
    HitPattern,
    _classify,
    _detect_flat_bottom,
    _valuation_bias,
)


def _daily_bars(n: int = 60, start: date | None = None):
    start = start or date(2024, 1, 2)
    bars = []
    px = 10.0
    for i in range(n):
        d = start + timedelta(days=i)
        # skip weekends roughly
        if d.weekday() >= 5:
            continue
        o, c = px, px + 0.1
        bars.append(
            SimpleNamespace(date=d, open=o, high=c + 0.2, low=o - 0.2, close=c, volume=1000.0)
        )
        px = c
    return bars


def test_to_monthly_folds_by_calendar_month():
    bars = []
    for day in range(1, 28):
        bars.append(
            SimpleNamespace(
                date=date(2024, 3, day),
                open=10,
                high=11,
                low=9,
                close=10.5,
                volume=100,
            )
        )
    for day in range(1, 10):
        bars.append(
            SimpleNamespace(
                date=date(2024, 4, day),
                open=12,
                high=13,
                low=11,
                close=12.5,
                volume=200,
            )
        )
    months = to_monthly(bars)
    assert len(months) == 2
    assert months[0].date == date(2024, 3, 27)
    assert months[0].open == 10
    assert months[0].close == 10.5
    assert months[1].date == date(2024, 4, 9)
    assert months[1].open == 12


def test_bars_to_candles_from_weekly():
    bars = _daily_bars(20)
    weekly = to_weekly(bars)
    candles = bars_to_candles(weekly)
    assert len(candles) == len(weekly)
    assert isinstance(candles[0], Candle)
    assert candles[0].timestamp.date() == weekly[0].date


def test_valuation_bias():
    assert _valuation_bias(10) == "cheap"
    assert _valuation_bias(90) == "rich"
    assert _valuation_bias(50) == "neutral"
    assert _valuation_bias(None) == "neutral"


def test_classify_bottom_and_top():
    bull = [HitPattern("锤子线", "2024-01-01", 70, True, "weekly")]
    bear = [HitPattern("黄昏星", "2024-01-01", 70, True, "weekly")]
    zone, _ = _classify(15.0, 20.0, bull, [])
    assert zone == "bottom"
    zone, _ = _classify(85.0, 5.0, [], bear)
    assert zone == "top"
    zone, _ = _classify(15.0, 20.0, [], bear)
    assert zone == "conflict"
    zone, _ = _classify(50.0, 12.0, [], [])
    assert zone == "mid"


def test_flat_bottom_detect():
    candles = []
    base = datetime(2024, 1, 1)
    # decline then flat lows
    for i in range(10):
        low = 8.0 if i >= 4 else 10 - i * 0.4
        candles.append(
            Candle(
                open=low + 0.3,
                high=low + 0.6,
                low=low,
                close=low + 0.2,
                volume=1000,
                timestamp=base.replace(day=min(i + 1, 28)),
            )
        )
    hit = _detect_flat_bottom(candles, lookback=8)
    assert hit is not None
    assert hit.pattern_name == "平底"
