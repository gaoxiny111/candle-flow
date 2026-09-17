"""Unit tests for market confluence strong-signal helpers."""

from app.core.confluence import SoftConflict
from app.services.market_confluence_service import (
    _apply_tiers,
    _combined_score,
    _fund_reject_reasons,
    _is_candidate,
    _tier_of,
    _kline_weight,
    _calc_peg,
    _detect_buy_signal,
    _fund_level,
)


def test_candidate_requires_combined_floor():
    assert _is_candidate(70, 2.0, []) is True  # 82
    assert _is_candidate(60, 2.0, []) is False  # 72
    assert _is_candidate(75, 3.0, []) is True


def test_soft_conflict_blocks_candidate():
    soft = [SoftConflict("emotion_extreme", "高位追涨")]
    assert _is_candidate(90, 4.0, soft) is False


def test_low_momentum_penalty():
    soft = [SoftConflict("low_momentum", "缩量反弹")]
    assert _combined_score(70, 3.0, soft) == 80.0
    assert _is_candidate(70, 3.0, soft) is True


def test_tier_boundaries():
    # 新阈值：按基本面评分 A≥85, B≥70, C≥55, D≥40, E<40
    assert _tier_of(90) == "A"
    assert _tier_of(85) == "A"
    assert _tier_of(84.9) == "B"
    assert _tier_of(70) == "B"
    assert _tier_of(69.9) == "C"
    assert _tier_of(55) == "C"
    assert _tier_of(54.9) == "D"
    assert _tier_of(40) == "D"
    assert _tier_of(39.9) == "E"
    assert _tier_of(0) == "E"


def test_apply_tiers_by_fundamental_score():
    rows = [
        {"symbol": "A", "fundamental_score": 90, "combined_score": 80},
        {"symbol": "B", "fundamental_score": 75, "combined_score": 75},
        {"symbol": "C", "fundamental_score": 60, "combined_score": 70},
        {"symbol": "D", "fundamental_score": 45, "combined_score": 65},
        {"symbol": "E", "fundamental_score": 30, "combined_score": 60},
    ]
    kept, tiers, counts = _apply_tiers(rows)
    assert [r["symbol"] for r in kept] == ["A", "B", "C", "D", "E"]
    assert counts == {"A": 1, "B": 1, "C": 1, "D": 1, "E": 1}
    assert tiers["A"][0]["tier"] == "A"
    assert tiers["E"][0]["tier"] == "E"


def test_fund_reject_honghe_like_weak_quality():
    """鸿合科技类：微利 + 净利暴跌 + 天价 PE → 应剔除。"""
    reasons = _fund_reject_reasons(
        profit=1e7,
        debt=30.0,
        roe=1.56,
        profit_yoy=-74.65,
        pe=514.5,
    )
    assert any("ROE" in r for r in reasons)
    assert any("净利同比" in r for r in reasons)
    assert any("PE" in r for r in reasons)


def test_fund_pass_healthy():
    assert _fund_reject_reasons(
        profit=1e9,
        debt=40.0,
        roe=12.0,
        profit_yoy=8.0,
        pe=15.0,
    ) == []


def test_fund_missing_fields_not_rejected():
    assert _fund_reject_reasons(profit=1e8) == []


# ── 动态权重 & 买点信号测试 ─────────────────────────────────

def test_kline_weight_high_fundamental():
    """基本面≥80 → K线权重30%。"""
    assert _kline_weight(85) == 0.30
    assert _kline_weight(80) == 0.30
    assert _kline_weight(100) == 0.30


def test_kline_weight_mid_fundamental():
    """基本面60-80 → K线权重50%~30%线性插值。"""
    assert _kline_weight(60) == 0.50
    assert _kline_weight(70) == 0.40
    assert _kline_weight(80) == 0.30


def test_kline_weight_low_fundamental():
    """基本面<60 → K线权重70%。"""
    assert _kline_weight(59) == 0.70
    assert _kline_weight(30) == 0.70
    assert _kline_weight(0) == 0.70


def test_calc_peg_normal():
    assert _calc_peg(20.0, 25.0) == 0.8
    assert _calc_peg(30.0, 15.0) == 2.0


def test_calc_peg_invalid():
    assert _calc_peg(None, 20.0) is None
    assert _calc_peg(20.0, None) is None
    assert _calc_peg(20.0, 0) is None
    assert _calc_peg(20.0, -5.0) is None


def test_fund_level_mapping():
    assert _fund_level(90) == "A"
    assert _fund_level(80) == "B+"
    assert _fund_level(70) == "B"
    assert _fund_level(60) == "C"
    assert _fund_level(45) == "D"
    assert _fund_level(30) == "E"


def test_detect_buy_signal_strong_buy():
    """强买入：基本面≥80 + PEG<1.5 + 突破MA20 + 放量。"""
    # 构造60根K线：收盘价逐步上升，最后突破MA20，量能放大
    closes = [10.0 + i * 0.1 for i in range(60)]
    volumes = [1000.0] * 55 + [2000.0] * 5  # 最后5日放量
    class Bar:
        def __init__(self, c, v):
            self.close = c
            self.volume = v
    klines = [Bar(c, v) for c, v in zip(closes, volumes)]
    result = _detect_buy_signal(klines, 85, 1.2, 20.0)
    assert result["signal"] == "strong_buy"
    assert result["label"] == "强买入"


def test_detect_buy_signal_watch():
    """观察：基本面≥80 + PEG>2 + 回踩MA60缩量。"""
    closes = [10.0] * 60  # 价格平稳在MA60附近
    volumes = [1000.0] * 55 + [500.0] * 5  # 缩量
    class Bar:
        def __init__(self, c, v):
            self.close = c
            self.volume = v
    klines = [Bar(c, v) for c, v in zip(closes, volumes)]
    result = _detect_buy_signal(klines, 85, 2.5, 30.0)
    assert result["signal"] == "watch"
    assert result["label"] == "观察"


def test_detect_buy_signal_short_term():
    """短线博弈：基本面<60 + 突破MA20 + 放量。"""
    closes = [10.0 + i * 0.1 for i in range(60)]
    volumes = [1000.0] * 55 + [2000.0] * 5
    class Bar:
        def __init__(self, c, v):
            self.close = c
            self.volume = v
    klines = [Bar(c, v) for c, v in zip(closes, volumes)]
    result = _detect_buy_signal(klines, 50, None, None)
    assert result["signal"] == "short_term"
    assert result["label"] == "短线博弈"
