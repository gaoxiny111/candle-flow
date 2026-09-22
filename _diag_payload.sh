#!/bin/bash
cd /opt/candle-flow/backend || exit 1
.venv/bin/python -u - <<'PY'
import json
from sqlalchemy import text
from app.database import SessionLocal

db = SessionLocal()
p = db.execute(text(
    "SELECT payload FROM factor_snapshots WHERE symbol='600722.SH'"
)).scalar()
d = json.loads(p)

def walk(o, pre="", depth=0):
    if depth > 3:
        return
    if isinstance(o, dict):
        for k, v in o.items():
            if isinstance(v, (dict, list)):
                walk(v, f"{pre}.{k}", depth + 1)
            else:
                pass

print("顶层键：", sorted(d.keys()))
print()
print("valuation 子键：", sorted((d.get("valuation") or {}).keys()))
print()
print("growth_stock_profile =", json.dumps(d.get("valuation", {}).get("growth_stock_profile"), ensure_ascii=False))
print()
for key in ("growth", "dim_scores", "market", "growth_detail", "growth_module"):
    v = d.get(key)
    if isinstance(v, dict):
        print(f"[{key}] keys =", sorted(v.keys())[:40])
print()
mk = d.get("market") or {}
print("market 关键项： pb=", mk.get("pb"), " pe_ttm=", mk.get("pe_ttm"),
      " pe_percentile=", mk.get("pe_percentile"), " pb_percentile=", mk.get("pb_percentile"),
      " dividend_yield=", mk.get("dividend_yield"))
print()
# 找 payload 里所有含 cagr / yoy 的标量路径
found = []
def scan(o, pre=""):
    if isinstance(o, dict):
        for k, v in o.items():
            if isinstance(v, (dict, list)):
                scan(v, f"{pre}.{k}")
            elif any(t in k.lower() for t in ("cagr", "yoy", "growth_rate")):
                found.append((f"{pre}.{k}", v))
    elif isinstance(o, list):
        for i, v in enumerate(o):
            if isinstance(v, (dict, list)):
                scan(v, f"{pre}[{i}]")
scan(d)
print("payload 中所有增速类标量：")
for k, v in found:
    print(f"   {k} = {v}")
PY
