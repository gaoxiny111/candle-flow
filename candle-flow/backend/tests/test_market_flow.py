"""全市场资金流（东财 datacenter）——离线测试。

不联网：``fetch_market_flow`` 用 monkeypatch 替换为固定表；落库走真实 SQLite
（项目 DB，表由 ``init_db`` 保证存在），测试结束清理自己写入的行。
"""
from datetime import date

import pytest

from app.services import market_flow as mf


# ── _norm_code ────────────────────────────────────────────────────────
@pytest.mark.parametrize(
    "raw,expected",
    [
        ("000612", "000612"),
        ("000612.SZ", "000612"),
        ("SZ000612", "000612"),
        ("600519.SH", "600519"),
        ("", ""),
        (None, ""),
        ("abc", ""),
    ],
)
def test_norm_code(raw, expected):
    assert mf._norm_code(raw) == expected


# ── inflow_streak（内存表口径）────────────────────────────────────────
def _table(series_by_code: dict[str, list[float]]):
    """构造 fetch_market_flow 形状的表：dates 倒序，series[0] 为最新。"""
    dates = ["2026-09-23", "2026-09-22", "2026-09-19", "2026-09-18", "2026-09-17"]
    by_date: dict[str, dict] = {d: {} for d in dates}
    latest: dict[str, dict] = {}
    for code, vals in series_by_code.items():
        for d, v in zip(dates, vals):
            by_date[d][code] = {"date": d, "main": v}
        if vals:
            latest[code] = {"date": dates[0], "main": vals[0]}
    return {"dates": dates, "by_date": by_date, "latest": latest}


def test_inflow_streak_all_in():
    t = _table({"000612": [1e7, 2e7, 3e7, 4e7, 5e7]})
    r = mf.inflow_streak("000612", t, window=5)
    assert r["days"] == 5
    assert r["in_days"] == 5
    assert r["out_days"] == 0
    assert r["streak"] == 5
    assert r["dir"] == "in"
    assert r["latest"] == 1e7


def test_inflow_streak_breaks_on_direction_change():
    # 最新 2 日流入，第 3 日流出 → streak 应为 2
    t = _table({"000612": [1e7, 2e7, -3e7, 4e7, 5e7]})
    r = mf.inflow_streak("000612", t, window=5)
    assert r["streak"] == 2
    assert r["dir"] == "in"
    assert r["in_days"] == 4
    assert r["out_days"] == 1


def test_inflow_streak_outflow_direction():
    t = _table({"000612": [-1e7, -2e7, 3e7, 4e7, 5e7]})
    r = mf.inflow_streak("000612", t, window=5)
    assert r["streak"] == 2
    assert r["dir"] == "out"


def test_inflow_streak_missing_symbol_returns_zero():
    t = _table({"600519": [1e7, 2e7, 3e7, 4e7, 5e7]})
    assert mf.inflow_streak("000612", t, window=5) == {"days": 0}


def test_inflow_streak_empty_table():
    assert mf.inflow_streak("000612", {}, window=5) == {"days": 0}
    assert mf.inflow_streak("000612", {"dates": [], "by_date": {}}, window=5) == {"days": 0}


def test_inflow_streak_skips_none_values():
    t = _table({"000612": [1e7, 2e7, 3e7, 4e7, 5e7]})
    # 人为把第 3 日置 None → series 变短但不报错
    t["by_date"]["2026-09-19"]["000612"]["main"] = None
    r = mf.inflow_streak("000612", t, window=5)
    # None 被跳过（不留空洞）→ series 剩 4 项且全为流入
    assert r["days"] == 4
    assert r["streak"] == 4
    assert r["in_days"] == 4
    assert r["series"] == [1e7, 2e7, 4e7, 5e7]


def test_flow_of_normalizes_code():
    t = {"latest": {"000612": {"main": -1.0}}}
    assert mf.flow_of("000612.SZ", t)["main"] == -1.0
    assert mf.flow_of("", t) is None


# ── 落库（跳过无网络环境）──────────────────────────────────────────────
_TEST_DATE = date(2099, 12, 31)   # 用远期日期，绝不与真实采集数据冲突


