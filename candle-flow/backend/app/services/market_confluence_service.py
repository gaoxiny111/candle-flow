"""全市场扫描：K线形态 + 基本面动态权重 + 买点叠加验证。"""

from __future__ import annotations

import logging
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from typing import Any, Callable, Literal

from sqlalchemy.orm import Session

from app.core.bull_tactics import is_st_name
from app.core.confluence import SoftConflict, evaluate_confluence
from app.core.nison_rules import WESTERN_NOT_CANDLES
from app.core.pattern_engine import PatternEngine, kline_to_candles
from app.database import SessionLocal
from app.models.stock import StockInfo
from app.services.fundamental_screen import (
    _fetch_debt_map,
    _num,
    _to_symbol,
    resolve_latest_report_frame,
)
from app.services.kline_service import KlineService
from app.services.main_board_kline_sync import list_scannable_main_board, target_trade_date
from app.services.stock_universe import ensure_seeded, lookup_name
from app.services.valuation import get_valuations
from app.services.watchlist import MAX_WATCHLIST
from app.utils.symbol import normalize_symbol

logger = logging.getLogger(__name__)

SCAN_WORKERS = 8
FUND_WORKERS = 8
KLINE_LIMIT = 90
MIN_BARS = 40
DEFAULT_RECENT_BARS = 2
# 扫描阶段仍用较低门槛收集候选；展示前再切 S/A/B
CANDIDATE_COMBINED = 80.0
# 动态权重后的综合分分层阈值（按基本面评分 A/B/C/D/E）
TIER_A_MIN = 85.0   # 基本面≥85 → A
TIER_B_MIN = 70.0   # 基本面≥70 → B
TIER_C_MIN = 55.0   # 基本面≥55 → C
TIER_D_MIN = 40.0   # 基本面≥40 → D，<40 → E
DEBT_MAX = 70.0
ROE_MIN = 5.0  # 过低盈利能力（如 ROE 1.5%）剔除
PROFIT_YOY_MIN = -30.0  # 最新净利同比暴跌剔除
PE_MAX = 80.0  # 估值极端（PE>80 且为正）剔除
CACHE_TTL_SEC = 600
CACHE_VERSION = 6

Outcome = Literal["hit", "ok", "skipped", "error"]
Tier = Literal["A", "B", "C", "D", "E"]
ProgressCb = Callable[[int, int, str], None]

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


def _tier_of(fundamental_score: float) -> Tier:
    """按基本面评分返回 A/B/C/D/E 等级。"""
    if fundamental_score >= TIER_A_MIN:
        return "A"
    if fundamental_score >= TIER_B_MIN:
        return "B"
    if fundamental_score >= TIER_C_MIN:
        return "C"
    if fundamental_score >= TIER_D_MIN:
        return "D"
    return "E"


# ── 动态权重 & 买点信号 ──────────────────────────────────────

def _kline_weight(fundamental_score: float) -> float:
    """根据基本面评分返回 K 线权重（剩余为基本面权重）。
    ≥80 → 30% K线（侧重趋势确认）
    60-80 → 50% K线（侧重拐点信号）
    <60 → 70% K线（仅短线博弈）
    """
    if fundamental_score >= 80:
        return 0.30
    if fundamental_score >= 60:
        # 60-80 线性插值：60→0.50, 80→0.30
        return 0.50 - (fundamental_score - 60) / 20.0 * 0.20
    return 0.70


def _calc_peg(pe: float | None, profit_yoy: float | None) -> float | None:
    """PEG = PE / 净利润增速(%)。增速≤0 时返回 None。"""
    if pe is None or profit_yoy is None:
        return None
    if profit_yoy <= 0:
        return None
    try:
        return round(float(pe) / float(profit_yoy), 2)
    except (TypeError, ValueError, ZeroDivisionError):
        return None


def _sma(values: list[float], period: int) -> float | None:
    if len(values) < period:
        return None
    return sum(values[-period:]) / period


