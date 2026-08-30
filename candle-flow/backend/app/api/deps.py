from fastapi import Depends, Header, HTTPException
from sqlalchemy.orm import Session

from app.database import get_db
from app.models.user_config import UserConfig


def parse_bearer(authorization: str | None) -> str | None:
    if not authorization:
        return None
    raw = authorization.strip()
    if raw.lower().startswith("bearer "):
        return raw[7:].strip() or None
    return raw or None


def get_optional_user(
    db: Session = Depends(get_db),
    authorization: str | None = Header(default=None),
) -> UserConfig | None:
    token = parse_bearer(authorization)
    if not token:
        return None
    return (
        db.query(UserConfig)
        .filter(UserConfig.auth_token == token, UserConfig.is_active.is_(True))
        .first()
    )


def require_user(user: UserConfig | None = Depends(get_optional_user)) -> UserConfig:
    if not user:
        raise HTTPException(status_code=401, detail="请先登录")
    return user
