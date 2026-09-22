#!/bin/bash
# 实时口径验证（不走快照，直接跑 run_full_analysis 最新代码）
cd /opt/candle-flow/backend || exit 1
.venv/bin/python -u - <<'PY'
from app.analysis.engine import analyze_symbol_full
from app.database import SessionLocal

CASES = [
    ("600722.SH", "金牛化工", "Claim1 PE193.5/PB8.49 泡沫股"),
    ("603407.SH", "长裕集团", "Claim2 新上市 PB11.7"),
    ("603995.SH", "甬金股份", "Claim3 特钢 PE15.7"),
    ("688368.SH", "晶丰明源", "PEG泄漏 PE285 增速300%"),
    ("600309.SH", "万华化学", "对照 真周期底部"),
    ("603659.SH", "璞泰来", "对照 真周期底部"),
    ("001287.SZ", "中电港", "对照 分销"),
    ("002371.SZ", "北方华创", "对照 真成长"),
    ("002705.SZ", "新宝股份", "对照 红利"),
    ("600519.SH", "贵州茅台", "对照 红利"),
]

db = SessionLocal()
rows = []
for sym, name, note in CASES:
    try:
        r = analyze_symbol_full(db, sym, use_cache=False)
    except Exception as e:
        print("!! %s %s 失败: %s: %s" % (sym, name, type(e).__name__, e))
        continue
    v = r.get("valuation") or {}
    rel = v.get("relative") or {}
    peg = rel.get("PEG") or {}
    gsp = v.get("growth_stock_profile") or {}
    mk = r.get("market") or {}
    bd = v.get("valuation_score_breakdown") or []
    comp = "、".join("%s%.0f" % (b.get("factor"), float(b.get("points") or 0)) for b in bd)
    anchor = "有" if "PEG核心锚" in comp else "无"
    reasons = " | ".join(gsp.get("reasons") or [])
    print("=" * 130)
    print("[%s] %s   # %s" % (sym, name, note))
    print("  PE=%s  PB=%s  股息率=%s" % (mk.get("pe_ttm"), mk.get("pb"), mk.get("dividend_yield")))
    print("  综合分=%s   估值分=%s   估值基=%s   折减=%s" % (
        r.get("composite_score"),
        v.get("composite_valuation_score"),
        v.get("valuation_score_base"),
        v.get("valuation_score_haircut"),
    ))
    print("  is_growth=%s  tier=%s  is_div=%s" % (
        v.get("is_growth_stock"), gsp.get("tier"), v.get("is_dividend_asset")))
    if reasons:
        print("  成长判定：%s" % reasons)
    print("  PEG=%s (%s)  PEG核心锚=%s" % (peg.get("value"), peg.get("growth_label"), anchor))
    if peg.get("note"):
        print("  PEG note：%s" % peg.get("note"))
    print("  估值构成：%s" % comp)
    rows.append((sym, name, r, v, peg, gsp, mk, comp))
PY
