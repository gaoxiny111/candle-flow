"""虎皮椒 / 迅虎扫码支付：下单、签名、回调开通会员。"""

from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal, ROUND_HALF_UP
from hashlib import md5
from typing import Any

import httpx

from app.config import settings
from app.models.payment import PaymentOrder
from app.models.user_config import UserConfig
from app.services.membership import PLAN_LABELS, PLAN_LIFETIME, PLAN_MONTH, PLAN_YEAR, activate_membership

PAID = "paid"
PENDING = "pending"
PAY_PLANS = {PLAN_MONTH, PLAN_YEAR, PLAN_LIFETIME}


def _utcnow() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def xh_hash(data: dict[str, Any], secret: str) -> str:
    items = []
    for key in sorted(data.keys()):
        if key == "hash":
            continue
        val = data[key]
        if val is None or val == "":
            continue
        items.append(f"{key}={val}")
    return md5(("&".join(items) + secret).encode("utf-8")).hexdigest()


def plan_amount(plan: str) -> Decimal:
    raw = {
        PLAN_MONTH: settings.membership_price_month,



        
        PLAN_YEAR: settings.membership_price_year,
        PLAN_LIFETIME: settings.membership_price_lifetime,
    }.get(plan, "")
    try:
        amount = Decimal(str(raw).strip())
    except Exception as e:
        raise ValueError("套餐价格未配置") from e
    if amount <= 0:
        raise ValueError("套餐价格无效")
    return amount.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


def channel_ready(channel: str) -> bool:
    appid, secret = channel_creds(channel)
    return bool(appid and secret)


def online_channels() -> dict[str, bool]:
    return {"wechat": channel_ready("wechat"), "alipay": channel_ready("alipay")}


def channel_creds(channel: str) -> tuple[str, str]:
    if channel == "alipay":
        return (settings.xunhupay_alipay_appid.strip(), settings.xunhupay_alipay_secret.strip())
    return (settings.xunhupay_wechat_appid.strip(), settings.xunhupay_wechat_secret.strip())


def secret_for_appid(appid: str) -> str:
    appid = (appid or "").strip()
    if appid and appid == settings.xunhupay_alipay_appid.strip():
        return settings.xunhupay_alipay_secret.strip()
    if appid and appid == settings.xunhupay_wechat_appid.strip():
        return settings.xunhupay_wechat_secret.strip()
    return settings.xunhupay_wechat_secret.strip() or settings.xunhupay_alipay_secret.strip()


def create_payment(
    *,
    trade_order_id: str,
    amount: Decimal,
    title: str,
    channel: str,
    notify_url: str,
    return_url: str,
) -> dict[str, Any]:
    appid, secret = channel_creds(channel)
    if not appid or not secret:
        raise ValueError("该支付方式尚未配置")
    payload: dict[str, Any] = {
        "version": "1.1",
        "appid": appid,
        "trade_order_id": trade_order_id,
        "total_fee": str(amount),
        "title": title,
        "time": int(_utcnow().timestamp()),
        "notify_url": notify_url,
        "return_url": return_url,
        "callback_url": return_url,
        "plugins": "candle-flow",
        "nonce_str": trade_order_id,
    }
    payload["hash"] = xh_hash(payload, secret)
    gateway = (settings.xunhupay_gateway or "https://api.xunhupay.com/payment/do.html").strip()
    with httpx.Client(timeout=20.0) as client:
        res = client.post(gateway, data={k: str(v) for k, v in payload.items()})
        res.raise_for_status()
        data = res.json()
    if int(data.get("errcode") or 0) != 0:
        raise ValueError(str(data.get("errmsg") or "支付下单失败"))
    return data


def amounts_match(expected: Decimal, paid: str) -> bool:
    try:
        got = Decimal(str(paid).strip())
    except Exception:
        return False
    return abs(Decimal(str(got)) - Decimal(str(expected))) <= Decimal("0.01")


def fulfill_order(order: PaymentOrder, user: UserConfig, transaction_id: str | None = None) -> PaymentOrder:
    if order.status == PAID:
        return order
    activate_membership(user, order.plan)
    order.status = PAID
    order.transaction_id = transaction_id or order.transaction_id
    order.paid_at = _utcnow()
    return order
