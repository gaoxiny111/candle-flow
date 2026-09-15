"""Tests for relative valuation PEG transparency (图四)."""

from app.analysis.models.relative import RelativeValuation


def test_peg_shows_numeric_value_and_thresholds():
    rel = RelativeValuation().analyze(
        {
            "PE_TTM": 27.24,
            "profit_growth_rate": 10.37,
            "profit_growth_label": "3年净利CAGR",
        }
    )
    peg = rel["PEG"]
    assert peg["value"] == 2.63
    assert peg["signal"] == "偏贵"
    assert peg["growth_label"] == "3年净利CAGR"
    assert "<1" in peg["thresholds"]


def test_peg_undervalued_below_one():
    peg = RelativeValuation().analyze(
        {"PE_TTM": 12.0, "profit_growth_rate": 20.0, "profit_growth_label": "3年净利CAGR"}
    )["PEG"]
    assert peg["value"] == 0.6
    assert peg["signal"] == "低估"


def test_peg_reasonable_band():
    peg = RelativeValuation().analyze(
        {"PE_TTM": 15.0, "profit_growth_rate": 10.0, "profit_growth_label": "3年净利CAGR"}
    )["PEG"]
    assert peg["value"] == 1.5
    assert peg["signal"] == "合理"
