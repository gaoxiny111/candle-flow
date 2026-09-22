# 用本地(修复后)代码回放服务器导出的 K 线
"""用法: venv python _diag_002545_replay.py <kline_json> [symbol]"""
import json
import sys
from pathlib import Path

_root = Path(__file__).resolve().parent
for cand in (_root / "candle-flow" / "backend", _root / "backend", _root):
    if (cand / "app").exists():
        sys.path.insert(0, str(cand))
        break

from app.core.candle import Candle  # noqa: E402
from app.core.pattern_engine import PatternEngine, kline_to_candles  # noqa: E402
from app.core.confluence import evaluate_confluence  # noqa: E402
from app.services.market_confluence_service import (  # noqa: E402
    _combined_score,
    _is_candidate,
)

data = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8-sig"))
rows = data["rows"] if isinstance(data, dict) and "rows" in data else data
symbol = sys.argv[2] if len(sys.argv) > 2 else (data.get("symbol", "?") if isinstance(data, dict) else "?")
from datetime import date as _date  # noqa: E402
from types import SimpleNamespace  # noqa: E402

klines = [
    SimpleNamespace(
        date=_date.fromisoformat(str(r["date"])[:10]),
        open=float(r["open"]),
        high=float(r["high"]),
        low=float(r["low"]),
        close=float(r["close"]),
        volume=float(r.get("volume") or 0),
    )
    for r in rows
]
print(f"== {symbol} bars={len(klines)}")

candles = kline_to_candles(klines)
results = PatternEngine(min_score=60.0).scan(candles)
last_idx = len(klines) - 1
min_idx = max(0, last_idx - 1)
for r in results:
    if r.direction != "bullish" or not (min_idx <= r.candle_index <= last_idx):
        continue
    conf = evaluate_confluence(klines, r.candle_index, r.direction)
    cs = _combined_score(float(r.score), conf.effective_count, conf.soft_conflict_items)
    cand = _is_candidate(float(r.score), conf.effective_count, conf.soft_conflict_items)
    print(f"  {r.pattern_name} score={float(r.score):.1f} "
          f"ok={conf.ok} eff={conf.effective_count} combined={cs:.1f} "
          f"candidate={cand}")
    print(f"    hits={conf.label}")
    print(f"    hard={conf.conflicts}")
    for sc in conf.soft_conflict_items:
        print(f"    soft({sc.kind}): {sc.message}")
