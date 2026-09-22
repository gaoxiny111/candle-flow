#!/bin/bash
API=http://127.0.0.1:8002/api/v1
cd /opt/candle-flow/backend || exit 1
PY=.venv/bin/python

echo "=== A. market regime (stale-basis fix) ==="
curl -s -m 180 "$API/fundamentals/market-scan/market-regime" > /tmp/regime.json
$PY - <<'PY'
import json
d = json.load(open("/tmp/regime.json"))["data"]
print("regime =", d["regime"], d["label"], "| scoring_impact =", d["scoring_impact"])
print("basis  =", d.get("basis"))
print("stale  =", d.get("stale_indexes"))
for it in d["items"]:
    print("  -", it["name"], it["regime"], "as_of=", it.get("as_of"), "stale=", it.get("stale"))
    print("    ", it.get("reason"))
PY

echo "=== B. PEG 口径核查（快照值 vs 单期同比重算）==="
$PY - <<'PY'
from sqlalchemy import text
from app.database import SessionLocal

db = SessionLocal()
rows = db.execute(text(
    "SELECT symbol, json_extract(payload,'$.name'), json_extract(payload,'$.relative.PEG.value'), "
    "json_extract(payload,'$.relative.PEG.growth_label'), json_extract(payload,'$.profit_yoy'), "
    "json_extract(payload,'$.market.pe_ttm'), json_extract(payload,'$.industry') "
    "FROM factor_snapshots WHERE symbol IN ('601088.SH','600519.SH','600777.SH','600019.SH')"
)).fetchall()
for sym, name, peg, label, py, pe, ind in rows:
    naive = round(pe / py, 2) if (pe and py and py > 0) else None
    print(f"{sym} {name} [{ind}] engine_PEG={peg} ({label}) 单期同比={py} PE={pe} -> 旧口径PEG={naive}")
PY
