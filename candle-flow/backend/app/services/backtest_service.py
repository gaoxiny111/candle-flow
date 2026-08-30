"""Walk historical candles and simulate Nison signals with pattern stops."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import date, datetime
from typing import Any

from app.core.confluence import evaluate_confluence
from app.core.nison_rules import NEEDS_NEXT_CONFIRM, NO_CHASE, is_extended_high, pattern_stop
from app.core.pattern_engine import PatternEngine, kline_to_candles
from app.core.price_targets import resolve_take_profits


@dataclass
class BacktestTrade:
    date: str
    exit_date: str
    pattern: str
    direction: str
    entry: float
    stop: float
    exit: float
    r_multiple: float
    result: str
    confluence: str


def _ymd(value) -> str:
    if isinstance(value, datetime):
        return value.date().isoformat()
    if isinstance(value, date):
        return value.isoformat()
    return str(value)[:10]


def run_backtest(klines, lookback_hold: int = 20) -> dict[str, Any]:
    if len(klines) < 40:
        return {"trades": [], "count": 0, "wins": 0, "win_rate": 0.0, "avg_r": 0.0, "sum_r": 0.0}
    candles = kline_to_candles(klines)
    engine = PatternEngine(min_score=60)
    hits = engine.scan(candles)
    trades: list[BacktestTrade] = []
    busy_until = -1
    for r in sorted(hits, key=lambda x: x.candle_index):
        i = r.candle_index
        if i <= busy_until or i >= len(klines) - 1:
            continue
        if r.direction == "neutral":
            continue
        if r.pattern_name in NO_CHASE and is_extended_high(klines, i):
            continue
        if r.pattern_name in NEEDS_NEXT_CONFIRM and i >= len(klines) - 1:
            continue
        conf = evaluate_confluence(klines, i, r.direction)
        if not conf.ok:
            continue
        entry = float(klines[i].close)
        stop = pattern_stop(klines, i, r.direction, r.pattern_name)
        if stop is None:
            continue
        risk = abs(entry - stop)
        if risk <= 0:
            continue
        bullish = r.direction == "bullish"
        tp, _, _ = resolve_take_profits(klines, i, r.direction, r.pattern_name, entry, stop)
        exit_px = None
        exit_i = None
        reason = "到期"
        for j in range(i + 1, min(len(klines), i + 1 + lookback_hold)):
            k = klines[j]
            if bullish:
                if float(k.low) <= stop:
                    exit_px, reason, exit_i = stop, "止损", j
                    break
                if float(k.close) >= tp:
                    exit_px, reason, exit_i = tp, "测幅/止盈", j
                    break
            else:
                if float(k.high) >= stop:
                    exit_px, reason, exit_i = stop, "止损", j
                    break
                if float(k.close) <= tp:
                    exit_px, reason, exit_i = tp, "测幅/止盈", j
                    break
        if exit_px is None:
            exit_i = min(len(klines) - 1, i + lookback_hold)
            exit_px = float(klines[exit_i].close)
            reason = "到期"
        r_mult = (exit_px - entry) / risk if bullish else (entry - exit_px) / risk
        trades.append(
            BacktestTrade(
                date=_ymd(klines[i].date),
                exit_date=_ymd(klines[exit_i].date),
                pattern=r.pattern_name,
                direction=r.direction,
                entry=round(entry, 4),
                stop=round(float(stop), 4),
                exit=round(float(exit_px), 4),
                r_multiple=round(r_mult, 2),
                result=reason,
                confluence=conf.label,
            )
        )
        busy_until = exit_i
    wins = sum(1 for t in trades if t.r_multiple > 0)
    sum_r = sum(t.r_multiple for t in trades)
    return {
        "trades": [asdict(t) for t in trades],
        "count": len(trades),
        "wins": wins,
        "win_rate": round(wins / len(trades) * 100, 1) if trades else 0.0,
        "avg_r": round(sum_r / len(trades), 2) if trades else 0.0,
        "sum_r": round(sum_r, 2),
    }
