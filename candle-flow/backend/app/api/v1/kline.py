from typing import Optional

from fastapi import APIRouter, Depends, Query
from sqlalchemy.orm import Session

from app.core.exceptions import DataSourceError
from app.database import get_db
from app.schemas.common import ApiResponse, ResponseMeta
from app.schemas.kline import KlineOut, KlineSyncRequest, KlineSyncResponse
from app.services.kline_service import KlineService
from app.services.stock_universe import resolve_symbol
from app.utils.symbol import SymbolError

router = APIRouter()


def _resolve_symbol(symbol: str, db: Session | None = None) -> str | ApiResponse:
    try:
        return resolve_symbol(symbol, db)
    except SymbolError as e:
        return ApiResponse(code=400101, message=str(e), data=None)


@router.get("/kline")
def list_kline(
    symbol: str = Query(...),
    start_date: Optional[str] = None,
    end_date: Optional[str] = None,
    period: str = "daily",
    page: int = 1,
    page_size: int = 500,
    refresh: bool = False,
    db: Session = Depends(get_db),
):
    resolved = _resolve_symbol(symbol, db)
    if isinstance(resolved, ApiResponse):
        return resolved
    symbol = resolved

    svc = KlineService(db)
    removed = svc.purge_outliers(symbol)
    items, total = svc.get_klines(symbol, start_date, end_date, page, page_size)
    items = svc.sanitize_rows(items)
    contaminated = svc.is_contaminated(symbol)
    need_sync = total == 0 or refresh or contaminated
    if need_sync:
        try:
            synced, _ = svc.sync(symbol, force=refresh or total == 0 or contaminated)
            items, total = svc.get_klines(symbol, start_date, end_date, page, page_size)
            items = svc.sanitize_rows(items)
        except SymbolError as e:
            if not items:
                return ApiResponse(code=400101, message=str(e), data=None)
        except DataSourceError as e:
            if not items:
                return ApiResponse(code=e.code, message=e.message, data=None)
    # Opening the chart should always try to attach/refresh today's bar (no Sync click).
    merged = svc.merge_today_spot(symbol)
    if not merged and svc.latest_is_stale(symbol):
        merged = svc.ensure_today_bar(symbol)
    if merged:
        items, total = svc.get_klines(symbol, start_date, end_date, page, page_size)
        items = svc.sanitize_rows(items)
    return ApiResponse(
        data=[KlineOut.model_validate(i) for i in items],
        meta=ResponseMeta(page=page, page_size=page_size, total=len(items)),
    )


@router.get("/kline/latest")
def latest_kline(symbol: str = Query(...), db: Session = Depends(get_db)):
    resolved = _resolve_symbol(symbol, db)
    if isinstance(resolved, ApiResponse):
        return resolved
    symbol = resolved

    svc = KlineService(db)
    svc.purge_outliers(symbol)
    item = svc.get_latest(symbol)
    contaminated = svc.is_contaminated(symbol)
    if not item or contaminated:
        try:
            svc.sync(symbol, force=True)
        except SymbolError as e:
            return ApiResponse(code=400101, message=str(e), data=None)
        except DataSourceError as e:
            return ApiResponse(code=e.code, message=e.message, data=None)
        item = svc.get_latest(symbol)
    elif svc.latest_is_stale(symbol):
        svc.ensure_today_bar(symbol)
        item = svc.get_latest(symbol)
    if not item:
        return ApiResponse(code=404101, message="symbol not found", data=None)
    return ApiResponse(data=KlineOut.model_validate(item))


@router.post("/kline/sync")
def sync_kline(body: KlineSyncRequest, db: Session = Depends(get_db)):
    try:
        symbol = resolve_symbol(body.symbol, db)
    except SymbolError as e:
        return ApiResponse(code=400101, message=str(e), data=None)

    svc = KlineService(db)
    removed = svc.purge_outliers(symbol)
    force = body.force or svc.is_contaminated(symbol)
    if not force and removed:
        return ApiResponse(data=KlineSyncResponse(synced_count=removed, purged=True))
    try:
        count, purged = svc.sync(symbol, force=force)
    except SymbolError as e:
        return ApiResponse(code=400101, message=str(e), data=None)
    except DataSourceError as e:
        return ApiResponse(code=e.code, message=e.message, data=None)
    except Exception as e:
        return ApiResponse(code=500101, message=f"data source error: {e}", data=None)
    # Even when hist sync "succeeds" with stale bars, retry spot backfill.
    if svc.latest_is_stale(symbol):
        svc.ensure_today_bar(symbol)
    return ApiResponse(data=KlineSyncResponse(synced_count=count, purged=purged or removed > 0))
