from decimal import Decimal

from app.services.risk_service import RiskService


def test_rr_from_target_not_assumed_2r():
    out = RiskService().calculate(
        entry_price=Decimal("66.85"),
        stop_loss=Decimal("63.5075"),
        capital=Decimal("100000"),
        risk_per_trade=Decimal("1.0"),
        take_profit=Decimal("73.20"),
        lot_round="up",
    )
    assert out.rr_source == "target"
    assert out.risk_reward_ratio == Decimal("1.90")
    assert out.rr_meets_min is True
    assert out.assumed_2r == Decimal("73.5350")
    # 1000 / 3.3425 ≈ 299.18 → 向上取整到 300
    assert out.position_size == 300
    assert out.raw_shares is not None
    assert out.raw_shares > Decimal("299")


def test_lot_round_down_is_conservative():
    out = RiskService().calculate(
        entry_price=Decimal("66.85"),
        stop_loss=Decimal("63.5075"),
        capital=Decimal("100000"),
        risk_per_trade=Decimal("1.0"),
        take_profit=Decimal("73.20"),
        lot_round="down",
    )
    assert out.position_size == 200


def test_rr_below_min_flags_warning():
    out = RiskService().calculate(
        entry_price=Decimal("66.85"),
        stop_loss=Decimal("57.4349"),
        take_profit=Decimal("73.2002"),
        lot_round="up",
    )
    # (73.2002-66.85)/(66.85-57.4349) ≈ 0.67
    assert out.rr_source == "target"
    assert out.risk_reward_ratio < Decimal("1.5")
    assert out.rr_meets_min is False


def test_no_target_is_assumed_2r():
    out = RiskService().calculate(
        entry_price=Decimal("66.85"),
        stop_loss=Decimal("63.5075"),
    )
    assert out.rr_source == "assumed_2r"
    assert out.risk_reward_ratio == Decimal("2.00")
    assert out.take_profit_1 == out.assumed_2r
