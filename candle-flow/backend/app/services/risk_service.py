from decimal import Decimal, ROUND_CEILING, ROUND_DOWN, ROUND_HALF_UP
from typing import List, Literal, Tuple

from sqlalchemy.orm import Session

from app.core.nison_rules import MIN_RISK_REWARD
from app.models.signal import TradingSignal
from app.schemas.risk import RiskCalculateResponse

LotRound = Literal["up", "down"]


class RiskService:
    LOT_SIZE = 100

    def calculate(
        self,
        entry_price: Decimal,
        stop_loss: Decimal,
        capital: Decimal = Decimal("100000"),
        risk_per_trade: Decimal = Decimal("1.0"),
        take_profit: Decimal | None = None,
        lot_round: LotRound = "up",
        position_factor: Decimal = Decimal("1"),
    ) -> RiskCalculateResponse:
        if entry_price <= 0 or capital <= 0:
            raise ValueError("entry_price and capital must be positive")
        risk_distance = abs(entry_price - stop_loss)
        if risk_distance == 0:
            raise ValueError("stop_loss must differ from entry_price")

        factor = position_factor if position_factor > 0 else Decimal("1")
        if factor > 1:
            factor = Decimal("1")

        capital_at_risk = (capital * risk_per_trade / Decimal("100")).quantize(Decimal("0.01"))
        raw_shares = (capital_at_risk / risk_distance) * factor
        lots = raw_shares / Decimal(self.LOT_SIZE)
        rounding = ROUND_CEILING if lot_round == "up" else ROUND_DOWN
        n_lots = int(lots.to_integral_value(rounding=rounding))
        position_size = n_lots * self.LOT_SIZE
        if position_size < self.LOT_SIZE:
            position_size = self.LOT_SIZE

        assumed_2r = (
            entry_price + risk_distance * 2 if entry_price > stop_loss else entry_price - risk_distance * 2
        ).quantize(Decimal("0.0001"))
        assumed_3r = (
            entry_price + risk_distance * 3 if entry_price > stop_loss else entry_price - risk_distance * 3
        ).quantize(Decimal("0.0001"))

        if take_profit is not None:
            reward = abs(take_profit - entry_price)
            rr = (reward / risk_distance).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
            rr_source = "target"
            tp1 = take_profit.quantize(Decimal("0.0001"))
            tp2 = assumed_3r
        else:
            rr = Decimal("2.00")
            rr_source = "assumed_2r"
            tp1 = assumed_2r
            tp2 = assumed_3r

        min_rr = Decimal(str(MIN_RISK_REWARD))
        notional = (Decimal(position_size) * entry_price).quantize(Decimal("0.01"))
        capital_pct = (notional / capital * Decimal("100")).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
        return RiskCalculateResponse(
            position_size=position_size,
            risk_reward_ratio=rr,
            capital_at_risk=capital_at_risk,
            risk_distance=risk_distance.quantize(Decimal("0.0001")),
            take_profit_1=tp1,
            take_profit_2=tp2,
            rr_source=rr_source,
            rr_meets_min=rr >= min_rr,
            assumed_2r=assumed_2r,
            raw_shares=raw_shares.quantize(Decimal("0.01")),
            lot_round=lot_round,
            position_factor=factor,
            position_capital_pct=capital_pct,
            position_notional=notional,
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
