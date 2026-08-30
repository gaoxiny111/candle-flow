from datetime import datetime

from sqlalchemy import Boolean, DateTime, Index, Integer, Numeric, String, Text, func
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base


class UserConfig(Base):
    __tablename__ = "user_config"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[str] = mapped_column(String(50), nullable=False)
    risk_per_trade: Mapped[float] = mapped_column(Numeric(5, 2), nullable=False, default=1.0)
    default_symbol: Mapped[str] = mapped_column(String(20), nullable=False, default="000001.SZ")
    preferred_period: Mapped[str] = mapped_column(String(10), nullable=False, default="daily")
    password_hash: Mapped[str | None] = mapped_column(String(128), nullable=True)
    auth_token: Mapped[str | None] = mapped_column(String(64), nullable=True)
    watchlist: Mapped[str | None] = mapped_column(Text, nullable=True)
    membership_plan: Mapped[str] = mapped_column(String(20), nullable=False, default="free")
    membership_expires_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), onupdate=func.now()
    )

    __table_args__ = (Index("ix_user_config_user_id", "user_id", unique=True),)
