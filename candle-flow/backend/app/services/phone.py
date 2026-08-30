"""Account identity helpers (phone or username)."""

from __future__ import annotations

import re

_PHONE_RE = re.compile(r"^1[3-9]\d{9}$")
_USER_RE = re.compile(r"^[\w\u4e00-\u9fff-]{2,20}$")


def normalize_phone(raw: str) -> str:
    """Normalize to 11-digit CN mobile; raise ValueError if invalid."""
    compact = re.sub(r"[\s\-()]", "", (raw or "").strip())
    if compact.startswith("+86"):
        compact = compact[3:]
    elif compact.startswith("86") and len(re.sub(r"\D", "", compact)) == 13:
        compact = compact[2:]
    if not compact.isdigit() or not _PHONE_RE.fullmatch(compact):
        raise ValueError("请输入11位中国大陆手机号")
    return compact


def normalize_account(raw: str) -> str:
    """Phone number if it is one; otherwise a 2–20 char username. Do not strip letters."""
    text = (raw or "").strip()
    if not text:
        raise ValueError("请输入手机号或用户名")
    try:
        return normalize_phone(text)
    except ValueError:
        pass
    if text.lower() == "default" or not _USER_RE.fullmatch(text):
        raise ValueError("请输入11位中国大陆手机号，或2–20位用户名")
    return text


def mask_phone(phone: str) -> str:
    if len(phone) == 11 and phone.isdigit():
        return f"{phone[:3]}****{phone[7:]}"
    return phone
