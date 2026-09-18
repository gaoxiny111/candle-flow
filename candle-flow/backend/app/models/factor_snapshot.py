from datetime import datetime

from sqlalchemy import DateTime, Float, Text, func
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy import String

from app.database import Base


class FactorSnapshot(Base):
    """盘后预构建的因子快照，存储 run_full_analysis 完整结果。"""

    __tablename__ = "factor_snapshots"

    symbol: Mapped[str] = mapped_column(String(20), primary_key=True)
    composite_score: Mapped[float | None] = mapped_column(Float, index=True, nullable=True)
    pe_ttm: Mapped[float | None] = mapped_column(Float, nullable=True)
    payload: Mapped[str] = mapped_column(Text, nullable=False)
    built_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now(), onupdate=func.now(), index=True)
