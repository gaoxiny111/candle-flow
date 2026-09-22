#!/bin/bash
cd /opt/candle-flow/backend || exit 1
.venv/bin/python - <<'PY'
import json
from collections import Counter
from sqlalchemy import text
from app.database import SessionLocal

db = SessionLocal()

print("########## A. is_growth_stock / PE>80 交叉影响面（factor_snapshots 全库）##########")
q = lambda s: db.execute(text(s)).scalar() or 0
tot = q("SELECT count(*) FROM factor_snapshots")
ig  = q("SELECT count(*) FROM factor_snapshots WHERE json_extract(payload,'$.valuation.is_growth_stock')=1")
idiv= q("SELECT count(*) FROM factor_snapshots WHERE json_extract(payload,'$.valuation.is_dividend_asset')=1")
pe80= q("SELECT count(*) FROM factor_snapshots WHERE json_extract(payload,'$.market.pe_ttm')>80")
pe80_ig = q("SELECT count(*) FROM factor_snapshots WHERE json_extract(payload,'$.market.pe_ttm')>80 "
            "AND json_extract(payload,'$.valuation.is_growth_stock')=1")
pe80_idiv = q("SELECT count(*) FROM factor_snapshots WHERE json_extract(payload,'$.market.pe_ttm')>80 "
              "AND json_extract(payload,'$.valuation.is_dividend_asset')=1")
print(f"快照总数        = {tot}")
print(f"is_growth_stock = {ig} ({ig/tot*100:.1f}%)")
print(f"is_dividend     = {idiv} ({idiv/tot*100:.1f}%)")
print(f"PE_TTM > 80     = {pe80} ({pe80/tot*100:.1f}%)")
print(f"  其中 is_growth= {pe80_ig} ({pe80_ig/max(pe80,1)*100:.1f}% of PE>80)")
print(f"  其中 is_div   = {pe80_idiv} ({pe80_idiv/max(pe80,1)*100:.1f}%)")
print("  → 「PE>80 → 判成长 → 估值不判高估」的闭环覆盖面 =",
      f"{pe80_ig/max(pe80,1)*100:.0f}% 的高 PE 票")

print()
print("########## B. 高 PE 票的估值分实际分布 ##########")
rows = db.execute(text(
    "SELECT json_extract(payload,'$.name'), industry, "
    "json_extract(payload,'$.market.pe_ttm') pe, "
    "json_extract(payload,'$.dim_scores.\"估值合理性\"') vs, "
    "json_extract(payload,'$.valuation.is_growth_stock') ig, "
    "json_extract(payload,'$.composite_score') cs "
    "FROM factor_snapshots WHERE pe > 80 ORDER BY pe DESC LIMIT 15"
)).fetchall()
print(f"{'名称':<10}{'行业':<12}{'PE':>9}{'估值分':>8}{'成长豁免':>9}{'综合':>7}")
for n, ind, pe, vs, ig, cs in rows:
    print(f"{str(n):<10}{str(ind):<12}{pe:>9.1f}{str(vs):>8}{'是' if ig else '否':>9}{str(cs):>7}")
PY
