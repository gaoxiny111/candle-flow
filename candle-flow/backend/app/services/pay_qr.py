"""Payment QR images for manual WeChat / Alipay transfer."""

from __future__ import annotations

from pathlib import Path

from app.config import settings

PAY_DIR = Path(__file__).resolve().parents[2] / "data" / "pay"


def _write_qr(path: Path, payload: str) -> None:
    import qrcode

    text = payload.strip()
    if not text:
        return
    qr = qrcode.QRCode(error_correction=qrcode.constants.ERROR_CORRECT_M, box_size=10, border=4)
    qr.add_data(text)
    qr.make(fit=True)
    img = qr.make_image(fill_color="black", back_color="white")
    path.parent.mkdir(parents=True, exist_ok=True)
    img.save(path)


def ensure_pay_qrs() -> None:
    PAY_DIR.mkdir(parents=True, exist_ok=True)
    wechat = (settings.membership_wechat or "").strip()
    alipay = (settings.membership_alipay_hint or "").strip()
    contact = PAY_DIR / "wechat-contact.png"
    alipay_contact = PAY_DIR / "alipay-contact.png"
    if wechat:
        _write_qr(contact, wechat)
    elif contact.exists():
        contact.unlink()
    if alipay:
        _write_qr(alipay_contact, alipay)
    elif alipay_contact.exists():
        alipay_contact.unlink()


def _url_if_exists(*names: str) -> str:
    for name in names:
        if (PAY_DIR / name).is_file():
            return f"/pay/{name}"
    return ""


def pay_qr_urls() -> tuple[str, str]:
    """Prefer uploaded 收款码 screenshots; otherwise QR of wechat/alipay text."""
    wechat = _url_if_exists("wechat.png", "wechat.jpg", "wechat.jpeg", "wechat-contact.png")
    alipay = _url_if_exists("alipay.png", "alipay.jpg", "alipay.jpeg", "alipay-contact.png")
    return wechat, alipay
