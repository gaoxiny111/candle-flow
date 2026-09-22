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
    "SELECT symbol, json_extract(payload,'$.valuation.growth_stock_profile'), "
    "json_extract(payload,'$.modules.growth.indicators'), "
    "json_extract(payload,'$.dim_scores.\"估值合理性\"'), "
    "json_extract(payload,'$.market.pe_ttm'), "
    "json_extract(payload,'$.name'), json_extract(payload,'$.industry') "
    "FROM factor_snapshots"
)).fetchall()

buckets = [("<=-30", -999, -30), ("-30~-15", -30, -15), ("-15~-5", -15, -5),
           ("-5~0", -5, 0), ("0~10", 0, 10), (">10", 10, 9999)]
tu_stat = defaultdict(list)
all_cagr = []

for sym, gsp, inds, vs, pe, nm, ind in rows:
    if not gsp:
        continue
    g = json.loads(gsp)
    if g.get("tier") != "turnaround":
        continue
    cagr = None
    try:
        for it in (json.loads(inds) if inds else []):
            if "净利润3年CAGR" in str(it.get("name")):
                cagr = it.get("value")
                break
    except Exception:
        pass
    if cagr is None:
        continue
    all_cagr.append(cagr)
    for label, lo, hi in buckets:
        if lo < float(cagr) <= hi:
            tu_stat[label].append((nm, ind, cagr, pe, vs))
            break

print("turnaround 组（可解析出3年净利CAGR的）= ", len(all_cagr))
print(f"  3年净利CAGR 分布: min={min(all_cagr):.1f} p25={sorted(all_cagr)[len(all_cagr)//4]:.1f} "
      f"中位={median(all_cagr):.1f} p75={sorted(all_cagr)[len(all_cagr)*3//4]:.1f} max={max(all_cagr):.1f}")
print()
print(f"{'CAGR 分桶':<12}{'只数':>6}{'占比':>8}   示例（名称/PE/估值分）")
for label, _, _ in buckets:
    v = tu_stat.get(label, [])
    if not v:
        continue
    samp = "; ".join(f"{n}(PE{pe if pe else 0:.0f},估{vs})" for n, i, c, pe, vs in v[:4])
    print(f"{label:<12}{len(v):>6}{len(v)/len(all_cagr)*100:>7.1f}%   {samp}")

print()
print("=== 若把 turnaround 阈值收紧为 CAGR<=-15%，会剔除多少只 ===")
drop = sum(len(tu_stat.get(b, [])) for b in ("-15~-5", "-5~0", "0~10", ">10"))
keep = sum(len(tu_stat.get(b, [])) for b in ("<=-30", "-30~-15"))
print(f"  保留 = {keep} 只 | 剔除 = {drop} 只 | 剔除占比 = {drop/len(all_cagr)*100:.1f}%")
print()
print("=== 被剔除里综合分最高的 12 只（关注是否有误伤的真反转票）===")
pool = []
for b in ("-15~-5", "-5~0", "0~10", ">10"):
    pool.extend(tu_stat.get(b, []))
pool.sort(key=lambda x: -(x[4] or 0))
for nm, ind, cagr, pe, vs in pool[:12]:
    print(f"  {str(nm):<10}{str(ind):<14}CAGR={cagr:>7.1f}% PE={pe if pe else 0:>9.1f} 估值={vs}")
PY
