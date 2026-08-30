from decimal import Decimal

from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

import app.models  # noqa: F401
from app.api.v1 import pay, system
from app.config import settings
from app.database import Base, get_db
from app.models.payment import PaymentOrder
from app.models.user_config import UserConfig
from app.services.membership import is_member
from app.services.payment import xh_hash


def _client(secret: str = "test-pay-secret") -> tuple[TestClient, sessionmaker]:
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    TestingSession = sessionmaker(autocommit=False, autoflush=False, bind=engine)
    Base.metadata.create_all(engine)
    settings.xunhupay_wechat_appid = "wx-app"
    settings.xunhupay_wechat_secret = secret
    settings.xunhupay_alipay_appid = ""
    settings.xunhupay_alipay_secret = ""
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


def test_xh_hash_stable():
    h = xh_hash({"b": "2", "a": "1", "hash": "ignore", "empty": ""}, "key")
    assert h == xh_hash({"a": "1", "b": "2"}, "key")
    assert len(h) == 32


def test_offer_shows_online_flags():
    client, _ = _client()
    offer = client.get("/api/v1/membership/offer").json()["data"]
    assert offer["online_wechat"] is True
    assert offer["online_alipay"] is False


def test_checkout_without_login_401():
    client, _ = _client()
    res = client.post("/api/v1/pay/checkout", json={"plan": "month", "channel": "wechat"})
    assert res.status_code == 401


def test_notify_invalid_hash_rejected():
    client, _ = _client()
    res = client.post(
        "/api/v1/pay/notify",
        data={"trade_order_id": "x", "status": "OD", "total_fee": "39", "hash": "deadbeef", "appid": "wx-app"},
    )
    assert res.status_code == 400


def test_notify_paid_activates_membership():
    client, Session = _client()
    token = client.post(
        "/api/v1/auth/register", json={"phone": "13900001111", "password": "pass1234"}
    ).json()["data"]["token"]
    db = Session()
    try:
        order = PaymentOrder(
            trade_order_id="cfpaytest01",
            user_id="13900001111",
            plan="month",
            channel="wechat",
            amount=Decimal("39"),
            status="pending",
        )
        db.add(order)
        db.commit()
    finally:
        db.close()
    payload = {
        "appid": "wx-app",
        "trade_order_id": "cfpaytest01",
        "total_fee": "39",
        "status": "OD",
        "transaction_id": "tx-1",
        "nonce_str": "n1",
        "time": "1",
    }
    payload["hash"] = xh_hash(payload, "test-pay-secret")
    ok = client.post("/api/v1/pay/notify", data=payload)
    assert ok.status_code == 200
    assert ok.text == "success"
    me = client.get("/api/v1/auth/me", headers={"Authorization": f"Bearer {token}"})
    assert me.json()["data"]["membership"]["is_member"] is True
    db = Session()
    try:
        user = db.query(UserConfig).filter(UserConfig.user_id == "13900001111").first()
        assert is_member(user)
        saved = db.query(PaymentOrder).filter(PaymentOrder.trade_order_id == "cfpaytest01").first()
        assert saved.status == "paid"
    finally:
        db.close()


def test_checkout_mocked_gateway(monkeypatch):
    client, _ = _client()
    token = client.post(
        "/api/v1/auth/register", json={"phone": "13900002222", "password": "pass1234"}
    ).json()["data"]["token"]

    def fake_create(**kwargs):
        return {"errcode": 0, "url": "https://pay.example/u", "url_qrcode": "https://pay.example/qr.png"}

    monkeypatch.setattr("app.api.v1.pay.create_payment", fake_create)
    res = client.post(
        "/api/v1/pay/checkout",
        json={"plan": "month", "channel": "wechat"},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert res.status_code == 200, res.text
    data = res.json()["data"]
    assert data["status"] == "pending"
    assert data["qrcode_url"] == "https://pay.example/qr.png"
    assert data["pay_url"] == "https://pay.example/u"