def _detect_buy_signal(
    klines: list,
    fundamental_score: float,
    peg: float | None,
    pe: float | None,
) -> dict[str, Any]:
    """买点信号叠加验证。
    - 强买入: 基本面≥80 + PEG<1.5 + 突破20日均线 + 成交量放大20%
    - 观察: 基本面≥80 + PEG>2 + 回踩60日均线缩量
    - 短线博弈: 基本面<60 + K线突破信号
    """
    if not klines or len(klines) < 60:
        return {"signal": "insufficient_data", "label": "数据不足"}

    closes = [float(k.close) for k in klines]
    volumes = [float(k.volume) for k in klines]
    idx = len(klines) - 1
    price = closes[idx]

    ma20 = _sma(closes, 20)
    ma60 = _sma(closes, 60)

    # 成交量：近5日均量 vs 近20日均量
    vol_5 = _sma(volumes, 5)
    vol_20 = _sma(volumes, 20)
    vol_ratio = (vol_5 / vol_20 - 1.0) if vol_5 and vol_20 and vol_20 > 0 else 0.0

    above_ma20 = ma20 is not None and price > ma20
    near_ma60 = ma60 is not None and abs(price - ma60) / ma60 < 0.03 if ma60 else False
    vol_up_20pct = vol_ratio > 0.20
    vol_shrink = vol_ratio < -0.10

    # 强买入信号
    if (
        fundamental_score >= 80
        and peg is not None and peg < 1.5
        and above_ma20
        and vol_up_20pct
    ):
        return {
            "signal": "strong_buy",
            "label": "强买入",
            "reasons": [
                f"基本面{fundamental_score:.0f}分（优质）",
                f"PEG={peg}（<1.5 估值合理）",
                f"价格{price:.2f} > MA20={ma20:.2f}（突破20日均线）",
                f"近5日量能较20日均量+{vol_ratio*100:.0f}%（放量）",
            ],
        }

    # 观察信号
    if (
        fundamental_score >= 80
        and peg is not None and peg > 2.0
        and near_ma60
        and vol_shrink
    ):
        return {
            "signal": "watch",
            "label": "观察",
            "reasons": [
                f"基本面{fundamental_score:.0f}分（优质）",
                f"PEG={peg}（>2 估值偏高）",
                f"价格{price:.2f} 回踩 MA60={ma60:.2f}（±3%）",
                f"近5日量能较20日均量{vol_ratio*100:.0f}%（缩量）",
            ],
            "note": "等待估值消化",
        }

    # 短线博弈（基本面<60 但有技术突破）
    if fundamental_score < 60 and above_ma20 and vol_up_20pct:
        return {
            "signal": "short_term",
            "label": "短线博弈",
            "reasons": [
                f"基本面{fundamental_score:.0f}分（偏弱）",
                f"价格突破MA20 + 放量{vol_ratio*100:.0f}%",
            ],
            "note": "严格止损，仅短线",
        }

    # 默认：技术信号描述
    reasons: list[str] = []
    if above_ma20:
        reasons.append(f"站上MA20({ma20:.2f})")
    elif ma20:
        reasons.append(f"低于MA20({ma20:.2f})")
    if vol_up_20pct:
        reasons.append(f"放量+{vol_ratio*100:.0f}%")
    elif vol_shrink:
        reasons.append(f"缩量{vol_ratio*100:.0f}%")

    return {
        "signal": "neutral",
        "label": "中性",
        "reasons": reasons or ["无明显技术信号"],
    }


def _extract_fundamental_fields(result: dict[str, Any]) -> dict[str, Any]:
    """从 run_full_analysis 返回 dict 中提取扫描所需字段。"""
    composite = result.get("composite_score")
    if composite is None:
        return {"score": None, "error": result.get("error", "no_composite_score")}
    modules = result.get("modules") or {}
    market = result.get("market") or {}
    return {
        "score": float(composite),
        "profitability": (modules.get("profitability") or {}).get("score"),
        "growth": (modules.get("growth") or {}).get("score"),
        "cashflow": (modules.get("cashflow") or {}).get("score"),
        "valuation_score": (modules.get("valuation") or {}).get("score"),
        "pe_ttm": market.get("pe_ttm"),
        "industry": result.get("industry") or "",
        "price": market.get("price"),
    }


def _compute_fundamental_score(symbol: str) -> dict[str, Any]:
    """对单只股票运行完整基本面分析，返回 composite_score 及关键指标。

    优先读因子库（毫秒级），miss/stale 时回退到 analyze_symbol_full（6h TTL 缓存）。
    """
    # 优先读因子库
    try:
        from app.services.factor_db import get, is_stale

        if not is_stale(symbol):
            cached = get(symbol)
            if cached:
                return _extract_fundamental_fields(cached)
    except Exception:
        pass
    # 回退：实时分析路径
    try:
        from app.analysis.engine import analyze_symbol_full

        result = analyze_symbol_full(db=None, symbol=symbol, use_cache=True)
        return _extract_fundamental_fields(result)
    except Exception as exc:
        logger.debug("fundamental analysis failed for %s: %s", symbol, exc)
        return {"score": None, "error": str(exc)}


