"""Tests for orthogonal confluence dimensions and soft-conflict tiers."""

from datetime import date, datetime, timedelta

from app.core.candle import Candle
from app.core.confluence import (
    CROSS_WEIGHT_BY_AGE,
    DIMENSION_BY_NAME,
    MIN_HITS,
    ConfluenceResult,
    evaluate_confluence,
)


def _make_candles(specs):
    base = datetime(2026, 1, 1)
    return [Candle(o, h, l, c, v, base + timedelta(days=i)) for i, (o, h, l, c, v) in enumerate(specs)]


class _K:
    def __init__(self, c, h, l, v=1000, o=None):
        self.open = o if o is not None else c
        self.close = c
        self.high = h
        self.low = l
        self.volume = v
        self.date = date(2020, 1, 1)


def test_momentum_dimension_keeps_single_best_hit():
    """RSI + MACD + 随机指标同属动量维度，正交后只计 1 项。"""
    result = ConfluenceResult()
    result.add("RSI", "RSI low", weight=1.0)
    result.add("MACD", "MACD up", weight=1.0)
    result.add("随机指标", "KDJ cross", weight=0.9)
    result.finalize()
    momentum_hits = [h for h in result.hits if h.dimension == "momentum"]
    assert len(momentum_hits) == 1
    assert momentum_hits[0].name == "MACD"
    assert result.effective_count == 1.0


def test_orthogonal_multi_dimension_count():
    result = ConfluenceResult()
    result.add("周线趋势", "up", weight=1.0)
    result.add("RSI", "rsi", weight=1.0)
    result.add("MACD", "macd", weight=1.0)
    result.add("放量", "vol", weight=1.0)
    result.add("低点", "low", weight=1.0)
    result.finalize()
    assert result.effective_count == 4.0
    assert len(result.hits) == 4
    assert result.ok


def test_cross_freshness_weights():
    from app.core.ma_cross import ma_cross_kind

    specs = [(10.0, 10.05, 9.95, 10.0, 1000)] * 22
    px = 10.0
    for _ in range(12):
        px += 0.35
        specs.append((px - 0.1, px + 0.05, px - 0.12, px, 1000))
    candles = _make_candles(specs)
    golden_idx = next(
        i for i in range(20, len(candles)) if ma_cross_kind(candles, i)[0] == "golden"
    )
    bars = []
    d0 = date(2020, 1, 1)
    for i, (o, h, l, c, v) in enumerate(specs):
        k = _K(c, h, l, v, o=o)
        k.date = d0 + timedelta(days=i)
        bars.append(k)
    # Same-day cross → weight 1.0
    same_day = evaluate_confluence(bars, golden_idx, "bullish")
    golden = next(h for h in same_day.hits if h.name == "金叉")
    assert golden.weight == CROSS_WEIGHT_BY_AGE[0]

    # T-2 cross → weight 0.8
    later = evaluate_confluence(bars, golden_idx + 2, "bullish")
    if any(h.name == "金叉" for h in later.hits):
        g2 = next(h for h in later.hits if h.name == "金叉")
        assert g2.weight == CROSS_WEIGHT_BY_AGE[2]


def test_soft_conflict_kinds_present():
    bars = []
    price = 10.0
    for _ in range(50):
        price *= 1.02
        bars.append(_K(price, price * 1.01, price * 0.995, 1500))
    result = evaluate_confluence(bars, len(bars) - 1, "bullish")
    if result.soft_conflict_items:
        kinds = {sc.kind for sc in result.soft_conflict_items}
        assert kinds <= {"emotion_extreme", "structure_flaw", "low_momentum"}


def test_all_hit_names_mapped_to_dimension():
    expected = {
        "周线趋势", "均线转多", "均线转空", "金叉", "死叉",
        "上升趋势线", "下降趋势线", "MACD", "RSI", "随机指标",
        "RSI背离", "MACD背离", "布林", "波动率拐点", "放量", "缩量回撤",
        "均线支撑", "均线阻力", "低点", "高点", "窗口支撑", "窗口阻力",
        "极性支撑", "极性阻力", "回撤支撑", "回撤阻力", "形态互证",
    }
    assert expected == set(DIMENSION_BY_NAME.keys())


def test_min_hits_uses_weighted_count():
    result = ConfluenceResult()
    result.add("周线趋势", "t", weight=1.0)
    result.add("RSI", "r", weight=0.8)
    result.finalize()
    assert result.effective_count == 1.8
    assert not result.ok
    result.add("放量", "v", weight=1.0)
    result.finalize()
    assert result.effective_count >= MIN_HITS
    assert result.ok
