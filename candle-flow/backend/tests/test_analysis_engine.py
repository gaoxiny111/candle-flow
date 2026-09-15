"""Tests for fundamental analysis engine."""

import pandas as pd

from app.analysis.base import AnalysisLevel
from app.analysis.engine import FundamentalEngine
from app.analysis.modules.cashflow import CashflowAnalyzer
from app.analysis.modules.growth import GrowthAnalyzer
from app.analysis.modules.profitability import ProfitabilityAnalyzer
from app.analysis.modules.risk import RiskAnalyzer


def _sample_financials() -> pd.DataFrame:
    rows = [
        {"revenue": 80e8, "net_profit": 8e8, "equity": 40e8, "operating_cashflow": 9e8, "capital_expenditure": 1e8,
         "operating_profit": 10e8, "cogs": 48e8, "total_assets": 100e8, "current_liabilities": 30e8,
         "accounts_receivable": 8e8, "goodwill": 2e8, "monetary_funds": 5e8, "short_term_borrowings": 1e8, "roe": 18.0},
        {"revenue": 90e8, "net_profit": 10e8, "equity": 45e8, "operating_cashflow": 11e8, "capital_expenditure": 1.2e8,
         "operating_profit": 12e8, "cogs": 54e8, "total_assets": 110e8, "current_liabilities": 32e8,
         "accounts_receivable": 9e8, "goodwill": 2e8, "monetary_funds": 6e8, "short_term_borrowings": 1e8, "roe": 20.0},
        {"revenue": 100e8, "net_profit": 12e8, "equity": 50e8, "operating_cashflow": 13e8, "capital_expenditure": 1.5e8,
         "operating_profit": 14e8, "cogs": 60e8, "total_assets": 120e8, "current_liabilities": 35e8,
         "accounts_receivable": 10e8, "goodwill": 2e8, "monetary_funds": 7e8, "short_term_borrowings": 1e8, "roe": 22.0},
    ]
    return pd.DataFrame(rows, index=["20211231", "20221231", "20231231"])


def test_extract_row_by_symbol():
    from app.analysis.financials import _extract_row

    df = pd.DataFrame({"股票代码": ["603236", "000001"], "净利润": [11.0, 22.0]})
    row = _extract_row(df, "603236.SH")
    assert row is not None
    assert row["净利润"] == 11.0


def test_profitability_analyzer_scores():
    result = ProfitabilityAnalyzer().analyze(_sample_financials())
    assert result.score >= 70
    assert result.level in (AnalysisLevel.EXCELLENT, AnalysisLevel.GOOD)
    assert any(i.name == "ROE(%)" for i in result.indicators)


def test_growth_analyzer_with_yoy():
    result = GrowthAnalyzer().analyze(
        _sample_financials(), revenue_yoy=15.0, profit_yoy=20.0
    )
    assert result.score > 0
    assert any("CAGR" in i.name for i in result.indicators)


def test_cashflow_analyzer():
    result = CashflowAnalyzer().analyze(_sample_financials(), ocf_per_share=1.2)
    assert result.score >= 60
    assert any("经营现金流" in i.name for i in result.indicators)


def test_risk_analyzer_high_score_for_healthy():
    result = RiskAnalyzer().analyze(_sample_financials())
    assert result.score >= 70


def test_engine_composite_without_db(monkeypatch):
    engine = FundamentalEngine()

    def fake_build(symbol: str, years: int = 5):
        return _sample_financials(), {
            "name": "测试股",
            "industry": "软件",
            "report_dates": list(_sample_financials().index),
            "revenue_yoy": 12.0,
            "profit_yoy": 18.0,
            "debt_ratio": 45.0,
            "latest_report": "20231231",
            "ocf_per_share": 1.1,
        }

    monkeypatch.setattr("app.analysis.engine.build_financial_dataframe", fake_build)
    monkeypatch.setattr("app.analysis.engine.industry_averages", lambda *a, **k: {"roe": 15, "revenue_yoy": 8})
    monkeypatch.setattr("app.analysis.engine.get_valuations", lambda *a, **k: [])
    monkeypatch.setattr(
        "app.analysis.engine.calculate_comparable_valuation",
        lambda *a, **k: {
            "stock_code": "600519.SH",
            "comparables": [],
            "avg_pe": None,
            "avg_pb": None,
            "valuation_range": {},
            "peer_count": 0,
        },
    )

    report = engine.run_full_analysis("600519.SH", db=None)
    assert report["composite_score"] > 0
    assert report["final_rating"] in ("A", "A-", "B+", "B", "B-", "C", "D", "E")
    assert "profitability" in report["modules"]
    assert "summary" in report


