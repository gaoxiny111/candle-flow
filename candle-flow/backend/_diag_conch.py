# -*- coding: utf-8 -*-
"""海螺水泥：价值陷阱 veto 与红利画像的冲突诊断。"""
import sys, json
sys.path.insert(0, ".")
from app.analysis.engine import FundamentalEngine

r = FundamentalEngine().run_full_analysis(sys.argv[1] if len(sys.argv) > 1 else "600585.SH", db=None)
val = r.get("valuation") or {}
prof = (r.get("modules") or {}).get("profitability") or {}
pm = prof.get("metadata") or {}
print("COMPOSITE:", r.get("composite_score"), r.get("final_rating"))
print("dim_scores:", json.dumps(r.get("dim_scores"), ensure_ascii=False))
print("--- valuation ---")
for k in ("composite_valuation_score", "valuation_score_base", "valuation_score_haircut",
          "is_dividend_asset", "is_growth_stock", "value_trap_veto", "value_trap_message"):
    print(f"  {k}: {val.get(k)}")
print("  rationale:", (val.get("valuation_rationale") or "")[:300])
print("  dividend_valuation_factors:", json.dumps(val.get("dividend_valuation_factors"), ensure_ascii=False)[:400])
print("--- profitability metadata ---")
for k in ("is_dividend_asset", "is_growth_stock", "roic_below_wacc", "wacc_pct", "roic_gross_capital_pct"):
    print(f"  {k}: {pm.get(k)}")
for ind in prof.get("indicators", []) or []:
    if "ROIC" in str(ind.get("name")) or "WACC" in str(ind.get("name")):
        print("   IND:", ind.get("name"), "=", ind.get("value"), "score", ind.get("score"), "|", (ind.get("comment") or "")[:120])
print("--- long_signals / summary ---")
print("  long_signals:", json.dumps(r.get("long_signals") or r.get("summary", {}).get("long_signals"), ensure_ascii=False))
print("  risk_level:", r.get("risk_level"), "| observe_events:", len(r.get("observe_risk_events") or []))
