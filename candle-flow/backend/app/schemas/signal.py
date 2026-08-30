from datetime import date, datetime
from decimal import Decimal
from typing import Any, Literal, Optional

from pydantic import field_validator

from app.schemas.common import BaseSchema


class ConfluenceHitOut(BaseSchema):
    name: str
    detail: str = ""


class SignalOut(BaseSchema):
    id: int
    symbol: str
    signal_type: str
    signal_level: str
    pattern_name: str
    pattern_date: Optional[date] = None
    pattern_id: Optional[int] = None
    pattern_direction: Optional[str] = None
    confluence_count: Optional[int] = None
    confluence_hits: Optional[str] = None
    confluence_detail: Optional[list[ConfluenceHitOut]] = None
    entry_price: Decimal
    stop_loss: Decimal
    take_profit_1: Optional[Decimal] = None
    take_profit_2: Optional[Decimal] = None
    risk_reward_ratio: Decimal
    position_size: int
    capital_at_risk: Decimal
    status: str
    last_price: Optional[Decimal] = None
    prev_close: Optional[Decimal] = None
    change_amount: Optional[Decimal] = None
    change_pct: Optional[Decimal] = None
    quote_date: Optional[date] = None
    created_at: datetime
    confirmed_at: Optional[datetime] = None
    closed_at: Optional[datetime] = None
    close_price: Optional[Decimal] = None
    pnl: Optional[Decimal] = None
    notes: Optional[str] = None

    @field_validator("confluence_detail", mode="before")
    @classmethod
    def parse_confluence_detail(cls, value: Any):
        if value is None or value == "":
            return None
        if isinstance(value, str):
            import json

            try:
                parsed = json.loads(value)
            except json.JSONDecodeError:
                return None
            return parsed
        return value


class SignalConfirmRequest(BaseSchema):
    signal_id: int
    action: Literal["confirm", "dismiss"]