def test_growth_v_shape_boost():
    """历史 CAGR 为负但最新同比强劲 → 应识别拐点且分数明显高于纯 CAGR。"""
    rows = [
        {"revenue": 600e8, "net_profit": 110e8, "equity": 400e8, "operating_cashflow": 100e8,
         "capital_expenditure": 20e8, "operating_profit": 120e8, "cogs": 400e8, "total_assets": 900e8,
         "current_liabilities": 300e8, "roe": 25.0},
        {"revenue": 530e8, "net_profit": 77e8, "equity": 420e8, "operating_cashflow": 90e8,
         "capital_expenditure": 20e8, "operating_profit": 90e8, "cogs": 360e8, "total_assets": 950e8,
         "current_liabilities": 310e8, "roe": 18.0},
        {"revenue": 520e8, "net_profit": 52e8, "equity": 450e8, "operating_cashflow": 95e8,
         "capital_expenditure": 25e8, "operating_profit": 60e8, "cogs": 350e8, "total_assets": 1000e8,
         "current_liabilities": 320e8, "roe": 12.0},
        {"revenue": 414e8, "net_profit": 51e8, "equity": 480e8, "operating_cashflow": 94e8,
         "capital_expenditure": 30e8, "operating_profit": 58e8, "cogs": 280e8, "total_assets": 1100e8,
         "current_liabilities": 340e8, "roe": 10.5},
    ]
    fd = pd.DataFrame(rows, index=["20221231", "20231231", "20241231", "20251231"])
    cold = GrowthAnalyzer().analyze(fd, revenue_yoy=-5.0, profit_yoy=-10.0)
    hot = GrowthAnalyzer().analyze(fd, revenue_yoy=13.5, profit_yoy=82.5)
    assert hot.metadata.get("v_shape") is True
    assert cold.metadata.get("v_shape") is False
    assert type(cold.metadata["v_shape"]) is bool
    assert hot.score > cold.score
    assert hot.score >= 50


def test_json_safe_strips_numpy_bool():
    import numpy as np
    from pydantic import BaseModel
    from typing import Any, Optional

    from app.analysis.engine import _json_safe

    class Wrap(BaseModel):
        data: Optional[Any] = None

    raw = {"v_shape": np.bool_(False), "score": np.float64(12.5), "nested": {"ok": np.True_}}
    safe = _json_safe(raw)
    assert safe["v_shape"] is False
    assert type(safe["v_shape"]) is bool
    assert type(safe["score"]) is float
    assert safe["nested"]["ok"] is True
    Wrap(data=safe).model_dump()


def test_rating_label_b_plus():
    from app.analysis.engine import rating_label

    assert rating_label(74.3) == "B+"
    assert rating_label(53.2) == "D"
    assert rating_label(82) == "A-"


def test_downgrade_rating():
    from app.analysis.engine import downgrade_rating

    assert downgrade_rating("C") == "D"
    assert downgrade_rating("A") == "A-"
    assert downgrade_rating("E") == "E"
    assert downgrade_rating("B+", 2) == "B-"


def test_cashflow_veto_masks_fatal_shortfall():
    """图四：高成长+差现金流不得以均分维持中性；应压分并触发否决降档。"""
    from app.analysis.config import CASHFLOW_VETO_MESSAGE, MODULE_WEIGHTS
    from app.analysis.engine import (
        _penalized_module_score,
        downgrade_rating,
        rating_label,
    )

    # 对照图例：盈利 75.7 / 成长 87 / 现金流 25.5 / 偿债 55 / 估值 65
    scores = {
        "profitability": 75.7,
        "growth": 87.0,
        "cashflow": 25.5,
        "solvency": 55.0,
        "valuation": 65.0,
    }
    composite = 0.0
    weight_sum = 0.0
    for name, w in MODULE_WEIGHTS.items():
        composite += _penalized_module_score(scores[name]) * w
        weight_sum += w
    composite = round(composite / weight_sum, 1)

    assert composite < 55  # 应落入 D 档分数区间（不再被均分成 C）
    letter = rating_label(composite)
    if scores["cashflow"] < 40:
        letter = downgrade_rating(letter, 1)
    assert letter in ("D", "E")
    assert CASHFLOW_VETO_MESSAGE


