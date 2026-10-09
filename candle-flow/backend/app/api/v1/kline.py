import logging
from typing import Optional

from fastapi import APIRouter, Depends, Query
from sqlalchemy.orm import Session

from app.core.exceptions import DataSourceError
from app.database import get_db
from app.schemas.common import ApiResponse, ResponseMeta
from app.schemas.kline import KlineOut, KlineSyncRequest, KlineSyncResponse, LiveQuoteOut
from app.services.akshare_client import akshare_client
from app.services.kline_service import KlineService
from app.services.stock_universe import resolve_symbol
from app.utils.symbol import SymbolError

logger = logging.getLogger(__name__)

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
    elif svc.latest_is_stale(symbol):
        # 本地系列落后于最近交易日：增量补齐中间缺失的历史蜡烛，而非只补今天
        try:
            svc.sync(symbol)
            items, total = svc.get_klines(symbol, start_date, end_date, page, page_size)
            items = svc.sanitize_rows(items)
        except Exception:
            logger.warning("incremental kline sync failed for %s", symbol, exc_info=True)
    # 图表打开：盘中也用实时价刷新「今日」这根日 K（仅当前标的，不跑全库）。
    merged = svc.merge_today_spot(symbol, allow_intraday=True)
    if not merged and svc.latest_is_stale(symbol):
        merged = svc.ensure_today_bar(symbol)
    if merged:
        items, total = svc.get_klines(symbol, start_date, end_date, page, page_size)
        items = svc.sanitize_rows(items)
    return ApiResponse(
        data=[KlineOut.model_validate(i) for i in items],
        meta=ResponseMeta(page=page, page_size=page_size, total=len(items)),
    )


@router.get("/kline/quote")
def live_quote(
    symbol: str = Query(...),
    update_daily: bool = Query(default=True),
    db: Session = Depends(get_db),
):
    """盘中实时价；默认顺带刷新该票今日日 K（allow_intraday），供图表轮询。"""
    from datetime import datetime

    from app.services.akshare_client import CN_TZ, trading_today

    resolved = _resolve_symbol(symbol, db)
    if isinstance(resolved, ApiResponse):
        return resolved
    symbol = resolved

    svc = KlineService(db)
    if update_daily:
        try:
            svc.merge_today_spot(symbol, allow_intraday=True)
        except Exception:
            logger.warning("quote-time daily merge failed for %s", symbol, exc_info=True)

    spot = akshare_client.fetch_spot(symbol)
    if not spot or spot.get("close") is None:
        return ApiResponse(code=404101, message="暂无实时行情", data=None)

    price = float(spot["close"])
    prev = spot.get("prev_close")
    if prev is None:
        latest = svc.get_latest(symbol)
        if latest is not None:
            rows, _ = svc.get_klines(symbol, page=1, page_size=3)
            rows = sorted(rows, key=lambda r: r.date)
            if rows and rows[-1].date == trading_today() and len(rows) >= 2:
                prev = float(rows[-2].close)
            else:
                prev = float(latest.close)

    change = None if prev is None else round(price - float(prev), 4)
    pct = None if prev in (None, 0) else round((price - float(prev)) / float(prev) * 100.0, 2)
    qd = spot.get("date")
    return ApiResponse(
        data=LiveQuoteOut(
            symbol=symbol,
            price=price,
            prev_close=float(prev) if prev is not None else None,
            change=change,
            change_pct=pct,
            open=float(spot["open"]) if spot.get("open") is not None else None,
            high=float(spot["high"]) if spot.get("high") is not None else None,
            low=float(spot["low"]) if spot.get("low") is not None else None,
            volume=int(spot["volume"]) if spot.get("volume") is not None else None,
            quote_date=qd,
            source=str(spot.get("source") or "spot"),
            as_of=datetime.now(CN_TZ),
        )
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
        # 先增量补齐缺失的历史交易日，再尝试补今天
        try:
            svc.sync(symbol)
        except Exception:
            logger.warning("incremental kline sync failed for %s", symbol, exc_info=True)
        if svc.latest_is_stale(symbol):
            svc.ensure_today_bar(symbol)
        item = svc.get_latest(symbol)
    if not item:
        return ApiResponse(code=404101, message="symbol not found", data=None)
    return ApiResponse(data=KlineOut.model_validate(item))


@router.get("/kline/tech-narrative")
def tech_narrative(symbol: str = Query(...), db: Session = Depends(get_db)):
    """技术面叙述分析：由系统指标生成成文报告（趋势/形态/指标/支撑压力/操作建议）。"""
    from app.services.tech_narrative import build_tech_narrative

    resolved = _resolve_symbol(symbol, db)
    if isinstance(resolved, ApiResponse):
        return resolved
    data = build_tech_narrative(db, resolved)
    if not data.get("ok"):
        return ApiResponse(code=400101, message=data.get("reason") or "无法生成技术面分析", data=data)
    return ApiResponse(data=data)


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
