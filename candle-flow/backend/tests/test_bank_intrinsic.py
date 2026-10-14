"""银行专用估值模板：禁 FCF-DCF，DDM 主锚 + PB-ROE 辅锚。"""
from __future__ import annotations

import app.analysis.models.intrinsic as I


def test_bank_style_classifies_before_dividend():
    row = {
        "industry": "银行Ⅱ",
        "dividend_yield": 3.57,
        "payout_ratio_pct": 35.0,
        "pe_ttm": 8.6,
        "price": 7.28,
        "is_bank": True,
    }
    d = I.classify_intrinsic_style_detail(row, is_bank=True)
    assert d["style"] == I.STYLE_BANK
    assert "DDM" in d["reason"] and "DCF" in d["reason"]


def test_bank_pb_roe_range_with_h1_improvement():
    a = I.bank_pb_roe_anchors(
        {
            "roe_ttm": 10.16,
            "bvps": 8.0,
            "profit_yoy": 4.93,
        }
    )
    assert a["price_lo"] is not None and a["price_hi"] is not None
    assert a["price_lo"] < a["price_hi"]
    # ROE10.16% × 9–11 → PB 0.91–1.12
    assert 0.85 <= a["pb_lo"] <= 0.95
    assert a["improving"] is True


def test_bank_ddm_main_no_dcf_in_pool():
    o = I.compute_intrinsic_value(
        {
            "industry": "银行Ⅱ",
            "price": 7.28,
            "dividend_yield": 3.57,
            "dividend_per_share": 0.25,
            "payout_ratio_pct": 35.0,
            "roe_ttm": 10.16,
            "bvps": 8.0,
            "profit_yoy": 4.93,
            "is_bank": True,
        },
        dcf={
            "value": 96.6,
            "note": "FCF DCF",
            "reliable": False,
            "role": "standard",
        },
        ddm={
            "value": 9.43,
            "note": "银行DDM r=4.7% g=2%",
            "reliable": True,
        },
        is_bank=True,
        is_dividend_asset=False,
        is_growth_stock=False,
    )
    assert o["style"] == I.STYLE_BANK
    assert "dcf" not in o["models"]
    assert "ddm" in o["models"]
    assert o["intrinsic_value_per_share"] == 9.43
    assert o["margin_of_safety_pct"] is not None
    assert o["margin_of_safety_pct"] > 20
    assert "DDM 内在价值" in o["note"]
    assert "不适用于银行" in o["note"]
    assert "pb_roe" in o["auxiliary"]
    assert o["auxiliary"]["pb_roe"]["price_lo"] is not None
    # 误注入的 FCF-DCF 进 auxiliary，文案标明剔除
    assert "dcf" in o["auxiliary"]
    assert "不适用于银行" in o["auxiliary"]["dcf"]["reason"]


def test_bank_ddm_formula_matches_user_example():
    """DPS0.25 / r4.7% / g2% → ≈9.44。"""
    d0, r, g = 0.25, 0.047, 0.02
    iv = d0 * (1 + g) / (r - g)
    assert abs(iv - 9.44) < 0.02
