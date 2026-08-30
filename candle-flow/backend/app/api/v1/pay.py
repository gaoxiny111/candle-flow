import secrets

from fastapi import APIRouter, Depends, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, PlainTextResponse
from pydantic import BaseModel
from sqlalchemy.orm import Session

from app.api.deps import require_user
from app.config import settings
from app.database import get_db
from app.models.payment import PaymentOrder
from app.models.payment_claim import PaymentClaim
from app.models.user_config import UserConfig
from app.schemas.common import ApiResponse
from app.services.membership import PLAN_LABELS
from app.services.pay_claim import (
    PENDING as CLAIM_PENDING,
    claim_out,
    claim_path,
    delete_claim_file,
    save_upload,
    upsert_pending,
)
from app.services.payment import (
    PAID,
    PAY_PLANS,
    PENDING,
    amounts_match,
    channel_ready,
    create_payment,
    fulfill_order,
    plan_amount,
    secret_for_appid,
    xh_hash,
)

router = APIRouter()


class CheckoutBody(BaseModel):
    plan: str
    channel: str = "wechat"


def _public_base() -> str:
    return (settings.public_base_url or "https://candle-flow.online").rstrip("/")


def _order_out(order: PaymentOrder) -> dict:
    return {
        "trade_order_id": order.trade_order_id,
        "plan": order.plan,
        "channel": order.channel,
        "amount": str(order.amount),
        "status": order.status,
        "pay_url": order.pay_url,
        "qrcode_url": order.qrcode_url,
        "paid": order.status == PAID,
    }


@router.post("/pay/checkout")
def pay_checkout(body: CheckoutBody, user: UserConfig = Depends(require_user), db: Session = Depends(get_db)):
    plan = body.plan.strip().lower()
    channel = body.channel.strip().lower()
    if plan not in PAY_PLANS:
        raise HTTPException(status_code=400, detail="请选择月卡、年卡或终身")
    if channel not in ("wechat", "alipay"):
        raise HTTPException(status_code=400, detail="请选择微信支付或支付宝")
    if not channel_ready(channel):
        raise HTTPException(status_code=400, detail="在线支付未开通。请在虎皮椒注册后把 APPID/密钥写入 .env")
    try:
        amount = plan_amount(plan)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    trade_id = f"cf{secrets.token_hex(8)}"
    title = f"Candle Flow{PLAN_LABELS.get(plan, plan)}"
    base = _public_base()
    try:
        paid = create_payment(
            trade_order_id=trade_id,
            amount=amount,
            title=title,
            channel=channel,
            notify_url=f"{base}/api/v1/pay/notify",
            return_url=f"{base}/settings",
        )
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"支付通道暂时不可用：{e}") from e
    order = PaymentOrder(
        trade_order_id=trade_id,
        user_id=user.user_id,
        plan=plan,
        channel=channel,
        amount=amount,
        status=PENDING,
        pay_url=paid.get("url") or None,
        qrcode_url=paid.get("url_qrcode") or None,
    )
    db.add(order)
    db.commit()
    db.refresh(order)
    return ApiResponse(data=_order_out(order))


@router.get("/pay/order/{trade_order_id}")
def pay_order_status(trade_order_id: str, user: UserConfig = Depends(require_user), db: Session = Depends(get_db)):
    order = db.query(PaymentOrder).filter(PaymentOrder.trade_order_id == trade_order_id).first()
    if not order or order.user_id != user.user_id:
        raise HTTPException(status_code=404, detail="订单不存在")
    return ApiResponse(data=_order_out(order))


@router.post("/pay/notify")
async def pay_notify(request: Request, db: Session = Depends(get_db)):
    ctype = (request.headers.get("content-type") or "").lower()
    if "json" in ctype:
        raw = await request.json()
        data = {str(k): "" if v is None else str(v) for k, v in dict(raw).items()}
    else:
        form = await request.form()
        data = {str(k): str(v) for k, v in form.items()}
    got = (data.get("hash") or "").strip().lower()
    secret = secret_for_appid(data.get("appid") or "")
    if not secret or not got or xh_hash(data, secret) != got:
        return PlainTextResponse("fail", status_code=400)
    if (data.get("status") or "").upper() != "OD":
        return PlainTextResponse("success")
    trade_id = data.get("trade_order_id") or ""
    order = db.query(PaymentOrder).filter(PaymentOrder.trade_order_id == trade_id).first()
    if not order:
        return PlainTextResponse("fail", status_code=404)
    if not amounts_match(order.amount, data.get("total_fee") or ""):
        return PlainTextResponse("fail", status_code=400)
    user = db.query(UserConfig).filter(UserConfig.user_id == order.user_id).first()
    if not user:
        return PlainTextResponse("fail", status_code=404)
    fulfill_order(order, user, data.get("transaction_id"))
    db.commit()
    return PlainTextResponse("success")


@router.post("/pay/claim")
def submit_pay_claim(
    plan: str = Form(...),
    note: str = Form(""),
    file: UploadFile = File(...),
    user: UserConfig = Depends(require_user),
    db: Session = Depends(get_db),
):
    existing = (
        db.query(PaymentClaim)
        .filter(PaymentClaim.user_id == user.user_id, PaymentClaim.status == CLAIM_PENDING)
        .order_by(PaymentClaim.id.desc())
        .first()
    )
    image_file = None
    try:
        image_file = save_upload(file)
        row = upsert_pending(user.user_id, plan.strip().lower(), note, image_file, existing)
    except ValueError as e:
        if image_file:
            delete_claim_file(image_file)
        raise HTTPException(status_code=400, detail=str(e)) from e
    if existing is None:
        db.add(row)
    db.commit()
    db.refresh(row)
    return ApiResponse(data=claim_out(row))


@router.get("/pay/claim/mine")
def my_pay_claim(user: UserConfig = Depends(require_user), db: Session = Depends(get_db)):
    row = db.query(PaymentClaim).filter(PaymentClaim.user_id == user.user_id).order_by(PaymentClaim.id.desc()).first()
    return ApiResponse(data=claim_out(row) if row else None)


@router.get("/pay/claim/{claim_id}/image")
def my_pay_claim_image(
    claim_id: int,
    user: UserConfig = Depends(require_user),
    db: Session = Depends(get_db),
):
    row = db.query(PaymentClaim).filter(PaymentClaim.id == claim_id, PaymentClaim.user_id == user.user_id).first()
    if not row:
        raise HTTPException(status_code=404, detail="凭证不存在")
    path = claim_path(row.image_file)
    if not path.is_file():
        raise HTTPException(status_code=404, detail="截图不存在")
    return FileResponse(path)
