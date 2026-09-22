#!/bin/bash
# 快照 payload 真实结构勘察（只读）
cd /opt/candle-flow/backend || exit 1
.venv/bin/python -u - <<'PY'
import json
from sqlalchemy import text
from app.database import SessionLocal

db = SessionLocal()


def dump(path, depth=0, max_depth=2):
    pad = "  " * depth
    if isinstance(path, dict):
        for k, v in path.items():
            t = type(v).__name__
            if isinstance(v, dict) and depth < max_depth:
                print(f"{pad}{k}: {{...}}")
                dump(v, depth + 1, max_depth)
            elif isinstance(v, dict):
                print(f"{pad}{k}: dict[{len(v)}] keys={list(v)[:8]}")
            elif isinstance(v, list):
                print(f"{pad}{k}: list[{len(v)}]" + (f" 首项键={list(v[0])[:8]}" if v and isinstance(v[0], dict) else ""))
            else:
                print(f"{pad}{k}: {t} = {str(v)[:60]}")


for sym in ("603995.SH", "600722.SH"):
    row = db.execute(text(
        "SELECT payload FROM factor_snapshots WHERE symbol=:s"), {"s": sym}).fetchone()
    print("=" * 110)
    print(f"{sym} 顶层结构")
    print("=" * 110)
    p = json.loads(row[0])
    dump(p, 0, 0)
    print(f"\n  顶层键：{list(p)}")
    print(f"\n  market 子键：{list(p.get('market') or {})}")
    print(f"  valuation 子键：{list(p.get('valuation') or {})}")
    print(f"  valuation.relative 子键：{list((p.get('valuation') or {}).get('relative') or {})}")
    v = p.get("valuation") or {}
    print(f"  valuation: score={v.get('score')} composite_valuation_score={v.get('composite_valuation_score')}")
    print(f"  valuation.is_growth_stock={v.get('is_growth_stock')}")
    print(f"  valuation.growth_stock_profile={v.get('growth_stock_profile')}")
    print(f"  market.pe_ttm={(p.get('market') or {}).get('pe_ttm')} "
          f"pb={(p.get('market') or {}).get('pb')} "
          f"roe={(p.get('market') or {}).get('roe')}")
    rel = v.get("relative") or {}
    for k in ("PE_TTM", "PB", "PEG"):
        print(f"    relative.{k} = {rel.get(k)}")
    print(f"  modules 键：{list(p.get('modules') or {})}")
    for mk, mv in (p.get("modules") or {}).items():
        if isinstance(mv, dict):
            print(f"    {mk}: score={mv.get('score')} keys={[x for x in mv if x != 'score'][:12]}")
    print()
PY
