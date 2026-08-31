"""扣非优先 / 周期附加分 / 双态估值预警."""

from app.services.fundamental_screen import ScreenRow, ScreenThresholds, _fill_extras
from app.services.fundamental_tracks import (
    enrich_track_metrics,
    metrics_line,
    profit_yoy_eff,
    score_cyclical_extra,
    valuation_flag,
    verdict,
)


def test_profit_yoy_eff_prefers_deducted():
    row = ScreenRow(
        symbol="600000.SH",
        name="x",
        industry="银行",
        themes=[],
        report_date="20241231",
        profit_yoy=80.0,
        deducted_profit_yoy=22.0,
    )
    assert profit_yoy_eff(row) == 22.0
    row.deducted_profit_yoy = None
    assert profit_yoy_eff(row) == 80.0


def test_fill_extras_manual_override_only_when_missing():
    row = ScreenRow(
        symbol="900948.SH",
        name="伊泰B股",
        industry="",
        themes=[],
        report_date="20241231",
        dividend_yield=11.0,
    )
    _fill_extras(row, {}, "900948.SH")
    assert row.dividend_yield == 11.0  # 已有股息不覆盖
    # fill 仅补缺失；强制校准见 _calibrate_filing
    assert row.profit_yoy == 82.45  # MANUAL 补缺失
    assert row.deducted_profit_yoy == 26.89


def test_calibrate_overrides_wrong_period_profit():
    from app.services.fundamental_screen import _calibrate_filing

    row = ScreenRow(
        symbol="900948.SH",
        name="伊泰B股",
        industry="煤炭开采",
        themes=[],
        report_date="20260630",
        revenue_yoy=13.5,
        profit_yoy=-1.4,  # 错期
        deducted_profit_yoy=None,
    )
    notes = _calibrate_filing(row)
    assert abs(row.profit_yoy - 82.45) < 0.01
    assert abs(row.deducted_profit_yoy - 26.89) < 0.01
    assert any("校准" in n for n in notes)


def test_anomaly_sentinel_revenue_up_profit_down():
    from app.services.fundamental_screen import flag_data_anomaly

    row = ScreenRow(
        symbol="X.SH",
        name="x",
        industry="煤炭",
        themes=[],
        report_date="20260630",
        revenue_yoy=13.5,
        profit_yoy=-1.4,
    )
    assert flag_data_anomaly(row) is not None
    row.profit_yoy = 82.0
    assert flag_data_anomaly(row) is None


def test_cyclical_extra_and_valuation_flag():
    th = ScreenThresholds()
    row = ScreenRow(
        symbol="900948.SH",
        name="伊泰B股",
        industry="煤炭开采",
        themes=[],
        report_date="20260630",
        roe=10.0,
        roe_avg=12.0,
        revenue_yoy=13.5,
        profit_yoy=82.0,
        deducted_profit_yoy=22.0,
        ocf_ps=1.16,
        eps=1.52,
        debt_ratio=45.0,
        dividend_yield=9.5,
        pe_percentile=82.0,
        pb_percentile=100.0,
        no_consec_loss=True,
    )
    extra = score_cyclical_extra(row)
    # 股息满12 + 现金比≈0.76→5 + 扣非22%→4.4 ≈ 21.4
    assert 20 <= extra <= 24
    assert valuation_flag(row) == "高估+改善·不追高"

    enrich_track_metrics(
        row,
        th=th,
        hist_roe={},
        hist_profit={},
        dates=["20251231"],
    )
    assert row.track == "cyclical"
    assert row.passed_hard is True
    assert row.score >= 40
    label, tone = verdict(row)
    assert tone == "strong"
    assert "周期轨" in label
    line = metrics_line(row)
    assert "扣非" in line
    assert "股息" in line
    assert "现金比76%" in line or "现金比0.76" in line or "现金比76" in line
    assert "不追高" in line


def test_valuation_flag_worsening():
    row = ScreenRow(
        symbol="1",
        name="x",
        industry="煤炭",
        themes=[],
        report_date="20241231",
        revenue_yoy=-10.0,
        profit_yoy=-20.0,
        pb_percentile=95.0,
    )
    assert valuation_flag(row) == "高估+恶化·回避"


def test_one_off_gain_blocked_by_deducted():
    """表观高增、扣非不足 → 成长轨增速硬条件不过。"""
    th = ScreenThresholds()
    row = ScreenRow(
        symbol="688001.SH",
        name="假成长",
        industry="软件",
        themes=["AI"],
        report_date="20241231",
        roe=20.0,
        roe_years_ok=3,
        revenue_yoy=5.0,
        profit_yoy=80.0,
        deducted_profit_yoy=8.0,
        ocf_ps=1.0,
        debt_ratio=30.0,
    )
    enrich_track_metrics(
        row,
        th=th,
        hist_roe={
            "20241231": {"688001.SH": 20.0},
            "20231231": {"688001.SH": 18.0},
            "20221231": {"688001.SH": 16.0},
        },
        hist_profit={
            "20241231": {"688001.SH": 10.0},
            "20231231": {"688001.SH": 9.0},
            "20221231": {"688001.SH": 8.0},
        },
        dates=["20241231", "20231231", "20221231"],
    )
    assert row.track == "growth"
    growth = next(c for c in row.checks if c.key == "growth")
    assert growth.ok is False
    assert "扣非" in growth.detail
