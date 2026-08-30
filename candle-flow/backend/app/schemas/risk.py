from decimal import Decimal

from app.schemas.common import BaseSchema


class RiskCalculateRequest(BaseSchema):
    entry_price: Decimal
    stop_loss: Decimal
    capital: Decimal = Decimal("100000")
    risk_per_trade: Decimal = Decimal("1.0")
    take_profit: Decimal | None = None


class RiskCalculateResponse(BaseSchema):
    position_size: int
    risk_reward_ratio: Decimal
    capital_at_risk: Decimal
    risk_distance: Decimal
    take_profit_1: Decimal | None = None
    take_profit_2: Decimal | None = None
