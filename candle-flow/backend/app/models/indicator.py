from datetime import date, datetime

from sqlalchemy import BigInteger, Date, DateTime, Index, Integer, Numeric, String, func
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base


class Indicator(Base):
    __tablename__ = "indicators"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    symbol: Mapped[str] = mapped_column(String(20), nullable=False)
    date: Mapped[date] = mapped_column(Date, nullable=False)
    indicator_type: Mapped[str] = mapped_column(String(20), nullable=False)
    period: Mapped[int] = mapped_column(Integer, nullable=False)
    value: Mapped[float] = mapped_column(Numeric(12, 4), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())

    __table_args__ = (
        Index("ix_indicator_symbol_type_date", "symbol", "indicator_type", "date"),
    )