def test_penalized_module_score_e_grade():
    from app.analysis.engine import _penalized_module_score

    assert _penalized_module_score(80) == 80
    assert _penalized_module_score(25.5) == 25.5 * 0.5


def test_engine_skips_etf_fundamentals(monkeypatch):
    engine = FundamentalEngine()

    def fail_build(*_a, **_k):
        raise AssertionError("ETF should not load stock financials")

    monkeypatch.setattr("app.analysis.engine.build_financial_dataframe", fail_build)
    monkeypatch.setattr(
        "app.analysis.engine.get_valuations",
        lambda *a, **k: (_ for _ in ()).throw(AssertionError("ETF should not fetch valuations")),
    )

    report = engine.run_full_analysis("510300.SH", db=None)
    assert report["skipped"] is True
    assert report["skip_reason"] == "etf"
    assert report["composite_score"] is None
    assert report["final_rating"] is None
    assert report["market"]["dividend_yield"] is None
    assert report["modules"] == {}


def test_dcf_shares_from_market_cap_and_reliability():
    from app.analysis.engine import FundamentalEngine
    from app.analysis.models.dcf import DCFModel

    # 股本错误（1e9）会把神华级别 FCF 打到数百元/股
    blown = DCFModel(wacc=0.08, terminal_growth=0.02).value(
        base_fcf=5e10, high_growth_rate=0.04, transition_growth_rate=0.03, shares_outstanding=1e9
    )
    assert blown["intrinsic_value_per_share"] and blown["intrinsic_value_per_share"] > 200

    # 用市值/股价还原股本后应回到合理量级
    eng = FundamentalEngine()
    fd = _sample_financials()
    # 放大到接近龙头量级
    fd = fd.copy()
    fd["operating_cashflow"] = [5e10, 6e10, 7e10]
    fd["capital_expenditure"] = [1e10, 1.2e10, 1.5e10]
    fd["net_profit"] = [4e10, 5e10, 5.5e10]
    fd["eps"] = [2.0, 2.5, 2.8]
    market = {"price": 48.0, "market_cap": 48.0 * 2e10}  # ~200 亿股
    meta = {"symbol": "601088.SH", "profit_yoy": 4.0, "debt_ratio": 25.0, "eps": 2.8}
    dcf = eng._build_dcf(fd, market, meta)
    assert dcf["shares_source"] == "market_cap/price"
    assert dcf["fcf_source"] == "ocf-capex"
    assert dcf.get("is_reliable") is True
    iv = dcf["intrinsic_value_per_share"]
    assert iv is not None
    assert 10 < iv < 150  # 相对 48 元现价不爆表
    assert abs(dcf["assumptions"]["wacc"] - 0.08) < 1e-9  # 低负债动态 WACC


def test_cashflow_paper_wealth_cuts_fcf_score():
    """连续亏损 + 正自由现金流 → 不给高分，触发纸面富贵预警。"""
    rows = [
        {"revenue": 100e8, "net_profit": -5e8, "equity": 40e8, "operating_cashflow": 8e8,
         "capital_expenditure": 1e8, "operating_profit": -4e8, "total_assets": 120e8,
         "current_liabilities": 50e8, "accounts_receivable": 30e8},
        {"revenue": 80e8, "net_profit": -8e8, "equity": 30e8, "operating_cashflow": 12e8,
         "capital_expenditure": 1e8, "operating_profit": -6e8, "total_assets": 110e8,
         "current_liabilities": 55e8, "accounts_receivable": 28e8},
        {"revenue": 40e8, "net_profit": -10e8, "equity": 20e8, "operating_cashflow": 18e8,
         "capital_expenditure": 4.5e8, "operating_profit": -8e8, "total_assets": 100e8,
         "current_liabilities": 60e8, "accounts_receivable": 25e8},
    ]
    fd = pd.DataFrame(rows, index=["20231231", "20241231", "20251231"])
    result = CashflowAnalyzer().analyze(fd)
    assert result.metadata.get("paper_wealth") is True
    fcf = next(i for i in result.indicators if i.name.startswith("自由现金流"))
    assert fcf.score <= 35
    assert any("纸面富贵" in w for w in result.warnings)
    assert any(i.name == "亏损现金背离" for i in result.indicators)


