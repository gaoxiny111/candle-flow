"""Watchlist limits and legacy membership fields (all features open)."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

from app.models.user_config import UserConfig

PLAN_FREE = "free"
PLAN_MONTH = "month"
PLAN_YEAR = "year"
PLAN_LIFETIME = "lifetime"
VALID_PLANS = {PLAN_FREE, PLAN_MONTH, PLAN_YEAR, PLAN_LIFETIME}

WATCHLIST_LIMIT = 50

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
    return True


def watchlist_limit(user: UserConfig | None) -> int:
    return WATCHLIST_LIMIT


def membership_snapshot(user: UserConfig | None) -> dict[str, Any]:
    return {
        "plan": PLAN_LIFETIME,
        "plan_label": "全部开放",
        "is_member": True,
        "expires_at": None,
        "watchlist_limit": WATCHLIST_LIMIT,
    }


def activate_membership(user: UserConfig, plan: str, days: int | None = None) -> UserConfig:
    """Legacy admin hook; membership gates are disabled."""
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
    if user.membership_expires_at and user.membership_expires_at > now:
        base = user.membership_expires_at
    user.membership_plan = plan
    user.membership_expires_at = base + timedelta(days=add_days)
    return user
