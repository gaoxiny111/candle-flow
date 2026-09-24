from datetime import date, datetime

from sqlalchemy import Date, DateTime, Index, Integer, Numeric, String, func
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base


class MarketFundFlowDaily(Base):
    """全市场主力资金流日频快照（东方财富 datacenter ``RPT_DMSK_TS_STOCKNEW``）。

    与 ``stock_fund_flow_daily`` 的分工：
      - 本表：**一次请求拿全市场**（500×11 页 ≈ 3.5s），用于全库扫描时的
        资金面展示 + 「持续净流入」判据；但该报表**只提供当日快照，无历史日期**
        （第 12 页为空、按日期 filter 不支持）→ 必须逐日采集累积。
      - ``stock_fund_flow_daily``：单只 push2his 日频，可回溯历史，但受全局
        串行锁约束，只适合按需查单只。

    口径：主力 = 超大单 + 大单（与 `stock_fund_flow.py` 一致，**非同花顺 DDX**）。
    单位：元。**仅展示，不参与打分**（口径铁律 13）；后续积累够历史后，由
    调用方自行决定是否升级为判据。
    """

    __tablename__ = "market_fund_flow_daily"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    symbol: Mapped[str] = mapped_column(String(20), nullable=False)
    date: Mapped[date] = mapped_column(Date, nullable=False)
    main: Mapped[float | None] = mapped_column(Numeric(20, 2))          # PRIME_INFLOW
    super_in: Mapped[float | None] = mapped_column(Numeric(20, 2))      # SUPERDEAL_INFLOW
    big_in: Mapped[float | None] = mapped_column(Numeric(20, 2))        # BIGDEAL_INFLOW
    big_buy_ratio: Mapped[float | None] = mapped_column(Numeric(12, 6))  # BUY_BIGDEAL_RATIO（买入占比，非净占比）
    org_participate: Mapped[float | None] = mapped_column(Numeric(12, 6))  # ORG_PARTICIPATE
    prime_cost: Mapped[float | None] = mapped_column(Numeric(12, 4))    # PRIME_COST
    close: Mapped[float | None] = mapped_column(Numeric(12, 4))
    chg: Mapped[float | None] = mapped_column(Numeric(12, 4))
    source: Mapped[str] = mapped_column(String(20), nullable=False, default="eastmoney_dc")
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), onupdate=func.now()
    )

    __table_args__ = (
        Index("ix_mkt_ff_symbol_date", "symbol", "date", unique=True),
        Index("ix_mkt_ff_date", "date"),
    )
