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
from app.services.valuation import get_valuations
from app.services.watchlist import MAX_WATCHLIST
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
ROE_MIN = 5.0  # 过低盈利能力（如 ROE 1.5%）剔除
PROFIT_YOY_MIN = -30.0  # 最新净利同比暴跌剔除
PE_MAX = 80.0  # 估值极端（PE>80 且为正）剔除
CACHE_TTL_SEC = 600
CACHE_VERSION = 4

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


def _fund_reject_reasons(
    *,
    name: str = "",
    profit: float | None = None,
    debt: float | None = None,
    roe: float | None = None,
    profit_yoy: float | None = None,
    pe: float | None = None,
) -> list[str]:
    """基本面排雷原因；有数据才判定，缺字段不拦截。"""
    if is_st_name(name) or "退" in name:
        return ["ST/退市"]
    reasons: list[str] = []
    if profit is not None and profit < 0:
        reasons.append("亏损")
    if debt is not None and debt > DEBT_MAX:
        reasons.append(f"负债率{debt:.0f}%")
    if roe is not None and roe < ROE_MIN:
        reasons.append(f"ROE{roe:.1f}%")
    if profit_yoy is not None and profit_yoy < PROFIT_YOY_MIN:
        reasons.append(f"净利同比{profit_yoy:.0f}%")
    if pe is not None and pe > 0 and pe > PE_MAX:
        reasons.append(f"PE{pe:.0f}")
    return reasons


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

    def _pe_map(self, symbols: list[str]) -> dict[str, float]:
        """批量取 PE_TTM（按关注列表上限分批）。"""
        out: dict[str, float] = {}
        if not symbols:
            return out
        batch_size = max(1, int(MAX_WATCHLIST) or 50)
        for i in range(0, len(symbols), batch_size):
            batch = symbols[i : i + batch_size]
            try:
                for v in get_valuations(batch, db=self.db):
                    pe = v.get("pe_ttm")
                    sym = v.get("symbol")
                    if sym and pe is not None:
                        try:
                            out[str(sym)] = float(pe)
                        except (TypeError, ValueError):
                            continue
            except Exception as exc:
                logger.warning("market scan PE enrich failed: %s", exc)
        return out

    def _fundamental_screen(self, items: list[dict]) -> tuple[list[dict], int]:
        """第三层排雷：亏损/高负债/低ROE/净利暴跌/极高PE（ST 已在宇宙阶段剔除）。"""
        if not items:
            return [], 0
        removed = 0
        fund_map: dict[str, dict[str, float]] = {}
        debt_map: dict[str, float] = {}
        try:
            snap_date, snap_df = resolve_latest_report_frame()
            if snap_df is not None and not snap_df.empty:
                for _, row in snap_df.iterrows():
                    sym = _to_symbol(row.get("股票代码"))
                    if not sym:
                        continue
                    entry: dict[str, float] = {}
                    np_ = _num(row.get("净利润")) or _num(row.get("归母净利润")) or _num(row.get("净利润-净利润"))
                    if np_ is not None:
                        entry["net_profit"] = float(np_)
                    roe = _num(row.get("净资产收益率"))
                    if roe is not None:
                        entry["roe"] = float(roe)
                    py = _num(row.get("净利润-同比增长")) or _num(row.get("净利润同比增长"))
                    if py is not None:
                        entry["profit_yoy"] = float(py)
                    if entry:
                        fund_map[sym] = entry
            # 负债率优先用最近年报
            debt_date = str(snap_date or "")
            if debt_date and not debt_date.endswith("1231") and len(debt_date) >= 4:
                debt_date = f"{debt_date[:4]}1231"
            if debt_date:
                debt_map = _fetch_debt_map(debt_date) or {}
        except Exception as exc:
            logger.warning("market scan fundamental enrich failed: %s", exc)

        pe_map = self._pe_map([r["symbol"] for r in items])

        kept: list[dict] = []
        for row in items:
            sym = row["symbol"]
            name = row.get("name") or ""
            fund = fund_map.get(sym) or {}
            profit = fund.get("net_profit")
            roe = fund.get("roe")
            profit_yoy = fund.get("profit_yoy")
            debt = debt_map.get(sym)
            pe = pe_map.get(sym)
            reasons = _fund_reject_reasons(
                name=name,
                profit=profit,
                debt=debt,
                roe=roe,
                profit_yoy=profit_yoy,
                pe=pe,
            )
            if reasons:
                removed += 1
                continue
            enriched = dict(row)
            if profit is not None:
                enriched["net_profit"] = profit
            if debt is not None:
                enriched["debt_ratio"] = round(float(debt), 2)
            if roe is not None:
                enriched["roe"] = round(float(roe), 2)
            if profit_yoy is not None:
                enriched["profit_yoy"] = round(float(profit_yoy), 2)
            if pe is not None:
                enriched["pe_ttm"] = round(float(pe), 1)
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
                "并剔除亏损、负债率>70%、ROE<5%、净利同比<-30%、PE>80、ST/退市风险股"
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
