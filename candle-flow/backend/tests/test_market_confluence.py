"""Unit tests for market confluence strong-signal helpers."""

from app.core.confluence import SoftConflict
from app.services.market_confluence_service import _combined_score, _is_strong


def test_strong_requires_high_combined():
    assert _is_strong(70, 2.0, []) is True  # 70+12=82
    assert _is_strong(60, 2.0, []) is False  # 72
    assert _is_strong(75, 3.0, []) is True


def test_soft_conflict_blocks_strong():
    soft = [SoftConflict("emotion_extreme", "高位追涨")]
    assert _is_strong(90, 4.0, soft) is False


def test_low_momentum_penalty():
    soft = [SoftConflict("low_momentum", "缩量反弹")]
    # 70+18-8=80 → still strong
    assert _combined_score(70, 3.0, soft) == 80.0
    assert _is_strong(70, 3.0, soft) is True
