from datetime import datetime, timedelta

from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

import app.models  # noqa: F401
from app.api.v1 import patterns, symbols, system
from app.config import settings
from app.database import Base, get_db
from app.services.membership import activate_membership, is_member


def _client(admin_key: str = "test-admin-key-12345678") -> TestClient:
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    TestingSession = sessionmaker(autocommit=False, autoflush=False, bind=engine)
    Base.metadata.create_all(engine)
    settings.membership_admin_key = admin_key

    def override_db():
        db = TestingSession()
        try:
            yield db
        finally:
            db.close()

    app = FastAPI()
    app.include_router(system.router, prefix="/api/v1")
    app.include_router(symbols.router, prefix="/api/v1")
    app.include_router(patterns.router, prefix="/api/v1")
    app.dependency_overrides[get_db] = override_db
    return TestClient(app), TestingSession


def _register(client: TestClient, name: str = "13800138101") -> str:
    # Accept legacy short names in older tests by mapping to phones
    phones = {
        "alice": "13800138101",
        "bob": "13800138102",
        "carol": "13800138103",
    }
    phone = phones.get(name, name if name.isdigit() else "13800138101")
    res = client.post("/api/v1/auth/register", json={"phone": phone, "password": "pass1234"})
    assert res.status_code == 200, res.text
    return res.json()["data"]["token"]


def test_admin_list_users():
    client, _ = _client()
    _register(client, "alice")
    _register(client, "bob")
    denied = client.get("/api/v1/admin/users")
    assert denied.status_code == 403
    ok = client.get(
        "/api/v1/admin/users",
        headers={"X-Admin-Key": "test-admin-key-12345678"},
    )
    assert ok.status_code == 200
    names = {u["username"] for u in ok.json()["data"]}
    assert names == {"13800138101", "13800138102"}
    filtered = client.get(
        "/api/v1/admin/users",
        params={"q": "8101"},
        headers={"X-Admin-Key": "test-admin-key-12345678"},
    )
    assert [u["username"] for u in filtered.json()["data"]] == ["13800138101"]


def test_free_user_blocked_from_valuations_and_backtest():
    client, _ = _client()
    token = _register(client)
    headers = {"Authorization": f"Bearer {token}"}
    val = client.get("/api/v1/symbols/valuations", params={"symbols": "600519.SH"}, headers=headers)
    assert val.status_code == 403
    bt = client.get("/api/v1/backtest/600519.SH", headers=headers)
    assert bt.status_code == 403
    scan = client.post("/api/v1/patterns/scan/watchlist", headers=headers)
    assert scan.status_code == 403


def test_admin_activate_month_unlocks_member_apis():
    client, Session = _client()
    phone = "13800138102"
    token = _register(client, phone)
    headers = {"Authorization": f"Bearer {token}"}
    offer = client.get("/api/v1/membership/offer")
    assert offer.status_code == 200
    assert offer.json()["data"]["price_month"]

    bad = client.post(
        "/api/v1/admin/membership",
        json={"admin_key": "wrong-key-xxxxxxxx", "username": phone, "plan": "month"},
    )
    assert bad.status_code == 403

    ok = client.post(
        "/api/v1/admin/membership",
        json={"admin_key": "test-admin-key-12345678", "username": phone, "plan": "month"},
    )
    assert ok.status_code == 200
    data = ok.json()["data"]["membership"]
    assert data["is_member"] is True
    assert data["plan"] == "month"
    assert data["expires_at"]

    me = client.get("/api/v1/auth/me", headers=headers)
    assert me.json()["data"]["membership"]["is_member"] is True

    # valuations still need network; membership gate should pass (not 403)
    val = client.get("/api/v1/symbols/valuations", params={"symbols": "600519.SH"}, headers=headers)
    assert val.status_code != 403

    bt = client.get("/api/v1/backtest/600519.SH", headers=headers)
    assert bt.status_code != 403

    # lifetime / free
    life = client.post(
        "/api/v1/admin/membership",
        json={"admin_key": "test-admin-key-12345678", "username": phone, "plan": "lifetime"},
    )
    assert life.json()["data"]["membership"]["plan"] == "lifetime"
    assert life.json()["data"]["membership"]["expires_at"] is None

    free = client.post(
        "/api/v1/admin/membership",
        json={"admin_key": "test-admin-key-12345678", "username": phone, "plan": "free"},
    )
    assert free.json()["data"]["membership"]["is_member"] is False

    db = Session()
    try:
        from app.models.user_config import UserConfig

        user = db.query(UserConfig).filter(UserConfig.user_id == phone).first()
        assert user is not None
        activate_membership(user, "month", days=1)
        user.membership_expires_at = datetime.utcnow() - timedelta(days=1)
        db.commit()
        db.refresh(user)
        assert is_member(user) is False
    finally:
        db.close()


def test_free_watchlist_limit(monkeypatch):
    client, _ = _client()
    phone = "13800138103"
    token = _register(client, phone)
    headers = {"Authorization": f"Bearer {token}"}

    def fake_resolve(raw: str, db=None):
        return raw.strip().upper()

    monkeypatch.setattr(system, "resolve_symbol", fake_resolve)
    symbols = [f"{600000 + i}.SH" for i in range(9)]
    res = client.post("/api/v1/config/watchlist", json={"symbols": symbols}, headers=headers)
    assert res.status_code == 400
    assert "最多 8" in res.json()["detail"]

    ok = client.post(
        "/api/v1/config/watchlist",
        json={"symbols": symbols[:8]},
        headers=headers,
    )
    assert ok.status_code == 200
    assert len(ok.json()["data"]["symbols"]) == 8
    assert ok.json()["data"]["limit"] == 8

    client.post(
        "/api/v1/admin/membership",
        json={"admin_key": "test-admin-key-12345678", "username": phone, "plan": "month"},
    )
    more = client.post(
        "/api/v1/config/watchlist",
        json={"symbols": symbols},
        headers=headers,
    )
    assert more.status_code == 200
    assert len(more.json()["data"]["symbols"]) == 9
    assert more.json()["data"]["limit"] == 50
