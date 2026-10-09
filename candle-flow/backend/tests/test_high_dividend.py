"""高股息筛选口径单测（不打网络）。"""

from __future__ import annotations

import pandas as pd
import pytest

from app.services import high_dividend as hd


def test_thresholds_match_new_rules():
    assert hd.MIN_AVG_YIELD_3Y == 4.0
    assert hd.MIN_LAST_YIELD == 3.0
    assert hd.MIN_DIV_YEARS == 3
    assert hd.MAX_DIV_YEARS == 5
    assert hd.PAYOUT_MIN == 30.0
    assert hd.PAYOUT_MAX == 80.0
    assert hd.PE_MAX == 15.0
    assert hd.PB_MAX == 1.5
    assert hd.ROE_MIN == 10.0
    assert hd.OCF_NP_MIN == 0.8
    assert hd.MIN_MARKET_CAP == 200e8


def test_annual_cash_dps_from_sina_payout():
    hist = pd.DataFrame(
        {
            "公告日期": ["2024-07-01", "2024-12-01", "2025-07-01"],
            "派息": [10.0, 5.0, 12.0],
        }
    )
    s = hd._annual_cash_dps(hist)
    assert s.loc[2024] == pytest.approx(1.5)
    assert s.loc[2025] == pytest.approx(1.2)


def test_annual_cash_dps_legacy_columns():
    hist = pd.DataFrame(
        {
            "报告期": ["2023-12-31", "2024-12-31", "2025-12-31"],
            "每股分红": [0.8, 1.0, 1.2],
        }
    )
    s = hd._annual_cash_dps(hist)
    assert list(s.index) == [2023, 2024, 2025]
    assert s.loc[2025] == pytest.approx(1.2)


def test_consecutive_div_years_streak():
    s = pd.Series({y: 0.5 for y in range(2018, 2026)}, dtype=float)
    assert hd._consecutive_div_years(s) == 8
    # 近 5 年窗口 → 封顶 5，长期分红股仍可进入 3～5 带
    assert hd._consecutive_div_years(s, window=5) == 5


def test_consecutive_div_years_breaks_on_gap():
    s = pd.Series({2018: 0.5, 2019: 0.5, 2021: 0.6, 2022: 0.6, 2023: 0.7}, dtype=float)
    assert hd._consecutive_div_years(s) == 3
    assert hd._consecutive_div_years(s, window=5) == 3


def _base_ok(**over):
    row = {
        "code": "601088",
        "name": "命中",
        "price": 40.0,
        "avg_div_yield_3y": 5.5,
        "last_year_yield": 4.0,
        "consecutive_div_years": 4,
        "payout_ratio": 55.0,
        "pe_ttm": 12.0,
        "pb": 1.2,
        "roe": 12.0,
        "ocf_to_np": 1.1,
        "market_cap": 3e11,
        "data_ok": True,
    }
    row.update(over)
    return row


def test_apply_filters_new_rules():
    df = pd.DataFrame(
        [
            _base_ok(),
            _base_ok(code="000001", name="息不够", avg_div_yield_3y=3.0),
            _base_ok(code="000002", name="连续年数超5", consecutive_div_years=8),
            _base_ok(code="000003", name="支付率过高", payout_ratio=90.0),
            _base_ok(code="000004", name="ROE低", roe=8.0),
            _base_ok(code="000005", name="市值不够", market_cap=100e8),
        ]
    )
    out = hd._apply_filters(df)
    assert list(out["code"]) == ["601088"]


def test_fail_reasons_lists_missing_conditions():
    assert hd._fail_reasons(_base_ok()) == []
    reasons = hd._fail_reasons(_base_ok(avg_div_yield_3y=2.0, roe=5.0))
    assert any("近3年均息" in r for r in reasons)
    assert any("ROE" in r for r in reasons)


def test_to_symbol():
    assert hd._to_symbol("601088") == "601088.SH"
    assert hd._to_symbol("000001") == "000001.SZ"


def test_exclude_beijing_exchange_codes():
    assert hd._is_hs_a_code("601088") is True
    assert hd._is_hs_a_code("000001") is True
    assert hd._is_hs_a_code("300750") is True
    assert hd._is_hs_a_code("920000") is False
    assert hd._is_hs_a_code("830001") is False
    assert hd._is_hs_a_code("430047") is False
