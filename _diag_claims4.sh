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

for sym in ["600722.SH","603407.SH","603995.SH","002705.SZ"]:
    p = data.get(sym)
    mkt = p.get("market") or {}
    print("=" * 96)
    print(f"{sym} {p.get('name')} PE={mkt.get('pe_ttm')} PB={mkt.get('pb')} "
          f"估值维度={ (p.get('dim_scores') or {}).get('估值合理性') }")
    # 找到所有含 valuation 字样的键
    vk = [k for k in p if 'val' in k.lower()]
    print("  含 val 的键:", vk)
    rel = p.get("relative") or {}
    for m in ("PE_TTM", "PB"):
        r = rel.get(m)
        if r:
            print(f"  {m}: 当前={r.get('current')} 5年分位={r.get('percentile_5y')} "
                  f"行业中位={r.get('industry_median')} 溢价={r.get('industry_premium_pct')} 信号={r.get('signal')}")
    for k in vk:
        v = p.get(k)
        if isinstance(v, dict) and 'breakdown' in k:
            for b in (v or []):
                print(f"     {b.get('factor'):<12} {b.get('points'):<7} {b.get('detail')}")
        elif isinstance(v, dict):
            for kk, vv in v.items():
                if isinstance(vv, (int, float, str)):
                    print(f"     {k}.{kk} = {vv}")
    print("  rationale:", (p.get('valuation_rationale') or '')[:400])
    print("  全部顶层键:", ", ".join(sorted(p.keys())))
PY
