#!/bin/bash
# 第五轮验证：左侧抄底防守改为「决策日口径」后，点名标的在榜单上的真实档位/买点信号
cd /opt/candle-flow/backend || exit 1
.venv/bin/python -u - <<'PY'
from app.database import SessionLocal
from app.services.market_scan import _overlay_one, _snapshot_peg_map, load_covered

db = SessionLocal()
NAMED = [
    ("002371.SZ", "北方华创"), ("002409.SZ", "雅克科技"), ("001359.SZ", "平安电工"),
    ("603496.SH", "恒为科技"), ("603456.SH", "九洲药业"), ("603995.SH", "甬金股份"),
    ("600722.SH", "金牛化工"), ("688012.SH", "中微公司"), ("603259.SH", "药明康德"),
]

rows, _fb = load_covered()
by_sym = {r.get("symbol"): r for r in rows}
pegs = _snapshot_peg_map(None, [s for s, _ in NAMED])

print("=" * 132)
print(f"{'代码':<12}{'名称':<10}{'基本面':>7}{'技术':>7}{'形态':<14}{'形态分':>7}"
      f"{'组合':>7}{'买点信号':<18}{'否决原因 / 说明'}")
print("-" * 132)
for sym, nm in NAMED:
    it = by_sym.get(sym) or {}
    fund = it.get("composite_score")
    r = _overlay_one(sym, nm, fund, pegs.get(sym))
    blocker = r.get("tech_blocker") or ""
    if not blocker:
        blocker = "；".join((r.get("buy_reasons") or [])[:2])
    print(f"{sym:<12}{nm:<10}{(fund or 0):>7.1f}{str(r.get('tech_score') or '--'):>7}"
          f"{str(r.get('pattern_name') or '--'):<14}{str(r.get('pattern_score') or '--'):>7}"
          f"{str(round(r.get('combined_score'), 1) if r.get('combined_score') else '--'):>7}"
          f"{str(r.get('buy_label') or '--'):<18}{blocker[:60]}")
print("=" * 132)
print("口径：技术面 None = 有形态但被否决（不回 0、不赋中性）；见 tech_blocker。")
PY
