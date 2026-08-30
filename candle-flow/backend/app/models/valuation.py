from datetime import datetime

from sqlalchemy import DateTime, String, Text, func
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base


class ValuationHistory(Base):
    """Cached PE / PB time series for percentile (survives process restart)."""

    __tablename__ = "valuation_history"

    symbol: Mapped[str] = mapped_column(String(20), primary_key=True)
    pe_json: Mapped[str] = mapped_column(Text, nullable=False, default="[]")
    pb_json: Mapped[str] = mapped_column(Text, nullable=False, default="[]")
    updated_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
