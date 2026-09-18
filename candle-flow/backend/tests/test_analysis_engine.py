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
    assert abs(dcf["assumptions"]["wacc"] - 0.08) < 1e-9  # 负债率25% → WACC 8%
    assert dcf.get("role") == "pessimistic_reference"
    assert "保守" in (dcf.get("note") or "") or "悲观" in (dcf.get("note") or "")


def test_dcf_blue_chip_lower_wacc():
    eng = FundamentalEngine()
    fd = _sample_financials()
    dcf = eng._build_dcf(
        fd,
        {"price": 1400.0, "market_cap": 1400.0 * 12.56e8},
        {"symbol": "600519.SH", "profit_yoy": 15.0, "debt_ratio": 16.42},
    )
    assert abs(dcf["assumptions"]["wacc"] - 0.07) < 1e-9
    assert abs(dcf["assumptions"]["terminal_growth"] - 0.025) < 1e-9

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


def test_ar_surge_warns_working_capital_not_fraud():
    """营收高增且应收增速≥2倍 → 营运资本占用/坏账减值，不指控虚增收入。"""
    rows = [
        {"revenue": 100e8, "net_profit": 10e8, "equity": 50e8, "operating_cashflow": 12e8,
         "capital_expenditure": 2e8, "accounts_receivable": 10e8},
        {"revenue": 130e8, "net_profit": 12e8, "equity": 55e8, "operating_cashflow": 8e8,
         "capital_expenditure": 2e8, "accounts_receivable": 28e8},
    ]
    fd = pd.DataFrame(rows, index=["20241231", "20251231"])
    result = CashflowAnalyzer().analyze(fd)
    assert any("营运资本占用" in w for w in result.warnings)
    assert any("坏账与减值" in w for w in result.warnings)
    assert not any("虚增收入" in w for w in result.warnings)


def test_ar_moderate_growth_warns_collection():
    """温和增收但应收增速更快 → 回款困难，而非营运资本/造假话术。"""
    rows = [
        {"revenue": 100e8, "net_profit": 5e8, "equity": 40e8, "operating_cashflow": 6e8,
         "capital_expenditure": 1e8, "accounts_receivable": 20e8},
        {"revenue": 113e8, "net_profit": 5.5e8, "equity": 41e8, "operating_cashflow": 4e8,
         "capital_expenditure": 1e8, "accounts_receivable": 28e8},
    ]
    fd = pd.DataFrame(rows, index=["20241231", "20251231"])
    result = CashflowAnalyzer().analyze(fd)
    assert any("回款极其困难" in w for w in result.warnings)
    assert not any("虚增收入" in w for w in result.warnings)
    assert not any("营运资本占用" in w for w in result.warnings)


def test_moutai_like_not_cash_debt_dual_high():
    """高现金 + 合同负债残差不得触发存贷双高；应定性资金充裕。"""
    rows = [
        {
            "revenue": 1500e8, "net_profit": 700e8, "equity": 2500e8,
            "operating_cashflow": 800e8, "monetary_funds": 1700e8,
            "short_term_borrowings": 0, "accounts_receivable": 1e6, "goodwill": 0,
        },
        {
            "revenue": 1600e8, "net_profit": 750e8, "equity": 2600e8,
            "operating_cashflow": 850e8, "monetary_funds": 1700e8,
            "short_term_borrowings": 0, "accounts_receivable": 1e6, "goodwill": 0,
        },
    ]
    fd = pd.DataFrame(rows, index=["20241231", "20251231"])
    result = RiskAnalyzer().analyze(
        fd,
        name="贵州茅台",
        industry="白酒",
        debt_ratio=16.42,
        interest_bearing_debt=0,
        short_term_borrowings=0,
    )
    assert not any("存贷双高" in w for w in result.warnings)
    # "资金极度充裕"是优势，已移至 highlights（metadata）而非 warnings
    highlights = result.metadata.get("highlights") or []
    assert any("资金极度充裕" in h for h in highlights)
    assert any("宏观与政策风险" in w for w in result.warnings)


