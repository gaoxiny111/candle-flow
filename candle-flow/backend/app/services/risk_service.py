from decimal import Decimal, ROUND_DOWN
from typing import List, Tuple

from sqlalchemy.orm import Session

from app.models.signal import TradingSignal
from app.schemas.risk import RiskCalculateResponse


class RiskService:
    LOT_SIZE = 100

    def calculate(
        self,
        entry_price: Decimal,
        stop_loss: Decimal,
        capital: Decimal = Decimal("100000"),
        risk_per_trade: Decimal = Decimal("1.0"),
        take_profit: Decimal | None = None,
    ) -> RiskCalculateResponse:
        if entry_price <= 0 or capital <= 0:
            raise ValueError("entry_price and capital must be positive")
        risk_distance = abs(entry_price - stop_loss)
        if risk_distance == 0:
            raise ValueError("stop_loss must differ from entry_price")

        capital_at_risk = (capital * risk_per_trade / Decimal("100")).quantize(Decimal("0.01"))
        raw_size = capital_at_risk / risk_distance
        position_size = int((raw_size // self.LOT_SIZE) * self.LOT_SIZE)
        if position_size < self.LOT_SIZE:
            position_size = self.LOT_SIZE

        tp1 = take_profit
        tp2 = None
        if take_profit is not None:
            reward = abs(take_profit - entry_price)
            rr = (reward / risk_distance).quantize(Decimal("0.01"))
        else:
            tp1 = entry_price + risk_distance * 2 if entry_price > stop_loss else entry_price - risk_distance * 2
            tp2 = entry_price + risk_distance * 3 if entry_price > stop_loss else entry_price - risk_distance * 3
            rr = Decimal("2.00")

        return RiskCalculateResponse(
            position_size=position_size,
            risk_reward_ratio=rr,
            capital_at_risk=capital_at_risk,
            risk_distance=risk_distance.quantize(Decimal("0.0001")),
            take_profit_1=tp1.quantize(Decimal("0.0001")) if tp1 else None,
            take_profit_2=tp2.quantize(Decimal("0.0001")) if tp2 else None,
        )

    def get_history(self, db: Session, page: int = 1, page_size: int = 20) -> Tuple[List[TradingSignal], int]:
        q = db.query(TradingSignal).filter(TradingSignal.status.in_(["confirmed", "closed", "active"]))
        total = q.count()
        items = (
            q.order_by(TradingSignal.created_at.desc())
            .offset((page - 1) * page_size)
            .limit(page_size)
            .all()
        )
        return items, total
