"""高股息 AkShare 筛选框架单测（不打网络）。"""

from __future__ import annotations

import pandas as pd
import pytest

from app.services import high_dividend as hd


def test_thresholds_match_user_script():
    assert hd.MIN_AVG_YIELD_5Y == 4.0
    assert hd.MIN_DIV_YEARS == 3
    assert hd.PE_MAX == 30.0
    assert hd.PB_MAX == 5.0
    assert hd.MIN_MARKET_CAP == 50e8


def test_annual_cash_dps_from_sina_payout():
    # 派息 = 每10股；同年度两笔应加总
    hist = pd.DataFrame(
        {
            "公告日期": ["2024-07-01", "2024-12-01", "2025-07-01"],
            "派息": [10.0, 5.0, 12.0],  # 1.0 + 0.5 = 1.5 / 股（2024）
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


def test_apply_filters_user_rules():
    df = pd.DataFrame(
        [
            # 命中
            {
                "code": "601088",
                "name": "中国神华",
                "price": 40.0,
                "avg_div_yield_5y": 5.5,
                "consecutive_div_years": 5,
                "pe_ttm": 12.0,
                "pb": 1.5,
                "market_cap": 8e11,
            },
            # 息不够
            {
                "code": "000001",
                "name": "低息",
                "price": 10.0,
                "avg_div_yield_5y": 3.0,
                "consecutive_div_years": 5,
                "pe_ttm": 8.0,
                "pb": 1.0,
                "market_cap": 1e11,
            },
            # PE 过高
            {
                "code": "000002",
                "name": "贵",
                "price": 10.0,
                "avg_div_yield_5y": 5.0,
                "consecutive_div_years": 5,
                "pe_ttm": 35.0,
                "pb": 1.0,
                "market_cap": 1e11,
            },
            # 市值过小
            {
                "code": "000003",
                "name": "小盘",
                "price": 10.0,
                "avg_div_yield_5y": 6.0,
                "consecutive_div_years": 4,
                "pe_ttm": 10.0,
                "pb": 1.0,
                "market_cap": 30e8,
            },
        ]
    )
    out = hd._apply_filters(df)
    assert list(out["code"]) == ["601088"]


def test_to_symbol():
    assert hd._to_symbol("601088") == "601088.SH"
    assert hd._to_symbol("000001") == "000001.SZ"
