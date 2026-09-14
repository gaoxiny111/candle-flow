"""Daily sync of main-board klines for fast tactic scans."""

from __future__ import annotations

import logging
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger
from sqlalchemy import func
from sqlalchemy.orm import Session

from app.core.bull_tactics import is_main_board, is_st_name
from app.database import SessionLocal
from app.models.kline import KlineData
from app.models.stock import StockInfo
from app.services.akshare_client import is_cn_weekday, trading_today
from app.services.kline_service import KlineService
from app.services.stock_universe import ensure_seeded, refresh_universe

logger = logging.getLogger(__name__)

SYNC_WORKERS = 6
SYNC_SLEEP_SEC = 0.08

# 新鲜度门槛：至少一半宇宙，或绝对数量达标，才认为可出「今日列表」
FRESH_MIN_RATIO = 0.5
FRESH_MIN_ABS = 500

_scheduler: BackgroundScheduler | None = None
_sync_lock = threading.Lock()
_sync_running = False


def target_trade_date(now: datetime | None = None) -> date:
    """最近一个 A 股交易日（周末回退到周五）。"""
    d = trading_today(now)
    while not is_cn_weekday(d):
        d -= timedelta(days=1)
    return d


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


def list_stale_main_board_symbols(db: Session, as_of: date | None = None) -> list[str]:
    """返回最新 K 线日 < as_of（或缺数据）的主板非 ST 代码。"""
    day = as_of or target_trade_date()
    symbols = _main_board_symbols(db)
    if not symbols:
        return []
    max_dates = dict(db.query(KlineData.symbol, func.max(KlineData.date)).group_by(KlineData.symbol).all())
    need: list[str] = []
    for sym in symbols:
        mx = max_dates.get(sym)
        if mx is None or mx < day:
            need.append(sym)
    return need


def is_fresh_enough(fresh: dict) -> bool:
    universe = int(fresh.get("universe_size") or 0)
    got = int(fresh.get("fresh_to_trade_date") or 0)
    if universe <= 0:
        return False
    if got >= FRESH_MIN_ABS:
        return True
    return (got / universe) >= FRESH_MIN_RATIO


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


def sync_main_board_klines(
    refresh_universe_list: bool = True,
    *,
    incremental: bool = True,
    as_of: date | None = None,
) -> dict:
    """
    同步主板非 ST 日 K。
    incremental=True（默认）：只补「最新日 < 目标交易日」或缺数据的标的。
    incremental=False：全量再拉一遍。
    """
    global _sync_running
    with _sync_lock:
        if _sync_running:
            return {"status": "already_running"}
        _sync_running = True

    started = datetime.now(ZoneInfo("Asia/Shanghai"))
    day = as_of or target_trade_date()
    synced = 0
    errors = 0
    skipped_fresh = 0
    try:
        db = SessionLocal()
        try:
            ensure_seeded(db)
            if refresh_universe_list:
                try:
                    refresh_universe(db, force=False)
                except Exception as exc:
                    logger.warning("universe refresh skipped during kline sync: %s", exc)
            universe = _main_board_symbols(db)
            if incremental:
                symbols = list_stale_main_board_symbols(db, as_of=day)
                skipped_fresh = max(0, len(universe) - len(symbols))
            else:
                symbols = universe
        finally:
            db.close()

        if not universe:
            return {
                "status": "empty",
                "trade_date": day.isoformat(),
                "universe_size": 0,
                "needed": 0,
                "synced": 0,
                "skipped_fresh": 0,
                "errors": 0,
                "incremental": incremental,
            }

        if not symbols:
            elapsed = (datetime.now(ZoneInfo("Asia/Shanghai")) - started).total_seconds()
            logger.info(
                "main-board kline sync skipped: all %s fresh as of %s (%.1fs)",
                len(universe),
                day,
                elapsed,
            )
            return {
                "status": "ok",
                "trade_date": day.isoformat(),
                "universe_size": len(universe),
                "needed": 0,
                "synced": 0,
                "skipped_fresh": skipped_fresh,
                "errors": 0,
                "elapsed_sec": round(elapsed, 1),
                "incremental": incremental,
            }

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
            "main-board kline sync done (%s): needed %s/%s, ok %s, errors %s, %.1fs, as_of %s",
            "incremental" if incremental else "full",
            len(symbols),
            len(universe),
            synced,
            errors,
            elapsed,
            day,
        )
        return {
            "status": "ok",
            "trade_date": day.isoformat(),
            "universe_size": len(universe),
            "needed": len(symbols),
            "synced": synced,
            "skipped_fresh": skipped_fresh,
            "errors": errors,
            "elapsed_sec": round(elapsed, 1),
            "incremental": incremental,
        }
    finally:
        with _sync_lock:
            _sync_running = False


def _scheduled_sync():
    try:
        sync_main_board_klines(refresh_universe_list=True, incremental=True)
    except Exception:
        logger.exception("scheduled main-board kline sync failed")
    # 收盘 K 线同步后立刻跑战法，输出当日符合条件列表
    try:
        from app.services.bull_tactics_daily import run_daily_bull_tactics_scan

        run_daily_bull_tactics_scan(refresh_list=False, sync_klines=False)
    except Exception:
        logger.exception("scheduled daily bull tactics scan failed")


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
    logger.info(
        "main-board incremental kline sync + bull tactics scheduler started (weekdays 16:35 Asia/Shanghai)"
    )


def stop_kline_sync_scheduler() -> None:
    global _scheduler
    if _scheduler is None:
        return
    _scheduler.shutdown(wait=False)
    _scheduler = None
