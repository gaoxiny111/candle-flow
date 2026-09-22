#!/bin/bash
cd /opt/candle-flow/backend || exit 1
.venv/bin/python - <<'PY'
import json
from sqlalchemy import text
from app.database import SessionLocal

db = SessionLocal()
rows = db.execute(text(
    "SELECT symbol, payload FROM factor_snapshots WHERE symbol IN "
    "('600722.SH','603995.SH','002705.SZ','603407.SH')"
)).fetchall()
data = {s: json.loads(p) for s, p in rows}

for sym in ["600722.SH","603995.SH","002705.SZ","603407.SH"]:
    p = data.get(sym)
    if not p: continue
    mkt = p.get("market") or {}
    print("=" * 92)
    print(f"{sym} {p.get('name')} [{p.get('industry')}] 综合={p.get('composite_score')} "
          f"PE={mkt.get('pe_ttm')} PB={mkt.get('pb')} 股息={mkt.get('dividend_yield')}")
    print("  维度分:", " | ".join(f"{k}={v}" for k, v in (p.get("dim_scores") or {}).items()))
    mods = p.get("modules") or {}
    for key in ("valuation",):
        m = mods.get(key) or {}
        print(f"  ── {key} score={m.get('score')} level={m.get('level')} ──")
        for ind in (m.get("indicators") or []):
            print(f"     {ind.get('name'):<20} 值={str(ind.get('value')):<12} 分={str(ind.get('score')):<7}"
                  f" {str(ind.get('level')):<3} w={ind.get('weight')}")
            c = ind.get("comment")
            if c: print(f"        ↳ {c}")
        for w in (m.get("warnings") or []):
            print(f"     ⚠ {w}")
    g = mods.get("growth") or {}
    print(f"  ── growth score={g.get('score')} level={g.get('level')} ──")
    for ind in (g.get("indicators") or []):
        print(f"     {ind.get('name'):<20} 值={str(ind.get('value')):<12} 分={str(ind.get('score')):<7} w={ind.get('weight')}")
PY
