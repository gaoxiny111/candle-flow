#!/bin/bash
cd /opt/candle-flow/backend || exit 1
.venv/bin/python - <<'PY'
from statistics import median
from collections import Counter
from sqlalchemy import text
from app.database import SessionLocal

db = SessionLocal()

print("########## B. 估值模块对绝对 PE 的区分度（按 PE 分桶看估值分中位数）##########")
buckets = [("0~10", 0, 10), ("10~20", 10, 20), ("20~40", 20, 40),
           ("40~80", 40, 80), ("80~150", 80, 150), ("150+", 150, 10**9)]
print(f"{'PE 区间':<10}{'只数':>6}{'估值分中位':>11}{'估值分均值':>11}{'豁免占比':>10}{'综合分中位':>11}")
for label, lo, hi in buckets:
    rows = db.execute(text(
        "SELECT json_extract(payload,'$.dim_scores.\"估值合理性\"') vs, "
        "json_extract(payload,'$.composite_score') cs, "
        "json_extract(payload,'$.valuation.is_growth_stock') ig "
        "FROM factor_snapshots WHERE json_extract(payload,'$.market.pe_ttm') > :lo "
        "AND json_extract(payload,'$.market.pe_ttm') <= :hi"
    ), {"lo": lo, "hi": hi}).fetchall()
    vs = [r[0] for r in rows if r[0] is not None]
    cs = [r[1] for r in rows if r[1] is not None]
    ig = sum(1 for r in rows if r[2])
    if not vs: continue
    print(f"{label:<10}{len(rows):>6}{median(vs):>11.1f}{sum(vs)/len(vs):>11.1f}"
          f"{ig/len(rows)*100:>9.0f}%{median(cs) if cs else 0:>11.1f}")

print()
print("########## C. 亏损股（PE 为负/缺失）的估值分 ##########")
rows = db.execute(text(
    "SELECT json_extract(payload,'$.dim_scores.\"估值合理性\"') vs, "
    "json_extract(payload,'$.valuation.is_growth_stock') ig, "
    "json_extract(payload,'$.composite_score') cs FROM factor_snapshots "
    "WHERE json_extract(payload,'$.market.pe_ttm') IS NULL "
    "OR json_extract(payload,'$.market.pe_ttm') <= 0"
)).fetchall()
vs = [r[0] for r in rows if r[0] is not None]
cs = [r[2] for r in rows if r[2] is not None]
if vs:
    print(f"PE<=0 或缺失 {len(rows)} 只 | 估值分 中位={median(vs):.1f} 均值={sum(vs)/len(vs):.1f} "
          f"min={min(vs):.1f} max={max(vs):.1f} | 综合分中位={median(cs) if cs else 0:.1f}")
    print("  豁免占比 =", f"{sum(1 for r in rows if r[1])/len(rows)*100:.0f}%")

print()
print("########## D. 共振榜单：买点信号 / 档位 全量分布 ##########")
from app.services.market_scan import resonance_view
data = resonance_view(db, top=100000, exclude_st=True, include_gem=False)
items = data.get("items") or []
print("榜单条目 =", len(items), "| matched =", data.get("matched"))
for fld in ("verdict_label", "buy_signal", "buy_label"):
    c = Counter(str(it.get(fld)) for it in items)
    print(f"  {fld:<14}", dict(c.most_common(8)))
withfund = sum(1 for it in items if (it.get("composite_score") or 0) >= 80)
peg_ok = sum(1 for it in items if it.get("peg") is not None)
peg_lt15 = sum(1 for it in items if it.get("peg") is not None and it["peg"] < 1.5)
above = sum(1 for it in items if "站上MA20" in str(it.get("buy_reasons")))
below = sum(1 for it in items if "低于MA20" in str(it.get("buy_reasons")))
print(f"  基本面≥80 的 = {withfund} | PEG 有值 = {peg_ok} | PEG<1.5 = {peg_lt15}")
print(f"  买点理由含「站上MA20」= {above} | 含「低于MA20」= {below}")
print("  → strong_buy 需 基本面≥80 且 PEG<1.5 且 站上MA20 且 放量：同时满足上限估算 =",
      sum(1 for it in items if (it.get("composite_score") or 0) >= 80
          and it.get("peg") is not None and it["peg"] < 1.5
          and "站上MA20" in str(it.get("buy_reasons"))))
PY
