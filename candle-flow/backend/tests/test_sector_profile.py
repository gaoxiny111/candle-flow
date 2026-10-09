"""行业口径隔离与自适应权重。"""

from __future__ import annotations

from app.analysis.sector_profile import (
    WEIGHTS_FINANCIAL,
    WEIGHTS_GENERAL,
    resolve_sector_profile,
)


def test_weights_sum_to_one():
    for w in (WEIGHTS_GENERAL, WEIGHTS_FINANCIAL):
        assert abs(sum(w.values()) - 1.0) < 1e-9


def test_priority_financial_over_dividend():
    # 银行即使被标红利，仍走金融画像
    p = resolve_sector_profile(
        industry="银行", name="工商银行", is_dividend_asset=True, is_growth_stock=True
    )
    assert p.kind == "bank"
    assert p.disable_generic_fcf is True


def test_utility_disables_roic_trap():
    p = resolve_sector_profile(industry="电力", name="长江电力")
    assert p.kind == "utility"
    assert p.disable_roic_wacc_trap is True


def test_growth_vs_cyclical():
    g = resolve_sector_profile(industry="软件开发", is_growth_stock=True)
    assert g.kind == "growth"
    c = resolve_sector_profile(industry="煤炭开采", is_cyclical=True)
    assert c.kind == "cyclical"
    assert c.weights["valuation"] >= 0.25
