# 导出 002545 近 90 根 K 线为 JSON（供本地回放）
"""服务器: cd /opt/candle-flow/backend && ./.venv/bin/python /opt/candle-flow/_dump_kline.py 002545 > /tmp/_kline_002545.json"""
import json
import sys
from pathlib import Path

_root = Path(__file__).resolve().parent
for cand in (_root / "backend", _root):
    if (cand / "app").exists():
        sys.path.insert(0, str(cand))
        break

from app.services.kline_service import KlineService  # noqa: E402
from app.database import SessionLocal  # noqa: E402

symbol = sys.argv[1]
db = SessionLocal()
klines, _ = KlineService(db).get_recent_klines(symbol, limit=90)
db.close()
print(json.dumps({
    "symbol": symbol,
    "rows": [
        {
            "date": str(k.date)[:10],
            "open": float(k.open),
            "high": float(k.high),
            "low": float(k.low),
            "close": float(k.close),
            "volume": float(k.volume or 0),
        }
        for k in klines
    ],
}, ensure_ascii=False))