def test_ar_warning_collection_vs_inflation():
    """营收暴跌而应收降幅更小 → 回款困难，而非虚增收入。"""
    rows = [
        {"revenue": 100e8, "net_profit": 5e8, "equity": 40e8, "operating_cashflow": 6e8,
         "capital_expenditure": 1e8, "accounts_receivable": 40e8},
        {"revenue": 48e8, "net_profit": -2e8, "equity": 35e8, "operating_cashflow": 3e8,
         "capital_expenditure": 1e8, "accounts_receivable": 38e8},
    ]
    fd = pd.DataFrame(rows, index=["20241231", "20251231"])
    result = CashflowAnalyzer().analyze(fd)
    assert any("回款极其困难" in w for w in result.warnings)
    assert not any("虚增收入" in w for w in result.warnings)


def test_value_trap_caps_valuation_for_st_loss(monkeypatch):
    """ST + 连续亏损时，低 PB 不得把估值分打到高分。"""
    from app.analysis.base import AnalysisLevel, ModuleResult
    from app.analysis.engine import FundamentalEngine

    engine = FundamentalEngine()

    def fake_build(symbol: str, years: int = 5):
        rows = [
            {"revenue": 100e8, "net_profit": -5e8, "equity": 40e8, "operating_cashflow": 10e8,
             "capital_expenditure": 2e8, "operating_profit": -4e8, "total_assets": 120e8,
             "current_liabilities": 50e8, "accounts_receivable": 30e8, "roe": -12.0},
            {"revenue": 50e8, "net_profit": -8e8, "equity": 25e8, "operating_cashflow": 15e8,
             "capital_expenditure": 2e8, "operating_profit": -6e8, "total_assets": 100e8,
             "current_liabilities": 55e8, "accounts_receivable": 28e8, "roe": -20.0},
            {"revenue": 40e8, "net_profit": -10e8, "equity": 15e8, "operating_cashflow": 18e8,
             "capital_expenditure": 4e8, "operating_profit": -8e8, "total_assets": 90e8,
             "current_liabilities": 60e8, "accounts_receivable": 26e8, "roe": -30.0},
        ]
        fd = pd.DataFrame(rows, index=["20231231", "20241231", "20251231"])
        return fd, {
            "name": "ST龙元",
            "industry": "房屋建设",
            "symbol": "600491.SH",
            "report_dates": list(fd.index),
            "annual_dates": list(fd.index),
            "revenue_yoy": -51.0,
            "profit_yoy": -80.0,
            "debt_ratio": 75.0,
            "latest_report": "20251231",
            "latest_roe": -30.0,
            "ocf_per_share": 1.2,
        }

    monkeypatch.setattr("app.analysis.engine.build_financial_dataframe", fake_build)
    monkeypatch.setattr("app.analysis.engine.industry_averages", lambda *a, **k: {})
    monkeypatch.setattr(
        "app.analysis.engine.get_valuations",
        lambda *a, **k: [
            {
                "symbol": "600491.SH",
                "name": "ST龙元",
                "price": 2.0,
                "pe_ttm": None,
                "pb": 0.42,
                "pe_percentile": None,
                "pb_percentile": 5.0,
                "market_cap": 5e9,
                "dividend_yield": 0.0,
            }
        ],
    )
    monkeypatch.setattr(
        "app.analysis.engine.calculate_comparable_valuation",
        lambda *a, **k: {
            "stock_code": "600491.SH",
            "comparables": [],
            "avg_pe": None,
            "avg_pb": None,
            "valuation_range": {},
            "peer_count": 0,
            "insufficient_sample": True,
        },
    )

    report = engine.run_full_analysis("600491.SH", db=None)
    val = report["valuation"]
    assert val.get("value_trap_veto") is True
    assert float(val.get("composite_valuation_score") or 99) <= 28
    assert "陷阱" in (val.get("value_trap_message") or "")


def test_dcf_default_shares_marked_unreliable():
    from app.analysis.engine import FundamentalEngine

    eng = FundamentalEngine()
    fd = _sample_financials()
    dcf = eng._build_dcf(fd, {"price": 10.0}, {"symbol": "TEST.SH", "profit_yoy": 5.0})
    assert dcf["shares_source"] == "default_1e9"
    assert dcf.get("is_reliable") is False


def test_format_report_period():
    from app.analysis.base import format_report_period

    assert format_report_period("20251231") == "2025年报"
    assert format_report_period("20250630") == "2025中报"
    assert format_report_period(None) == ""


