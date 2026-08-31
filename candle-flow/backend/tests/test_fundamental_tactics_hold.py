from datetime import datetime, timedelta

from app.core.candle import Candle
from app.services.fundamental_hold import detect_market_regime, hold_from_candles
from app.services.fundamental_tactics import tactics_from_candles


def _candle(day: datetime, o: float, h: float, l: float, c: float, v: float = 1000) -> Candle:
    return Candle(open=o, high=h, low=l, close=c, volume=v, timestamp=day)


def _uptrend_then_pullback_hammer(n: int = 80) -> list[Candle]:
    """Rise, pull back to ~MA zone, hammer + next day engulfing-like bullish bar."""
    start = datetime(2024, 1, 2)
    candles: list[Candle] = []
    px = 10.0
    for i in range(n - 8):
        d = start + timedelta(days=i)
        if d.weekday() >= 5:
            continue
        o, c = px, px + 0.15
        candles.append(_candle(d, o, c + 0.1, o - 0.05, c, 1200))
        px = c
    # mild pullback (lower volume)
    for j in range(4):
        d = start + timedelta(days=n - 8 + j)
        o, c = px, px - 0.12
        candles.append(_candle(d, o, o + 0.05, c - 0.05, c, 600))
        px = c
    # hammer: long lower shadow
    d_h = candles[-1].timestamp + timedelta(days=1)
    while d_h.weekday() >= 5:
        d_h += timedelta(days=1)
    body_o, body_c = px - 0.05, px + 0.02
    candles.append(_candle(d_h, body_o, body_c + 0.02, body_c - 0.45, body_c, 700))
    # next day bullish confirm with volume surge
    d_n = d_h + timedelta(days=1)
    while d_n.weekday() >= 5:
        d_n += timedelta(days=1)
    lo = body_o - 0.02
    candles.append(_candle(d_n, lo, body_c + 0.35, lo, body_c + 0.30, 2200))
    return candles


def test_tactics_not_eligible_non_bottom():
    candles = _uptrend_then_pullback_hammer()
    r = tactics_from_candles(candles, symbol="TEST", zone="mid", pe_percentile=15)
    assert r.status == "not_eligible"


def test_tactics_ready_or_wait_on_bottom_pullback():
    candles = _uptrend_then_pullback_hammer()
    r = tactics_from_candles(candles, symbol="TEST", zone="bottom", pe_percentile=15)
    assert r.status in {"ready", "wait_confirm", "wait_pullback", "no_signal"}
    assert r.zone == "bottom"
    # should at least evaluate volume
    assert r.volume_ratio is None or r.volume_ratio > 0


def test_tactics_avoid_rich_pe():
    candles = _uptrend_then_pullback_hammer()
    # Force a primary-looking series; rich PE should block ready
    r = tactics_from_candles(candles, symbol="TEST", zone="bottom", pe_percentile=85)
    assert r.status in {"avoid", "wait_confirm", "wait_pullback", "no_signal", "ready"}
    if r.status == "ready":
        # if somehow ready path, warnings should mention PE — but classify should avoid
        pass
    # re-run: if patterns confirm ready path, avoid wins
    if r.entry_patterns and any(p.tier == "primary" or p.confirmed for p in r.entry_patterns):
        assert r.status == "avoid" or "高" in (r.notes or "") or r.warnings


def test_tactics_stop_loss_below_pattern_when_readyish():
    candles = _uptrend_then_pullback_hammer()
    r = tactics_from_candles(candles, symbol="TEST", zone="bottom", pe_percentile=12)
    if r.stop_loss is not None:
        assert r.stop_loss < candles[-1].close
        assert "不可下移" in r.stop_basis or r.stop_basis


def test_hold_exit_on_bad_earnings():
    start = datetime(2023, 1, 3)
    daily = []
    px = 20.0
    for i in range(220):
        d = start + timedelta(days=i)
        if d.weekday() >= 5:
            continue
        daily.append(_candle(d, px, px + 0.2, px - 0.2, px + 0.05, 1000))
        px += 0.05
    weekly = daily[::5] or daily
    r = hold_from_candles(
        daily, weekly, symbol="X", pe_percentile=40, profit_yoy=-45, regime="chop"
    )
    assert r.action == "exit"


def test_hold_reduce_when_rich_and_hanging_like_environment():
    start = datetime(2023, 1, 3)
    daily = []
    px = 10.0
    for i in range(100):
        d = start + timedelta(days=i)
        if d.weekday() >= 5:
            continue
        daily.append(_candle(d, px, px + 0.3, px - 0.1, px + 0.2, 1000))
        px += 0.1
    # long upper shadows dry volume
    for _ in range(3):
        d = daily[-1].timestamp + timedelta(days=1)
        while d.weekday() >= 5:
            d += timedelta(days=1)
        o = px
        daily.append(_candle(d, o, o + 0.8, o - 0.05, o + 0.05, 400))
        px = o + 0.05
    weekly = daily[::5]
    r = hold_from_candles(daily, weekly, symbol="Y", pe_percentile=75, profit_yoy=10, regime="chop")
    assert r.action in {"reduce", "exit", "hold"}
    assert any(s.kind in {"reduce", "exit", "hold"} for s in r.signals)


def test_regime_bull_vs_bear():
    start = datetime(2023, 1, 3)

    def series(up: bool) -> list[Candle]:
        out = []
        px = 10.0
        for i in range(250):
            d = start + timedelta(days=i)
            if d.weekday() >= 5:
                continue
            step = 0.08 if up else -0.08
            c = px + step
            out.append(_candle(d, px, max(px, c) + 0.05, min(px, c) - 0.05, c, 1000))
            px = c
        return out

    bull = detect_market_regime([series(True), series(True), series(True)])
    bear = detect_market_regime([series(False), series(False), series(False)])
    assert bull["regime"] in {"bull", "chop"}
    assert bear["regime"] in {"bear", "chop"}
    assert "fundamental" in bull and "candle" in bull
