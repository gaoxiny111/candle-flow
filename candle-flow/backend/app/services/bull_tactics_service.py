"""Scan main-board bull tactics for symbols."""

from __future__ import annotations

import logging
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from typing import Callable, Literal

from sqlalchemy.orm import Session

from app.core.bull_tactics import (
    HEIMA,
    N_FAN,
    NIU_SAN,
    MIN_SCAN_BARS,
    is_main_board,
    is_st_name,
    kline_limit_for_tactics,
    normalize_tactics,
    scan_tactics,
)
from app.core.pattern_engine import kline_to_candles
from app.database import SessionLocal
from app.models.stock import StockInfo
from app.services.kline_service import KlineService
from app.services.main_board_kline_sync import list_scannable_main_board, target_trade_date
from app.services.stock_universe import ensure_seeded, lookup_name, refresh_universe
from app.utils.symbol import normalize_symbol, SymbolError

logger = logging.getLogger(__name__)

SCAN_WORKERS = 6

ProgressCb = Callable[[int, int, str], None]


TACTIC_RULES = {
    HEIMA: "主板非 ST；近 7 个交易日内连续三天涨停（第三天可炸板未回封）；三连板后缩量回调不破十日线。",
    N_FAN: "主板非 ST；近 7 个交易日内放量涨停（量大于 5 日均量）；涨停后缩量回调不破涨停阳线开盘价。",
    NIU_SAN: "非 ST；跳空高开大阳线（涨幅≥7%）或涨停；之后缩量回调不破该阳线开盘价，且不跌破 39 日均线。",
}


@dataclass(frozen=True)
class _ScanJob:
    symbol: str
    name: str
    recent_bars: int
    tactics: list[str] | None
    kline_limit: int


ScanOutcome = Literal["hit", "ok", "skipped", "error"]


