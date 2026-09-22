#!/bin/bash
cd /opt/candle-flow/backend || exit 1
.venv/bin/python - <<'PY'
from collections import Counter
from sqlalchemy import text
from app.database import SessionLocal
from app.services import market_scan as ms

db = SessionLocal()
print("重建共振索引（本进程内）...", flush=True)
print(ms.resonance_index_build(), flush=True)
data = ms.resonance_view(db, top=100000, exclude_st=True, include_gem=False)
items = data.get("items") or []
print("榜单条目 =", len(items), "| matched =", data.get("matched"))
for fld in ("verdict_label", "buy_signal", "buy_label"):
    c = Counter(str(it.get(fld)) for it in items)
    print(f"  {fld:<14}", dict(c.most_common(10)))
withfund = sum(1 for it in items if (it.get("composite_score") or 0) >= 80)
peg_ok = sum(1 for it in items if it.get("peg") is not None)
peg_lt15 = sum(1 for it in items if it.get("peg") is not None and it["peg"] < 1.5)
above = sum(1 for it in items if "站上MA20" in str(it.get("buy_reasons")))
below = sum(1 for it in items if "低于MA20" in str(it.get("buy_reasons")))
print(f"  基本面≥80 = {withfund} | PEG 有值 = {peg_ok} | PEG<1.5 = {peg_lt15}")
print(f"  买点理由含「站上MA20」= {above} | 含「低于MA20」= {below}")
sat = sum(1 for it in items if (it.get("composite_score") or 0) >= 80
          and it.get("peg") is not None and it["peg"] < 1.5
          and "站上MA20" in str(it.get("buy_reasons")))
print(f"  → 同时满足 基本面≥80 + PEG<1.5 + 站上MA20（放量前）= {sat}")
PY
