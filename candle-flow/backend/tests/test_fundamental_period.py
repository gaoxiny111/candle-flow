"""最新报告期增速口径 + 现金比每股对齐."""

from app.services.fundamental_screen import (
    ScreenRow,
    ScreenThresholds,
    report_period_label,
    resolve_latest_report_frame,
)
from app.services.fundamental_tracks import (
    cash_ratio,
    enrich_track_metrics,
    is_cash_stabilizer,
    metrics_line,
    valuation_flag,
    verdict,
)


def test_report_period_label():
    assert report_period_label("20260630") == "2026H1"
    assert report_period_label("20251231") == "2025A"


def test_cash_ratio_same_period_per_share():
    shenhua = ScreenRow(
        symbol="601088.SH",
        name="中国神华",
        industry="煤炭开采",
        themes=[],
        report_date="20260630",
        ocf_ps=2.52,
        eps=1.34,
    )
    yitai = ScreenRow(
        symbol="900948.SH",
        name="伊泰B股",
        industry="煤炭开采",
        themes=[],
        report_date="20260630",
        ocf_ps=1.16,
        eps=1.52,
    )
    assert abs(cash_ratio(shenhua) - 1.88) < 0.02
    assert abs(cash_ratio(yitai) - 0.76) < 0.02
    assert is_cash_stabilizer(
        ScreenRow(
            symbol="601088.SH",
            name="中国神华",
            industry="煤炭开采",
            themes=[],
            report_date="20260630",
            track="cyclical",
            ocf_ps=2.52,
            eps=1.34,
        )
    )


def test_shenhua_h1_positive_growth_not_worsening():
    th = ScreenThresholds()
    row = ScreenRow(
        symbol="601088.SH",
        name="中国神华",
        industry="煤炭开采",
        themes=[],
        report_date="20260630",
        roe=5.83,
        roe_avg=11.8,
        revenue_yoy=7.93,
        profit_yoy=4.1,
        deducted_profit_yoy=11.0,
        ocf_ps=2.52,
        eps=1.34,
        debt_ratio=40.0,
        dividend_yield=6.6,
        pe_percentile=99.0,
        pb_percentile=99.0,
        no_consec_loss=True,
    )
    enrich_track_metrics(row, th=th, hist_roe={}, hist_profit={}, dates=["20251231"])
    assert row.track == "cyclical"
    assert row.passed_hard is True
    assert valuation_flag(row) == "高估+改善·不追高"
    label, tone = verdict(row)
    assert tone == "strong"
    assert "偏强" in label
    assert "稳定器" in label
    line = metrics_line(row)
    assert "营收+7.9%" in line or "营收+7.93%" in line
    assert "现金比188%" in line or "现金比1" in line
    assert "2026H1" in line


def test_resolve_latest_prefers_interim(monkeypatch):
    import pandas as pd
    import app.services.fundamental_screen as mod

    calls: list[str] = []

    def fake_yjbb(d: str):
        calls.append(d)
        if d == "20260630":
            return pd.DataFrame({"股票代码": ["601088"], "股票简称": ["中国神华"]})
        return pd.DataFrame()

    monkeypatch.setattr(mod, "_fetch_yjbb", fake_yjbb)
    d, df = resolve_latest_report_frame(today=__import__("datetime").date(2026, 8, 31))
    assert d == "20260630"
    assert not df.empty
    assert calls[0] == "20260630"
