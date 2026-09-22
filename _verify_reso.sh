#!/bin/bash
API=http://127.0.0.1:8002/api/v1
cd /opt/candle-flow/backend || exit 1

echo "=== 1. restart vs source mtime ==="
systemctl show candle-flow -p ExecMainStartTimestamp
stat -c '%y  %n' app/core/confluence.py app/services/market_scan.py app/services/market_regime.py app/services/market_confluence_service.py

echo "=== 2. health ==="
curl -s -m 15 "$API/health"; echo

echo "=== 3. kline depth (180-bar window needs >=180) ==="
.venv/bin/python - <<'PY'
from app.database import SessionLocal
from sqlalchemy import text
db = SessionLocal()
for sym in ("600519.SH", "002545.SZ", "603136.SH", "000300.SH", "000001.SH"):
    n = db.execute(text("SELECT COUNT(*) FROM kline_data WHERE symbol=:s"), {"s": sym}).scalar()
    print(sym, "bars =", n)
PY

echo "=== 4. market regime ==="
curl -s -m 150 "$API/fundamentals/market-scan/market-regime"; echo
