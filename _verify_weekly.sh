#!/bin/bash
cd /opt/candle-flow/backend || exit 1
.venv/bin/python - <<'PY'
from sqlalchemy import text
from app.database import SessionLocal
from app.services.kline_service import KlineService
from app.core.timeframe import weekly_trend_at

db = SessionLocal()
kws = KlineService(db)

rows = db.execute(text("SELECT DISTINCT symbol FROM kline_data ORDER BY symbol LIMIT 400")).fetchall()
syms = [r[0] for r in rows]

def trend_today(kl, n):
    """以今日为锚回看 n 根"""
    sl = kl[-n:]
    return weekly_trend_at(sl, len(sl) - 1)

pairs = [(90,180), (120,180), (150,180), (180,240)]
stat = {p: {"same":0, "diff":0} for p in pairs}
samples = {p: [] for p in pairs}
no240 = 0

for sym in syms:
    kl, _ = kws.get_recent_klines(sym, limit=240)
    if len(kl) < 180:
        continue
    t180 = trend_today(kl, 180)
    for a, b in pairs:
        if len(kl) < b:
            continue
        ta = trend_today(kl, a)
        if ta == t180:
            stat[(a,b)]["same"] += 1
        else:
            stat[(a,b)]["diff"] += 1
            if len(samples[(a,b)]) < 8:
                samples[(a,b)].append((sym, ta, t180))

print("K线>=180 的比对样本:", stat[(90,180)]["same"] + stat[(90,180)]["diff"])
print("K线<240 的:", sum(1 for s in syms if len(kws.get_recent_klines(s, limit=240)[0]) < 240))
print()
for a, b in pairs:
    tot = stat[(a,b)]["same"] + stat[(a,b)]["diff"]
    if not tot: continue
    print(f"[{a} 根 vs {b} 根] 以今日为锚，周线判定不一致 {stat[(a,b)]['diff']}/{tot} = {stat[(a,b)]['diff']/tot*100:.1f}%")
    for s, x, y in samples[(a,b)]:
        print(f"      {s}: {a}根={x}  {b}根={y}")
    print()
PY
