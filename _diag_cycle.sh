#!/bin/bash
# 周期陷阱判据的线上爆炸半径评估（只读，不写库）
cd /opt/candle-flow/backend || exit 1
.venv/bin/python -u - <<'PY'
import json
from collections import Counter
from sqlalchemy import text
from app.database import SessionLocal

db = SessionLocal()

CYCLE_KW = ("化工", "农化", "农化制品", "化肥", "磷", "矿", "煤炭", "有色", "钢铁", "石油", "天然气", "农药")

print("=" * 116)
print("A. 行业字符串盘点（快照 payload 里 industry 的实际取值，看 _cycle_kw 覆盖度）")
print("=" * 116)
rows = db.execute(text(
    "SELECT json_extract(payload,'$.industry') ind, COUNT(*) n FROM factor_snapshots "
    "WHERE json_extract(payload,'$.industry') IS NOT NULL GROUP BY ind ORDER BY n DESC"
)).fetchall()
print(f"  行业名总数 = {len(rows)}")
hit = [(i, n) for i, n in rows if any(k in str(i) for k in CYCLE_KW)]
print(f"  命中 _cycle_kw 的行业名 {len(hit)} 个，覆盖 {sum(n for _, n in hit)} 只：")
for i, n in hit[:40]:
    print(f"    {i:<18}{n:>5}")
print("\n  未命中的前 30 个行业名（若含周期行业则说明关键词有缺口）：")
miss = [(i, n) for i, n in rows if not any(k in str(i) for k in CYCLE_KW)]
for i, n in miss[:30]:
    print(f"    {i:<18}{n:>5}")

print("\n" + "=" * 116)
print("B. 周期陷阱判据命中统计（PE分位≤30 且 PB分位≥70 且 ROE≥15）")
print("=" * 116)


def f(v):
    try:
        return None if v is None else float(v)
    except (TypeError, ValueError):
        return None


stat = Counter()
flagged = []
for sym, name, ind, payload in db.execute(text(
    "SELECT symbol, json_extract(payload,'$.name'), json_extract(payload,'$.industry'), payload "
    "FROM factor_snapshots"
)):
    if not ind or not any(k in str(ind) for k in CYCLE_KW):
        continue
    try:
        p = json.loads(payload)
    except Exception:
        continue
    rel = ((p.get("valuation") or {}).get("relative")) or {}
    pe_pct = f((rel.get("PE_TTM") or {}).get("percentile_5y"))
    pb_pct = f((rel.get("PB") or {}).get("percentile_5y"))
    roe = f(p.get("latest_roe")) or f((p.get("market") or {}).get("latest_roe"))
    mkt = p.get("market") or {}
    if roe is None:
        roe = f(mkt.get("roe"))
    stat["周期股总数"] += 1
    if pe_pct is None or pb_pct is None:
        stat["分位缺失→不预警"] += 1
        continue
    if roe is None:
        stat["ROE缺失→不预警"] += 1
        continue
    if pe_pct <= 30 and pb_pct >= 70 and roe >= 15:
        stat["命中 → 折减 12 分"] += 1
        flagged.append((sym, name, ind, pe_pct, pb_pct, roe,
                        f((p.get("valuation") or {}).get("score")),
                        f(p.get("composite_score"))))
    else:
        stat["不命中"] += 1

for k, n in stat.most_common():
    print(f"  {k:<24}{n:>6}")
print(f"\n  命中明细（前 25）：")
print(f"    {'代码':<11}{'名称':<11}{'行业':<10}{'PE分位':>8}{'PB分位':>8}{'ROE':>7}{'估值分':>8}{'综合分':>8}")
for sym, name, ind, a, b, c, vs, cs in flagged[:25]:
    print(f"    {sym:<11}{(name or '')[:9]:<11}{str(ind)[:8]:<10}{a:>8.0f}{b:>8.0f}{c:>7.1f}"
          f"{(vs if vs is not None else -1):>8.0f}{(cs if cs is not None else -1):>8.1f}")

print("\n" + "=" * 116)
print("C. 用户点名标的的判据输入")
print("=" * 116)
NAMED = [("603995.SH", "甬金股份"), ("600722.SH", "金牛化工"), ("600309.SH", "万华化学"),
         ("603659.SH", "璞泰来")]
for sym, nm in NAMED:
    r = db.execute(text(
        "SELECT json_extract(payload,'$.industry'), payload FROM factor_snapshots WHERE symbol=:s"
    ), {"s": sym}).fetchone()
    if not r:
        print(f"  {sym} {nm}: 无快照")
        continue
    ind, payload = r
    p = json.loads(payload)
    rel = ((p.get("valuation") or {}).get("relative")) or {}
    mkt = p.get("market") or {}
    print(f"  {sym} {nm}  industry={ind!r}")
    print(f"    PE={mkt.get('pe_ttm')} PE分位={(rel.get('PE_TTM') or {}).get('percentile_5y')} "
          f"PB={mkt.get('pb')} PB分位={(rel.get('PB') or {}).get('percentile_5y')} "
          f"ROE={p.get('latest_roe')}")
    print(f"    估值分={(p.get('valuation') or {}).get('score')} "
          f"综合分={p.get('composite_score')} 成长股={(p.get('valuation') or {}).get('is_growth_stock')}")
    print("    周期陷阱=" + str((p.get("valuation") or {}).get("cycle_trap_warning")))
PY
