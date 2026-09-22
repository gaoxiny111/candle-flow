from datetime import date, datetime

from sqlalchemy import Date, DateTime, Index, Integer, Numeric, String, func
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base


class StockFundFlowDaily(Base):
    """个股主力资金日频（东方财富成交结构口径）。

    主力 = 超大单 + 大单；净额单位元。该数据源对新建连接有速率冷却，
    故落库缓存 + 增量同步，展示层优先读库，避免每次页面加载都打外部接口。
    """

    __tablename__ = "stock_fund_flow_daily"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    symbol: Mapped[str] = mapped_column(String(20), nullable=False)
    date: Mapped[date] = mapped_column(Date, nullable=False)
    main: Mapped[float | None] = mapped_column(Numeric(20, 2))
    small: Mapped[float | None] = mapped_column(Numeric(20, 2))
    mid: Mapped[float | None] = mapped_column(Numeric(20, 2))
    large: Mapped[float | None] = mapped_column(Numeric(20, 2))
    xlarge: Mapped[float | None] = mapped_column(Numeric(20, 2))
    main_ratio: Mapped[float | None] = mapped_column(Numeric(10, 4))
    large_ratio: Mapped[float | None] = mapped_column(Numeric(10, 4))
    xlarge_ratio: Mapped[float | None] = mapped_column(Numeric(10, 4))
    close: Mapped[float | None] = mapped_column(Numeric(10, 4))
    chg: Mapped[float | None] = mapped_column(Numeric(10, 4))
    source: Mapped[str] = mapped_column(String(20), nullable=False, default="eastmoney")
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), onupdate=func.now()
    )

    __table_args__ = (
        Index("ix_stock_ff_symbol_date", "symbol", "date", unique=True),
        Index("ix_stock_ff_date", "date"),
    )