def test_growth_quality_divergence_warning():
    """净利高增 + 现金流/净利很差 → 增长质量背离指标。"""
    rows = [
        {"revenue": 80e8, "net_profit": 8e8, "equity": 40e8, "operating_cashflow": 9e8,
         "capital_expenditure": 1e8, "operating_profit": 10e8, "total_assets": 100e8,
         "current_liabilities": 30e8},
        {"revenue": 100e8, "net_profit": 12e8, "equity": 50e8, "operating_cashflow": -8e8,
         "capital_expenditure": 1e8, "operating_profit": 14e8, "total_assets": 120e8,
         "current_liabilities": 35e8},
    ]
    fd = pd.DataFrame(rows, index=["20241231", "20251231"])
    result = CashflowAnalyzer().analyze(fd, profit_yoy=27.84, latest_report="20250630")
    names = [i.name for i in result.indicators]
    assert "增长质量背离度" in names
    div = next(i for i in result.indicators if i.name == "增长质量背离度")
    assert div.score < 55
    assert any("增长质量背离" in w for w in result.warnings)


def test_roic_vs_wacc_comment():
    fd = _sample_financials()
    # 压低 operating_profit 使 ROIC 偏低
    fd = fd.copy()
    fd["operating_profit"] = [2e8, 2.2e8, 2.5e8]
    result = ProfitabilityAnalyzer().analyze(fd, debt_ratio=60.0, latest_roe=8.0)
    roic = next(i for i in result.indicators if i.name == "ROIC(%)")
    assert "WACC" in (roic.comment or "")
    assert any("WACC" in w for w in result.warnings)


def test_solvency_interest_bearing_and_liquidity():
    from app.analysis.modules.solvency import SolvencyAnalyzer

    result = SolvencyAnalyzer().analyze(
        pd.DataFrame(),
        debt_ratio=59.66,
        industry="电子元件",
        interest_bearing_ratio=28.0,
        current_ratio=1.4,
        quick_ratio=1.0,
        balance_sheet={"report_date": "20251231"},
    )
    names = [i.name for i in result.indicators]
    assert "资产负债率(%)" in names
    assert "有息负债率(%)" in names
    assert "流动比率" in names
    assert "速动比率" in names
    debt = next(i for i in result.indicators if i.name == "资产负债率(%)")
    assert debt.period == "2025年报"
    assert any("偏高" in w for w in result.warnings)


def test_valuation_rationale_and_cashflow_haircut(monkeypatch):
    engine = FundamentalEngine()

    def fake_build(symbol: str, years: int = 5):
        fd = _sample_financials().copy()
        # 差现金流：经营现金流远低于利润
        fd["operating_cashflow"] = [-2e8, -1e8, -3e8]
        return fd, {
            "name": "测试股",
            "industry": "电子",
            "symbol": "000001.SZ",
            "report_dates": list(fd.index),
            "annual_dates": list(fd.index),
            "revenue_yoy": 20.0,
            "profit_yoy": 28.0,
            "debt_ratio": 50.0,
            "latest_report": "20250630",
            "ocf_per_share": -0.5,
            "latest_roe": 15.0,
        }

    monkeypatch.setattr("app.analysis.engine.build_financial_dataframe", fake_build)
    monkeypatch.setattr("app.analysis.engine.industry_averages", lambda *a, **k: {})
    monkeypatch.setattr(
        "app.analysis.engine.get_valuations",
        lambda *a, **k: [
            {
                "symbol": "000001.SZ",
                "name": "测试股",
                "price": 10.0,
                "pe_ttm": 8.0,
                "pb": 1.2,
                "pe_percentile": 11.0,
                "pb_percentile": 23.0,
                "market_cap": 1e10,
                "dividend_yield": 1.0,
                "total_shares": 1e9,
            }
        ],
    )
    monkeypatch.setattr(
        "app.analysis.engine.calculate_comparable_valuation",
        lambda *a, **k: {
            "stock_code": "000001.SZ",
            "comparables": [],
            "avg_pe": None,
            "avg_pb": None,
            "valuation_range": {},
            "peer_count": 0,
            "insufficient_sample": True,
        },
    )

    report = engine.run_full_analysis("000001.SZ", db=None)
    val = report["valuation"]
    assert val.get("valuation_rationale")
    assert "valuation_score_breakdown" in val
    assert report.get("latest_report") == "20250630"
    growth = report["modules"]["growth"]
    yoy = next(i for i in growth["indicators"] if "营收同比" in i["name"])
    assert yoy.get("period")
    assert "中报" in yoy["period"] or "2025" in yoy["period"]
