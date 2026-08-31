from datetime import datetime

from sqlalchemy import DateTime, Float, Integer, String, Text, func
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base


class FundamentalCandidate(Base):
    """Layer-1 fundamental screen result (strategic watch pool)."""

    __tablename__ = "fundamental_candidates"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    symbol: Mapped[str] = mapped_column(String(20), nullable=False, index=True)
    name: Mapped[str] = mapped_column(String(64), nullable=False, default="")
    industry: Mapped[str] = mapped_column(String(64), nullable=False, default="")
    themes: Mapped[str] = mapped_column(String(128), nullable=False, default="")
    report_date: Mapped[str] = mapped_column(String(16), nullable=False, default="")
    score: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    roe: Mapped[float | None] = mapped_column(Float, nullable=True)
    roe_years_ok: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    revenue_yoy: Mapped[float | None] = mapped_column(Float, nullable=True)
    profit_yoy: Mapped[float | None] = mapped_column(Float, nullable=True)
    ocf_ps: Mapped[float | None] = mapped_column(Float, nullable=True)
    debt_ratio: Mapped[float | None] = mapped_column(Float, nullable=True)
    pe_ttm: Mapped[float | None] = mapped_column(Float, nullable=True)
    pb: Mapped[float | None] = mapped_column(Float, nullable=True)
    pe_percentile: Mapped[float | None] = mapped_column(Float, nullable=True)
    pb_percentile: Mapped[float | None] = mapped_column(Float, nullable=True)
    peg: Mapped[float | None] = mapped_column(Float, nullable=True)
    checks_json: Mapped[str] = mapped_column(Text, nullable=False, default="[]")
    notes: Mapped[str] = mapped_column(Text, nullable=False, default="")
    pool_run_id: Mapped[str] = mapped_column(String(32), nullable=False, index=True, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
