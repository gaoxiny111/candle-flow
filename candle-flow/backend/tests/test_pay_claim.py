from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

import app.models  # noqa: F401
from app.api.v1 import pay, system
from app.config import settings
from app.database import Base, get_db
from app.services.membership import is_member
from app.models.user_config import UserConfig

MIN_PNG = (
    b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01"
    b"\x08\x02\x00\x00\x00\x90wS\xde\x00\x00\x00\x0cIDATx\x9cc\xf8\x0f\x00"
    b"\x00\x01\x01\x00\x05\x18\xd8N\x00\x00\x00\x00IEND\xaeB`\x82"
)
ADMIN = "test-admin-key-12345678"


def _client(tmp_path):
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    TestingSession = sessionmaker(autocommit=False, autoflush=False, bind=engine)
    Base.metadata.create_all(engine)
    settings.membership_admin_key = ADMIN
    settings.membership_price_month = "39"

    def override_db():
        db = TestingSession()
        try:
            yield db
        finally:
            db.close()

    app = FastAPI()
    app.include_router(system.router, prefix="/api/v1")
    app.include_router(pay.router, prefix="/api/v1")
    app.dependency_overrides[get_db] = override_db
    return TestClient(app), TestingSession


def test_claim_upload_and_admin_approve(tmp_path, monkeypatch):
    monkeypatch.setattr("app.services.pay_claim.CLAIMS_DIR", tmp_path)
    client, Session = _client(tmp_path)
    token = client.post(
        "/api/v1/auth/register", json={"phone": "13900003333", "password": "pass1234"}
    ).json()["data"]["token"]
    auth = {"Authorization": f"Bearer {token}"}
    denied = client.post("/api/v1/pay/claim", data={"plan": "month"}, files={"file": ("a.png", MIN_PNG, "image/png")})
    assert denied.status_code == 401
    bad = client.post(
        "/api/v1/pay/claim",
        data={"plan": "month"},
        files={"file": ("a.bin", b"not-an-image", "application/octet-stream")},
        headers=auth,
    )
    assert bad.status_code == 400
    ok = client.post(
        "/api/v1/pay/claim",
        data={"plan": "month", "note": "已转39"},
        files={"file": ("shot.png", MIN_PNG, "image/png")},
        headers=auth,
    )
    assert ok.status_code == 200, ok.text
    claim = ok.json()["data"]
    assert claim["status"] == "pending"
    assert claim["plan"] == "month"
    assert claim["note"] == "已转39"
    mine = client.get("/api/v1/pay/claim/mine", headers=auth)
    assert mine.json()["data"]["id"] == claim["id"]
    img = client.get(f"/api/v1/pay/claim/{claim['id']}/image", headers=auth)
    assert img.status_code == 200
    assert img.content.startswith(b"\x89PNG")
    listed = client.get("/api/v1/admin/claims", headers={"X-Admin-Key": ADMIN})
    assert listed.status_code == 200
    assert listed.json()["data"][0]["username"] == "13900003333"
    admin_img = client.get(
        f"/api/v1/admin/claims/{claim['id']}/image",
        headers={"X-Admin-Key": ADMIN},
    )
    assert admin_img.status_code == 200
    reviewed = client.post(
        f"/api/v1/admin/claims/{claim['id']}/review",
        json={"admin_key": ADMIN, "action": "approve"},
    )
    assert reviewed.status_code == 200, reviewed.text
    db = Session()
    try:
        user = db.query(UserConfig).filter(UserConfig.user_id == "13900003333").first()
        assert is_member(user)
    finally:
        db.close()
    me = client.get("/api/v1/auth/me", headers=auth)
    assert me.json()["data"]["membership"]["is_member"] is True
    again = client.post(
        f"/api/v1/admin/claims/{claim['id']}/review",
        json={"admin_key": ADMIN, "action": "approve"},
    )
    assert again.status_code == 400


def test_claim_reject(tmp_path, monkeypatch):
    monkeypatch.setattr("app.services.pay_claim.CLAIMS_DIR", tmp_path)
    client, Session = _client(tmp_path)
    token = client.post(
        "/api/v1/auth/register", json={"phone": "13900004444", "password": "pass1234"}
    ).json()["data"]["token"]
    auth = {"Authorization": f"Bearer {token}"}
    ok = client.post(
        "/api/v1/pay/claim",
        data={"plan": "year"},
        files={"file": ("shot.png", MIN_PNG, "image/png")},
        headers=auth,
    )
    claim_id = ok.json()["data"]["id"]
    client.post(
        f"/api/v1/admin/claims/{claim_id}/review",
        json={"admin_key": ADMIN, "action": "reject"},
    )
    db = Session()
    try:
        user = db.query(UserConfig).filter(UserConfig.user_id == "13900004444").first()
        assert not is_member(user)
    finally:
        db.close()
    mine = client.get("/api/v1/pay/claim/mine", headers=auth).json()["data"]
    assert mine["status"] == "rejected"
