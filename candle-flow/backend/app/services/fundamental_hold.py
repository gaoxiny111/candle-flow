"""Layer-4 hold management: add / reduce / exit on daily + weekly candles."""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Sequence

from sqlalchemy.orm import Session

from app.core.candle import Candle
from app.core.pattern_engine import PatternEngine, kline_to_candles
from app.core.timeframe import bars_to_candles, to_weekly
from app.core.windows import active_windows, collect_windows
from app.services.fundamental_screen import candidate_out, list_pool
from app.services.fundamental_tactics import IRON_RULES, _sma
from app.services.kline_service import KlineService
from app.services.valuation import get_valuations

logger = logging.getLogger(__name__)

PE_RICH_HOLD = 70.0
RECENT = 5

ADD_PATTERNS = frozenset({"上升三法", "上升窗口", "跳空并列阳线", "升窗回测"})
REDUCE_PATTERNS = frozenset({"上吊线", "流星线", "黄昏星", "十字黄昏星", "看跌吞没", "乌云盖顶"})
EXIT_WEEKLY = frozenset({"看跌吞没", "黄昏星", "十字黄昏星", "三只乌鸦", "乌云盖顶", "圆形顶部"})

REGIME_WEIGHTS = {
    "bull": {"fundamental": 0.4, "candle": 0.6, "tip": "侧重持续形态（三法、窗口），持股待涨"},
    "chop": {"fundamental": 0.6, "candle": 0.4, "tip": "低估区找买点、高估区找卖点，做波段"},
    "bear": {"fundamental": 0.8, "candle": 0.2, "tip": "空仓或极低仓位，仅极端低估+强底部试探"},
    "black_swan": {"fundamental": 0.0, "candle": 0.0, "tip": "暂停交易，等待秩序恢复"},
}


@dataclass
class HoldSignal:
    kind: str  # add | reduce | exit | hold
    reason: str
    strength: float = 60.0

    def to_dict(self) -> dict[str, Any]:
        return {"kind": self.kind, "reason": self.reason, "strength": round(self.strength, 1)}


@dataclass
class HoldResult:
    symbol: str
    action: str  # add | hold | reduce | exit
    label: str
    signals: list[HoldSignal] = field(default_factory=list)
    pe_percentile: float | None = None
    above_ma200: bool | None = None
    open_rising_window: bool = False
    regime_hint: str = ""
    warnings: list[str] = field(default_factory=list)
    notes: str = ""
    error: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "action": self.action,
            "label": self.label,
            "signals": [s.to_dict() for s in self.signals],
            "pe_percentile": self.pe_percentile,
            "above_ma200": self.above_ma200,
            "open_rising_window": self.open_rising_window,
            "regime_hint": self.regime_hint,
            "warnings": self.warnings,
            "notes": self.notes,
            "error": self.error,
        }


ACTION_LABEL = {
    "add": "可加仓",
    "hold": "持有跟踪",
    "reduce": "分批减仓",
    "exit": "清仓信号",
}


def _candle_date(c: Candle) -> str:
    ts = c.timestamp
    if isinstance(ts, datetime):
        return ts.date().isoformat()
    return str(ts)[:10]


def _long_upper_shadow_dry(candles: list[Candle], index: int) -> bool:
    """日线连续长上影且量能萎缩。"""
    if index < 3:
        return False
    avg = sum(c.volume for c in candles[max(0, index - 20) : index] if c.volume > 0)
    n = len([c for c in candles[max(0, index - 20) : index] if c.volume > 0])
    avg_vol = avg / n if n else 0
    count = 0
    for j in range(index - 2, index + 1):
        c = candles[j]
        body = abs(c.close - c.open)
        upper = c.high - max(c.open, c.close)
        if body <= 0:
            body = max(c.high - c.low, 1e-9) * 0.1
        if upper >= body * 1.5 and (avg_vol <= 0 or c.volume <= avg_vol * 0.85):
            count += 1
    return count >= 2


