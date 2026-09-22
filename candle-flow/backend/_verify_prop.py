# -*- coding: utf-8 -*-
"""核验用户第四轮建议的四维度补丁：逐条测命中率与误杀。"""
import json, sqlite3, sys, collections
sys.path.insert(0, ".")
from app.analysis.engine import STRONG_CYCLICAL_INDUSTRIES

def f(x):
    try:
        return None if x is None else float(x)
    except (TypeError, ValueError):
        return None

DB = "/opt/candle-flow/backend/data/candle_flow.db"
con = sqlite3.connect(DB)
rows = con.execute("SELECT symbol, payload FROM factor_snapshots").fetchall()
print("total", len(rows))

# ── 红线1：PE > 行业均值 × 1.5 ──
r1 = []
# ── 红线2：PE > 行业均值×1.5 或 PB > 9 ──
pb9 = []
# ── 红线3：连续2季 营收<0 且 净利>30% ──
r3 = []
# ── 红线4：换手率（快照是否有？）──
to_key = collections.Counter()
# ── 利润质量：OCF/净利 连续2季<0.8 ──
r5 = []
avail = collections.Counter()
for sym, pay in rows:
    try:
        p = json.loads(pay)
    except Exception:
        continue
    v = p.get("valuation") or {}
    rel = v.get("relative") or {}
    ind = ((p.get("modules") or {}).get("profitability") or {}).get("indicators") or []
    rel_pe = rel.get("PE_TTM") or {}
    cur = f(rel_pe.get("current"))
    med = f(rel_pe.get("industry_median"))
    pb = f(p.get("pb")) or f(v.get("pb"))
    if cur is not None and med is not None and med > 0:
        avail["pe_AND_median"] += 1
        if cur > med * 1.5:
            r1.append((sym, p.get("name"), p.get("industry"), cur, med, f(p.get("composite_score"))))
    if pb is not None and pb > 9:
        pb9.append((sym, p.get("name"), pb, f(p.get("composite_score"))))

print(f"\n红线1「PE > 行业均值×1.5」命中 {len(r1)} 只 (分母 {avail['pe_AND_median']})")
print(f"红线2b「PB > 9」命中 {len(pb9)} 只")
# 换手率字段是否存在
sample = None
for sym, pay in rows[:200]:
    try: p = json.loads(pay)
    except: continue
    sample = p; break
if sample:
    print("\n快照顶层键:", sorted(sample.keys()))
    print("market 键:", sorted((sample.get("market") or {}).keys()))
con.close()
