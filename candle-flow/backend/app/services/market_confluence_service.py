"""全市场扫描：今日看涨形态 + 强技术共振，按综合强度分层并做基本面排雷。"""

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
from app.services.fundamental_screen import (
    _fetch_debt_map,
    _num,
    _to_symbol,
    resolve_latest_report_frame,
)
from app.services.kline_service import KlineService
from app.services.stock_universe import ensure_seeded, lookup_name
from app.utils.symbol import SymbolError, normalize_symbol

logger = logging.getLogger(__name__)

SCAN_WORKERS = 8
KLINE_LIMIT = 90
MIN_BARS = 40
DEFAULT_RECENT_BARS = 2
# 扫描阶段仍用较低门槛收集候选；展示前再切 S/A/B
CANDIDATE_COMBINED = 80.0
TIER_B_MIN = 110.0
TIER_A_MIN = 115.0
TIER_S_MIN = 120.0
DEBT_MAX = 70.0
CACHE_TTL_SEC = 600
CACHE_VERSION = 3

Outcome = Literal["hit", "ok", "skipped", "error"]
Tier = Literal["S", "A", "B"]

_cache: dict[str, Any] = {"ts": 0.0, "payload": None, "version": 0}


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


def _is_candidate(pattern_score: float, effective: float, soft_items: list[SoftConflict]) -> bool:
    for sc in soft_items:
        if sc.kind in ("emotion_extreme", "structure_flaw"):
            return False
    return _combined_score(pattern_score, effective, soft_items) >= CANDIDATE_COMBINED


def _tier_of(score: float) -> Tier | None:
    if score >= TIER_S_MIN:
        return "S"
    if score >= TIER_A_MIN:
        return "A"
    if score >= TIER_B_MIN:
        return "B"
    return None


