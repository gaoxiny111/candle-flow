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
    assert hot.score > cold.score
    assert hot.score >= 50


def test_rating_label_b_plus():
    from app.analysis.engine import rating_label

    assert rating_label(74.3) == "B+"
    assert rating_label(53.2) == "D"
    assert rating_label(82) == "A-"
