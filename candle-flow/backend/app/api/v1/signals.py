from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session

from app.api.deps import get_optional_user
from app.database import get_db
from app.models.user_config import UserConfig
from app.schemas.common import ApiResponse, ResponseMeta
from app.schemas.signal import SignalConfirmRequest, SignalOut
from app.services.kline_service import KlineService
from app.services.signal_service import SignalService
from app.services.watchlist import parse_watchlist

router = APIRouter()


def _watchlist_symbols(user: UserConfig | None, fallback: str | None) -> list[str]:
    if user:
        return parse_watchlist(user.watchlist)
    if fallback:
        return [s.strip().upper() for s in fallback.split(",") if s.strip()]
    return []


@router.get("/signals")
def list_signals(
    symbol: Optional[str] = None,
    status: Optional[str] = None,
    page: int = 1,
    page_size: int = 20,
    watchlist_only: bool = Query(False),
    symbols: Optional[str] = None,
    db: Session = Depends(get_db),
    user: UserConfig | None = Depends(get_optional_user),
):
    svc = SignalService(db)
    symbol_list = None
    if watchlist_only:
        symbol_list = _watchlist_symbols(user, symbols)
        page_size = min(max(page_size, 100), 200)
    if symbol:
        svc.expire_reached_pending(symbol)
        svc.purge_outlier_signals(symbol)
    svc.refresh_open_positions(None if symbol_list is not None else symbol)
    items, total = svc.get_signals(symbol, status, page, page_size, symbols=symbol_list)
    quotes: dict = {}
    kline_svc = KlineService(db)
    for item in items:
        if item.symbol not in quotes:
            quotes[item.symbol] = kline_svc.get_quote(item.symbol)
    return ApiResponse(
        data=[svc.to_signal_out(i, quotes.get(i.symbol)) for i in items],
        meta=ResponseMeta(page=page, page_size=page_size, total=total),
    )


@router.post("/signals/confirm")
def confirm_signal(body: SignalConfirmRequest, db: Session = Depends(get_db)):
    svc = SignalService(db)
    signal = svc.confirm(body.signal_id, body.action)
    if not signal:
        raise HTTPException(status_code=404, detail="signal not found")
    quote = KlineService(db).get_quote(signal.symbol)
    return ApiResponse(data=svc.to_signal_out(signal, quote))


@router.get("/signals/{signal_id}")
def get_signal(signal_id: int, db: Session = Depends(get_db)):
    svc = SignalService(db)
    signal = svc.get_by_id(signal_id)
    if not signal:
        raise HTTPException(status_code=404, detail="signal not found")
    quote = KlineService(db).get_quote(signal.symbol)
    return ApiResponse(data=svc.to_signal_out(signal, quote))