def _apply_tiers(items: list[dict]) -> tuple[list[dict], dict[str, list[dict]], dict[str, int]]:
    """第二层：看涨已过滤后的列表按综合强度切 S/A/B。"""
    tiers: dict[str, list[dict]] = {"S": [], "A": [], "B": []}
    kept: list[dict] = []
    for row in items:
        score = float(row.get("combined_score") or 0)
        tier = _tier_of(score)
        if not tier:
            continue
        row = dict(row)
        row["tier"] = tier
        tiers[tier].append(row)
        kept.append(row)
    for t in tiers:
        tiers[t].sort(key=lambda r: r.get("combined_score", 0), reverse=True)
    kept.sort(key=lambda r: r.get("combined_score", 0), reverse=True)
    counts = {t: len(tiers[t]) for t in ("S", "A", "B")}
    return kept, tiers, counts


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
                # 第一层：只保留看涨
                if r.direction != "bullish":
                    continue
                if r.candle_index < min_idx or r.candle_index > last_idx:
                    continue
                conf = evaluate_confluence(klines, r.candle_index, r.direction)
                if not conf.ok:
                    continue
                if not _is_candidate(float(r.score), conf.effective_count, conf.soft_conflict_items):
                    continue
                combined = _combined_score(float(r.score), conf.effective_count, conf.soft_conflict_items)
                if combined <= best_score:
                    continue
                best_score = combined
                bar = klines[r.candle_index]
                best = {
                    "symbol": job.symbol,
                    "name": job.name,
                    "direction": "bullish",
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

    def _fundamental_screen(self, items: list[dict]) -> tuple[list[dict], int]:
        """第三层：亏损 / 高负债排雷（ST 已在宇宙阶段剔除；商誉暂无可靠字段）。"""
        if not items:
            return [], 0
        removed = 0
        profit_map: dict[str, float] = {}
        debt_map: dict[str, float] = {}
        try:
            snap_date, snap_df = resolve_latest_report_frame()
            if snap_df is not None and not snap_df.empty:
                for _, row in snap_df.iterrows():
                    sym = _to_symbol(row.get("股票代码"))
                    if not sym:
                        continue
                    np_ = _num(row.get("净利润")) or _num(row.get("归母净利润")) or _num(row.get("净利润-净利润"))
                    if np_ is not None:
                        profit_map[sym] = float(np_)
            # 负债率优先用最近年报
            debt_date = str(snap_date or "")
            if debt_date and not debt_date.endswith("1231") and len(debt_date) >= 4:
                debt_date = f"{debt_date[:4]}1231"
            if debt_date:
                debt_map = _fetch_debt_map(debt_date) or {}
        except Exception as exc:
            logger.warning("market scan fundamental enrich failed: %s", exc)

        kept: list[dict] = []
        for row in items:
            sym = row["symbol"]
            name = row.get("name") or ""
            if is_st_name(name) or "退" in name:
                removed += 1
                continue
            profit = profit_map.get(sym)
            debt = debt_map.get(sym)
            reasons: list[str] = []
            if profit is not None and profit < 0:
                reasons.append("亏损")
            if debt is not None and debt > DEBT_MAX:
                reasons.append(f"负债率{debt:.0f}%")
            if reasons:
                removed += 1
                continue
            enriched = dict(row)
            if profit is not None:
                enriched["net_profit"] = profit
            if debt is not None:
                enriched["debt_ratio"] = round(float(debt), 2)
            kept.append(enriched)
        return kept, removed

    def scan_market(self, recent_bars: int = DEFAULT_RECENT_BARS, force: bool = False) -> dict[str, Any]:
        now = time.time()
        if (
            not force
            and _cache["payload"] is not None
            and int(_cache.get("version") or 0) == CACHE_VERSION
            and now - float(_cache["ts"]) < CACHE_TTL_SEC
        ):
            cached = dict(_cache["payload"])
            cached["cached"] = True
            cached["cache_age_sec"] = int(now - float(_cache["ts"]))
            return cached

        universe = self._symbols_with_klines()
        jobs = [_Job(sym, name, recent_bars) for sym, name in universe]
        raw_hits: list[dict] = []
        skipped = 0
        errors = 0
        workers = min(SCAN_WORKERS, max(1, len(jobs)))
        if jobs:
            with ThreadPoolExecutor(max_workers=workers) as pool:
                futures = {pool.submit(self._scan_job, job): job for job in jobs}
                for fut in as_completed(futures):
                    result, outcome = fut.result()
                    if outcome == "hit" and result:
                        raw_hits.append(result)
                    elif outcome == "skipped":
                        skipped += 1
                    elif outcome == "error":
                        errors += 1

        # 第一层已在 job 内完成（仅看涨）；此处统计候选
        bullish_candidates = sorted(raw_hits, key=lambda r: r.get("combined_score", 0), reverse=True)
        # 第二层：强度分层（丢弃 <110）
        tiered, _, _ = _apply_tiers(bullish_candidates)
        # 第三层：基本面排雷
        screened, fund_removed = self._fundamental_screen(tiered)
        items, tiers, tier_counts = _apply_tiers(screened)

        payload = {
            "items": items,
            "tiers": tiers,
            "tier_counts": tier_counts,
            "count": len(items),
            "raw_hit_count": len(raw_hits),
            "bullish_count": len(bullish_candidates),
            "tiered_before_fund": len(tiered),
            "fund_removed": fund_removed,
            "scanned": len(jobs),
            "universe_size": len(universe),
            "skipped": skipped,
            "errors": errors,
            "recent_bars": recent_bars,
            "cached": False,
            "cache_age_sec": 0,
            "description": (
                "仅看涨；按综合强度分 S(≥120)/A(115-119)/B(110-114)；"
                "并剔除亏损、负债率>70%、ST/退市风险股"
            ),
        }
        _cache["ts"] = now
        _cache["payload"] = payload
        _cache["version"] = CACHE_VERSION
        return payload

    def latest(self) -> dict[str, Any] | None:
        if _cache["payload"] is None or int(_cache.get("version") or 0) != CACHE_VERSION:
            return None
        now = time.time()
        payload = dict(_cache["payload"])
        payload["cached"] = True
        payload["cache_age_sec"] = int(now - float(_cache["ts"]))
        return payload
