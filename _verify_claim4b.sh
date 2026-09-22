#!/bin/bash
# 读层验证（定向版）：直接调 _overlay_one，不构建全量索引
cd /opt/candle-flow/backend || exit 1
.venv/bin/python -u - <<'PY'
from collections import Counter
from sqlalchemy import text
from app.database import SessionLocal
from app.services.kline_service import KlineService
from app.services.market_scan import (
    _overlay_one, _verdict, VERDICT_MIN_FUND, VERDICT_MIN_TECH,
    VERDICT_CORE_SCORE, VERDICT_VETO_SCORE, _tech_score,
)
from app.services.market_confluence_service import KLINE_LIMIT

db = SessionLocal()

NAMED = [("002371.SZ", "北方华创"), ("603456.SH", "九洲药业"), ("001359.SZ", "平安电工"),
         ("002409.SZ", "雅克科技"), ("603496.SH", "恒为科技"), ("603995.SH", "甬金股份"),
         ("600722.SH", "金牛化工")]

print("=" * 118)
print("A. 点名标的：读层叠加结果（改后）")
print("=" * 118)
for sym, nm in NAMED:
    row = db.execute(text(
        "SELECT json_extract(payload,'$.composite_score'), "
        "json_extract(payload,'$.valuation.relative.PEG.value') "
        "FROM factor_snapshots WHERE symbol=:s"), {"s": sym}).fetchone()
    fund = (row[0] if row else None)
    peg = (row[1] if row else None)
    ov = _overlay_one(sym, nm, float(fund or 0), peg)
    kl, _ = KlineService(db).get_recent_klines(sym, limit=KLINE_LIMIT)
    c = [float(k.close) for k in kl]
    v = [float(k.volume) for k in kl]
    ma60 = sum(c[-60:]) / 60 if len(c) >= 60 else None
    vr = v[-1] / (sum(v[-21:-1]) / 20) if sum(v[-21:-1]) > 0 else None
    vd, vr_reasons = _verdict(
        ov.get("verdict") and fund or fund, ov.get("tech_score"),
        min_fund=VERDICT_MIN_FUND, min_tech=VERDICT_MIN_TECH,
        core=VERDICT_CORE_SCORE, veto=VERDICT_VETO_SCORE,
        tech_blocker=ov.get("tech_blocker"),
    )
    print(f"\n[{sym}] {nm}  基本面={fund}  技术面={ov.get('tech_score')}  决策={vd}")
    print(f"  买点信号={ov.get('buy_signal')}（{ov.get('buy_label')}）")
    print(f"  收盘={c[-1]:.2f} MA60={ma60 and ma60:.2f} 当日量比={vr and round(vr,2)} "
          f"形态={ov.get('pattern_name')} 形态分={ov.get('pattern_score')} "
          f"combined={ov.get('combined_score')}")
    print(f"  买点理由：{' / '.join(ov.get('buy_reasons') or [])}")
    if ov.get("buy_note"):
        print(f"  备注：{ov['buy_note']}")
    if ov.get("tech_blocker"):
        print(f"  ⛔ tech_blocker：{ov['tech_blocker']}")
    print(f"  决策理由：{' / '.join(vr_reasons)}")
PY
