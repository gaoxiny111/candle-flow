"""分红档案（stock_dividend_profile）单测。

这是读层「真高股息」判据的数据底座：连续分红年限 / 近三年年均每股分红。
口径最容易错的两处：
  ① 「近三年」必须是**完整会计年度**（有末期 1231 预案），只算中期的年度
     不构成一个年度分红口径 —— 否则连续年限与均值都会被单期中报虚增；
  ② 表里只存与股价无关的每股分红，股息率由读层用快照价现算。
"""

from __future__ import annotations

import json

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.database import Base
from app.models.dividend_profile import StockDividendProfile
from app.services import dividend_store as ds


def _hist(plans, consecutive: int = 5) -> dict:
    return {"plans": plans, "consecutive_years": consecutive, "latest_fy": None}


def _plan(report_date: str, dps: float | None, ex: str | None = None) -> dict:
    return {"report_date": report_date, "dps": dps, "ex_date": ex, "status": "实施"}


def test_fy_totals_requires_final_plan_and_sums_midterm():
    """同一会计年度的中期+末期要合计；只有中期的年度不算完整年度。"""
    hist = _hist(
        [
            _plan("20260630", 0.98),  # 2026 只有中期 → 不算
            _plan("20251231", 1.03),
            _plan("20250630", 0.98),  # 2025 = 0.98 + 1.03 = 2.01
            _plan("20241231", 2.26),
            _plan("20231231", None),  # 无分红额 → 跳过
        ]
    )
    assert ds._fy_totals(hist["plans"]) == {2025: 2.01, 2024: 2.26}


def test_compute_profile_three_year_average_and_passthrough():
    """近三年均值取最近 3 个完整年度；连续年限透传东财口径。"""
    hist = _hist(
        [
            _plan("20251231", 2.00),
            _plan("20241231", 2.20),
            _plan("20231231", 2.40),
            _plan("20221231", 3.00),  # 超出三年窗口，不参与均值
        ],
        consecutive=19,
    )
    prof = ds.compute_profile(hist)
    assert prof is not None
    assert prof["consecutive_years"] == 19
    assert prof["latest_fy"] == 2025
    assert prof["dps_latest_fy"] == 2.0
    assert prof["dps_3y_avg"] == pytest.approx(2.2)  # (2.00+2.20+2.40)/3
    assert prof["dps_series"] == {2025: 2.0, 2024: 2.2, 2023: 2.4, 2022: 3.0}


def test_compute_profile_none_when_no_valid_records():
    """无任何完整年度记录（新股/从未分红/接口空）→ None，不写行。"""
    assert ds.compute_profile(None) is None
    assert ds.compute_profile({}) is None
    assert ds.compute_profile(_hist([])) is None
    assert ds.compute_profile(_hist([_plan("20260630", 1.0)])) is None


def _mem_db():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine)()


def test_attach_profiles_merges_into_light_rows():
    """合并到轻量行：有档案 → 写 div_years/dps_3y_avg/div_latest_fy；无档案 → 不写键。"""
    db = _mem_db()
    db.add(
        StockDividendProfile(
            symbol="600519.SH", consecutive_years=25, dps_3y_avg=27.6, latest_fy=2025
        )
    )
    db.commit()
    rows = [{"symbol": "600519.SH"}, {"symbol": "600000.SH"}]
    hit = ds.attach_profiles(rows, db)
    assert hit == 1
    assert rows[0]["div_years"] == 25 and rows[0]["dps_3y_avg"] == pytest.approx(27.6)
    # div_latest_fy 是「档案新鲜度闸门」（high_dividend.hd_required_fy）的输入，
    # 漏写会让闸门恒判「档案年度缺」→ 整张名单清空，故必须由测试钉住。
    assert rows[0]["div_latest_fy"] == 2025
    # 无档案的行不得被写入任何键（缺失 != 0，判据按「缺数据」处理）
    assert "div_years" not in rows[1]
    assert "div_latest_fy" not in rows[1]
    db.close()


def test_upsert_then_load_roundtrip(monkeypatch):
    """写入后能读回（含 dps_series 的 JSON 落库形态）。"""
    db = _mem_db()
    monkeypatch.setattr(ds, "SessionLocal", lambda: db)
    n = ds.upsert_profiles(
        [
            (
                "601088.SH",
                {
                    "consecutive_years": 19,
                    "latest_fy": 2025,
                    "dps_latest_fy": 2.01,
                    "dps_3y_avg": 2.1767,
                    "dps_series": {2025: 2.01, 2024: 2.26, 2023: 2.26},
                },
            )
        ]
    )
    assert n == 1
    row = db.query(StockDividendProfile).one()
    assert json.loads(row.dps_series)["2025"] == 2.01
    assert row.source == "eastmoney"
    loaded = ds.load_all(db)
    assert loaded["601088.SH"]["consecutive_years"] == 19
    assert loaded["601088.SH"]["dps_3y_avg"] == pytest.approx(2.1767)
    db.close()


def test_refresh_profile_swallows_fetch_errors(monkeypatch):
    """抓取失败不得抛出（快照构建链路不能被拖挂），返回 False。"""
    import app.analysis.dividend_data as dd

    monkeypatch.setattr(dd, "fetch_dividend_history", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("net")))
    assert ds.refresh_profile("600519.SH") is False


def test_refresh_profile_writes_when_fetch_ok(monkeypatch):
    """抓取成功 → 落库；无有效记录 → 不写且返回 False。"""
    import app.analysis.dividend_data as dd

    db = _mem_db()
    monkeypatch.setattr(ds, "SessionLocal", lambda: db)
    monkeypatch.setattr(
        dd,
        "fetch_dividend_history",
        lambda *a, **k: _hist([_plan("20251231", 1.5), _plan("20241231", 1.4)], consecutive=7),
    )
    assert ds.refresh_profile("600001.SH") is True
    assert db.query(StockDividendProfile).count() == 1
    monkeypatch.setattr(dd, "fetch_dividend_history", lambda *a, **k: _hist([]))
    assert ds.refresh_profile("600002.SH") is False
    assert db.query(StockDividendProfile).count() == 1
    db.close()
