"""Tests for multi-track fundamental rules."""

from app.services.fundamental_screen import ScreenRow, ScreenThresholds
from app.services.fundamental_tracks import (
    enrich_track_metrics,
    no_consec_loss,
    sector_classify,
    verdict,
)


def test_sector_classify_tracks():
    assert sector_classify("煤炭开采") == "cyclical"
    assert sector_classify("银行") == "value"
    assert sector_classify("半导体") == "growth"
    assert sector_classify("", name="伊泰B股") == "cyclical"


def test_no_consec_loss():
    ok, n, _ = no_consec_loss([10.0, -1.0, 5.0])
    assert ok is True
    assert n == 1
    ok2, n2, _ = no_consec_loss([-1.0, -2.0, 3.0])
    assert ok2 is False
    assert n2 == 2
    ok3, _, detail = no_consec_loss([None, None, None])
    assert ok3 is None
    assert "不足" in detail


def test_cyclical_verdict_uses_avg_roe_not_growth():
    th = ScreenThresholds()
    row = ScreenRow(
        symbol="900948.SH",
        name="伊泰B股",
        industry="煤炭开采",
        themes=[],
        report_date="20241231",
        roe=12.0,
        roe_years_ok=1,
        revenue_yoy=-5.0,
        profit_yoy=-10.0,
        ocf_ps=1.5,
        debt_ratio=45.0,
        pe_percentile=30.0,
        pb_percentile=25.0,
    )
    hist_roe = {
        "20241231": {"900948.SH": 12.0},
        "20231231": {"900948.SH": 11.0},
        "20221231": {"900948.SH": 10.0},
    }
    hist_profit = {
        "20241231": {"900948.SH": 100.0},
        "20231231": {"900948.SH": 80.0},
        "20221231": {"900948.SH": 60.0},
    }
    dates = ["20241231", "20231231", "20221231"]
    enrich_track_metrics(row, th=th, hist_roe=hist_roe, hist_profit=hist_profit, dates=dates)
    assert row.track == "cyclical"
    assert row.roe_avg is not None and row.roe_avg >= 10
    assert row.passed_hard is True
    label, tone = verdict(row)
    assert "周期轨" in label
    assert tone in ("strong", "mid")


def test_growth_still_needs_streak_and_growth():
    th = ScreenThresholds()
    row = ScreenRow(
        symbol="688981.SH",
        name="中芯国际",
        industry="半导体",
        themes=["AI"],
        report_date="20241231",
        roe=12.0,
        roe_years_ok=1,
        revenue_yoy=5.0,
        profit_yoy=5.0,
        ocf_ps=1.0,
        debt_ratio=40.0,
    )
    hist_roe = {"20241231": {"688981.SH": 12.0}}
    hist_profit = {"20241231": {"688981.SH": 10.0}}
    enrich_track_metrics(
        row,
        th=th,
        hist_roe=hist_roe,
        hist_profit=hist_profit,
        dates=["20241231"],
    )
    assert row.track == "growth"
    assert row.passed_hard is False
    label, tone = verdict(row)
    assert "成长轨" in label or tone == "weak"
