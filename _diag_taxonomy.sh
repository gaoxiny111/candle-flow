#!/bin/bash
# 全行业名录 + 盈利模块指标键（用于确认 ROE 可取路径）
cd /opt/candle-flow/backend || exit 1
.venv/bin/python -u - <<'PY'
import json
from sqlalchemy import text
from app.database import SessionLocal

db = SessionLocal()

print("=" * 100)
print("全部行业名（129 个，按只数降序）")
print("=" * 100)
rows = db.execute(text(
    "SELECT json_extract(payload,'$.industry') ind, COUNT(*) n FROM factor_snapshots "
    "WHERE json_extract(payload,'$.industry') IS NOT NULL GROUP BY ind ORDER BY n DESC"
)).fetchall()
for i, (ind, n) in enumerate(rows):
    print(f"  {str(ind):<16}{n:>5}", end="\n" if i % 3 == 2 else "")

print("\n" + "=" * 100)
print("盈利模块 indicators / metadata 的键（找 ROE 可取路径）")
print("=" * 100)
row = db.execute(text("SELECT payload FROM factor_snapshots WHERE symbol='603995.SH'")).fetchone()
p = json.loads(row[0])
prof = (p.get("modules") or {}).get("profitability") or {}
print("  profitability.metadata =", json.dumps(prof.get("metadata"), ensure_ascii=False)[:600])
inds = prof.get("indicators") or []
print(f"  profitability.indicators 共 {len(inds)} 项：")
for it in inds:
    print(f"    {json.dumps(it, ensure_ascii=False)[:180]}")
print()
print("  dim_scores =", json.dumps(p.get("dim_scores"), ensure_ascii=False))
print("  growth.metadata =", json.dumps(((p.get("modules") or {}).get("growth") or {}).get("metadata"), ensure_ascii=False)[:500])
print("  valuation.valuation_score_breakdown =",
      json.dumps((p.get("valuation") or {}).get("valuation_score_breakdown"), ensure_ascii=False))
print("  valuation.valuation_score_base =", (p.get("valuation") or {}).get("valuation_score_base"))
print("  valuation.valuation_score_haircut =", (p.get("valuation") or {}).get("valuation_score_haircut"))
print("  valuation.valuation_rationale =", (p.get("valuation") or {}).get("valuation_rationale"))
PY
