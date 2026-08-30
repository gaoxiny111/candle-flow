from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session

from app.database import get_db
from app.schemas.common import ApiResponse, ResponseMeta
from app.schemas.risk import RiskCalculateRequest, RiskCalculateResponse
from app.schemas.signal import SignalOut
from app.services.risk_service import RiskService

router = APIRouter()


@router.post("/risk/calculate")
def calculate_risk(body: RiskCalculateRequest):
    svc = RiskService()
    try:
        result = svc.calculate(
            entry_price=body.entry_price,
            stop_loss=body.stop_loss,
            capital=body.capital,
            risk_per_trade=body.risk_per_trade,
            take_profit=body.take_profit,
        )
        return ApiResponse(data=result)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.get("/risk/history")
def risk_history(page: int = 1, page_size: int = 20, db: Session = Depends(get_db)):
    svc = RiskService()
    items, total = svc.get_history(db, page, page_size)
    return ApiResponse(
        data=[SignalOut.model_validate(i) for i in items],
        meta=ResponseMeta(page=page, page_size=page_size, total=total),
    )
