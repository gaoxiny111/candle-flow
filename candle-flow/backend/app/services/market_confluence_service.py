"""全市场扫描：今日（近 N 根）形态 + 强技术共振。"""

from __future__ import annotations

import logging
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from typing import Any, Literal

from sqlalchemy import distinct
from sqlalchemy.orm import Session

from app.core.bull_tactics import is_main_board, is_st_name
from app.core.confluence import SoftConflict, evaluate_confluence
from app.core.nison_rules import WESTERN_NOT_CANDLES
from app.core.pattern_engine import PatternEngine, kline_to_candles
from app.database import SessionLocal
from app.models.kline import KlineData
from app.models.stock import StockInfo
from app.services.kline_service import KlineService
from app.services.stock_universe import ensure_seeded, lookup_name
from app.utils.symbol import SymbolError, normalize_symbol

logger = logging.getLogger(__name__)

SCAN_WORKERS = 8
KLINE_LIMIT = 90
MIN_BARS = 40
DEFAULT_RECENT_BARS = 2
STRONG_COMBINED = 80.0
CACHE_TTL_SEC = 600

Outcome = Literal["hit", "ok", "skipped", "error"]

_cache: dict[str, Any] = {"ts": 0.0, "payload": None}


@dataclass(frozen=True)
class _Job:
    symbol: str
    name: str
    recent_bars: int


def _combined_score(pattern_score: float, effective: float, soft_items: list[SoftConflict]) -> float:
    score = float(pattern_score) + float(effective) * 6.0
    for sc in soft_items:
        if sc.kind == "low_momentum":
            score -= 8
    return score


def _is_strong(pattern_score: float, effective: float, soft_items: list[SoftConflict]) -> bool:
    for sc in soft_items:
        if sc.kind in ("emotion_extreme", "structure_flaw"):
            return False
    return _combined_score(pattern_score, effective, soft_items) >= STRONG_COMBINED


class MarketConfluenceService:
    def __init__(self, db: Session):
        self.db = db
        self.engine = PatternEngine(min_score=60.0)

    def _symbols_with_klines(self) -> list[tuple[str, str]]:
        ensure_seeded(self.db)
        rows = self.db.query(distinct(KlineData.symbol)).all()
        name_map = {r.symbol: (r.name or "") for r in self.db.query(StockInfo).all()}
        out: list[tuple[str, str]] = []
        for (sym,) in rows:
            try:
                symbol = normalize_symbol(sym)
            except SymbolError:
                continue
            if not is_main_board(symbol):
                continue
            name = name_map.get(symbol) or lookup_name(self.db, symbol) or ""
            if is_st_name(name):
                continue
            out.append((symbol, name))
        out.sort(key=lambda x: x[0])
        return out

    def _scan_job(self, job: _Job) -> tuple[dict | None, Outcome]:
        db = SessionLocal()
        try:
            klines, _ = KlineService(db).get_recent_klines(job.symbol, limit=KLINE_LIMIT)
            if len(klines) < MIN_BARS:
                return None, "skipped"
            candles = kline_to_candles(klines)
            results = PatternEngine(min_score=60.0).scan(candles)
            if not results:
                return None, "ok"

            last_idx = len(klines) - 1
            min_idx = max(0, last_idx - max(job.recent_bars, 1) + 1)
            best: dict | None = None
            best_score = -1.0

            for r in results:
                if r.pattern_name in WESTERN_NOT_CANDLES:
                    continue
                if r.direction not in ("bullish", "bearish"):
                    continue
                if r.candle_index < min_idx or r.candle_index > last_idx:
                    continue
                conf = evaluate_confluence(klines, r.candle_index, r.direction)
                if not conf.ok:
                    continue
                if not _is_strong(float(r.score), conf.effective_count, conf.soft_conflict_items):
                    continue
                combined = _combined_score(float(r.score), conf.effective_count, conf.soft_conflict_items)
                if combined <= best_score:
                    continue
                best_score = combined
                bar = klines[r.candle_index]
                best = {
                    "symbol": job.symbol,
                    "name": job.name,
                    "direction": r.direction,
                    "pattern_name": r.pattern_name,
                    "pattern_score": round(float(r.score), 1),
                    "confluence_count": conf.count,
                    "confluence_effective": round(conf.effective_count, 2),
                    "confluence_hits": conf.label,
                    "confluence_detail": [
                        {"name": h.name, "detail": h.detail} for h in conf.hits
                    ],
                    "combined_score": round(combined, 1),
                    "signal_level": "strong",
                    "candle_date": str(bar.date),
                    "close": round(float(bar.close), 4),
                }
            if best:
                return best, "hit"
            return None, "ok"
        except Exception as exc:
            logger.debug("market confluence scan failed for %s: %s", job.symbol, exc)
            return None, "error"
        finally:
            db.close()

    def scan_market(self, recent_bars: int = DEFAULT_RECENT_BARS, force: bool = False) -> dict[str, Any]:
        now = time.time()
        if (
            not force
            and _cache["payload"] is not None
            and now - float(_cache["ts"]) < CACHE_TTL_SEC
        ):
            cached = dict(_cache["payload"])
            cached["cached"] = True
            cached["cache_age_sec"] = int(now - float(_cache["ts"]))
            return cached

        universe = self._symbols_with_klines()
        jobs = [_Job(sym, name, recent_bars) for sym, name in universe]
        items: list[dict] = []
        skipped = 0
        errors = 0
        workers = min(SCAN_WORKERS, max(1, len(jobs)))
        if jobs:
            with ThreadPoolExecutor(max_workers=workers) as pool:
                futures = {pool.submit(self._scan_job, job): job for job in jobs}
                for fut in as_completed(futures):
                    result, outcome = fut.result()
                    if outcome == "hit" and result:
                        items.append(result)
                    elif outcome == "skipped":
                        skipped += 1
                    elif outcome == "error":
                        errors += 1

        items.sort(key=lambda r: r.get("combined_score", 0), reverse=True)
        payload = {
            "items": items,
            "count": len(items),
            "scanned": len(jobs),
            "universe_size": len(universe),
            "skipped": skipped,
            "errors": errors,
            "recent_bars": recent_bars,
            "cached": False,
            "cache_age_sec": 0,
            "description": (
                "扫描本地已有 K 线的主板非 ST 股票，筛选近几日出现形态且技术共振达到「强」的标的"
            ),
        }
        _cache["ts"] = now
        _cache["payload"] = payload
        return payload

    def latest(self) -> dict[str, Any] | None:
        if _cache["payload"] is None:
            return None
        now = time.time()
        payload = dict(_cache["payload"])
        payload["cached"] = True
        payload["cache_age_sec"] = int(now - float(_cache["ts"]))
        return payload
