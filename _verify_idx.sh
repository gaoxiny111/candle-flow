#!/bin/bash
cd /opt/candle-flow/backend || exit 1
.venv/bin/python - <<'PY'
from app.database import SessionLocal
from sqlalchemy import text

db = SessionLocal()
for sym in ("000300.SH", "000001.SH"):
    rows = db.execute(
        text("SELECT date, close FROM kline_data WHERE symbol=:s ORDER BY date DESC LIMIT 25"),
        {"s": sym},
    ).fetchall()
    print("=====", sym, "count=", len(rows))
    for d, c in reversed(rows):
        print(str(d), c)
PY
