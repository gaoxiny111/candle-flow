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
from app.services.membership import WATCHLIST_LIMIT, activate_membership, is_member


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


def test_free_user_can_access_member_apis():
    client, _ = _client()
    token = _register(client)
    headers = {"Authorization": f"Bearer {token}"}
    val = client.get("/api/v1/symbols/valuations", params={"symbols": "600519.SH"}, headers=headers)
    assert val.status_code != 403
    bt = client.get("/api/v1/backtest/600519.SH", headers=headers)
    assert bt.status_code != 403
    scan = client.post("/api/v1/patterns/scan/watchlist", headers=headers)
    assert scan.status_code != 403


def test_anonymous_can_access_valuations_and_backtest():
    client, _ = _client()
    val = client.get("/api/v1/symbols/valuations", params={"symbols": "600519.SH"})
    assert val.status_code != 403
    assert val.status_code != 401
    bt = client.get("/api/v1/backtest/600519.SH")
    assert bt.status_code != 403
    assert bt.status_code != 401


def test_membership_snapshot_always_open():
    client, _ = _client()
    token = _register(client, "13800138102")
    me = client.get("/api/v1/auth/me", headers={"Authorization": f"Bearer {token}"})
    m = me.json()["data"]["membership"]
    assert m["is_member"] is True
    assert m["watchlist_limit"] == WATCHLIST_LIMIT


def test_watchlist_limit_for_all_users(monkeypatch):
    client, _ = _client()
    phone = "13800138103"
    token = _register(client, phone)
    headers = {"Authorization": f"Bearer {token}"}

    def fake_resolve(raw: str, db=None):
        return raw.strip().upper()

    monkeypatch.setattr(system, "resolve_symbol", fake_resolve)
    symbols = [f"{600000 + i}.SH" for i in range(9)]
    res = client.post("/api/v1/config/watchlist", json={"symbols": symbols}, headers=headers)
    assert res.status_code == 200
    assert len(res.json()["data"]["symbols"]) == 9
    assert res.json()["data"]["limit"] == WATCHLIST_LIMIT


def test_admin_create_and_delete_user():
    client, Session = _client()
    key = "test-admin-key-12345678"
    phone = "13900139001"
    created = client.post(
        "/api/v1/admin/users",
        json={"admin_key": key, "username": phone, "password": "pass1234"},
    )
    assert created.status_code == 200, created.text
    body = created.json()["data"]
    assert body["username"] == phone
    assert body["membership"]["is_member"] is True

    listed = client.get("/api/v1/admin/users", headers={"X-Admin-Key": key})
    assert listed.status_code == 200
    assert any(u["username"] == phone for u in listed.json()["data"])

    dup = client.post(
        "/api/v1/admin/users",
        json={"admin_key": key, "username": phone, "password": "pass1234"},
    )
    assert dup.status_code == 400

    deleted = client.post(
        "/api/v1/admin/users/delete",
        json={"admin_key": key, "username": phone},
    )
    assert deleted.status_code == 200
    assert deleted.json()["data"]["deleted"] is True

    listed2 = client.get("/api/v1/admin/users", headers={"X-Admin-Key": key})
    assert not any(u["username"] == phone for u in listed2.json()["data"])

    missing = client.post(
        "/api/v1/admin/users/delete",
        json={"admin_key": key, "username": phone},
    )
    assert missing.status_code == 404


def test_legacy_activate_membership_still_works():
    client, Session = _client()
    phone = "13800138104"
    _register(client, phone)
    ok = client.post(
        "/api/v1/admin/membership",
        json={"admin_key": "test-admin-key-12345678", "username": phone, "plan": "month"},
    )
    assert ok.status_code == 200
    assert is_member(None) is True

    db = Session()
    try:
        from app.models.user_config import UserConfig

        user = db.query(UserConfig).filter(UserConfig.user_id == phone).first()
        assert user is not None
        activate_membership(user, "month", days=1)
        user.membership_expires_at = datetime.utcnow() - timedelta(days=1)
        db.commit()
        assert is_member(user) is True
    finally:
        db.close()
