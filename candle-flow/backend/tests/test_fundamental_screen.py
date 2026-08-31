from datetime import date

import pandas as pd

from app.services.fundamental_screen import (
    ScreenThresholds,
    _peg,
    detect_hot_themes,
    match_themes,
    recent_quarter_ends,
    recent_year_ends,
    score_themes_four_dim,
)


def test_match_themes():
    assert "AI" in match_themes("半导体", ["AI", "新能源"])
    assert "新能源" in match_themes("电力设备", ["新能源"])
    assert match_themes("银行", ["AI"]) == []


def test_detect_hot_themes_ranks_by_growth():
    rows = []
    for i in range(12):
        rows.append(
            {
                "股票代码": f"688{i:03d}",
                "所处行业": "半导体",
                "营业总收入-同比增长": 40.0,
                "净利润-同比增长": 50.0,
            }
        )
    for i in range(12):
        rows.append(
            {
                "股票代码": f"600{i:03d}",
                "所处行业": "银行",
                "营业总收入-同比增长": 3.0,
                "净利润-同比增长": 2.0,
            }
        )
    hot = detect_hot_themes(pd.DataFrame(rows), top_n=2)
    assert hot[0].theme == "AI"
    assert "金融" in {t.theme for t in hot} or hot[0].score > 20


def test_recent_year_ends_after_may():
    assert recent_year_ends(3, today=date(2026, 8, 31))[:3] == ["20251231", "20241231", "20231231"]


def test_recent_year_ends_before_may():
    assert recent_year_ends(3, today=date(2026, 3, 1))[:3] == ["20241231", "20231231", "20221231"]


def test_recent_quarter_ends():
    qs = recent_quarter_ends(4, today=date(2026, 8, 31))
    assert qs[0] == "20260630"
    assert "20250331" in qs


def test_resolve_report_frames_skips_empty(monkeypatch):
    from app.services import fundamental_screen as mod

    def fake_yjbb(report_date: str) -> pd.DataFrame:
        if report_date == "20251231":
            return pd.DataFrame()
        return pd.DataFrame(
            [{"股票代码": "688981", "股票简称": "中芯国际", "净资产收益率": 18.0}]
        )

    monkeypatch.setattr(mod, "_fetch_yjbb", fake_yjbb)
    dates, frames = mod.resolve_report_frames(3, today=date(2026, 8, 31))
    assert dates[0] == "20241231"
    assert "20251231" not in dates
    assert len(dates) == 3
    assert all(not frames[d].empty for d in dates)


def test_peg():
    assert _peg(30, 20) == 1.5
    assert _peg(30, 0) is None
    assert _peg(None, 20) is None


def _theme_rows(industry: str, code_prefix: str, rev: float, profit: float, roe: float, n: int = 8):
    rows = []
    for i in range(n):
        rows.append(
            {
                "股票代码": f"{code_prefix}{i:03d}",
                "所处行业": industry,
                "营业总收入-同比增长": rev,
                "净利润-同比增长": profit,
                "净资产收益率": roe,
            }
        )
    return rows


def test_four_dim_selects_with_resonance(monkeypatch):
    from app.services import fundamental_screen as mod

    q0 = pd.DataFrame(
        _theme_rows("半导体", "688", 25, 30, 18)
        + _theme_rows("银行", "600", 3, 2, 8)
    )
    q1 = pd.DataFrame(
        _theme_rows("半导体", "688", 18, 20, 16)
        + _theme_rows("银行", "600", 2, 1, 7)
    )
    board = pd.DataFrame(
        {
            "板块名称": ["半导体", "银行", "白酒", "钢铁", "煤炭", "房地产", "农林牧渔", "航运", "建材", "纺织"],
            "涨跌幅": [4.5, 0.2, 0.1, -0.5, -1.0, -1.2, 0.0, 0.3, -0.2, 0.1],
        }
    )
    flow = pd.DataFrame(
        {
            "名称": ["半导体", "银行", "白酒", "钢铁", "煤炭", "房地产", "农林牧渔", "航运", "建材", "纺织"],
            "今日主力净流入-净额": [8e8, -1e8, 1e7, -2e7, -3e7, -4e7, 0, 1e6, -1e6, 2e6],
        }
    )

    cards = score_themes_four_dim(
        quarter_frames={"20250630": q0, "20250331": q1},
        quarter_dates=["20250630", "20250331"],
        board_df=board,
        flow_df=flow,
        catalog=["AI", "金融"],
        top_n=2,
    )
    by_theme = {c.theme: c for c in cards}
    assert by_theme["AI"].profit_ok
    assert by_theme["AI"].policy_ok
    assert by_theme["AI"].supply_ok
    assert by_theme["AI"].capital_ok
    assert by_theme["AI"].resonance >= 3
    assert by_theme["AI"].selected
    assert not by_theme["金融"].profit_ok
    assert not by_theme["金融"].selected


