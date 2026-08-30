"""Manual pay proof: user uploads a screenshot, admin reviews on /admin."""

from __future__ import annotations

import secrets
from datetime import datetime, timezone
from pathlib import Path

from fastapi import UploadFile

from app.models.payment_claim import PaymentClaim
from app.services.membership import PLAN_LABELS
from app.services.payment import PAY_PLANS, plan_amount

CLAIMS_DIR = Path(__file__).resolve().parents[2] / "data" / "pay" / "claims"
MAX_BYTES = 5 * 1024 * 1024
PENDING = "pending"
APPROVED = "approved"
REJECTED = "rejected"
STATUS_LABELS = {PENDING: "待开通", APPROVED: "已开通", REJECTED: "已驳回"}


def _utcnow() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def sniff_ext(data: bytes) -> str:
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return ".png"
    if data.startswith(b"\xff\xd8\xff"):
        return ".jpg"
    if len(data) >= 12 and data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return ".webp"
    raise ValueError("请上传 JPG / PNG / WebP 截图")


def claim_path(filename: str) -> Path:
    return CLAIMS_DIR / filename


def delete_claim_file(filename: str | None) -> None:
    if not filename:
        return
    path = claim_path(filename)
    if path.is_file():
        path.unlink()


def save_upload(file: UploadFile) -> str:
    raw = file.file.read(MAX_BYTES + 1)
    if not raw:
        raise ValueError("请选择付款截图")
    if len(raw) > MAX_BYTES:
        raise ValueError("截图不能超过 5MB")
    ext = sniff_ext(raw)
    CLAIMS_DIR.mkdir(parents=True, exist_ok=True)
    name = f"{secrets.token_hex(16)}{ext}"
    claim_path(name).write_bytes(raw)
    return name


def claim_out(row: PaymentClaim) -> dict:
    created = row.created_at
    return {
        "id": row.id,
        "username": row.user_id,
        "plan": row.plan,
        "plan_label": PLAN_LABELS.get(row.plan, row.plan),
        "amount": str(row.amount),
        "note": row.note or "",
        "status": row.status,
        "status_label": STATUS_LABELS.get(row.status, row.status),
        "created_at": created.isoformat(sep=" ", timespec="seconds") if created else None,
        "has_image": bool(row.image_file),
    }


def upsert_pending(user_id: str, plan: str, note: str, image_file: str, existing: PaymentClaim | None) -> PaymentClaim:
    if plan not in PAY_PLANS:
        raise ValueError("请选择月卡、年卡或终身")
    amount = plan_amount(plan)
    text = (note or "").strip()[:200]
    if existing:
        delete_claim_file(existing.image_file)
        existing.plan = plan
        existing.amount = amount
        existing.note = text
        existing.image_file = image_file
        existing.status = PENDING
        existing.reviewed_at = None
        existing.review_note = None
        existing.created_at = _utcnow()
        return existing
    return PaymentClaim(
        user_id=user_id,
        plan=plan,
        amount=amount,
        note=text,
        image_file=image_file,
        status=PENDING,
    )
