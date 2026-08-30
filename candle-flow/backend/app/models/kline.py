from datetime import date, datetime

from sqlalchemy import BigInteger, Date, DateTime, Index, Integer, Numeric, String, func
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base


class KlineData(Base):
    __tablename__ = "kline_data"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    symbol: Mapped[str] = mapped_column(String(20), nullable=False)
    date: Mapped[date] = mapped_column(Date, nullable=False)
    open: Mapped[float] = mapped_column(Numeric(10, 4), nullable=False)
    high: Mapped[float] = mapped_column(Numeric(10, 4), nullable=False)
    low: Mapped[float] = mapped_column(Numeric(10, 4), nullable=False)
    close: Mapped[float] = mapped_column(Numeric(10, 4), nullable=False)
    volume: Mapped[int] = mapped_column(BigInteger, nullable=False)
    source: Mapped[str] = mapped_column(String(50), nullable=False, default="akshare")
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), onupdate=func.now()
    )

    __table_args__ = (
        Index("ix_kline_symbol_date", "symbol", "date", unique=True),
        Index("ix_kline_date", "date"),
    )