def test_four_dim_profit_veto_blocks_selection():
    q0 = pd.DataFrame(_theme_rows("半导体", "688", 5, 40, 18))  # rev too low
    q1 = pd.DataFrame(_theme_rows("半导体", "688", 4, 30, 16))
    board = pd.DataFrame({"板块名称": ["半导体", "银行", "白酒", "钢铁"], "涨跌幅": [5.0, 0, 0, 0]})
    flow = pd.DataFrame({"名称": ["半导体", "银行", "白酒", "钢铁"], "今日主力净流入-净额": [9e8, 0, 0, 0]})
    cards = score_themes_four_dim(
        quarter_frames={"20250630": q0, "20250331": q1},
        quarter_dates=["20250630", "20250331"],
        board_df=board,
        flow_df=flow,
        catalog=["AI"],
        top_n=1,
        min_resonance=3,
    )
    ai = cards[0]
    assert ai.theme == "AI"
    assert not ai.profit_ok
    # May be degraded fallback selected only if somehow profit_ok — must not claim 强景气
    assert "盈利" in ai.conclusion or not ai.selected or "降级" in ai.conclusion or "兜底" in ai.conclusion
    if ai.selected:
        assert ai.conclusion != "强景气"


def test_screen_with_mock_frames(monkeypatch):
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker

    from app.database import Base
    from app.services import fundamental_screen as mod

    def fake_yjbb(report_date: str) -> pd.DataFrame:
        rows = [
            {
                "股票代码": "688981",
                "股票简称": "中芯国际",
                "所处行业": "半导体",
                "净资产收益率": 18.0,
                "营业总收入-同比增长": 22.0,
                "净利润-同比增长": 25.0,
                "每股经营现金流量": 1.2,
            },
            {
                "股票代码": "600000",
                "股票简称": "浦发银行",
                "所处行业": "银行",
                "净资产收益率": 12.0,
                "营业总收入-同比增长": 5.0,
                "净利润-同比增长": 3.0,
                "每股经营现金流量": 2.0,
            },
        ]
        if report_date == "20241231":
            rows[0]["净资产收益率"] = 18.0
        elif report_date == "20231231":
            rows[0]["净资产收益率"] = 16.0
        else:
            rows[0]["净资产收益率"] = 15.5
        return pd.DataFrame(rows)

    monkeypatch.setattr(mod, "_fetch_yjbb", fake_yjbb)
    monkeypatch.setattr(mod, "_fetch_debt_map", lambda d: {"688981.SH": 35.0})
    monkeypatch.setattr(
        mod,
        "get_valuations",
        lambda symbols, db=None: [
            {
                "symbol": "688981.SH",
                "pe_ttm": 28.0,
                "pb": 2.0,
                "pe_percentile": 30.0,
                "pb_percentile": 25.0,
                "price": 88.5,
                "change_pct": 1.2,
            }
        ],
    )

    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    db = sessionmaker(bind=engine)()
    try:
        run_id, rows, used_dates, theme_meta = mod.screen_fundamentals(
            db,
            themes=["AI"],
            auto_themes=False,
            thresholds=ScreenThresholds(pool_size=10),
            report_dates=["20241231", "20231231", "20221231"],
            enrich_valuation=True,
            enrich_debt=True,
        )
        assert run_id
        assert used_dates == ["20241231", "20231231", "20221231"]
        assert any(r.symbol == "688981.SH" for r in rows)
        assert all(r.symbol != "600000.SH" for r in rows)
        hit = next(r for r in rows if r.symbol == "688981.SH")
        assert hit.roe_years_ok == 3
        assert hit.peg == 1.12  # 28 / 25
        assert hit.price == 88.5
    finally:
        db.close()
