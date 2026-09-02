"""Unit tests for market confluence strong-signal helpers."""

from app.core.confluence import SoftConflict
from app.services.market_confluence_service import (
    _apply_tiers,
    _combined_score,
    _fund_reject_reasons,
    _is_candidate,
    _tier_of,
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
    assert _tier_of(120) == "S"
    assert _tier_of(119.9) == "A"
    assert _tier_of(115) == "A"
    assert _tier_of(114.9) == "B"
    assert _tier_of(110) == "B"
    assert _tier_of(109.9) is None


def test_apply_tiers_only_keeps_b_and_above():
    rows = [
        {"symbol": "A", "combined_score": 125, "direction": "bullish"},
        {"symbol": "B", "combined_score": 117, "direction": "bullish"},
        {"symbol": "C", "combined_score": 112, "direction": "bullish"},
        {"symbol": "D", "combined_score": 100, "direction": "bullish"},
    ]
    kept, tiers, counts = _apply_tiers(rows)
    assert [r["symbol"] for r in kept] == ["A", "B", "C"]
    assert counts == {"S": 1, "A": 1, "B": 1}
    assert tiers["S"][0]["tier"] == "S"


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
