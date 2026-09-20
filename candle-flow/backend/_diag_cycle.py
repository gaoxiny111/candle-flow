# -*- coding: utf-8 -*-
"""诊断周期/资源股在红利框架上的溢价因子是否触发。"""
import sys, json
sys.path.insert(0, ".")
from app.analysis.engine import FundamentalEngine

SYMBOLS = ["601088.SH", "601225.SH", "600188.SH", "600985.SH", "600096.SH", "601898.SH"]
for sym in SYMBOLS:
    try:
        eng = FundamentalEngine()
        res = eng.run_full_analysis(sym)
        val = res.get("valuation") or {}
        breakdown = val.get("breakdown") or []
        hits = [b for b in breakdown if b.get("factor") in ("资源壁垒溢价", "红利定价锚")]
        dvf = val.get("dividend_valuation_factors")
        print(f"{sym} {res.get('name','?')} comp={res.get('composite_score')} val={val.get('composite_valuation_score')}",
              "| premium:", json.dumps(hits, ensure_ascii=False) if hits else "无",
              "| is_div:", bool(dvf))
    except Exception as e:
        print(sym, "ERROR:", type(e).__name__, str(e)[:120])
