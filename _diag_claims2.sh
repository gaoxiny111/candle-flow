#!/bin/bash
cd /opt/candle-flow/backend || exit 1
.venv/bin/python - <<'PY'
import json
from sqlalchemy import text
from app.database import SessionLocal

db = SessionLocal()
TARGETS = ["600722.SH","603995.SH","002705.SZ","603407.SH"]
rows = db.execute(text(
    "SELECT symbol, payload FROM factor_snapshots WHERE symbol IN ('600722.SH','603995.SH','002705.SZ','603407.SH')"
)).fetchall()
data = {s: json.loads(p) for s, p in rows}

for sym in TARGETS:
    p = data.get(sym)
    if not p:
        print(f"!! {sym} 不在 factor_snapshots"); continue
    print("=" * 92)
    print(f"{sym} {p.get('name')} | 行业={p.get('industry')} | 市值={p.get('market_cap_yi')}亿 "
          f"| 综合={p.get('composite_score')} | 评级={p.get('rating')} | 打分版本={p.get('scoring_version')}")
    dims = p.get("dim_scores") or {}
    print("  维度分:", " | ".join(f"{k}={v}" for k, v in dims.items()))
    mkt = p.get("market") or {}
    print("  行情  : PE_TTM=", mkt.get("pe_ttm"), " PB=", mkt.get("pb"),
          " 股息率=", mkt.get("dividend_yield"), " 价=", mkt.get("price"))
    rel = p.get("relative") or {}
    print("  PEG   :", rel.get("PEG"))
    # 找估值与成长相关的模块明细
    mods = p.get("modules") or p.get("module_details") or {}
    if isinstance(mods, dict):
        for k in ("valuation", "growth"):
            m = mods.get(k)
            if m:
                print(f"  ── {k} 模块 ──")
                print(json.dumps(m, ensure_ascii=False, indent=4)[:2200])
    print("  顶层键:", ", ".join(sorted(p.keys())))
PY