class BullTacticsService:
    def __init__(self, db: Session):
        self.db = db

    def _eligible(self, symbol: str) -> tuple[bool, str, str]:
        try:
            symbol = normalize_symbol(symbol)
        except SymbolError:
            return False, symbol, ""
        if not is_main_board(symbol):
            return False, symbol, ""
        name = lookup_name(self.db, symbol) or ""
        if is_st_name(name):
            return False, symbol, name
        return True, symbol, name

    def _main_board_stocks(self) -> list[StockInfo]:
        ensure_seeded(self.db)
        rows = self.db.query(StockInfo).filter(StockInfo.market.in_(("SH", "SZ"))).all()
        out: list[StockInfo] = []
        for row in rows:
            if not is_main_board(row.symbol):
                continue
            if is_st_name(row.name):
                continue
            out.append(row)
        out.sort(key=lambda r: r.symbol)
        return out

    def _scan_candles(
        self,
        symbol: str,
        name: str,
        candles,
        recent_bars: int,
        tactics: list[str] | None = None,
    ) -> dict:
        hits = scan_tactics(candles, recent_bars=recent_bars, tactics=tactics)
        return {
            "symbol": symbol,
            "name": name,
            "hits": [_hit_out(h) for h in hits],
        }

    def _scan_job(self, job: _ScanJob) -> tuple[dict | None, ScanOutcome]:
        db = SessionLocal()
        try:
            klines, _ = KlineService(db).get_recent_klines(job.symbol, limit=job.kline_limit)
            if len(klines) < MIN_SCAN_BARS:
                return None, "skipped"
            candles = kline_to_candles(klines)
            result = BullTacticsService(db)._scan_candles(
                job.symbol,
                job.name,
                candles,
                job.recent_bars,
                job.tactics,
            )
            if result["hits"]:
                return result, "hit"
            return result, "ok"
        except Exception as exc:
            logger.debug("tactic scan failed for %s: %s", job.symbol, exc)
            return None, "error"
        finally:
            db.close()

    def scan_symbol(self, symbol: str, recent_bars: int = 30, tactics: list[str] | None = None) -> dict | None:
        ok, symbol, name = self._eligible(symbol)
        if not ok:
            return None
        kline_limit = kline_limit_for_tactics(tactics)
        job = _ScanJob(symbol, name, recent_bars, tactics, kline_limit)
        result, outcome = self._scan_job(job)
        if outcome in ("skipped", "error"):
            return None
        return result

    def _parallel_scan_jobs(
        self,
        jobs: list[_ScanJob],
        progress: ProgressCb | None = None,
    ) -> tuple[list[dict], int, int]:
        if not jobs:
            if progress:
                progress(0, 0, "scan")
            return [], 0, 0
        items: list[dict] = []
        skipped = 0
        errors = 0
        workers = min(SCAN_WORKERS, len(jobs))
        done = 0
        total = len(jobs)
        with ThreadPoolExecutor(max_workers=workers) as pool:
            futures = {pool.submit(self._scan_job, job): job for job in jobs}
            for fut in as_completed(futures):
                result, outcome = fut.result()
                done += 1
                if outcome == "hit" and result:
                    items.append(result)
                elif outcome == "error":
                    errors += 1
                elif outcome == "skipped":
                    skipped += 1
                if progress:
                    progress(done, total, "scan")
        items.sort(key=lambda r: max((h["score"] for h in r["hits"]), default=0), reverse=True)
        return items, skipped, errors

    def scan_symbols(self, symbols: list[str], recent_bars: int = 30, tactics: list[str] | None = None) -> dict:
        kline_limit = kline_limit_for_tactics(tactics)
        jobs: list[_ScanJob] = []
        skipped: list[str] = []
        for raw in symbols:
            sym = (raw or "").strip()
            if not sym:
                continue
            ok, symbol, name = self._eligible(sym)
            if not ok:
                skipped.append(sym)
                continue
            jobs.append(_ScanJob(symbol, name, recent_bars, tactics, kline_limit))
        items, scan_skipped, errors = self._parallel_scan_jobs(jobs)
        selected = normalize_tactics(tactics)
        return {
            "items": items,
            "skipped": skipped,
            "count": len(items),
            "tactic": selected[0] if len(selected) == 1 else None,
            "scan_skipped": scan_skipped,
            "errors": errors,
        }

    def scan_market(
        self,
        recent_bars: int = 30,
        refresh_list: bool = True,
        tactics: list[str] | None = None,
        *,
        require_fresh: bool = True,
        progress: ProgressCb | None = None,
    ) -> dict:
        """Scan scannable main-board non-ST stocks from local kline cache."""
        ensure_seeded(self.db)
        if refresh_list:
            try:
                refresh_universe(self.db, force=False)
            except Exception as exc:
                logger.warning("universe refresh skipped: %s", exc)

        stocks = self._main_board_stocks()
        name_map = {r.symbol: r.name for r in stocks}
        filt = list_scannable_main_board(
            self.db,
            as_of=target_trade_date(),
            min_bars=MIN_SCAN_BARS,
            require_fresh=require_fresh,
        )
        symbols = filt["symbols"]
        kline_limit = kline_limit_for_tactics(tactics)
        jobs = [
            _ScanJob(sym, name_map.get(sym) or lookup_name(self.db, sym) or "", recent_bars, tactics, kline_limit)
            for sym in symbols
        ]
        if progress:
            progress(0, len(jobs), "scan")
        items, skipped, errors = self._parallel_scan_jobs(jobs, progress=progress)
        selected = normalize_tactics(tactics)
        return {
            "items": items,
            "scanned": len(jobs),
            "universe_size": filt["universe_size"],
            "prefiltered": filt["scannable"],
            "prefilter": {
                "as_of": filt["as_of"],
                "no_kline": filt["no_kline"],
                "short_bars": filt["short_bars"],
                "stale_kline": filt["stale_kline"],
            },
            "skipped": skipped,
            "errors": errors,
            "count": len(items),
            "tactic": selected[0] if len(selected) == 1 else None,
        }


def _hit_out(hit) -> dict:
    return {
        "tactic": hit.tactic,
        "buy_date": hit.buy_date,
        "buy_price": hit.buy_price,
        "setup_date": hit.setup_date,
        "score": hit.score,
        "details": hit.details,
    }
