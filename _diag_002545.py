# 诊断 002545：为什么空头趋势里看涨形态能过共振 → 核心持仓
"""用法: cd backend && ./venv/Scripts/python.exe ../_diag_002545.py [symbol]"""
import sys
from pathlib import Path

_root = Path(__file__).resolve().parent
for cand in (_root / "candle-flow" / "backend", _root / "backend", _root):
    if (cand / "app").exists():
        sys.path.insert(0, str(cand))
        break

from app.database import SessionLocal  # noqa: E402
from app.core.pattern_engine import PatternEngine, kline_to_candles  # noqa: E402
from app.core.confluence import evaluate_confluence, macd_at, rsi_at, _sma  # noqa: E402
from app.core.timeframe import weekly_trend_at  # noqa: E402
from app.services.kline_service import KlineService  # noqa: E402
from app.services.market_confluence_service import (  # noqa: E402
    KLINE_LIMIT,
    _combined_score,
    _is_candidate,
    _Job,
)

symbol = sys.argv[1] if len(sys.argv) > 1 else "002545"

db = SessionLocal()
klines, meta = KlineService(db).get_recent_klines(symbol, limit=KLINE_LIMIT)
db.close()
print(f"== {symbol} bars={len(klines)} latest={klines[-1].date} close={klines[-1].close}")

closes = [float(k.close) for k in klines]
n = len(klines) - 1
ma5, ma10, ma20 = _sma(closes, 5, n), _sma(closes, 10, n), _sma(closes, 20, n)
print(f"MA5={ma5:.2f} MA10={ma10:.2f} MA20={ma20:.2f}  "
      f"空头排列={ma5 < ma10 < ma20}")
m = macd_at(closes, n)
print(f"MACD DIF={m[0]:.3f} DEA={m[1]:.3f} hist={m[2]:.3f} prev_hist={m[3]:.3f}")
print(f"RSI14={rsi_at(closes, n):.2f}")
wk = weekly_trend_at(klines, n)
print(f"周线趋势={wk}  (down 时看涨形态才被硬否决)")

candles = kline_to_candles(klines)
results = PatternEngine(min_score=60.0).scan(candles)
print(f"\n-- PatternEngine 近期形态（含非 bullish）--")
for r in results[-8:]:
    print(f"  idx={r.candle_index} date={str(klines[r.candle_index].date)[:10]} "
          f"{r.direction:7s} {r.pattern_name} score={float(r.score):.1f}")

last_idx = len(klines) - 1
min_idx = max(0, last_idx - 1)  # recent_bars=2，与榜单口径一致
for r in results:
    if r.direction != "bullish" or not (min_idx <= r.candle_index <= last_idx):
        continue
    conf = evaluate_confluence(klines, r.candle_index, r.direction)
    print(f"\n== 看涨形态 {r.pattern_name} @ {str(klines[r.candle_index].date)[:10]} "
          f"score={float(r.score):.1f}")
    print(f"   ok={conf.ok} blocked={conf.blocked} effective={conf.effective_count} "
          f"count={conf.count}")
    print(f"   hits: {conf.label}")
    for h in conf.hits:
        print(f"     [{h.dimension}] {h.name}: {h.detail[:80]} w={h.weight}")
    print(f"   conflicts(硬): {conf.conflicts}")
    for sc in conf.soft_conflict_items:
        print(f"   soft({sc.kind}, x{sc.position_factor}): {sc.message[:90]}")
    cs = _combined_score(float(r.score), conf.effective_count, conf.soft_conflict_items)
    cand = _is_candidate(float(r.score), conf.effective_count, conf.soft_conflict_items)
    print(f"   combined={cs:.1f} candidate={cand}")
