from datetime import date, datetime

from sqlalchemy import Date, DateTime, Index, Integer, Numeric, String, func
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base


class PatternRecord(Base):
    __tablename__ = "pattern_records"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    symbol: Mapped[str] = mapped_column(String(20), nullable=False)
    pattern_name: Mapped[str] = mapped_column(String(50), nullable=False)
    direction: Mapped[str] = mapped_column(String(10), nullable=False)
    score: Mapped[float] = mapped_column(Numeric(5, 2), nullable=False, default=0.0)
    candle_date: Mapped[date] = mapped_column(Date, nullable=False)
    prev_trend: Mapped[str | None] = mapped_column(String(10), nullable=True)
    confirmation_status: Mapped[str] = mapped_column(String(20), nullable=False, default="pending")
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())

    __table_args__ = (
        Index("ix_pattern_symbol_date", "symbol", "candle_date"),
        Index("ix_pattern_direction", "direction"),
    )