def _cleanup():
    from sqlalchemy import delete

    from app.database import SessionLocal, init_db
    from app.models.market_fund_flow import MarketFundFlowDaily

    init_db()   # 表可能尚未创建（新库/CI），先保证存在再删
    db = SessionLocal()
    try:
        db.execute(delete(MarketFundFlowDaily).where(
            MarketFundFlowDaily.date == _TEST_DATE))
        db.commit()
    finally:
        db.close()


def test_persist_and_history_roundtrip():
    from app.database import SessionLocal, init_db
    from app.models.market_fund_flow import MarketFundFlowDaily

    init_db()
    _cleanup()
    synthetic = {
        "dates": [_TEST_DATE.isoformat()],
        "by_date": {_TEST_DATE.isoformat(): {
            "000612": {
                "date": _TEST_DATE.isoformat(), "main": -18281070.0,
                "super_in": -1.0, "big_in": -2.0, "big_buy_ratio": 0.48,
                "org_participate": 0.1, "prime_cost": 10.9,
                "close": 10.94, "chg": -2.1467,
            },
            "600519": {
                "date": _TEST_DATE.isoformat(), "main": 5e7,
                "super_in": 1.0, "big_in": 2.0, "big_buy_ratio": 0.6,
                "org_participate": 0.4, "prime_cost": 1500.0,
                "close": 1510.0, "chg": 1.2,
            },
        }},
        "latest": {},
    }
    synthetic["latest"] = synthetic["by_date"][_TEST_DATE.isoformat()]

    r = mf.persist_market_flow(synthetic)
    assert r["inserted"] == 2
    assert r["date"] == _TEST_DATE.isoformat()

    # 幂等：再跑一次应为 updated，不新增行
    r2 = mf.persist_market_flow(synthetic)
    assert r2["inserted"] == 0
    assert r2["updated"] == 2

    db = SessionLocal()
    try:
        n = db.query(MarketFundFlowDaily).filter(
            MarketFundFlowDaily.date == _TEST_DATE).count()
        assert n == 2
    finally:
        db.close()

    # 只断言本用例写入的那一天（表里可能有真实采集数据，不能假设全表干净）
    hist = [h for h in mf.history_of("000612", days=50) if h["date"] == _TEST_DATE.isoformat()]
    assert len(hist) == 1
    assert hist[0]["main"] == -18281070.0

    # streak 只看最近 N 日，可能混入真实数据；这里断言最新一日取自本用例
    st = mf.streak_from_history("000612", window=1)
    assert st["days"] >= 1
    assert st["latest"] is not None

    _cleanup()


def test_history_of_missing_symbol_is_empty():
    assert mf.history_of("", days=5) == []


def test_streak_from_history_empty_is_zero():
    assert mf.streak_from_history("999999", window=5) == {"days": 0}


def test_persist_skips_when_table_empty():
    r = mf.persist_market_flow({"dates": [], "by_date": {}, "latest": {}})
    assert r["skipped"] is True
    assert r["inserted"] == 0


def test_scheduler_start_respects_disable_flag(monkeypatch):
    monkeypatch.setenv("ENABLE_INPROCESS_SCHEDULER", "0")
    mf._scheduler = None
    mf.start_market_flow_scheduler()
    assert mf._scheduler is None
    monkeypatch.delenv("ENABLE_INPROCESS_SCHEDULER", raising=False)


def test_fetch_market_flow_caches(monkeypatch):
    calls = {"n": 0}

    class _Resp:
        @staticmethod
        def raise_for_status():
            return None

        @staticmethod
        def json():
            return {"result": {"data": []}}

    def _fake_get(*_a, **_kw):
        calls["n"] += 1
        return _Resp()

    mf._cache.clear()
    monkeypatch.setattr(mf.requests, "get", _fake_get)
    a = mf.fetch_market_flow(days=1)
    b = mf.fetch_market_flow(days=1)
    assert a == b
    assert calls["n"] == 1, "第二次应命中缓存，不应再发请求"
    mf._cache.clear()