def test_true_cash_debt_dual_high_still_flags():
    rows = [
        {
            "revenue": 100e8, "net_profit": 5e8, "equity": 50e8,
            "operating_cashflow": 6e8, "monetary_funds": 50e8,
            "short_term_borrowings": 40e8, "accounts_receivable": 10e8, "goodwill": 0,
        },
        {
            "revenue": 110e8, "net_profit": 6e8, "equity": 55e8,
            "operating_cashflow": 7e8, "monetary_funds": 55e8,
            "short_term_borrowings": 45e8, "accounts_receivable": 11e8, "goodwill": 0,
        },
    ]
    fd = pd.DataFrame(rows, index=["20241231", "20251231"])
    result = RiskAnalyzer().analyze(fd, name="测试股", industry="化工", debt_ratio=55.0)
    assert any("存贷双高" in w for w in result.warnings)


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
    monkeypatch.setattr(
        "app.analysis.engine.detect_major_risk_events",
        lambda *_a, **_k: {"fatal": False, "events": [], "event_count": 0, "message": ""},
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


def test_shenhua_like_not_cash_debt_dual_high():
    """高现金 + 少量真实短贷（短债/现金≪40%）≠存贷双高，应定性资金充裕。"""
    rows = [
        {
            "revenue": 3000e8, "net_profit": 600e8, "equity": 5500e8,
            "operating_cashflow": 800e8, "monetary_funds": 1410e8,
            "short_term_borrowings": 131e8, "accounts_receivable": 160e8, "goodwill": 0,
        },
        {
            "revenue": 3200e8, "net_profit": 620e8, "equity": 6000e8,
            "operating_cashflow": 850e8, "monetary_funds": 1410e8,
            "short_term_borrowings": 131e8, "accounts_receivable": 165e8, "goodwill": 0,
        },
    ]
    fd = pd.DataFrame(rows, index=["20241231", "20251231"])
    result = RiskAnalyzer().analyze(
        fd,
        name="中国神华",
        industry="煤炭开采",
        debt_ratio=33.0,
        interest_bearing_debt=860e8,
        short_term_borrowings=131e8,
        dividend_yield=4.5,
        pe_ttm=18.0,
    )
    assert not any("存贷双高" in w for w in result.warnings)
    # "资金极度充裕/现金奶牛"是优势，已移至 highlights（metadata）
    highlights = result.metadata.get("highlights") or []
    assert any("资金极度充裕" in h or "现金奶牛" in h for h in highlights)


def test_high_dividend_roic_wacc_exemption():
    """高股息资产：ROIC 用红利 WACC≈4.5%，不标毁灭价值、不触发否决 flag。"""
    fd = _sample_financials().copy()
    # 压低经营利润 → ROIC 约 5%~6%：低于成长股 WACC≈10%，但覆盖红利 WACC≈4.5%
    fd["operating_profit"] = [6e8, 6.5e8, 7e8]
    result = ProfitabilityAnalyzer().analyze(
        fd,
        debt_ratio=33.0,
        latest_roe=12.0,
        dividend_yield=6.0,
        pe_ttm=12.0,
    )
    assert result.metadata.get("is_dividend_asset") is True
    assert result.metadata.get("roic_below_wacc") is False
    assert abs(float(result.metadata.get("wacc_pct") or 0) - 4.5) < 1e-6
    roic = next(i for i in result.indicators if i.name == "ROIC(%)")
    assert float(roic.value) >= 4.5
    assert "毁灭" not in (roic.comment or "")
    assert "红利" in (roic.comment or "") or "现金回报" in (roic.comment or "")
    assert not any("毁灭" in w for w in result.warnings)


def test_high_dividend_valuation_skips_peg_and_value_trap(monkeypatch):
    """红利资产估值不用 PEG 锁分，也不因 ROIC<成长股WACC 触发价值陷阱。"""
    engine = FundamentalEngine()

    def fake_build(symbol: str, years: int = 5):
        fd = _sample_financials().copy()
        fd["operating_profit"] = [4e8, 4.2e8, 4.5e8]
        fd["monetary_funds"] = [100e8, 120e8, 141e8]
        fd["short_term_borrowings"] = [10e8, 12e8, 13e8]
        return fd, {
            "name": "中国神华",
            "industry": "煤炭开采",
            "symbol": "601088.SH",
            "report_dates": list(fd.index),
            "annual_dates": list(fd.index),
            "revenue_yoy": 3.0,
            "profit_yoy": 1.0,
            "debt_ratio": 33.0,
            "latest_report": "20251231",
            "ocf_per_share": 2.0,
            "latest_roe": 12.0,
        }

    monkeypatch.setattr("app.analysis.engine.build_financial_dataframe", fake_build)
    monkeypatch.setattr("app.analysis.engine.industry_averages", lambda *a, **k: {})
    monkeypatch.setattr(
        "app.analysis.engine.get_valuations",
        lambda *a, **k: [
            {
                "symbol": "601088.SH",
                "name": "中国神华",
                "price": 47.0,
                "pe_ttm": 18.8,
                "pb": 2.2,
                "pe_percentile": 96.7,
                "pb_percentile": 80.0,
                "market_cap": 1e12,
                "dividend_yield": 6.5,
                "total_shares": 2e10,
            }
        ],
    )
    monkeypatch.setattr(
        "app.analysis.engine.calculate_comparable_valuation",
        lambda *a, **k: {
            "stock_code": "601088.SH",
            "comparables": [],
            "avg_pe": None,
            "avg_pb": None,
            "valuation_range": {},
            "peer_count": 0,
            "insufficient_sample": True,
        },
    )
    monkeypatch.setattr(
        "app.analysis.engine.detect_major_risk_events",
        lambda *_a, **_k: {"fatal": False, "events": [], "event_count": 0, "message": ""},
    )

    report = engine.run_full_analysis("601088.SH", db=None)
    val = report["valuation"]
    assert val.get("is_dividend_asset") is True
    assert val.get("value_trap_veto") is not True
    assert float(val.get("composite_valuation_score") or 0) > 28
    rel = val.get("relative") or {}
    assert "股息国债利差" in rel
    peg = rel.get("PEG") or {}
    assert peg.get("skipped_for_dividend_asset") is True or peg.get("signal") in (None, "—", "")
    assert "红利" in (val.get("valuation_rationale") or "") or "股息" in (val.get("valuation_rationale") or "")
    prof = report["modules"]["profitability"]
    assert prof["metadata"].get("roic_below_wacc") is False
    risk_warnings = report["modules"]["risk"]["warnings"]
    assert not any("存贷双高" in w for w in risk_warnings)


def test_classify_dividend_asset_soft_tier():
    from app.analysis.dividend_profile import classify_dividend_asset

    # 股息率 4.3% + 估分红率≈80% → soft 红利
    p = classify_dividend_asset(dividend_yield_pct=4.3, pe_ttm=18.8)
    assert p["is_dividend_asset"] is True
    assert p["tier"] == "soft"
    assert p["wacc_pct"] == 4.5


def test_high_growth_quality_wacc_and_roic_exemption():
    """毛利率>40% + 净利增速>50%：WACC→7%，不作毁灭价值否决。"""
    from app.analysis.growth_quality import classify_high_growth_quality

    hq = classify_high_growth_quality(gross_margin_pct=48.8, profit_yoy_pct=91.0)
    assert hq["is_high_growth_quality"] is True
    assert hq["wacc_pct"] == 7.0

    fd = _sample_financials().copy()
    fd["cogs"] = [40e8, 45e8, 48e8]  # 毛利率约 50%/50%/52%
    fd["operating_profit"] = [10e8, 12e8, 14e8]
    fd["equity"] = [40e8, 45e8, 50e8]
    fd["interest_bearing_debt"] = [5e8, 5e8, 5e8]
    fd["monetary_funds"] = [8e8, 9e8, 10e8]
    result = ProfitabilityAnalyzer().analyze(
        fd,
        debt_ratio=35.0,
        latest_roe=22.0,
        profit_yoy=91.0,
        latest_gross_margin=48.8,
    )
    assert result.metadata.get("is_high_growth_quality") is True
    assert abs(float(result.metadata.get("wacc_pct") or 0) - 7.0) < 1e-6
    assert result.metadata.get("roic_below_wacc") is False
    roic = next(i for i in result.indicators if i.name == "ROIC(%)")
    assert float(roic.value) >= 7.0
    assert "毁灭" not in (roic.comment or "")


def test_growth_business_transform_bonus():
    """高毛利+利润高增 → 业务结构转型加分。"""
    fd = _sample_financials().copy()
    fd["cogs"] = [40e8, 45e8, 48e8]
    fd["gross_margin"] = [50.0, 50.0, 52.0]
    result = GrowthAnalyzer().analyze(
        fd,
        revenue_yoy=40.0,
        profit_yoy=91.0,
        latest_report="20260630",
        latest_gross_margin=48.8,
        annual_dates=list(fd.index),
    )
    names = [i.name for i in result.indicators]
    assert "业务结构转型" in names
    assert result.metadata.get("business_transform") is True


def test_roic_uses_equity_plus_ibd_minus_cash():
    """投入资本=权益+有息债−现金，避免总资产−流动负债压低 ROIC。"""
    rows = [
        {
            "revenue": 100e8, "net_profit": 12e8, "equity": 50e8,
            "operating_cashflow": 13e8, "operating_profit": 20e8,
            "total_assets": 120e8, "current_liabilities": 60e8,
            "interest_bearing_debt": 10e8, "monetary_funds": 20e8,
            "roe": 22.0, "cogs": 50e8,
        },
        {
            "revenue": 120e8, "net_profit": 15e8, "equity": 55e8,
            "operating_cashflow": 16e8, "operating_profit": 24e8,
            "total_assets": 130e8, "current_liabilities": 65e8,
            "interest_bearing_debt": 10e8, "monetary_funds": 22e8,
            "roe": 24.0, "cogs": 55e8,
        },
    ]
    fd = pd.DataFrame(rows, index=["20241231", "20251231"])
    result = ProfitabilityAnalyzer().analyze(fd, debt_ratio=40.0, latest_roe=24.0)
    roic = next(i for i in result.indicators if i.name == "ROIC(%)")
    # NOPAT=24e8*0.75=18e8；IC=55+10-22=43e8 → ROIC≈41.9%
    assert float(roic.value) > 30
    assert "毁灭" not in (roic.comment or "")


def test_dcf_high_growth_marked_unreliable_when_far_below_price():
    eng = FundamentalEngine()
    fd = _sample_financials().copy()
    fd["operating_cashflow"] = [9e8, 11e8, 13e8]
    fd["capital_expenditure"] = [1e8, 1.2e8, 1.5e8]
    dcf = eng._build_dcf(
        fd,
        {"price": 220.0, "pe_ttm": 130.0, "dividend_yield": 0.1, "total_shares": 3e8},
        {
            "symbol": "300548.SZ",
            "profit_yoy": 91.0,
            "debt_ratio": 30.0,
            "latest_gross_margin": 48.8,
        },
    )
    assert dcf.get("high_growth_quality") is True
    assert abs(float(dcf["assumptions"]["wacc"]) - 0.07) < 1e-9
    if dcf.get("margin_of_safety_pct") is not None and float(dcf["margin_of_safety_pct"]) < -50:
        assert dcf.get("is_reliable") is False
        assert "极端悲观" in (dcf.get("note") or "") or "勿" in (dcf.get("note") or "")


def test_marginal_recovery_growth_bonus():
    """年报 CAGR 为负 + 最新同比企稳 → 业绩边际改善加分，不提示持续下滑。"""
    rows = [
        {"revenue": 120e8, "net_profit": 30e8, "equity": 80e8, "operating_cashflow": 35e8},
        {"revenue": 110e8, "net_profit": 26e8, "equity": 82e8, "operating_cashflow": 30e8},
        {"revenue": 100e8, "net_profit": 22e8, "equity": 85e8, "operating_cashflow": 28e8},
        {"revenue": 95e8, "net_profit": 20e8, "equity": 88e8, "operating_cashflow": 26e8},
    ]
    fd = pd.DataFrame(rows, index=["20221231", "20231231", "20241231", "20251231"])
    result = GrowthAnalyzer().analyze(
        fd,
        revenue_yoy=7.93,
        profit_yoy=4.10,
        latest_report="20260630",
        annual_dates=list(fd.index),
    )
    assert result.metadata.get("marginal_recovery") is True
    assert any(i.name == "业绩边际改善" for i in result.indicators)
    assert not any("持续下滑" in w for w in result.warnings)
    assert any("边际改善" in w or "企稳" in w for w in result.warnings)
    assert result.score >= 70


def test_solvency_supply_chain_power_for_cash_cow():
    from app.analysis.modules.solvency import SolvencyAnalyzer

    result = SolvencyAnalyzer().analyze(
        pd.DataFrame(),
        debt_ratio=33.0,
        interest_bearing_ratio=9.5,
        current_ratio=1.43,
        quick_ratio=1.30,
        industry="煤炭开采",
        balance_sheet={
            "report_date": "20251231",
            "total_assets": 9000e8,
            "operating_liabilities": 1000e8,
            "short_term_borrowings": 130e8,
            "interest_bearing_debt": 860e8,
            "interest_bearing_ratio": 9.5,
            "current_ratio": 1.43,
            "quick_ratio": 1.30,
        },
    )
    assert result.metadata.get("supply_chain_power") is True
    assert any(i.name == "产业链话语权" for i in result.indicators)
    assert result.score >= 78


def test_dividend_asset_dcf_suppressed():
    eng = FundamentalEngine()
    fd = _sample_financials().copy()
    dcf = eng._build_dcf(
        fd,
        {
            "price": 47.0,
            "pe_ttm": 12.0,
            "dividend_yield": 6.5,
            "total_shares": 2e10,
        },
        {"symbol": "601088.SH", "profit_yoy": 4.0, "debt_ratio": 33.0},
    )
    assert dcf.get("suppressed") is True
    assert dcf.get("intrinsic_value_per_share") is None
    assert dcf.get("margin_of_safety_pct") is None
    assert "红利" in (dcf.get("note") or "")


def test_warning_dedupe_across_modules():
    from app.analysis.engine import FundamentalEngine

    # 直接测聚合去重逻辑：模拟两模块同文案
    warnings = ["应收账款周转恶化，回款极其困难", "资金充裕", "应收账款周转恶化，回款极其困难"]
    seen: set[str] = set()
    out = []
    for w in warnings:
        if w not in seen:
            seen.add(w)
            out.append(w)
    assert out == ["应收账款周转恶化，回款极其困难", "资金充裕"]
    assert FundamentalEngine  # 模块可导入


def test_ar_warning_softened_for_soe_cash_cow():
    """神华类：应收上升不得写成「回款极其困难」。"""
    rows = [
        {
            "revenue": 3000e8, "net_profit": 600e8, "equity": 5500e8,
            "operating_cashflow": 1100e8, "monetary_funds": 1410e8,
            "short_term_borrowings": 131e8, "accounts_receivable": 100e8, "goodwill": 0,
        },
        {
            "revenue": 3200e8, "net_profit": 620e8, "equity": 6000e8,
            "operating_cashflow": 1160e8, "monetary_funds": 1410e8,
            "short_term_borrowings": 131e8, "accounts_receivable": 160e8, "goodwill": 0,
        },
    ]
    fd = pd.DataFrame(rows, index=["20241231", "20251231"])
    result = RiskAnalyzer().analyze(
        fd,
        name="中国神华",
        industry="煤炭开采",
        debt_ratio=33.0,
        interest_bearing_debt=860e8,
        dividend_yield=4.5,
        pe_ttm=18.0,
        latest_cash_ratio=1.88,
    )
    assert not any("回款极其困难" in w for w in result.warnings)
    assert any("结算周期" in w or "周转效率" in w or "回款节奏" in w for w in result.warnings)


def test_dividend_valuation_skips_pe_pb_percentile_drag():
    """红利资产估值不以 PE/PB 高分位拉低均分。"""
    eng = FundamentalEngine()
    fd = _sample_financials().copy()
    val = eng._run_valuation(
        fd,
        {
            "price": 47.0,
            "pe_ttm": 18.8,
            "pb": 2.2,
            "pe_percentile": 96.3,
            "pb_percentile": 98.3,
            "dividend_yield": 4.5,
            "market_cap": 1e12,
            "total_shares": 2e10,
            "symbol": "601088.SH",
        },
        {
            "symbol": "601088.SH",
            "profit_yoy": 4.0,
            "debt_ratio": 33.0,
            "name": "中国神华",
            "industry": "煤炭开采",
        },
        cashflow_score=85.0,
    )
    assert val.get("is_dividend_asset") is True
    assert float(val.get("composite_valuation_score") or 0) >= 65
    assert "历史分位" in (val.get("valuation_rationale") or "") or "股息" in (
        val.get("valuation_rationale") or ""
    )
    rel = val.get("relative") or {}
    assert rel.get("PE_TTM", {}).get("signal") != "高估" or rel.get("PE_TTM", {}).get(
        "dividend_percentile_softened"
    )


def test_marginal_recovery_cycle_language():
    rows = [
        {"revenue": 120e8, "net_profit": 30e8, "equity": 80e8, "operating_cashflow": 35e8},
        {"revenue": 110e8, "net_profit": 26e8, "equity": 82e8, "operating_cashflow": 30e8},
        {"revenue": 100e8, "net_profit": 22e8, "equity": 85e8, "operating_cashflow": 28e8},
        {"revenue": 95e8, "net_profit": 20e8, "equity": 88e8, "operating_cashflow": 26e8},
    ]
    fd = pd.DataFrame(rows, index=["20221231", "20231231", "20241231", "20251231"])
    result = GrowthAnalyzer().analyze(
        fd,
        revenue_yoy=7.93,
        profit_yoy=4.10,
        latest_report="20260630",
        annual_dates=list(fd.index),
        name="中国神华",
        industry="煤炭开采",
        deducted_net_profit=22e8,
        parent_net_profit=20e8,
    )
    ind = next(i for i in result.indicators if i.name == "业绩边际改善")
    assert "边际修复" in (ind.comment or "")
    assert "衰退" not in (ind.comment or "")


# ══════════════════════════════════════════════════════════════════
# 商络电子（300975）复盘：2026-09 基本面报告关键错误回归
# ══════════════════════════════════════════════════════════════════


def test_normalize_profit_yoy_keeps_truth_and_derives_forward():
    """真实同比保留（493.25 不再被截断成 300），前瞻口径单独折减。"""
    from app.analysis.financials import normalize_profit_yoy

    m = normalize_profit_yoy(493.25)
    assert m["profit_yoy"] == 493.25  # 展示值不再失真
    assert m["profit_yoy_raw"] == 493.25
    assert m["profit_yoy_extreme"] is True
    assert m["profit_yoy_forward"] == 300.0  # 仅外推口径折减

    m2 = normalize_profit_yoy(28.5)
    assert m2["profit_yoy"] == 28.5
    assert m2["profit_yoy_extreme"] is False
    assert m2["profit_yoy_forward"] == 28.5

    m3 = normalize_profit_yoy(None)
    assert m3["profit_yoy"] is None and m3["profit_yoy_forward"] is None


def test_cashflow_fcf_negative_unit_not_blown_up():
    """FCF 为负时不得跳过 /1e8 换算（曾显示成 -1119194300亿）。"""
    result = CashflowAnalyzer().analyze(
        _sample_financials(),
        dividend_info={
            "fcf": -1119194300.0,
            "cash_total": 0.0,
            "fcf_dividend_gap": -1119194300.0,
        },
    )
    cover = next(i for i in result.indicators if i.name == "FCF覆盖分红")
    assert "1119194300" not in cover.comment
    assert "-11亿" in cover.comment


def test_cashflow_small_amount_keeps_one_decimal():
    """小额金额不得被 %.0f 舍成 0亿（0.45亿分红曾显示成「现金分红 0亿」）。"""
    result = CashflowAnalyzer().analyze(
        _sample_financials(),
        dividend_info={
            "fcf": -1119194300.0,
            "cash_total": 45342370.0,
            "fcf_dividend_gap": -1164536670.0,
        },
    )
    cover = next(i for i in result.indicators if i.name == "FCF覆盖分红")
    assert "现金分红 0亿" not in cover.comment
    assert "现金分红 0.5亿" in cover.comment


def test_growth_extreme_yoy_shown_truthfully_with_warning():
    """中报 493.25% 必须原样展示，同时给出「不可线性外推」提示。"""
    fd = _sample_financials()
    result = GrowthAnalyzer().analyze(
        fd,
        revenue_yoy=45.0,
        profit_yoy=493.25,
        latest_report="20260630",
        annual_dates=list(fd.index),
    )
    fwd = next(i for i in result.indicators if i.name == "前瞻成长性(%)")
    assert abs(float(fwd.value) - 493.25) < 1e-6
    assert "极端" in (fwd.comment or "") or "300%" in (fwd.comment or "")
    assert any("极端值" in w for w in result.warnings)


def test_merger_consolidation_factor_and_warning():
    """并表增长需单独标注/折减，不能直接当有机增长。"""
    fd = _sample_financials()
    result = GrowthAnalyzer().analyze(
        fd,
        name="商络电子",
        symbol="300975",
        industry="电子元件",
        revenue_yoy=45.0,
        profit_yoy=493.25,
        latest_report="20260630",
        annual_dates=list(fd.index),
    )
    ind = next(i for i in result.indicators if i.name == "并购并表调整因子")
    assert "立功电子" in (ind.comment or "")
    assert ind.score <= 72  # 未拆分并表贡献 → 按中性计分，不给高成长满分
    assert any("并表" in w for w in result.warnings)


def test_distribution_cashflow_thresholds_relaxed():
    """分销商扩张期经营现金流为负 → 不应按制造业口径打成 E 级。"""
    rows = [
        {"revenue": 60e8, "net_profit": 1.2e8, "equity": 20e8, "operating_cashflow": -3e8,
         "capital_expenditure": 0.2e8, "accounts_receivable": 15e8, "inventory": 12e8},
        {"revenue": 85e8, "net_profit": 2.0e8, "equity": 22e8, "operating_cashflow": -9e8,
         "capital_expenditure": 0.3e8, "accounts_receivable": 24e8, "inventory": 18e8},
    ]
    fd = pd.DataFrame(rows, index=["20241231", "20251231"])
    result = CashflowAnalyzer().analyze(
        fd,
        name="商络电子",
        symbol="300975",
        industry="电子元件",
        revenue_yoy=45.0,
        profit_yoy=493.25,
        latest_report="20260630",
        latest_cash_ratio=-2.88,
        annual_dates=list(fd.index),
        ar_metrics={
            "period": "2025年报",
            "days_latest": 88.0,
            "days_5y_ago": 43.0,
            "days_delta_5y": 45.0,
        },
    )
    assert result.metadata.get("business_model") == "distribution"
    assert result.metadata.get("distribution_expanding") is True
    assert any(i.name == "分销模式观察项" for i in result.indicators)
    assert result.score >= 52  # 制造业口径会落到 30 分档
    div = next(i for i in result.indicators if i.name == "增长质量背离度")
    assert div.score >= 45
    assert "分销" in (div.comment or "")
    assert any("应收" in w for w in result.warnings)


def test_distribution_roic_below_wacc_not_vetoed():
    """分销商 ROIC<WACC 但 ROE 健康 → 不触发价值毁灭一票否决；WACC 不按含应付负债率抬高。"""
    fd = _sample_financials().copy()
    fd["operating_profit"] = [2e8, 2.1e8, 2.2e8]
    fd["equity"] = [50e8, 50e8, 50e8]
    fd["monetary_funds"] = [7e8, 7e8, 7e8]
    fd["interest_bearing_debt"] = [1e8, 1e8, 1e8]
    result = ProfitabilityAnalyzer().analyze(
        fd,
        name="商络电子",
        symbol="300975",
        industry="电子元件",
        debt_ratio=75.76,
        latest_roe=17.76,
    )
    assert result.metadata.get("business_model") == "distribution"
    assert result.metadata.get("roic_below_wacc") is False
    assert float(result.metadata.get("wacc_pct") or 0) <= 10.0
    assert result.metadata.get("roic_3y_avg") is not None  # 归一化 ROIC 生效
    roic = next(i for i in result.indicators if i.name == "ROIC(%)")
    assert "分销" in (roic.comment or "")
    # 应明确表述为「不构成价值毁灭」，而非旧版「可能在毁灭股东价值」的负面判定。
    # 注意不能粗暴地断言 "毁灭" not in —— 否定句本身含该词。
    assert "不构成价值毁灭" in (roic.comment or "")
    assert "可能在毁灭股东价值" not in (roic.comment or "")


def test_wacc_prefers_interest_bearing_ratio():
    """有息负债率优先于含应付的资产负债率：分销类高应付不应抬到 12%。"""
    from app.analysis.modules.profitability import _estimate_wacc_pct

    assert _estimate_wacc_pct(75.76, business_model="distribution") == 10.0
    assert _estimate_wacc_pct(75.76, interest_bearing_ratio=12.0) == 7.0
    assert _estimate_wacc_pct(75.76, interest_bearing_ratio=65.0) == 12.0


def test_peg_growth_fallback_uses_forward_yoy():
    """年报序列不足时 PEG 回退同比，须用折减口径，不得让 493% 把 PEG 压到 0.05。"""
    import pandas as pd

    meta = {
        "profit_yoy": 493.25,
        "profit_yoy_forward": 300.0,
        "profit_yoy_extreme": True,
    }
    # 年报序列不足 2 期 → 走回退分支
    fd = pd.DataFrame({"net_profit": [2e8]}, index=["20251231"])
    g, src = FundamentalEngine._growth_for_peg(fd, meta)
    assert g == 300.0
    assert "前瞻口径" in src

    # 非极端情形不受影响，来源文案保持简洁
    g2, src2 = FundamentalEngine._growth_for_peg(fd, {"profit_yoy": 28.5, "profit_yoy_forward": 28.5})
    assert g2 == 28.5
    assert src2 == "最新净利同比"


def test_comps_uses_explicit_distribution_peers(monkeypatch):
    """可比公司改为分销同业，不再拿被动元件制造商锚定分销商估值。"""
    from app.analysis.models import comps as comps_mod
    from app.analysis.config.company_profiles import peers_for

    peers = peers_for("商络电子", "300975", "电子元件")
    assert "300184" in peers and "000062" in peers and "300975" not in peers

    quotes = {
        s: {
            "symbol": s,
            "name": f"分销{s}",
            "pe_ttm": 20 + i,
            "pb": 1.5 + i * 0.1,
            "market_cap": 1.0e10 + i * 1e9,
            "price": 10.0 + i,
        }
        for i, s in enumerate(peers)
    }
    monkeypatch.setattr(
        comps_mod,
        "_batch_quotes",
        lambda symbols, db=None: {s: quotes[s] for s in symbols if s in quotes},
    )
    sel = comps_mod.select_comparables(
        "300975",
        industry="电子元件",
        report_date="20251231",
        target_market_cap=1.0e10,
        peer_symbols=peers,
        limit=8,
    )
    assert sel
    got = {c["symbol"] for c in sel}
    assert got <= set(peers)
    assert "300975" not in got


# ══════════════════════════════════════════════════════════════════
# 深圳华强（000062）复盘：14.4 分 E 级的成因与修复回归
# 关键结论：14.4 的成因不是单一「现金流一票否决」，而是
#   cashflow 权重 2 倍 × E 档 0.5 打折 × 风险乘数 0.44 的复合叠加。
# ══════════════════════════════════════════════════════════════════


def test_distribution_recognized_from_peer_list_by_symbol():
    """分销同业名单内的公司即认定为分销模式，且各种代码写法都能归一化。"""
    from app.analysis.config.company_profiles import business_model_of, is_distribution

    for code in ("000062", "000062.SZ", "SZ000062", "sh000062"):
        assert is_distribution("", code, "其他电子Ⅱ") is True, code
    # 非分销样本不应被误判（东财行业同为泛行业的白酒股）
    assert is_distribution("贵州茅台", "600519.SH", "酿酒行业") is False
    assert business_model_of("", "000062.SZ", "其他电子Ⅱ") == "distribution"


def test_peers_for_distributor_excludes_self_and_is_stable():
    """分销同业清单必须排除目标自身，避免拿自己和比自己。"""
    from app.analysis.config.company_profiles import peers_for

    for sym in ("000062", "000062.SZ", "300975.SZ"):
        peers = peers_for("", sym, "其他电子Ⅱ")
        assert peers, sym
        code = sym[:6]
        assert code not in peers
        assert all(len(p) == 6 and p.isdigit() for p in peers)


def test_cashflow_indicator_name_value_score_agree():
    """指标名/展示值/得分必须同口径：不得出现「(5年均值)」标签配当期值和当期得分。"""
    rows = [
        {"revenue": 60e8, "net_profit": 1.2e8, "equity": 20e8, "operating_cashflow": 1.4e8,
         "capital_expenditure": 0.2e8, "accounts_receivable": 8e8, "inventory": 5e8,
         "total_assets": 60e8, "current_liabilities": 20e8, "monetary_funds": 4e8,
         "short_term_borrowings": 3e8, "cogs": 55e8, "operating_profit": 2e8, "roe": 6.0},
        {"revenue": 80e8, "net_profit": 1.5e8, "equity": 21e8, "operating_cashflow": 1.6e8,
         "capital_expenditure": 0.3e8, "accounts_receivable": 10e8, "inventory": 6e8,
         "total_assets": 70e8, "current_liabilities": 24e8, "monetary_funds": 5e8,
         "short_term_borrowings": 4e8, "cogs": 74e8, "operating_profit": 2.4e8, "roe": 7.0},
        {"revenue": 100e8, "net_profit": 1.8e8, "equity": 22e8, "operating_cashflow": 1.9e8,
         "capital_expenditure": 0.4e8, "accounts_receivable": 12e8, "inventory": 7e8,
         "total_assets": 80e8, "current_liabilities": 28e8, "monetary_funds": 6e8,
         "short_term_borrowings": 5e8, "cogs": 92.3e8, "operating_profit": 2.8e8, "roe": 8.0},
    ]
    fd = pd.DataFrame(rows, index=["20231231", "20241231", "20251231"])
    # 当期（2026中报）严重为负，年报序列健康 → 触发「当期口径」分支
    result = CashflowAnalyzer().analyze(
        fd,
        name="深圳华强",
        symbol="000062",
        industry="其他电子Ⅱ",
        revenue_yoy=60.6,
        profit_yoy=66.13,
        latest_report="20260630",
        latest_cash_ratio=-2.04,
        annual_dates=list(fd.index),
    )
    ind = next(i for i in result.indicators if "经营现金流/净利" in i.name)
    # 不变式：标签声明的口径必须与展示值一致（二者不得互相矛盾）
    if "5年均值" in ind.name:
        assert abs(float(ind.value) - 1.1) < 0.05, f"标签称5年均值但值为{ind.value}"
    else:
        assert ind.name.endswith("(当期)")
        assert abs(float(ind.value) - (-2.04)) < 1e-6, f"标签称当期但值为{ind.value}"
    assert "当期" in (ind.comment or "")


def test_distribution_gross_margin_uses_own_thresholds():
    """分销商毛利率 7.7% 属正常区间，不得按制造业 (50/30/15) 阈值打成低分。"""
    rows = [
        {"revenue": 100e8, "net_profit": 2e8, "equity": 40e8, "operating_cashflow": 3e8,
         "capital_expenditure": 0.5e8, "cogs": 92.3e8, "roe": 5.0, "operating_profit": 3e8,
         "total_assets": 90e8, "current_liabilities": 40e8, "monetary_funds": 8e8,
         "short_term_borrowings": 6e8, "accounts_receivable": 20e8, "inventory": 15e8,
         "interest_bearing_debt": 20e8},
        {"revenue": 120e8, "net_profit": 2.5e8, "equity": 42e8, "operating_cashflow": 3.5e8,
         "capital_expenditure": 0.6e8, "cogs": 110.8e8, "roe": 6.0, "operating_profit": 3.5e8,
         "total_assets": 100e8, "current_liabilities": 45e8, "monetary_funds": 9e8,
         "short_term_borrowings": 7e8, "accounts_receivable": 22e8, "inventory": 16e8,
         "interest_bearing_debt": 22e8},
    ]
    fd = pd.DataFrame(rows, index=["20241231", "20251231"])
    dist = ProfitabilityAnalyzer().analyze(
        fd, name="", symbol="000062", industry="其他电子Ⅱ", latest_roe=6.66,
    )
    mfg = ProfitabilityAnalyzer().analyze(
        fd, name="", symbol="600519", industry="酿酒行业", latest_roe=6.66,
    )
    gm_d = next(i for i in dist.indicators if i.name == "毛利率(%)")
    gm_m = next(i for i in mfg.indicators if i.name == "毛利率(%)")
    assert gm_d.value == gm_m.value  # 同一份数据
    assert gm_d.score > gm_m.score  # 但分销口径更宽容
    assert "分销" in (gm_d.comment or "")


def test_risk_penalty_is_bounded_not_multiplicative():
    """风险扣分必须限幅：原实现 composite *= risk/100 会把 32.8 砍到 14.4。"""
    from app.analysis.config import RISK_MAX_PENALTY, RISK_THRESHOLD

    assert RISK_MAX_PENALTY <= 0.30, "风险扣分上限不应超过 30%"

    # 复算：每个模块都给 50 分、风险 44 分时的综合分
    scores = {"profitability": 50.0, "growth": 50.0, "solvency": 50.0, "cashflow": 50.0}
    weights = {"profitability": 0.20, "growth": 0.16, "solvency": 0.16, "cashflow": 0.32}
    base = sum(scores[k] * weights[k] for k in weights) / sum(weights.values())
    risk = 44.0
    gap = (RISK_THRESHOLD - risk) / RISK_THRESHOLD
    bounded = base * (1.0 - RISK_MAX_PENALTY * gap)
    old = base * (risk / 100.0)
    assert bounded > old
    assert bounded >= base * (1.0 - RISK_MAX_PENALTY) - 1e-9


def test_engine_ctx_passes_symbol_to_modules():
    """引擎必须把 symbol 传给各模块，否则基于代码的画像/商业模式/同业匹配全部失效。"""
    import inspect

    from app.analysis import engine as eng

    src = inspect.getsource(eng.FundamentalEngine.run_full_analysis)
    assert '"symbol": sym' in src, "ctx 缺少 symbol，模块将无法按代码匹配画像"


# ══════════════════════════════════════════════════════════════════
# 同业相对基准（分销/贸易）回归
#
# 绝对阈值是按制造业设定的，套到分销商身上会把整个行业判成不合格：
# 实测 7 家 A 股电子元器件分销商 2026 中报，ROIC 三年均值全部落在
# 1.5%~3.8%，无一达到 WACC≈10%；经营现金流/净利 7 家中 6 家为负。
# 故分销模式改用同业中位数做锚，同时必须保留行业内的区分度。
# ══════════════════════════════════════════════════════════════════


def test_benchmark_distribution_reaches_neutral_at_industry_median():
    """达到行业中位应得中性分，而非按制造业阈值判不合格。"""
    from app.analysis.config.company_profiles import distribution_benchmarks
    from app.analysis.modules.profitability import _score_vs_benchmark

    b = distribution_benchmarks()
    mid, _ = _score_vs_benchmark(b["net_margin_pct"], b["net_margin_pct"])
    assert 55.0 <= mid <= 60.0, mid
    # 深圳华强实测：ROE 6.66 / ROIC3年均 3.78 / 净利率 1.86，均不低于同业中位
    assert _score_vs_benchmark(6.66, b["roe_pct"])[0] >= 55.0
    assert _score_vs_benchmark(3.78, b["roic_3y_pct"])[0] >= 55.0
    assert _score_vs_benchmark(1.86, b["net_margin_pct"])[0] >= 55.0


def test_benchmark_keeps_within_industry_discrimination():
    """相对评分不得把全行业一起豁免：最差者仍应判低分，优秀者高分。"""
    from app.analysis.config.company_profiles import distribution_benchmarks
    from app.analysis.modules.profitability import _score_vs_benchmark

    b = distribution_benchmarks()
    worst, _ = _score_vs_benchmark(1.50, b["roic_3y_pct"])  # 中电港，同业最差
    best, _ = _score_vs_benchmark(13.20, b["roe_pct"])  # 商络电子，同业最优
    assert worst <= 45.0, worst
    assert best >= 75.0, best
    assert best > worst


def test_distribution_ratios_use_benchmark_not_manufacturing():
    """分销模式下 ROE/ROIC/净利率 应走同业基准，不套用制造业绝对阈值。"""
    from app.analysis.modules.profitability import ProfitabilityAnalyzer
    import pandas as pd

    # 分销商的典型弱值：净利率 1.86% 在制造业口径下必然不及格
    fd = pd.DataFrame(
        {
            "revenue": [1.0e10, 1.2e10, 1.4e10],
            "net_profit": [2.0e8, 2.3e8, 2.6e8],
            "equity": [4.0e9, 4.1e9, 4.2e9],
            "total_assets": [1.0e10, 1.05e10, 1.1e10],
            "operating_profit": [3.0e8, 3.3e8, 3.6e8],
            "operating_cashflow": [3.0e8, 3.5e8, 3.8e8],
            "interest_bearing_debt": [1e8, 1e8, 1e8],
            "roe": [5.0, 5.6, 6.2],
        },
        index=["20241231", "20251231", "20260630"],
    )
    res = ProfitabilityAnalyzer().analyze(
        fd,
        name="深圳华强",
        symbol="000062",
        industry="其他电子Ⅱ",
        debt_ratio=61.42,
        latest_roe=6.66,
    )
    assert res.metadata.get("business_model") == "distribution"
    # 分销模式的三个相对指标都应带上同业口径说明
    for name in ("ROE(%)", "ROIC(%)", "净利率(%)"):
        ind = next(i for i in res.indicators if i.name == name)
        assert "同业" in (ind.comment or ""), name
    nm = next(i for i in res.indicators if i.name == "净利率(%)")
    assert nm.score >= 50.0, f"净利率 1.86% 相对同业中位不应不及格：{nm.score}"


def test_solvency_debt_ratio_uses_distribution_benchmark():
    """61.42% 是分销同业中位，不应按轻资产（电子）严阈值判成偿债压力大。"""
    from app.analysis.config.company_profiles import distribution_benchmarks
    from app.analysis.modules.solvency import SolvencyAnalyzer
    import pandas as pd

    b = distribution_benchmarks()
    fd = pd.DataFrame({"total_assets": [1e10], "equity": [3.8e9]}, index=["20260630"])
    res = SolvencyAnalyzer().analyze(
        fd,
        name="深圳华强",
        symbol="000062",
        industry="其他电子Ⅱ",
        debt_ratio=b["debt_ratio_pct"],
    )
    dr = next(i for i in res.indicators if i.name == "资产负债率(%)")
    assert dr.score >= 55.0, dr.score
    assert "同业中位" in (dr.comment or "")

    # 同行业但负债显著高于中位者仍应被扣分（保留区分度）
    res2 = SolvencyAnalyzer().analyze(
        fd,
        name="中电港",
        symbol="001287",
        industry="其他电子Ⅱ",
        debt_ratio=88.65,  # 同业最高
    )
    dr2 = next(i for i in res2.indicators if i.name == "资产负债率(%)")
    assert dr2.score < dr.score, (dr2.score, dr.score)


def test_comps_matches_quotes_with_exchange_suffix(monkeypatch):
    """真实场景：清单写 6 位代码，行情返回带交易所后缀的 symbol，必须命中。

    曾因两者格式不一致导致 quotes.get() 全部落空，可比公司静默变成 0 家。
    """
    from app.analysis.models import comps as comps_mod

    peers = ["001287", "001298", "300184"]
    quotes = {
        f"{s}.SZ": {
            "symbol": f"{s}.SZ",
            "name": f"分销{s}",
            "pe_ttm": 30.0,
            "pb": 3.0,
            "market_cap": 1.0e10,
            "price": 10.0,
        }
        for s in peers
    }

    def _fake_batch(symbols, db=None):
        # 模拟真实 _batch_quotes：接受任意写法输入，返回带后缀的 key
        out = {}
        for s in symbols:
            digits = "".join(ch for ch in str(s) if ch.isdigit())
            key = f"{digits}.SZ"
            if key in quotes:
                out[key] = quotes[key]
        return out

    monkeypatch.setattr(comps_mod, "_batch_quotes", _fake_batch)
    sel = comps_mod.select_comparables(
        "000062.SZ",
        industry="其他电子Ⅱ",
        report_date="20260630",
        target_market_cap=1.0e10,
        peer_symbols=peers,
        limit=5,
    )
    assert sel, "6 位清单 + 带后缀行情 key 下应能命中同业，而不是 0 家"
    assert {c["symbol"] for c in sel} <= set(peers)
    assert "000062.SZ" not in {c["symbol"] for c in sel}


def test_cashflow_handles_loss_making_company_without_crash():
    """净利为负时必须能正常出分。

    is_anomaly 原先只在「净利为正」分支初始化，但「年报对照」等处在分支外读取它，
    导致亏损股（实测万科A 000002）抛 NameError 并使整个基本面分析中断。
    """
    from app.analysis.modules.cashflow import CashflowAnalyzer
    import pandas as pd

    fd = pd.DataFrame(
        {
            "revenue": [1.0e9, 9.0e8, 8.0e8],
            "net_profit": [-1.0e8, -2.0e8, -3.0e8],
            "operating_cashflow": [-5.0e7, -8.0e7, -1.2e8],
            "capital_expenditure": [1.0e7, 1.0e7, 1.0e7],
        },
        index=["20231231", "20241231", "20251231"],
    )
    # 不应抛异常
    res = CashflowAnalyzer().analyze(
        fd, name="万科A", symbol="000002", industry="房地产开发", eps=-0.5
    )
    assert res.score is not None and res.score >= 0
    # 亏损股走失真提示分支，而不是按含金量扣分
    ind = next((i for i in res.indicators if "经营现金流/净利" in i.name), None)
    assert ind is not None
    assert "失真" in (ind.comment or "")


# ══════════════════════════════════════════════════════════════════
# 现金流口径一致性回归（深圳华强 000062）
#
# 1) 应收告警与「分销模式观察项」必须同口径：同一批应收数据不能在 cashflow
#    模块被判「账期基本稳定」、在 risk 模块被判「回款极其困难」。
# 2) 年报对照锚的措辞必须与事实一致：上年年报同样为负时，
#    不得写「勿被上年年报高含金量掩盖」。
# 3) 下限保护必须留痕：模块分停在 52.0 时，报告要能解释原始加权分是多少。
# ══════════════════════════════════════════════════════════════════


def _huachang_annual_rows():
    """复刻深圳华强 5 年年报 OCF/净利 比值序列（-1.05/2.39/-0.16/6.77/-2.14）。"""
    ratios = [-1.05, 2.39, -0.16, 6.77, -2.14]
    rows = [
        {
            "revenue": 220e8, "net_profit": 1e8, "equity": 50e8,
            "operating_cashflow": r * 1e8, "capital_expenditure": 0.1e8,
            "accounts_receivable": 50e8,
        }
        for r in ratios
    ]
    return pd.DataFrame(rows, index=["20211231", "20221231", "20231231", "20241231", "20251231"])


def test_ar_warning_softened_for_distribution_biz():
    """分销/贸易：应收随营收扩张放大属营运资本占用，不得定性「回款极其困难」。"""
    rows = [
        {"revenue": 100e8, "net_profit": 2e8, "equity": 40e8, "operating_cashflow": -5e8,
         "capital_expenditure": 1e8, "accounts_receivable": 30e8},
        {"revenue": 113e8, "net_profit": 3.3e8, "equity": 41e8, "operating_cashflow": -9e8,
         "capital_expenditure": 1e8, "accounts_receivable": 42e8},
    ]
    fd = pd.DataFrame(rows, index=["20241231", "20251231"])
    res = CashflowAnalyzer().analyze(
        fd, name="深圳华强", symbol="000062.SZ", industry="其他电子Ⅱ",
        revenue_yoy=60.7, profit_yoy=66.1,
    )
    assert not any("回款极其困难" in w for w in res.warnings)
    assert any("营运资本占用" in w for w in res.warnings)


def test_risk_ar_deduction_matches_distribution_context():
    """同一批应收数据在 risk 模块也不得按「回款危机」扣分。"""
    rows = [
        {"revenue": 100e8, "net_profit": 2e8, "equity": 40e8, "operating_cashflow": -5e8,
         "capital_expenditure": 1e8, "accounts_receivable": 30e8, "monetary_funds": 25e8,
         "goodwill": 0},
        {"revenue": 113e8, "net_profit": 3.3e8, "equity": 41e8, "operating_cashflow": -9e8,
         "capital_expenditure": 1e8, "accounts_receivable": 42e8, "monetary_funds": 29e8,
         "goodwill": 0},
    ]
    fd = pd.DataFrame(rows, index=["20241231", "20251231"])
    res = RiskAnalyzer().analyze(
        fd, name="深圳华强", symbol="000062.SZ", industry="其他电子Ⅱ",
        latest_cash_ratio=-2.04,
    )
    assert not any("回款极其困难" in w for w in res.warnings)
    assert any("营运资本占用" in w for w in res.warnings)


def test_cashflow_annual_anchor_text_matches_reality():
    """上年年报同样为负时，不得写「勿被上年年报高含金量掩盖」。"""
    res = CashflowAnalyzer().analyze(
        _huachang_annual_rows(),
        latest_cash_ratio=-2.04, latest_report="20260630", profit_yoy=66.1,
    )
    ind = next(i for i in res.indicators if i.name == "经营现金流/净利润(当期)")
    comment = ind.comment or ""
    assert "高含金量" not in comment, comment
    assert "同样不健康" in comment, comment


def test_cashflow_floor_exposes_raw_weighted_score():
    """下限保护必须留痕：模块分停在 52.0 时，报告要能解释原始加权分。"""
    res = CashflowAnalyzer().analyze(
        _huachang_annual_rows(),
        name="深圳华强", symbol="000062.SZ", industry="其他电子Ⅱ",
        revenue_yoy=60.7, profit_yoy=66.1,
        latest_cash_ratio=-2.04, latest_report="20260630",
    )
    assert res.metadata.get("floor_applied") == "distribution_expanding"
    raw = res.metadata.get("raw_weighted_score")
    assert raw is not None and raw < 52.0, raw
    assert res.score == 52.0
    assert any("下限保护" in w and f"{raw:.1f}" in w for w in res.warnings)



# ── 银行偿债口径 ──────────────────────────────────────────────────
# 银行负债以客户存款为主，资产负债率天然 90%+。实测 20 家 A 股银行 2026 中报
# 分布在 90.18%~93.95%（中位 91.94%），套用制造业「>75% 即危险」会把整个
# 银行业判成 E 级——招商银行 90.18% 恰是样本中杠杆最低的一家，却被打 25 分
# 并提示「偿债压力较大」，同时它的流动/速动比率还被数据源缺失导致的 0.0
# 打成 0 分。以下测试锁定修复后的行为。


def _bank_frame() -> pd.DataFrame:
    return pd.DataFrame({"total_assets": [1e13], "equity": [1e12]}, index=["20260630"])


def test_solvency_bank_uses_capital_buffer_not_manufacturing_threshold():
    from app.analysis.modules.solvency import SolvencyAnalyzer

    res = SolvencyAnalyzer().analyze(
        _bank_frame(),
        name="招商银行",
        symbol="600036.SH",
        industry="银行Ⅱ",
        debt_ratio=90.18,  # 20 家银行样本里杠杆最低
        interest_bearing_ratio=0.99,
        current_ratio=0.0,  # 数据源对银行未映射货币资金/应收/存货
        quick_ratio=0.0,
    )
    assert res.score >= 55.0, res.score
    dr = next(i for i in res.indicators if i.name == "资产负债率(%)")
    assert dr.score >= 55.0, dr.score
    assert "同业中位" in (dr.comment or "")
    # 不得再输出制造业口径结论
    assert not any("偿债压力较大" in w for w in res.warnings)
    # 流动/速动比率不适用于存款类机构；有息负债率口径不含吸收存款（反向失真）
    names = [i.name for i in res.indicators]
    assert "流动比率" not in names
    assert "速动比率" not in names
    assert "有息负债率(%)" not in names
    # 单看杠杆不足以判定银行安全性，必须显式声明资产质量不可评估
    assert res.metadata["bank_leverage_only"] is True
    assert res.metadata["asset_quality_metrics_available"] is False
    assert any("不良贷款率" in w for w in res.warnings)


def test_solvency_bank_ladder_keeps_discrimination():
    """资本缓冲更厚者得分更高，最差者仍被判低分，不能被一并豁免。"""
    from app.analysis.modules.solvency import SolvencyAnalyzer

    def _score(dr: float) -> float:
        r = SolvencyAnalyzer().analyze(
            _bank_frame(), name="某银行", symbol="600036.SH",
            industry="银行Ⅱ", debt_ratio=dr,
        )
        return next(i for i in r.indicators if i.name == "资产负债率(%)").score

    best, median, worst = _score(90.18), _score(91.94), _score(93.95)
    assert best > median > worst, (best, median, worst)
    assert median <= 60.0, median  # 达到同业中位 ≈ 中性，不应给高评价

    r = SolvencyAnalyzer().analyze(
        _bank_frame(), name="某银行", symbol="600036.SH",
        industry="银行Ⅱ", debt_ratio=93.95,
    )
    assert any("杠杆在业内偏高" in w for w in r.warnings)


def test_bank_model_excludes_securities_and_insurance():
    """证券/保险没有不良率与拨备覆盖率，套用银行口径会把正常评分压成常数。

    实测中信证券 63.1 C、中国平安 72.2 B，若被并入银行监管理论口径会双双
    掉到 ~52 分（五个子项全部取缺省值），是比现状更差的结果。
    """
    from app.analysis.config.company_profiles import business_model_of, is_bank
    from app.analysis.modules.solvency import SolvencyAnalyzer

    assert business_model_of("招商银行", "600036.SH", "银行Ⅱ") == "bank"
    for nm, sym, ind in (
        ("中信证券", "600030.SH", "证券Ⅱ"),
        ("中国平安", "601318.SH", "保险Ⅱ"),
        ("东方财富", "300059.SZ", "多元金融"),
    ):
        assert business_model_of(nm, sym, ind) != "bank", nm
        assert is_bank(nm, sym, ind) is False, nm

    res = SolvencyAnalyzer().analyze(
        pd.DataFrame({"total_assets": [1e12], "equity": [1.5e11]}, index=["20260630"]),
        name="中信证券", symbol="600030.SH", industry="证券Ⅱ",
        debt_ratio=85.55, current_ratio=1.8, quick_ratio=1.8,
    )
    names = [i.name for i in res.indicators]
    assert "流动比率" in names and "速动比率" in names
    assert res.metadata.get("bank_leverage_only") is False


def test_missing_liquidity_ratio_not_scored_as_zero():
    """分子为 0 属「数据源未映射」而非「流动性枯竭」，不得产生 0 分指标。"""
    from app.analysis.modules.solvency import SolvencyAnalyzer

    res = SolvencyAnalyzer().analyze(
        pd.DataFrame(),
        debt_ratio=45.0,
        industry="电子",
        balance_sheet={"report_date": "20251231", "current_ratio": 0.0, "quick_ratio": 0.0},
    )
    names = [i.name for i in res.indicators]
    assert "流动比率" not in names and "速动比率" not in names
    assert all(i.score > 0 for i in res.indicators), [i.name for i in res.indicators]
