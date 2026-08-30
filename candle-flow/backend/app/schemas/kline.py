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


class KlineQuery(BaseSchema):
    symbol: str
    start_date: Optional[str] = None
    end_date: Optional[str] = None
    period: str = "daily"
    page: int = 1
    page_size: int = 100
