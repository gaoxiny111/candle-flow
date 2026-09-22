# -*- coding: utf-8 -*-
"""条件2 深查：PE>行业均值x2 的分布合理性 + 行业均值字段质量。"""
import json
import sqlite3
import sys
from collections import defaultdict

sys.path.insert(0, "/opt/candle-flow/backend")
from app.analysis.engine import STRONG_CYCLICAL_INDUSTRIES  # noqa

conn = sqlite3.connect("/opt/candle-flow/backend/data/candle_flow.db")
conn.row_factory = sqlite3.Row

D = []
for r in conn.execute("select symbol, composite_score, pe_ttm, payload from factor_snapshots"):
    p = json.loads(r["payload"])
    mk = p.get("market") or {}
    rel = ((p.get("valuation") or {}).get("relative")) or {}
    e = rel.get("PE_TTM") or {}
    D.append({
        "sym": r["symbol"], "name": p.get("name") or "", "ind": p.get("industry") or "",
        "comp": r["composite_score"] or 0,
        "pe": r["pe_ttm"] if r["pe_ttm"] is not None else mk.get("pe_ttm"),
        "med": e.get("industry_median"), "prem": e.get("industry_premium_pct"),
        "pctl": e.get("percentile_5y"),
    })

N = len(D)
hit = [d for d in D if d["pe"] is not None and d["med"] and float(d["med"]) > 0
       and d["pe"] > 2 * float(d["med"])]
print("PE > 行业均值x2 = %d (%.2f%%)" % (len(hit), 100.0 * len(hit) / N))
print()

# 按行业统计命中率 —— 看是否集中在「行业均值极小」的行业
byind = defaultdict(lambda: [0, 0])
for d in D:
    if d["med"] is None:
        continue
    byind[d["ind"]][1] += 1
    if d["pe"] is not None and float(d["med"]) > 0 and d["pe"] > 2 * float(d["med"]):
        byind[d["ind"]][0] += 1

print("命中率最高的 20 个行业（命中/总数，行业均值中位数）：")
meds = defaultdict(list)
for d in D:
    if d["med"] is not None:
        meds[d["ind"]].append(float(d["med"]))
rows = sorted(byind.items(), key=lambda kv: -(kv[1][0] / max(1, kv[1][1])))[:20]
for ind, (h, t) in rows:
    if t < 3:
        continue
    m = sorted(meds[ind])[len(meds[ind]) // 2] if meds[ind] else 0
    print("   %-14s %3d/%-3d = %5.1f%%   行业PE均值中位=%.1f" % (ind, h, t, 100.0 * h / t, m))
print()

print("行业均值极小（<15）的行业 —— 双倍阈值形同虚设：")
low = [(k, sorted(v)[len(v) // 2]) for k, v in meds.items() if len(v) >= 5]
low.sort(key=lambda x: x[1])
for k, m in low[:15]:
    print("   %-14s 行业PE均值中位=%.2f  -> 2x = %.2f" % (k, m, 2 * m))
print()

print("=" * 78)
print("用户点名：立霸股份 PE 23.16 —— 它过得了这条吗？")
print("=" * 78)
for d in D:
    if d["sym"] == "603519.SH":
        print("  PE=%.2f  行业=%s  行业均值=%s  溢价=%s%%  分位=%s"
              % (d["pe"], d["ind"], d["med"], d["prem"], d["pctl"]))
        print("  -> PE 远低于行业均值(%.1f)，**这条排雷规则根本拦不住它**" % float(d["med"]))
print()

print("=" * 78)
print("对照：真正的高估值票（PE>80 且 comp>=70）")
print("=" * 78)
h = [d for d in D if d["pe"] is not None and d["pe"] > 80 and d["comp"] >= 70]
for d in sorted(h, key=lambda x: -x["comp"])[:18]:
    print("   %-11s %-9s comp=%-5.1f PE=%-8.1f 分位=%-5s 行业=%s"
          % (d["sym"], d["name"][:8], d["comp"], d["pe"],
             ("%.0f" % d["pctl"]) if d["pctl"] is not None else "-", d["ind"][:12]))
print("  共 %d 只" % len(h))
