#!/bin/bash
# 周期陷阱判据的**真实**爆炸半径（用快照 payload 复算，ROE 取 profitability.indicators）
cd /opt/candle-flow/backend || exit 1
.venv/bin/python -u - <<'PY'
import json
from collections import Counter
from sqlalchemy import text
from app.database import SessionLocal
STRONG_CYCLICAL_INDUSTRIES = frozenset({
    "\u666e\u94a2", "\u7279\u94a2\u2161", "\u51b6\u94a2\u539f\u6599",
    "\u5de5\u4e1a\u91d1\u5c5e", "\u5c0f\u91d1\u5c5e", "\u80fd\u6e90\u91d1\u5c5e",
    "\u8d35\u91d1\u5c5e", "\u91d1\u5c5e\u65b0\u6750\u6599",
    "\u7164\u70ad\u5f00\u91c7", "\u7126\u70ad\u2161",
    "\u5316\u5b66\u539f\u6599", "\u5316\u5b66\u5236\u54c1", "\u5316\u5b66\u7ea4\u7ef4",
    "\u519c\u5316\u5236\u54c1", "\u5851\u6599", "\u6a61\u80f6",
    "\u73bb\u7483\u73bb\u7ea4", "\u6c34\u6ce5", "\u975e\u91d1\u5c5e\u6750\u6599\u2161",
    "\u70bc\u5316\u53ca\u8d38\u6613", "\u6cb9\u670d\u5de5\u7a0b", "\u6cb9\u6c14\u5f00\u91c7\u2161",
    "\u822a\u8fd0\u6e2f\u53e3", "\u822a\u7a7a\u673a\u573a",
    "\u623f\u5730\u4ea7\u5f00\u53d1", "\u9020\u7eb8", "\u517b\u6b96\u4e1a", "\u7eba\u7ec7\u5236\u9020",
})

def cycle_trap_hit(pe_pct, pb_pct, roe):
    if pe_pct is None or pb_pct is None or roe is None:
        return False
    return float(pe_pct) <= 30.0 and float(pb_pct) >= 70.0 and float(roe) >= 15.0

db = SessionLocal()


def f(v):
    try:
        return None if v is None else float(v)
    except (TypeError, ValueError):
        return None


def roe_of(p):
    for it in ((p.get("modules") or {}).get("profitability") or {}).get("indicators") or []:
        if str(it.get("name") or "").startswith("ROE"):
            return f(it.get("value"))
    return None


stat = Counter()
flagged = []
for sym, name, ind, payload in db.execute(text(
    "SELECT symbol, json_extract(payload,'$.name'), json_extract(payload,'$.industry'), payload "
    "FROM factor_snapshots"
)):
    if ind not in STRONG_CYCLICAL_INDUSTRIES:
        continue
    p = json.loads(payload)
    stat["强周期总数"] += 1
    rel = (p.get("valuation") or {}).get("relative") or {}
    pe_pct = f((rel.get("PE_TTM") or {}).get("percentile_5y"))
    pb_pct = f((rel.get("PB") or {}).get("percentile_5y"))
    roe = roe_of(p)
    if roe is None:
        stat["ROE缺失→不预警"] += 1
        continue
    if pe_pct is None or pb_pct is None:
        stat["分位缺失→不预警"] += 1
        continue
    if cycle_trap_hit(pe_pct, pb_pct, roe):
        stat["✅ 命中 → 折减12分"] += 1
        v = p.get("valuation") or {}
        flagged.append((
            sym, name, ind, pe_pct, pb_pct, roe,
            f(v.get("composite_valuation_score")), f(p.get("composite_score")),
            f(v.get("valuation_score_haircut")) or 0.0,
        ))
    else:
        stat["不命中"] += 1

print("=" * 118)
print("强周期行业（精确集合）下的周期陷阱命中统计")
print("=" * 118)
for k, n in stat.most_common():
    print(f"  {k:<26}{n:>6}")

print(f"\n命中明细（{len(flagged)} 只）。'折减后估值分'= 现值 - 12（下限 20）")
print(f"  {'代码':<11}{'名称':<10}{'行业':<10}{'PE分位':>7}{'PB分位':>7}{'ROE':>7}"
      f"{'估值分':>8}{'折减后':>8}{'综合分':>8}{'现折减':>7}")
for sym, name, ind, a, b, c, vs, cs, hc in sorted(flagged, key=lambda r: -(r[7] or 0)):
    after = max(20.0, (vs or 20) - 12.0)
    print(f"  {sym:<11}{(name or '')[:8]:<10}{ind:<10}{a:>7.0f}{b:>7.0f}{c:>7.1f}"
          f"{(vs or -1):>8.0f}{after:>8.0f}{(cs or -1):>8.1f}{hc:>7.0f}")

print("\n" + "=" * 118)
print("用户点名标的")
print("=" * 118)
for sym, nm in (("603995.SH", "甬金股份"), ("600722.SH", "金牛化工"),
                ("600309.SH", "万华化学"), ("600019.SH", "宝钢股份")):
    r = db.execute(text(
        "SELECT json_extract(payload,'$.industry'), payload FROM factor_snapshots WHERE symbol=:s"
    ), {"s": sym}).fetchone()
    if not r:
        print(f"  {sym} {nm}: 无快照")
        continue
    ind, payload = r
    p = json.loads(payload)
    rel = (p.get("valuation") or {}).get("relative") or {}
    pe_pct = f((rel.get("PE_TTM") or {}).get("percentile_5y"))
    pb_pct = f((rel.get("PB") or {}).get("percentile_5y"))
    roe = roe_of(p)
    in_set = ind in STRONG_CYCLICAL_INDUSTRIES
    hit = cycle_trap_hit(pe_pct, pb_pct, roe)
    print(f"  {sym} {nm} industry={ind!r} 在强周期集合={in_set}")
    print(f"    PE分位={pe_pct} PB分位={pb_pct} ROE={roe} → 周期陷阱={hit}")
PY
