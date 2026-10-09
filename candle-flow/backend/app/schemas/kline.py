from datetime import date, datetime
from decimal import Decimal
from typing import Optional

from pydantic import Field

from app.schemas.common import BaseSchema


class KlineOut(BaseSchema):
    id: Optional[int] = None
    symbol: str
    date: date
    open: Decimal
    high: Decimal
    low: Decimal
    close: Decimal
    volume: int
    source: str = "akshare"


class KlineSyncRequest(BaseSchema):
    symbol: str
    force: bool = False


class KlineSyncResponse(BaseSchema):
    synced_count: int
    purged: bool = False


class LiveQuoteOut(BaseSchema):
    """盘中实时行情（只读，不写日 K）。"""

    symbol: str
    price: float
    prev_close: Optional[float] = None
    change: Optional[float] = None
    change_pct: Optional[float] = None
    open: Optional[float] = None
    high: Optional[float] = None
    low: Optional[float] = None
    volume: Optional[int] = None
    quote_date: Optional[date] = None
    source: str = "spot"
    as_of: Optional[datetime] = None


class KlineQuery(BaseSchema):
    symbol: str
    start_date: Optional[str] = None
    end_date: Optional[str] = None
    period: str = "daily"
    page: int = 1
    page_size: int = 100
