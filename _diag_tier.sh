#!/bin/bash
cd /opt/candle-flow/backend || exit 1
.venv/bin/python - <<'PY'
import json
from collections import defaultdict
from statistics import median
from sqlalchemy import text
from app.database import SessionLocal

db = SessionLocal()
rows = db.execute(text(
    "SELECT json_extract(payload,'$.valuation.growth_stock_profile'), "
    "json_extract(payload,'$.market.pe_ttm'), "
    "json_extract(payload,'$.dim_scores.\"估值合理性\"'), "
    "json_extract(payload,'$.composite_score'), "
    "json_extract(payload,'$.name'), "
    "json_extract(payload,'$.industry') "
    "FROM factor_snapshots"
)).fetchall()

by_tier = defaultdict(list)
no_profile = 0
for gsp, pe, vs, cs, nm, ind in rows:
    if not gsp:
        no_profile += 1
        continue
    g = json.loads(gsp)
    tier = g.get("tier") or ("(非成长)" if not g.get("is_growth_stock") else "(unknown)")
    by_tier[tier].append((vs, cs, pe, nm, ind))

print("快照总数 =", len(rows), "| 无 growth_stock_profile =", no_profile)
print()
hdr = f"{'tier':<16}{'只数':>6}{'占比':>8}{'估值分中位':>11}{'综合分中位':>11}{'PE中位':>9}"
print(hdr)
tot = sum(len(v) for v in by_tier.values())
for tier, v in sorted(by_tier.items(), key=lambda x: -len(x[1])):
    vs = [x[0] for x in v if x[0] is not None]
    cs = [x[1] for x in v if x[1] is not None]
    pe = [x[2] for x in v if x[2] is not None and x[2] > 0]
    print(f"{tier:<16}{len(v):>6}{len(v)/tot*100:>7.1f}%{median(vs) if vs else 0:>11.1f}"
          f"{median(cs) if cs else 0:>11.1f}{median(pe) if pe else 0:>9.1f}")

print()
print("=== pe_distorted 按 PE 降序前 15 ===")
pd = by_tier.get("pe_distorted", [])
for vs, cs, pe, nm, ind in sorted(pd, key=lambda x: -(x[2] or 0))[:15]:
    print(f"  {str(nm):<10}{str(ind):<14}PE={pe:>8.1f} 估值={vs} 综合={cs}")
if pd:
    vv = [x[0] for x in pd if x[0] is not None]
    print("  pe_distorted 只数 =", len(pd), "| 估值分中位 =", median(vv), "| 均值 =", round(sum(vv)/len(vv), 1))

print()
print("=== turnaround 按 PE 降序前 12（含噪声级负 CAGR 的嫌疑票）===")
tu = by_tier.get("turnaround", [])
for vs, cs, pe, nm, ind in sorted(tu, key=lambda x: -(x[2] or 0))[:12]:
    print(f"  {str(nm):<10}{str(ind):<14}PE={pe:>8.1f} 估值={vs} 综合={cs}")
if tu:
    vv = [x[0] for x in tu if x[0] is not None]
    print("  turnaround 只数 =", len(tu), "| 估值分中位 =", median(vv))

print()
print("=== high_growth / star / hq_growth 对照（真成长）===")
for t in ("high_growth", "star", "hq_growth"):
    v = by_tier.get(t, [])
    if not v:
        continue
    vv = [x[0] for x in v if x[0] is not None]
    cs = [x[1] for x in v if x[1] is not None]
    pe = [x[2] for x in v if x[2] is not None and x[2] > 0]
    print(f"  {t:<12} n={len(v):<5} 估值中位={median(vv) if vv else 0:>6.1f} "
          f"综合中位={median(cs) if cs else 0:>6.1f} PE中位={median(pe) if pe else 0:>7.1f}")
    for vs, cs2, pe2, nm, ind in sorted(v, key=lambda x: -(x[2] or 0))[:5]:
        print(f"      {str(nm):<10}{str(ind):<14}PE={pe2:>8.1f} 估值={vs} 综合={cs2}")
PY
