from datetime import date, datetime
from decimal import Decimal
from typing import Optional

from app.schemas.common import BaseSchema


class PatternOut(BaseSchema):
    id: int
    symbol: str
    pattern_name: str
    direction: str
    score: Decimal
    candle_date: date
    prev_trend: Optional[str] = None
    confirmation_status: str
    created_at: datetime


class PatternScanRequest(BaseSchema):
    symbol: str
    lookback_days: int = 60


class PatternScanResponse(BaseSchema):
    found_count: int


class WatchlistScanFailed(BaseSchema):
    symbol: str
    error: str


class WatchlistScanResponse(BaseSchema):
    scanned: int
    found_count: int
    failed: list[WatchlistScanFailed] = []
