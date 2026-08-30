"""Manual membership: plans, expiry, watchlist limits, gate helpers."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

from fastapi import HTTPException

from app.models.user_config import UserConfig

PLAN_FREE = "free"
PLAN_MONTH = "month"
PLAN_YEAR = "year"
PLAN_LIFETIME = "lifetime"
VALID_PLANS = {PLAN_FREE, PLAN_MONTH, PLAN_YEAR, PLAN_LIFETIME}

FREE_WATCHLIST = 8
MEMBER_WATCHLIST = 50

PLAN_LABELS = {
    PLAN_FREE: "免费",
    PLAN_MONTH: "月卡",
    PLAN_YEAR: "年卡",
    PLAN_LIFETIME: "终身",
}

_PLAN_DAYS = {
    PLAN_MONTH: 30,
    PLAN_YEAR: 365,
}


def _utcnow() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def is_member(user: UserConfig | None) -> bool:
    if not user:
        return False
    plan = (user.membership_plan or PLAN_FREE).strip().lower()
    if plan == PLAN_FREE:
        return False
    if plan == PLAN_LIFETIME:
        return True
    if plan not in (PLAN_MONTH, PLAN_YEAR):
        return False
    expires = user.membership_expires_at
    if not expires:
        return False
    return expires >= _utcnow()


def watchlist_limit(user: UserConfig | None) -> int:
    return MEMBER_WATCHLIST if is_member(user) else FREE_WATCHLIST


def membership_snapshot(user: UserConfig | None) -> dict[str, Any]:
    if not user:
        return {
            "plan": PLAN_FREE,
            "plan_label": PLAN_LABELS[PLAN_FREE],
            "is_member": False,
            "expires_at": None,
            "watchlist_limit": FREE_WATCHLIST,
        }
    plan = (user.membership_plan or PLAN_FREE).strip().lower()
    if plan not in VALID_PLANS:
        plan = PLAN_FREE
    active = is_member(user)
    # Expired paid plan surfaces as free for UX
    if not active and plan != PLAN_FREE:
        plan = PLAN_FREE
    expires = user.membership_expires_at if active and plan != PLAN_LIFETIME else None
    return {
        "plan": plan if active else PLAN_FREE,
        "plan_label": PLAN_LABELS.get(plan if active else PLAN_FREE, PLAN_LABELS[PLAN_FREE]),
        "is_member": active,
        "expires_at": expires.isoformat(sep=" ", timespec="seconds") if expires else None,
        "watchlist_limit": watchlist_limit(user),
    }


def require_member(user: UserConfig | None) -> UserConfig:
    if not user:
        raise HTTPException(status_code=401, detail="请先登录后再使用会员功能")
    if not is_member(user):
        raise HTTPException(
            status_code=403,
            detail="该功能需要会员。请到「设置」按说明付款后，联系管理员人工开通",
        )
    return user


def activate_membership(user: UserConfig, plan: str, days: int | None = None) -> UserConfig:
    plan = plan.strip().lower()
    if plan not in VALID_PLANS:
        raise ValueError(f"无效套餐: {plan}")
    if plan == PLAN_FREE:
        user.membership_plan = PLAN_FREE
        user.membership_expires_at = None
        return user
    if plan == PLAN_LIFETIME:
        user.membership_plan = PLAN_LIFETIME
        user.membership_expires_at = None
        return user
    add_days = days if days is not None else _PLAN_DAYS[plan]
    if add_days < 1:
        raise ValueError("天数至少为 1")
    now = _utcnow()
    base = now
    if is_member(user) and user.membership_expires_at and user.membership_expires_at > now:
        base = user.membership_expires_at
    user.membership_plan = plan
    user.membership_expires_at = base + timedelta(days=add_days)
    return user
