"""GET /kline 在本地数据落后于最近交易日时，应增量补齐缺失的历史蜡烛。"""

from datetime import date

import pandas as pd
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

import app.models  # noqa: F401
from app.api.v1 import kline as kline_api
from app.database import Base, get_db
from app.models.kline import KlineData
from app.services import akshare_client as ak_mod


def _client() -> tuple[TestClient, sessionmaker]:
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    TestingSession = sessionmaker(autocommit=False, autoflush=False, bind=engine)
    Base.metadata.create_all(engine)

    def override_db():
        db = TestingSession()
        try:
            yield db
        finally:
            db.close()

    app = FastAPI()
    app.include_router(kline_api.router, prefix="/api/v1")
    app.dependency_overrides[get_db] = override_db
    return TestClient(app), TestingSession


def _bar(d: date, close: float, volume: int = 100000) -> KlineData:
    return KlineData(
        symbol="600519.SH",
        date=d,
        open=close - 5,
        high=close + 5,
        low=close - 8,
        close=close,
        volume=volume,
        source="akshare",
    )


def _hist_df(rows: list[tuple[date, float]]) -> pd.DataFrame:
    return pd.DataFrame(
        {
            "date": [r[0] for r in rows],
            "open": [r[1] - 5 for r in rows],
            "high": [r[1] + 5 for r in rows],
            "low": [r[1] - 8 for r in rows],
            "close": [r[1] for r in rows],
            "volume": [100000 for _ in rows],
        }
    )


def test_kline_backfills_missing_history_when_stale(monkeypatch):
    client, TestingSession = _client()
    db = TestingSession()
    # 本地只有上周五(8/28)的蜡烛，周一(8/31)、周二(9/1)缺失
    db.add(_bar(date(2026, 8, 28), 1450.0))
    db.commit()
    db.close()

    # 今天是周三 9/2（交易日）
    monkeypatch.setattr("app.services.kline_service.trading_today", lambda now=None: date(2026, 9, 2))
    monkeypatch.setattr("app.services.kline_service.is_cn_weekday", lambda d=None: True)
    # 历史源返回 8/28 ~ 9/1
    monkeypatch.setattr(
        ak_mod.akshare_client,
        "fetch_daily",
        lambda symbol, start_date=None, end_date=None: _hist_df(
            [(date(2026, 8, 28), 1450.0), (date(2026, 8, 31), 1462.0), (date(2026, 9, 1), 1470.0)]
        ),
    )
    # 盘中实时价补今天 9/2
    monkeypatch.setattr(
        ak_mod.akshare_client,
        "fetch_spot",
        lambda symbol: {
            "date": date(2026, 9, 2),
            "open": 1471.0,
            "high": 1480.0,
            "low": 1465.0,
            "close": 1477.0,
            "volume": 200000,
            "source": "tencent",
        },
    )

    res = client.get("/api/v1/kline", params={"symbol": "600519.SH"})
    assert res.status_code == 200
    rows = res.json()["data"]
    dates = [r["date"] for r in rows]
    assert dates == ["2026-08-28", "2026-08-31", "2026-09-01", "2026-09-02"]
    assert float(rows[-1]["close"]) == 1477.0


def test_kline_no_sync_when_up_to_date(monkeypatch):
    client, TestingSession = _client()
    db = TestingSession()
    db.add(_bar(date(2026, 9, 2), 1477.0))
    db.commit()
    db.close()

    monkeypatch.setattr("app.services.kline_service.trading_today", lambda now=None: date(2026, 9, 2))
    monkeypatch.setattr("app.services.kline_service.is_cn_weekday", lambda d=None: True)

    def _boom(*a, **k):
        raise AssertionError("fetch_daily should not be called when local bar is current")

    monkeypatch.setattr(ak_mod.akshare_client, "fetch_daily", _boom)
    monkeypatch.setattr(ak_mod.akshare_client, "fetch_spot", lambda symbol: None)

    res = client.get("/api/v1/kline", params={"symbol": "600519.SH"})
    assert res.status_code == 200
    dates = [r["date"] for r in res.json()["data"]]
    assert dates == ["2026-09-02"]


def test_kline_stale_but_sync_fails_falls_back(monkeypatch):
    """增量同步失败时仍返回本地数据，不报错。"""
    client, TestingSession = _client()
    db = TestingSession()
    db.add(_bar(date(2026, 8, 28), 1450.0))
    db.commit()
    db.close()

    monkeypatch.setattr("app.services.kline_service.trading_today", lambda now=None: date(2026, 9, 2))
    monkeypatch.setattr("app.services.kline_service.is_cn_weekday", lambda d=None: True)

    def _fail(*a, **k):
        raise RuntimeError("network down")

    monkeypatch.setattr(ak_mod.akshare_client, "fetch_daily", _fail)
    monkeypatch.setattr(ak_mod.akshare_client, "fetch_spot", lambda symbol: None)

    res = client.get("/api/v1/kline", params={"symbol": "600519.SH"})
    assert res.status_code == 200
    dates = [r["date"] for r in res.json()["data"]]
    assert dates == ["2026-08-28"]
