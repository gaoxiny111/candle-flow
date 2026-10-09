"""抄底信号单测（不打网络）。"""

from __future__ import annotations

import pandas as pd

from app.services.bottom_fishing import BIG_YANG_PCT, VOL_SURGE_RATIO, compute_signals


def _base_frame(n: int = 40) -> pd.DataFrame:
    dates = pd.bdate_range("2025-01-02", periods=n)
    close = [10.0 + i * 0.01 for i in range(n)]
    volume = [1_000_000.0] * n
    return pd.DataFrame({"date": dates.date, "close": close, "volume": volume})


def test_yang_surge_triggers():
    df = _base_frame(30)
    # last day: +4% and 2x volume
    df.loc[df.index[-1], "close"] = df.loc[df.index[-2], "close"] * (1 + BIG_YANG_PCT / 100 + 0.01)
    df.loc[df.index[-1], "volume"] = df.loc[df.index[-2], "volume"] * (VOL_SURGE_RATIO + 0.1)
    out = compute_signals(df)
    assert bool(out.iloc[-1]["yang_surge"])
    assert bool(out.iloc[-1]["bottom_signal"])


def test_shrink_stabilize_needs_two_of_three():
    df = _base_frame(40)
    # Stable prices near end so prior 3-day min is not broken
    base = float(df.loc[df.index[-10], "close"])
    for i in range(-10, 0):
        df.loc[df.index[i], "close"] = base + (i + 10) * 0.001
    # MA20 ~ 1e6; shrink last 3 days below 0.7 * ma
    for i in range(-3, 0):
        df.loc[df.index[i], "volume"] = 500_000.0
    out = compute_signals(df)
    assert bool(out.iloc[-1]["shrink_stabilize"])
    assert bool(out.iloc[-1]["bottom_signal"])


def test_operator_precedence_fix_does_not_apply():
    """确保不是 ``(sum >= (2 & no_new_low))`` 那种恒假写法。"""
    df = _base_frame(40)
    base = float(df.loc[df.index[-5], "close"])
    for i in range(-5, 0):
        df.loc[df.index[i], "close"] = base
        df.loc[df.index[i], "volume"] = 400_000.0
    out = compute_signals(df)
    # 近 3 日缩量且不创新低 → 应为 True
    assert bool(out.iloc[-1]["shrink_stabilize"])


def test_new_low_blocks_shrink_signal():
    df = _base_frame(40)
    for i in range(-3, 0):
        df.loc[df.index[i], "volume"] = 400_000.0
    # Break below prior 3-day min of closes
    df.loc[df.index[-1], "close"] = float(df.loc[df.index[-4], "close"]) - 1.0
    out = compute_signals(df)
    assert not bool(out.iloc[-1]["shrink_stabilize"])
