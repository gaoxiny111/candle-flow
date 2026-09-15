from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session

from app.api.deps import get_optional_user
from app.database import get_db
from app.models.user_config import UserConfig
from app.schemas.common import ApiResponse, ResponseMeta
from app.schemas.signal import SignalConfirmRequest, SignalOut
from app.services.kline_service import KlineService
from app.services.market_confluence_service import MarketConfluenceService
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


@router.get("/signals/market-scan")
def get_market_confluence_scan(db: Session = Depends(get_db)):
    """返回最近一次全市场强共振扫描缓存（若无则触发一次扫描）。"""
    svc = MarketConfluenceService(db)
    cached = svc.latest()
    if cached is not None:
        return ApiResponse(data=cached)
    data = svc.scan_market(force=False)
    return ApiResponse(data=data)


@router.get("/signals/scan/market/progress")
def market_confluence_progress(job_id: Optional[str] = Query(None)):
    from app.services.job_progress import get_job

    job = get_job(job_id, kind="market_confluence")
    if not job:
        return ApiResponse(data={"status": "empty"})
    return ApiResponse(data=job)


@router.post("/signals/scan/market")
def scan_market_confluence(
    recent_bars: int = Query(2, ge=1, le=5),
    force: bool = Query(False),
    background: bool = Query(True, description="后台扫描并返回 job_id"),
    db: Session = Depends(get_db),
):
    """扫描本地已过滤的有效主板 K 线标的，筛选今日强技术共振信号。"""
    if background:
        from app.services.job_progress import finish_job, get_job, run_in_background, update_job

        existing = get_job(kind="market_confluence")
        if existing and existing.get("status") == "running":
            raise HTTPException(status_code=409, detail="市场扫描正在进行中")

        def _run(job_id: str) -> None:
            from app.database import SessionLocal

            def progress(done: int, total: int, phase: str) -> None:
                labels = {
                    "scan": "扫描",
                    "tier": "分层",
                    "fundamentals": "基本面排雷",
                    "cache": "读取缓存",
                    "done": "完成",
                }
                update_job(
                    job_id,
                    phase=phase,
                    done=done,
                    total=total,
                    message=f"{labels.get(phase, phase)} {done}/{total}" if total else labels.get(phase, phase),
                )

            try:
                session = SessionLocal()
                try:
                    data = MarketConfluenceService(session).scan_market(
                        recent_bars=recent_bars,
                        force=force,
                        progress=progress,
                    )
                finally:
                    session.close()
                finish_job(job_id, data)
            except Exception as exc:
                finish_job(job_id, error=str(exc))

        job_id = run_in_background("market_confluence", _run, phase="starting", message="市场扫描已启动")
        return ApiResponse(data={"status": "started", "job_id": job_id, "progress": get_job(job_id)})

    data = MarketConfluenceService(db).scan_market(recent_bars=recent_bars, force=force)
    return ApiResponse(data=data)


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