def _window_broken_unfilled(candles: list[Candle], index: int) -> bool:
    """跌破上升窗口且 3 日内未回补。"""
    zones = collect_windows(candles)
    for z in zones:
        if z.kind != "rising" or z.start_index >= index:
            continue
        # broken: close below bottom
        broken_at = None
        for j in range(z.start_index + 1, index + 1):
            if float(candles[j].close) <= z.bottom:
                broken_at = j
                break
        if broken_at is None:
            continue
        if index - broken_at < 2:
            continue
        # unfilled within 3 days after break
        filled = False
        for j in range(broken_at, min(broken_at + 4, index + 1)):
            if float(candles[j].close) > z.bottom:
                filled = True
                break
        if not filled:
            return True
    return False


def _recent_patterns(
    candles: list[Candle], engine: PatternEngine, names: frozenset[str], direction: str | None = None
) -> list[tuple[str, float, int]]:
    if len(candles) < 8:
        return []
    results = engine.scan(candles)
    n = len(candles)
    out = []
    for r in results:
        if r.candle_index < n - RECENT:
            continue
        if r.pattern_name not in names:
            continue
        if direction and r.direction != direction:
            continue
        out.append((r.pattern_name, float(r.score), r.candle_index))
    return out


def detect_market_regime(sample_candles: list[list[Candle]]) -> dict[str, Any]:
    """Classify regime from a sample of daily series (pool stocks)."""
    if not sample_candles:
        return {"regime": "chop", **REGIME_WEIGHTS["chop"], "sample": 0}

    bull = bear = chop = 0
    vol_spikes = 0
    for candles in sample_candles:
        if len(candles) < 60:
            chop += 1
            continue
        closes = [c.close for c in candles]
        idx = len(candles) - 1
        ma60 = _sma(closes, 60, idx)
        ma200 = _sma(closes, 200, idx) if idx >= 199 else _sma(closes, min(120, idx + 1), idx)
        close = closes[idx]
        # ATR-ish spike
        if idx >= 20:
            ranges = [candles[i].high - candles[i].low for i in range(idx - 19, idx + 1)]
            atr = sum(ranges) / 20
            recent = candles[idx].high - candles[idx].low
            if atr > 0 and recent > atr * 3.5:
                vol_spikes += 1
        if ma60 and close > ma60 and (ma200 is None or ma60 >= ma200 * 0.98):
            bull += 1
        elif ma60 and close < ma60 and (ma200 is None or close < ma200):
            bear += 1
        else:
            chop += 1

    n = max(bull + bear + chop, 1)
    if vol_spikes >= max(2, n // 3):
        regime = "black_swan"
    elif bull / n >= 0.55:
        regime = "bull"
    elif bear / n >= 0.55:
        regime = "bear"
    else:
        regime = "chop"

    meta = REGIME_WEIGHTS[regime]
    return {
        "regime": regime,
        "fundamental": meta["fundamental"],
        "candle": meta["candle"],
        "tip": meta["tip"],
        "sample": n,
        "bull_share": round(bull / n, 2),
        "bear_share": round(bear / n, 2),
    }


def hold_from_candles(
    daily: list[Candle],
    weekly: list[Candle],
    *,
    symbol: str,
    pe_percentile: float | None = None,
    profit_yoy: float | None = None,
    regime: str = "chop",
) -> HoldResult:
    warnings: list[str] = []
    signals: list[HoldSignal] = []

    if len(daily) < 40:
        return HoldResult(
            symbol=symbol,
            action="hold",
            label=ACTION_LABEL["hold"],
            pe_percentile=pe_percentile,
            notes="日线不足，仅持有观察",
            error="insufficient_klines",
        )

    if regime == "black_swan":
        return HoldResult(
            symbol=symbol,
            action="hold",
            label="暂停交易",
            pe_percentile=pe_percentile,
            regime_hint=REGIME_WEIGHTS["black_swan"]["tip"],
            warnings=["黑天鹅环境：暂停交易（策略表）"],
            notes="等待市场恢复秩序",
        )

    engine = PatternEngine(min_score=50.0)
    idx = len(daily) - 1
    closes = [c.close for c in daily]
    ma200 = _sma(closes, 200, idx)
    above_ma200 = ma200 is not None and closes[idx] >= ma200

    open_wins = [z for z in active_windows(daily, idx) if z.kind == "rising"]
    open_rising = bool(open_wins)

    # —— 清仓优先 ——
    if profit_yoy is not None and profit_yoy < -30:
        signals.append(HoldSignal("exit", "业绩大幅恶化（净利 YoY 深度为负），不等蜡烛图立即清仓", 95))
        warnings.append("基本面一票否决（铁律1）")

    weekly_exits = _recent_patterns(weekly, engine, EXIT_WEEKLY, "bearish") if len(weekly) >= 8 else []
    for name, score, _ in weekly_exits:
        signals.append(HoldSignal("exit", f"周线出现{name}，趋势反转确认清仓", score))

    if ma200 and closes[idx] < ma200:
        # 无法收回：近 5 日均在均线下方
        below = sum(1 for i in range(max(0, idx - 4), idx + 1) if closes[i] < ma200)
        if below >= 4:
            signals.append(HoldSignal("exit", "跌破200日均线且无法收回，长期趋势破坏", 80))

    # —— 减仓 ——
    rich = pe_percentile is not None and pe_percentile > PE_RICH_HOLD
    reduce_pats = _recent_patterns(daily, engine, REDUCE_PATTERNS, "bearish")
    if rich and reduce_pats:
        for name, score, _ in reduce_pats:
            signals.append(
                HoldSignal("reduce", f"PE分位>{PE_RICH_HOLD:.0f}% 且日线{name}，分批减仓", score)
            )
    elif reduce_pats and pe_percentile is not None and pe_percentile > 55:
        for name, score, _ in reduce_pats[:1]:
            signals.append(HoldSignal("reduce", f"估值偏高区域出现{name}，考虑减仓", score * 0.9))

    if _long_upper_shadow_dry(daily, idx):
        signals.append(HoldSignal("reduce", "连续长上影且量能萎缩，上方抛压加重", 65))

    if _window_broken_unfilled(daily, idx):
        signals.append(HoldSignal("reduce", "跌破上升窗口且3日内未回补，支撑失效减仓", 75))

    # —— 加仓 ——
    add_pats = _recent_patterns(daily, engine, ADD_PATTERNS, "bullish")
    if regime != "bear":
        for name, score, _ in add_pats:
            signals.append(HoldSignal("add", f"上升趋势中出现{name}，回调结束可加仓", score))
        if open_rising and not rich:
            signals.append(HoldSignal("add", "向上跳空窗口未回补，趋势强劲可持有或加仓", 70))

    if regime == "bear" and any(s.kind == "add" for s in signals):
        warnings.append("熊市环境降低蜡烛图权重，加仓信号仅作极小仓试探")
        signals = [s for s in signals if s.kind != "add"] + [
            HoldSignal("hold", "熊市：忽略加仓，维持极低仓位", 50)
        ]

    # 决议优先级：exit > reduce > add > hold
    kinds = {s.kind for s in signals}
    if "exit" in kinds:
        action = "exit"
    elif "reduce" in kinds:
        action = "reduce"
    elif "add" in kinds:
        action = "add"
    else:
        action = "hold"
        signals.append(HoldSignal("hold", "无明确加减仓信号，按持仓跟踪", 50))

    tip = REGIME_WEIGHTS.get(regime, REGIME_WEIGHTS["chop"])["tip"]
    return HoldResult(
        symbol=symbol,
        action=action,
        label=ACTION_LABEL.get(action, action),
        signals=signals,
        pe_percentile=pe_percentile,
        above_ma200=above_ma200,
        open_rising_window=open_rising,
        regime_hint=tip,
        warnings=warnings,
        notes=f"市场环境={regime}",
    )


def _load_series(db: Session, symbol: str, lookback: int = 500) -> tuple[list[Candle], list[Candle]]:
    kline_svc = KlineService(db)
    if kline_svc.get_latest(symbol) is None or kline_svc.is_contaminated(symbol):
        kline_svc.sync(symbol, force=True)
    klines, _ = kline_svc.get_recent_klines(symbol, limit=lookback)
    if len(klines) < 60:
        kline_svc.sync(symbol, force=True)
        klines, _ = kline_svc.get_recent_klines(symbol, limit=lookback)
    daily = kline_to_candles(klines)
    weekly = bars_to_candles(to_weekly(klines))
    return daily, weekly


def hold_one(
    db: Session,
    *,
    symbol: str,
    pe_percentile: float | None = None,
    profit_yoy: float | None = None,
    regime: str = "chop",
) -> HoldResult:
    try:
        daily, weekly = _load_series(db, symbol)
        return hold_from_candles(
            daily,
            weekly,
            symbol=symbol,
            pe_percentile=pe_percentile,
            profit_yoy=profit_yoy,
            regime=regime,
        )
    except Exception as e:
        logger.warning("hold_one %s failed: %s", symbol, e)
        return HoldResult(
            symbol=symbol,
            action="hold",
            label=ACTION_LABEL["hold"],
            pe_percentile=pe_percentile,
            notes=f"持仓扫描失败：{e}",
            error=str(e),
        )


def hold_pool(db: Session) -> dict[str, Any]:
    rows = list_pool(db)
    quotes = {v["symbol"]: v for v in get_valuations([r.symbol for r in rows], db=db)} if rows else {}

    # Preload a few series for regime
    sample: list[list[Candle]] = []
    series_cache: dict[str, tuple[list[Candle], list[Candle]]] = {}
    for row in rows[:12]:
        try:
            daily, weekly = _load_series(db, row.symbol)
            series_cache[row.symbol] = (daily, weekly)
            sample.append(daily)
        except Exception:
            continue
    regime_info = detect_market_regime(sample)
    regime = regime_info["regime"]

    items: list[dict[str, Any]] = []
    counts = {"add": 0, "hold": 0, "reduce": 0, "exit": 0}
    for row in rows:
        base = candidate_out(row, quotes.get(row.symbol))
        pe = base.get("pe_percentile")
        if pe is None and quotes.get(row.symbol):
            pe = quotes[row.symbol].get("pe_percentile")
        try:
            if row.symbol in series_cache:
                daily, weekly = series_cache[row.symbol]
            else:
                daily, weekly = _load_series(db, row.symbol)
            hold = hold_from_candles(
                daily,
                weekly,
                symbol=row.symbol,
                pe_percentile=float(pe) if pe is not None else None,
                profit_yoy=row.profit_yoy,
                regime=regime,
            )
        except Exception as e:
            hold = HoldResult(
                symbol=row.symbol,
                action="hold",
                label=ACTION_LABEL["hold"],
                notes=f"失败：{e}",
                error=str(e),
            )
        counts[hold.action] = counts.get(hold.action, 0) + 1
        base["hold"] = hold.to_dict()
        items.append(base)

    order = {"exit": 0, "reduce": 1, "add": 2, "hold": 3}
    items.sort(
        key=lambda x: (
            order.get((x.get("hold") or {}).get("action"), 9),
            -(x.get("score") or 0),
        )
    )
    return {
        "count": len(items),
        "counts": counts,
        "regime": regime_info,
        "items": items,
        "iron_rules": IRON_RULES,
        "note": "第四层持仓管理：加仓（三法/窗口）· 减仓（高估+顶部形态/破窗）· 清仓（基本面/周线反转/破MA200）。",
    }
