from datetime import datetime

from sqlalchemy import DateTime, Float, Integer, String, Text, func
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base


class StockDividendProfile(Base):
    """个股分红历史派生档案（读层「真高股息」判据的数据底座）。

    为什么不塞进 ``factor_snapshots.payload``：
      分红历史来自东财实时接口（``dividend_data.fetch_dividend_history``），
      原本只在分析期取用、不落地；而「连续分红年限 / 近三年年均股息率」是
      **读层筛选**要用的字段。要进 payload 就得改评分口径版本号 + 全库重建
      （约 3 天），建独立小表可独立回补与刷新，且完全不碰分数。

    为什么只存「每股分红」不存「股息率」：
      股息率 = 每股分红 ÷ 股价，股价是每日变的行情量。表里存与股价无关的
      ``dps_3y_avg``，股息率在读层用**当前快照价**现算（``market_scan.
      yield_3y_avg_of``）→ 行情更新后自动新鲜，不必等重建。
    """

    __tablename__ = "stock_dividend_profile"

    symbol: Mapped[str] = mapped_column(String(20), primary_key=True)
    # 连续现金分红年数（东财口径，含最近一个完整会计年度）
    consecutive_years: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    latest_fy: Mapped[int | None] = mapped_column(Integer, nullable=True)
    dps_latest_fy: Mapped[float | None] = mapped_column(Float, nullable=True)
    # 近 3 个完整会计年度的年均每股分红（元/股，含税）
    dps_3y_avg: Mapped[float | None] = mapped_column(Float, nullable=True)
    # {会计年度: 该年度每股分红合计} 的 JSON 文本（保留近 6 个完整年度）
    dps_series: Mapped[str | None] = mapped_column(Text, nullable=True)
    source: Mapped[str | None] = mapped_column(String(32), nullable=True)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), onupdate=func.now(), nullable=False
    )
