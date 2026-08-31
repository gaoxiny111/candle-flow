"""Daily sync of main-board klines for fast tactic scans."""

from __future__ import annotations

import logging
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from zoneinfo import ZoneInfo

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger
from sqlalchemy.orm import Session

from app.core.bull_tactics import is_main_board, is_st_name
from app.database import SessionLocal
from app.models.stock import StockInfo
from app.services.kline_service import KlineService
from app.services.stock_universe import ensure_seeded, refresh_universe

logger = logging.getLogger(__name__)

SYNC_WORKERS = 4
SYNC_SLEEP_SEC = 0.12

_scheduler: BackgroundScheduler | None = None
_sync_lock = threading.Lock()
_sync_running = False


def _main_board_symbols(db: Session) -> list[str]:
    rows = db.query(StockInfo).filter(StockInfo.market.in_(("SH", "SZ"))).all()
    out: list[str] = []
    for row in rows:
        if not is_main_board(row.symbol):
            continue
        if is_st_name(row.name):
            continue
        out.append(row.symbol)
    out.sort()
    return out


def sync_one_symbol(symbol: str) -> tuple[str, int, str | None]:
    db = SessionLocal()
    try:
        count, _ = KlineService(db).sync(symbol)
        return symbol, count, None
    except Exception as exc:
        logger.debug("kline sync failed for %s: %s", symbol, exc)
        return symbol, 0, str(exc)
    finally:
        db.close()
        time.sleep(SYNC_SLEEP_SEC)


def sync_main_board_klines(refresh_universe_list: bool = True) -> dict:
    """Sync daily klines for all main-board non-ST symbols. Safe to call manually."""
    global _sync_running
    with _sync_lock:
        if _sync_running:
            return {"status": "already_running"}
        _sync_running = True

    started = datetime.now(ZoneInfo("Asia/Shanghai"))
    synced = 0
    errors = 0
    try:
        db = SessionLocal()
        try:
            ensure_seeded(db)
            if refresh_universe_list:
                try:
                    refresh_universe(db, force=False)
                except Exception as exc:
                    logger.warning("universe refresh skipped during kline sync: %s", exc)
            symbols = _main_board_symbols(db)
        finally:
            db.close()

        if not symbols:
            return {"status": "empty", "universe_size": 0, "synced": 0, "errors": 0}

        workers = min(SYNC_WORKERS, len(symbols))
        with ThreadPoolExecutor(max_workers=workers) as pool:
            futures = [pool.submit(sync_one_symbol, sym) for sym in symbols]
            for fut in as_completed(futures):
                _, _count, err = fut.result()
                if err:
                    errors += 1
                else:
                    synced += 1

        elapsed = (datetime.now(ZoneInfo("Asia/Shanghai")) - started).total_seconds()
        logger.info(
            "main-board kline sync done: %s/%s ok, %s errors, %.1fs",
            synced,
            len(symbols),
            errors,
            elapsed,
        )
        return {
            "status": "ok",
            "universe_size": len(symbols),
            "synced": synced,
            "errors": errors,
            "elapsed_sec": round(elapsed, 1),
        }
    finally:
        with _sync_lock:
            _sync_running = False


def _scheduled_sync():
    try:
        sync_main_board_klines(refresh_universe_list=True)
    except Exception:
        logger.exception("scheduled main-board kline sync failed")


def start_kline_sync_scheduler() -> None:
    global _scheduler
    if _scheduler is not None:
        return
    _scheduler = BackgroundScheduler(timezone=ZoneInfo("Asia/Shanghai"))
    _scheduler.add_job(
        _scheduled_sync,
        CronTrigger(hour=16, minute=35, day_of_week="mon-fri"),
        id="main_board_kline_sync",
        replace_existing=True,
    )
    _scheduler.start()
    logger.info("main-board kline sync scheduler started (weekdays 16:35 Asia/Shanghai)")


def stop_kline_sync_scheduler() -> None:
    global _scheduler
    if _scheduler is None:
        return
    _scheduler.shutdown(wait=False)
    _scheduler = None
