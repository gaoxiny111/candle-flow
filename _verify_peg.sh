#!/bin/bash
cd /opt/candle-flow/backend || exit 1
.venv/bin/python - <<'PY'
import json
from sqlalchemy import text, create_engine
from sqlalchemy.orm import sessionmaker
from app.database import SessionLocal
from app.services.market_scan import _snapshot_peg_map
from app.services.market_confluence_service import _detect_buy_signal
from app.services.kline_service import KlineService

db = SessionLocal()

print("########## A. PEG 路径修复验证 ##########")
syms = ["603995.SH", "600722.SH", "603407.SH", "600309.SH", "002371.SZ", "600519.SH"]
m = _snapshot_peg_map(db, syms)
print("_snapshot_peg_map =", m)
raw = db.execute(text(
    "SELECT symbol, json_extract(payload,'$.valuation.relative.PEG.value'), "
    "json_extract(payload,'$.valuation.relative.PEG.growth_label') "
    "FROM factor_snapshots WHERE symbol IN ('603995.SH','600722.SH','603407.SH')"
)).fetchall()
print("快照原始值：")
for s, v, lab in raw:
    print(f"   {s} PEG={v} label={lab}")
ok = sum(1 for v in m.values() if v is not None)
print(f"→ 取到值的 {ok}/{len(syms)}（修复前恒为 0/{len(syms)}）")

print()
print("########## B. 买点信号是否恢复可触发 ##########")
for sym, name in (("603995.SH", "甬金股份"), ("600722.SH", "金牛化工"), ("600309.SH", "万华化学")):
    kl, _ = KlineService(db).get_recent_klines(sym, limit=180)
    if len(kl) < 60:
        print(f"  {sym} K线不足"); continue
    close = float(kl[-1].close)
    ma20 = sum(float(k.close) for k in kl[-20:]) / 20
    ma60 = sum(float(k.close) for k in kl[-60:]) / 60
    q = db.execute(text(
        "SELECT json_extract(payload,'$.composite_score') FROM factor_snapshots WHERE symbol=:s"
    ), {"s": sym}).scalar()
    res = _detect_buy_signal(kl, float(q or 0), m.get(sym), None)
    print(f"  {name} 收{close:.2f} MA20={ma20:.2f} MA60={ma60:.2f} 基本面={q} PEG={m.get(sym)}"
          f" → {res['label']} | {res.get('reasons')}")
PY
