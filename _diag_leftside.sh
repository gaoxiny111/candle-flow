#!/bin/bash
# 左侧防守为何未触发：dump 形态所在K线及其量比
cd /opt/candle-flow/backend || exit 1
.venv/bin/python -u - <<'PY'
from app.database import SessionLocal
from app.core.pattern_engine import PatternEngine, kline_to_candles
from app.core.confluence import evaluate_confluence, _avg_vol_ratio, _sma
from app.services.kline_service import KlineService
from app.services.market_confluence_service import KLINE_LIMIT, WESTERN_NOT_CANDLES

db = SessionLocal()
NAMED = [("002371.SZ", "北方华创"), ("002409.SZ", "雅克科技"), ("001359.SZ", "平安电工"),
         ("603496.SH", "恒为科技"), ("603456.SH", "九洲药业"), ("603995.SH", "甬金股份")]

for sym, nm in NAMED:
    kl, _ = KlineService(db).get_recent_klines(sym, limit=KLINE_LIMIT)
    closes = [float(k.close) for k in kl]
    n = len(kl)
    print("=" * 110)
    print(f"[{sym}] {nm}  共 {n} 根")
    print(f"  最近 6 根（收盘 / 量 / 对前20日均量比）：")
    for i in range(n - 6, n):
        r = _avg_vol_ratio(kl, i)
        ma60 = _sma(closes, 60, i)
        ab = "MA60上方" if (ma60 and closes[i] >= ma60) else "MA60下方"
        print(f"    {kl[i].date}  close={closes[i]:>9.2f}  vol={float(kl[i].volume):>14,.0f}  "
              f"量比={r if r is None else round(r, 2):>6}  MA60={ma60 and round(ma60,2)}  {ab}")

    candles = kline_to_candles(kl)
    results = PatternEngine(min_score=60.0).scan(candles)
    last_idx = n - 1
    bull = [r for r in results if r.direction == "bullish"
            and r.pattern_name not in WESTERN_NOT_CANDLES
            and r.candle_index >= last_idx - 1]
    print(f"  近 2 根的看涨形态：")
    for r in bull:
        conf = evaluate_confluence(kl, r.candle_index, r.direction, r.pattern_name)
        flaws = [sc.message for sc in conf.soft_conflict_items if "左侧超跌形态" in sc.message]
        vr = _avg_vol_ratio(kl, r.candle_index)
        ma60 = _sma(closes, 60, r.candle_index)
        print(f"    idx={r.candle_index}/{last_idx} date={kl[r.candle_index].date} "
              f"形态={r.pattern_name} 分={float(r.score):.0f}")
        print(f"      close={closes[r.candle_index]:.2f} MA60={ma60 and round(ma60,2)} "
              f"量比={vr and round(vr,2)}  ok={conf.ok} blocked={conf.blocked} "
              f"左侧否决={'是' if flaws else '否'}")
        print(f"      命中：{conf.label}")
        if flaws:
            print(f"      ★ {flaws[0]}")
        softs = [f"{s.kind}:{s.message[:60]}" for s in conf.soft_conflict_items if "左侧超跌形态" not in s.message]
        if softs:
            print(f"      其他软冲突：{' | '.join(softs)}")
PY
