#!/bin/bash
cd /opt/candle-flow/backend || exit 1
.venv/bin/python - <<'PY'
import json
from sqlalchemy import text
from app.database import SessionLocal

db = SessionLocal()

# 目标票（应下降） + 对照票（应基本不动）
SAMPLE = {
    "600722.SH": "目标·金牛化工 PE193.5",
    "603407.SH": "目标·长裕集团 PE87",
    "600118.SH": "目标·中国卫星 PE700",
    "603683.SH": "目标·东方材料 PE1286",
    "600309.SH": "对照·万华化学(真周期底部 低PE)",
    "603659.SH": "对照·璞泰来(真周期底部 低PE)",
    "001287.SZ": "对照·中电港",
    "002371.SZ": "对照·北方华创(真成长 high_growth)",
    "603995.SH": "对照·甬金股份(普通口径)",
    "002705.SZ": "对照·新宝股份(红利口径)",
}

CORE_W = {"PE历史分位": 0.50, "绝对PE": 0.50, "相对PE": 0.30, "可比公司": 0.30, "股息率": 0.20}

def weighted(items, growth_w):
    tot = 0.0
    acc = 0.0
    for f, p in items:
        w = CORE_W.get(f, growth_w)
        acc += p * w
        tot += w
    return acc / tot if tot else 0.0

rows = db.execute(text(
    "SELECT symbol, payload FROM factor_snapshots WHERE symbol IN "
    "('600722.SH','603407.SH','600118.SH','603683.SH','600309.SH',"
    "'603659.SH','001287.SZ','002371.SZ','603995.SH','002705.SZ')"
)).fetchall()
data = {s: json.loads(p) for s, p in rows}

print(f"{'标的':<30}{'tier':<13}{'PE':>9}{'PB':>7}{'现估值':>8}{'补0.5':>8}{'补0.3':>8}  构成")
print("-" * 150)
for sym, label in SAMPLE.items():
    p = data.get(sym)
    if not p:
        print(f"{label:<30} 不在快照"); continue
    v = p.get("valuation") or {}
    gsp = v.get("growth_stock_profile") or {}
    tier = gsp.get("tier") or ("-" if not gsp.get("is_growth_stock") else "?")
    bd = v.get("valuation_score_breakdown") or []
    items = [(b.get("factor"), float(b.get("points") or 0)) for b in bd]
    mk = p.get("market") or {}
    cur = v.get("valuation_score_base")
    w05 = weighted(items, 0.5)
    w03 = weighted(items, 0.3)
    comp = "、".join(f"{f}{p_}" for f, p_ in items)
    pe = mk.get("pe_ttm")
    pb = mk.get("pb")
    print(f"{label:<30}{tier:<13}{(pe if pe else 0):>9.1f}{(pb if pb else 0):>7.2f}"
          f"{cur:>8.1f}{w05:>8.1f}{w03:>8.1f}  {comp}")
    for k in ("is_dividend_asset", "is_growth_stock"):
        pass
    print(f"{'':<30}is_growth={v.get('is_growth_stock')} is_div={v.get('is_dividend_asset')} "
          f"| haircut={v.get('valuation_score_haircut')} | 综合={p.get('composite_score')} "
          f"| 全量豁免权重={v.get('valuation_full_exempt_weight')}")
PY
