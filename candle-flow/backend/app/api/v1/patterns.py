from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session

from app.api.deps import get_optional_user
from app.database import get_db
from app.models.user_config import UserConfig
from app.schemas.common import ApiResponse, ResponseMeta
from app.schemas.kline import KlineOut
from app.schemas.pattern import (
    PatternOut,
    PatternScanRequest,
    PatternScanResponse,
    WatchlistScanResponse,
)
from app.services.kline_service import KlineService
from app.services.membership import require_member
from app.services.pattern_service import PatternService
from app.services.signal_service import SignalService
from app.services.watchlist import parse_watchlist

router = APIRouter()


def _watchlist_symbols(user: UserConfig | None, fallback: str | None) -> list[str]:
    if user:
        return parse_watchlist(user.watchlist)
    if fallback:
        return [s.strip().upper() for s in fallback.split(",") if s.strip()]
    return []


@router.get("/patterns")
def list_patterns(
    symbol: Optional[str] = None,
    direction: Optional[str] = None,
    status: Optional[str] = None,
    page: int = 1,
    page_size: int = 20,
    watchlist_only: bool = Query(False),
    symbols: Optional[str] = None,
    db: Session = Depends(get_db),
    user: UserConfig | None = Depends(get_optional_user),
):
    svc = PatternService(db)
    symbol_list = None
    if watchlist_only:
        symbol_list = _watchlist_symbols(user, symbols)
        page_size = min(max(page_size, 100), 200)
    items, total = svc.get_patterns(symbol, direction, status, page, page_size, symbols=symbol_list)
    return ApiResponse(
        data=[PatternOut.model_validate(i) for i in items],
        meta=ResponseMeta(page=page, page_size=page_size, total=total),
    )


@router.post("/patterns/scan")
def scan_patterns(body: PatternScanRequest, db: Session = Depends(get_db)):
    svc = PatternService(db)
    count = svc.scan(body.symbol, body.lookback_days)
    SignalService(db).regenerate(body.symbol)
    return ApiResponse(data=PatternScanResponse(found_count=count))


@router.post("/patterns/scan/watchlist")
def scan_watchlist(
    lookback_days: int = 60,
    symbols: Optional[str] = None,
    db: Session = Depends(get_db),
    user: UserConfig | None = Depends(get_optional_user),
):
    require_member(user)
    watch = _watchlist_symbols(user, symbols)
    if not watch:
        return ApiResponse(
            data=WatchlistScanResponse(scanned=0, found_count=0, failed=[]),
            message="还没有关注股票",
        )
    svc = PatternService(db)
    sig = SignalService(db)
    found = 0
    failed: list[dict] = []
    scanned = 0
    for symbol in watch:
        try:
            found += svc.scan(symbol, lookback_days)
            sig.regenerate(symbol)
            scanned += 1
        except Exception as e:
            db.rollback()
            failed.append({"symbol": symbol, "error": str(e)})
    return ApiResponse(
        data=WatchlistScanResponse(scanned=scanned, found_count=found, failed=failed)
    )


@router.get("/patterns/{pattern_id}")
def get_pattern(pattern_id: int, db: Session = Depends(get_db)):
    svc = PatternService(db)
    pattern = svc.get_by_id(pattern_id)
    if not pattern:
        raise HTTPException(status_code=404, detail="pattern not found")
    kline_svc = KlineService(db)
    klines, _ = kline_svc.get_recent_klines(pattern.symbol, limit=10)
    return ApiResponse(
        data={
            "pattern": PatternOut.model_validate(pattern),
            "klines": [KlineOut.model_validate(k) for k in klines[-5:]],
        }
    )
