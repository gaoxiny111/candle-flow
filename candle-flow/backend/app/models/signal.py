from datetime import date, datetime

from sqlalchemy import Date, DateTime, Index, Integer, Numeric, String, Text, func
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base


class TradingSignal(Base):
    __tablename__ = "trading_signals"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    symbol: Mapped[str] = mapped_column(String(20), nullable=False)
    signal_type: Mapped[str] = mapped_column(String(10), nullable=False)
    signal_level: Mapped[str] = mapped_column(String(10), nullable=False)
    pattern_name: Mapped[str] = mapped_column(String(50), nullable=False)
    pattern_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    pattern_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    confluence_count: Mapped[int | None] = mapped_column(Integer, nullable=True)
    confluence_hits: Mapped[str | None] = mapped_column(String(200), nullable=True)
    confluence_detail: Mapped[str | None] = mapped_column(Text, nullable=True)
    entry_price: Mapped[float] = mapped_column(Numeric(10, 4), nullable=False)
    stop_loss: Mapped[float] = mapped_column(Numeric(10, 4), nullable=False)
    take_profit_1: Mapped[float | None] = mapped_column(Numeric(10, 4), nullable=True)
    take_profit_2: Mapped[float | None] = mapped_column(Numeric(10, 4), nullable=True)
    risk_reward_ratio: Mapped[float] = mapped_column(Numeric(5, 2), nullable=False)
    position_size: Mapped[int] = mapped_column(Integer, nullable=False)
    capital_at_risk: Mapped[float] = mapped_column(Numeric(12, 2), nullable=False)
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="pending")
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
    confirmed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    closed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    close_price: Mapped[float | None] = mapped_column(Numeric(10, 4), nullable=True)
    pnl: Mapped[float | None] = mapped_column(Numeric(12, 2), nullable=True)
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)

    __table_args__ = (
        Index("ix_signal_status", "status"),
        Index("ix_signal_symbol_created", "symbol", "created_at"),
    )