def _fund_level(score: float) -> str:
    """基本面评分→字母等级。"""
    if score >= 85:
        return "A"
    if score >= 75:
        return "B+"
    if score >= 65:
        return "B"
    if score >= 55:
        return "C"
    if score >= 40:
        return "D"
    return "E"


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
    """按基本面评分切 A/B/C/D/E。"""
    tiers: dict[str, list[dict]] = {"A": [], "B": [], "C": [], "D": [], "E": []}
    kept: list[dict] = []
    for row in items:
        fund_score = float(row.get("fundamental_score") or 0)
        tier = _tier_of(fund_score)
        row = dict(row)
        row["tier"] = tier
        tiers[tier].append(row)
        kept.append(row)
    for t in tiers:
        tiers[t].sort(key=lambda r: r.get("fundamental_score", 0), reverse=True)
    kept.sort(key=lambda r: r.get("fundamental_score", 0), reverse=True)
    counts = {t: len(tiers[t]) for t in ("A", "B", "C", "D", "E")}
    return kept, tiers, counts


class MarketConfluenceService:
    def __init__(self, db: Session):
        self.db = db
        self.engine = PatternEngine(min_score=60.0)

    def _symbols_with_klines(self, *, require_fresh: bool = True) -> tuple[list[tuple[str, str]], dict[str, Any]]:
        ensure_seeded(self.db)
        filt = list_scannable_main_board(
            self.db,
            as_of=target_trade_date(),
            min_bars=MIN_BARS,
            require_fresh=require_fresh,
        )
        name_map = {r.symbol: (r.name or "") for r in self.db.query(StockInfo).all()}
        out: list[tuple[str, str]] = []
        for sym in filt["symbols"]:
            name = name_map.get(sym) or lookup_name(self.db, sym) or ""
            out.append((sym, name))
        return out, filt

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
                # 附带 K 线数据用于后续买点信号检测
                best["_closes"] = [round(float(k.close), 4) for k in klines]
                best["_volumes"] = [float(k.volume) for k in klines]
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

    def _fundamental_prescreen(
        self, universe: list[tuple[str, str]]
    ) -> tuple[list[tuple[str, str]], dict[str, int]]:
        """第一层：对全量宇宙股做轻量基本面预筛，只保留合格股进入 K 线扫描。
        使用已有的财报快照 + PE 数据，不跑完整分析引擎。
        返回 (qualified_list, stats)。
        """
        if not universe:
            return [], {"total": 0, "qualified": 0, "rejected": 0}

        stats = {"total": len(universe), "qualified": 0, "rejected": 0, "no_data": 0}
        sym_list = [s for s, _ in universe]

        # 批量取 PE
        pe_map = self._pe_map(sym_list)

        # 财报快照
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
            debt_date = str(snap_date or "")
            if debt_date and not debt_date.endswith("1231") and len(debt_date) >= 4:
                debt_date = f"{debt_date[:4]}1231"
            if debt_date:
                debt_map = _fetch_debt_map(debt_date) or {}
        except Exception as exc:
            logger.warning("prescreen fundamental enrich failed: %s", exc)

        qualified: list[tuple[str, str]] = []
        for sym, name in universe:
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
                stats["rejected"] += 1
                continue
            if not fund and pe is None:
                # 无任何基本面数据，也跳过
                stats["no_data"] += 1
                continue
            qualified.append((sym, name))
            stats["qualified"] += 1

        return qualified, stats

    def scan_market(
        self,
        recent_bars: int = DEFAULT_RECENT_BARS,
        force: bool = False,
        *,
        require_fresh: bool = False,
        progress: ProgressCb | None = None,
    ) -> dict[str, Any]:
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
            if progress:
                progress(1, 1, "cache")
            return cached

        universe, filt = self._symbols_with_klines(require_fresh=require_fresh)

        # ── 第一层：基本面预筛（全量宇宙 → 合格股） ──
        if progress:
            progress(0, len(universe), "prescreen")
        qualified_universe, prescreen_stats = self._fundamental_prescreen(universe)
        if progress:
            progress(len(universe), len(universe), "prescreen")

        # ── 第二层：对合格股跑完整基本面分析 ──
        fund_scores: dict[str, dict[str, Any]] = {}
        fund_symbols = [sym for sym, _ in qualified_universe]
        total = len(fund_symbols)
        fund_start = time.time()
        if progress:
            progress(0, total, "fundamentals")
        if fund_symbols:
            fund_workers = min(FUND_WORKERS, max(1, len(fund_symbols)))
            # per-stock timeout：单只超时视为 error 跳过，不让慢股票拖死整批
            per_stock_timeout = 15.0
            # batch deadline：冷启动网络抖动时，不让整批超过 180s（前端 poll 上限 600s）
            batch_deadline = 180.0
            with ThreadPoolExecutor(max_workers=fund_workers) as pool:
                fund_futures = {
                    pool.submit(_compute_fundamental_score, sym): sym
                    for sym in fund_symbols
                }
                done = 0
                for fut in as_completed(fund_futures):
                    # 批级 deadline：超时则取消剩余任务，用已完成的部分继续
                    if time.time() - fund_start > batch_deadline:
                        remaining = sum(1 for f in fund_futures if not f.done())
                        if remaining:
                            logger.warning(
                                "market scan fundamentals BATCH DEADLINE %.1fs reached, cancelling %d remaining",
                                batch_deadline, remaining,
                            )
                            for f in fund_futures:
                                if not f.done():
                                    f.cancel()
                        break
                    sym = fund_futures[fut]
                    try:
                        fund_scores[sym] = fut.result(timeout=per_stock_timeout)
                    except TimeoutError:
                        logger.warning("fundamental analysis TIMEOUT for %s (>%.1fs), skipping", sym, per_stock_timeout)
                        fund_scores[sym] = {"score": None, "error": "timeout"}
                    except Exception:
                        fund_scores[sym] = {"score": None, "error": "unknown"}
                    done += 1
                    if progress:
                        progress(done, total, "fundamentals")
            logger.info(
                "market scan fundamentals: %d/%d symbols in %.1fs (workers=%d)",
                len(fund_scores), total, time.time() - fund_start, fund_workers,
            )

        # ── 第三层：构建展示数据 + 分层 ──
        enriched_items: list[dict] = []
        fund_no_score = 0
        for sym, name in qualified_universe:
            fund = fund_scores.get(sym) or {}
            fund_score = fund.get("score")
            if fund_score is None:
                fund_no_score += 1
                continue

            pe = fund.get("pe_ttm")
            profit_yoy = fund.get("profit_yoy")
            peg = _calc_peg(pe, profit_yoy)

            enriched = {
                "symbol": sym,
                "name": name,
                "industry": fund.get("industry") or "",
                "fundamental_score": round(fund_score, 1),
                "fundamental_level": _fund_level(fund_score),
                "combined_score": round(fund_score, 1),
                "peg": peg,
                "pe_ttm": pe,
                "price": fund.get("price"),
                "profit_yoy": profit_yoy,
                "roe": fund.get("roe"),
                "debt_ratio": fund.get("debt_ratio"),
                "fund_modules": {
                    "profitability": fund.get("profitability"),
                    "growth": fund.get("growth"),
                    "cashflow": fund.get("cashflow"),
                    "valuation": fund.get("valuation_score"),
                },
            }
            enriched_items.append(enriched)

        # 按基本面评分分层 A/B/C/D/E
        if progress:
            progress(total, total, "tier")
        items, tiers, tier_counts = _apply_tiers(enriched_items)

        payload = {
            "items": items,
            "tiers": tiers,
            "tier_counts": tier_counts,
            "count": len(items),
            "fund_analyzed": len(fund_scores),
            "fund_no_score": fund_no_score,
            "prescreen": prescreen_stats,
            "scanned": total,
            "universe_size": filt.get("universe_size", len(universe)),
            "prefiltered": filt.get("scannable", len(universe)),
            "prefilter": {
                "as_of": filt.get("as_of"),
                "no_kline": filt.get("no_kline", 0),
                "short_bars": filt.get("short_bars", 0),
                "stale_kline": filt.get("stale_kline", 0),
            },
            "skipped": 0,
            "errors": 0,
            "recent_bars": recent_bars,
            "cached": False,
            "cache_age_sec": 0,
            "description": (
                "基本面预筛 → 完整基本面分析 → 按评分分层；"
                f"全量 {prescreen_stats['total']} 只 → 基本面合格 {prescreen_stats['qualified']} 只 → "
                f"展示 {len(items)} 只；"
                "分层 A(≥85)/B(70-84)/C(55-69)/D(40-54)/E(<40)"
            ),
        }
        _cache["ts"] = now
        _cache["payload"] = payload
        _cache["version"] = CACHE_VERSION
        if progress:
            progress(total, total, "done")
        return payload

    def latest(self) -> dict[str, Any] | None:
        if _cache["payload"] is None or int(_cache.get("version") or 0) != CACHE_VERSION:
            return None
        now = time.time()
        payload = dict(_cache["payload"])
        payload["cached"] = True
        payload["cache_age_sec"] = int(now - float(_cache["ts"]))
        return payload
