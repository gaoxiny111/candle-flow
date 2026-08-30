from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

import app.models  # noqa: F401
from app.api.v1 import system
from app.database import Base, get_db


def _client() -> TestClient:
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
    app.include_router(system.router, prefix="/api/v1")
    app.dependency_overrides[get_db] = override_db
    return TestClient(app)


def test_guest_cannot_save_watchlist():
    client = _client()
    res = client.post("/api/v1/config/watchlist", json={"add": "600519.SH"})
    assert res.status_code == 401
    empty = client.get("/api/v1/config/watchlist")
    assert empty.status_code == 200
    assert empty.json()["data"]["symbols"] == []


def test_two_users_isolated_watchlists():
    client = _client()
    alice = client.post(
        "/api/v1/auth/register", json={"phone": "13800138101", "password": "pass1234"}
    )
    bob = client.post("/api/v1/auth/register", json={"phone": "13800138102", "password": "pass1234"})
    assert alice.status_code == 200
    assert bob.status_code == 200
    token_a = alice.json()["data"]["token"]
    token_b = bob.json()["data"]["token"]
    ha = {"Authorization": f"Bearer {token_a}"}
    hb = {"Authorization": f"Bearer {token_b}"}

    add_a = client.post("/api/v1/config/watchlist", json={"add": "600519.SH"}, headers=ha)
    add_b = client.post("/api/v1/config/watchlist", json={"add": "000001.SZ"}, headers=hb)
    assert add_a.status_code == 200
    assert add_b.status_code == 200
    assert add_a.json()["data"]["symbols"] == ["600519.SH"]
    assert add_b.json()["data"]["symbols"] == ["000001.SZ"]

    wa = client.get("/api/v1/config/watchlist", headers=ha).json()["data"]["symbols"]
    wb = client.get("/api/v1/config/watchlist", headers=hb).json()["data"]["symbols"]
    assert wa == ["600519.SH"]
    assert wb == ["000001.SZ"]

    me_a = client.get("/api/v1/auth/me", headers=ha)
    assert me_a.status_code == 200
    assert me_a.json()["data"]["username"] == "13800138101"
    assert me_a.json()["data"]["watchlist"] == ["600519.SH"]


def test_login_returns_own_watchlist():
    client = _client()
    client.post("/api/v1/auth/register", json={"phone": "13800138103", "password": "pass1234"})
    login = client.post("/api/v1/auth/login", json={"phone": "13800138103", "password": "pass1234"})
    assert login.status_code == 200
    token = login.json()["data"]["token"]
    headers = {"Authorization": f"Bearer {token}"}
    client.post("/api/v1/config/watchlist", json={"add": "601318.SH"}, headers=headers)
    again = client.post("/api/v1/auth/login", json={"phone": "13800138103", "password": "pass1234"})
    assert again.json()["data"]["watchlist"] == ["601318.SH"]
    stale = client.get("/api/v1/auth/me", headers=headers)
    assert stale.status_code == 401
    guest = client.get("/api/v1/config/watchlist", headers=headers)
    assert guest.json()["data"]["symbols"] == []


def test_register_rejects_duplicate_and_invalid_phone():
    client = _client()
    first = client.post("/api/v1/auth/register", json={"phone": "13800138104", "password": "pass1234"})
    assert first.status_code == 200
    dup = client.post("/api/v1/auth/register", json={"phone": "13800138104", "password": "pass1234"})
    assert dup.status_code == 400
    reserved = client.post(
        "/api/v1/auth/register", json={"phone": "default", "password": "pass1234"}
    )
    assert reserved.status_code == 400


def test_register_accepts_username_and_wechat():
    client = _client()
    by_name = client.post(
        "/api/v1/auth/register",
        json={"username": "alice", "password": "pass1234"},
    )
    assert by_name.status_code == 200, by_name.text
    assert by_name.json()["data"]["username"] == "alice"
    wechat = client.post(
        "/api/v1/auth/register",
        json={"phone": "gcx13948673732", "password": "pass1234"},
    )
    assert wechat.status_code == 200, wechat.text
    assert wechat.json()["data"]["username"] == "gcx13948673732"
