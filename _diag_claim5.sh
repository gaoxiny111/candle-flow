#!/bin/bash
# 定向证据：只对用户点名的标的，跑「形态 → 共振 → 决策 → 买点」全链路 + 均线/量能快照
cd /opt/candle-flow/backend || exit 1
.venv/bin/python -u - <<'PY'
import json
from sqlalchemy import text
from app.database import SessionLocal
from app.core.pattern_engine import PatternEngine, kline_to_candles
from app.core.confluence import evaluate_confluence
from app.services.kline_service import KlineService
from app.services.market_scan import (
    _verdict, VERDICT_MIN_FUND, VERDICT_MIN_TECH,
    VERDICT_CORE_SCORE, VERDICT_VETO_SCORE,
)
from app.services.market_confluence_service import (
    _detect_buy_signal, _is_candidate, _combined_score, KLINE_LIMIT, WESTERN_NOT_CANDLES,
)

NAMED = [("002371.SZ", "北方华创"), ("603456.SH", "九洲药业"), ("001359.SZ", "平安电工"),
         ("002409.SZ", "雅克科技"), ("603496.SH", "恒为科技"),
         ("603995.SH", "甬金股份"), ("600722.SH", "金牛化工")]

db = SessionLocal()


def ctx_of(sym):
    row = db.execute(text(
        "SELECT json_extract(payload,'$.composite_score'), "
        "json_extract(payload,'$.valuation.relative.PB.value'), "
        "json_extract(payload,'$.valuation.relative.PE_TTM.value'), "
        "json_extract(payload,'$.valuation.score'), "
        "json_extract(payload,'$.scoring_version') "
        "FROM factor_snapshots WHERE symbol=:s"), {"s": sym}).fetchone()
    return row


def industry_of(sym):
    for sql in (
        "SELECT industry FROM stock_infos WHERE symbol=:s",
        "SELECT industry FROM stocks WHERE symbol=:s",
    ):
        try:
            r = db.execute(text(sql), {"s": sym}).fetchone()
            return r[0] if r else None
        except Exception:
            continue
    return None


print("=" * 118)
print("A. 点名标的：形态 / 共振 / 均线结构 / 量能 / 是否入选 / 买点")
print("=" * 118)
for sym, nm in NAMED:
    kl, _ = KlineService(db).get_recent_klines(sym, limit=KLINE_LIMIT)
    if len(kl) < 40:
        print(f"\n[{sym}] {nm}: K线不足（{len(kl)}）")
        continue
    c = [float(k.close) for k in kl]
    v = [float(k.volume) for k in kl]
    ma5 = sum(c[-5:]) / 5
    ma10 = sum(c[-10:]) / 10
    ma20 = sum(c[-20:]) / 20
    ma60 = sum(c[-60:]) / 60 if len(c) >= 60 else None
    volr = v[-1] / (sum(v[-21:-1]) / 20) if sum(v[-21:-1]) > 0 else None
    row = ctx_of(sym)
    fund = row[0] if row else None
    pb = row[1] if row else None
    pe = row[2] if row else None
    vs = row[3] if row else None
    sv = row[4] if row else None
    arr = "空头排列" if ma5 < ma10 < ma20 else ("多头排列" if ma5 > ma10 > ma20 else "缠绕")
    ab60 = ("MA60上方" if (ma60 and c[-1] >= ma60) else "MA60下方") if ma60 else "MA60不可算"
    print(f"\n[{sym}] {nm}  industry={industry_of(sym)}  快照ver={sv}")
    print(f"  基本面={fund}  估值={vs}  PE={pe}  PB={pb}")
    print(f"  收盘={c[-1]:.2f}  MA5={ma5:.2f} MA10={ma10:.2f} MA20={ma20:.2f} "
          f"MA60={ma60:.2f}  →  {arr} / {ab60}")
    print(f"  当日量比(20日均量)={volr:.2f}" if volr else "  量比不可算")

    candles = kline_to_candles(kl)
    results = PatternEngine(min_score=60.0).scan(candles)
    last_idx = len(kl) - 1
    min_idx = last_idx - 1
    bull = [r for r in results if r.direction == "bullish"
            and r.pattern_name not in WESTERN_NOT_CANDLES
            and min_idx <= r.candle_index <= last_idx]
    if not bull:
        bull = [r for r in results if r.direction == "bullish"
                and r.pattern_name not in WESTERN_NOT_CANDLES]
    print(f"  近2根看涨形态 {len(bull)} 个：")
    for r in sorted(bull, key=lambda x: -float(x.score))[:6]:
        conf = evaluate_confluence(kl, r.candle_index, r.direction)
        comb = _combined_score(float(r.score), conf.effective_count, conf.soft_conflict_items)
        ok = _is_candidate(float(r.score), conf.effective_count, conf.soft_conflict_items)
        softs = " | ".join(f"{s.kind}:{s.message[:46]}" for s in conf.soft_conflict_items)
        print(f"     · {r.pattern_name:<12} 形态分={float(r.score):.0f} 共振={conf.effective_count:.1f} "
              f"combined={comb:.0f} 入选={ok} ok={conf.ok} blocked={conf.blocked}")
        print(f"       命中：{conf.label}")
        if softs:
            print(f"       软冲突：{softs}")
    # 快照里的买点
    br = db.execute(text(
        "SELECT json_extract(payload,'$.technical.buy_signal'), "
        "json_extract(payload,'$.technical.buy_label'), "
        "json_extract(payload,'$.technical.tech_score') "
        "FROM factor_snapshots WHERE symbol=:s"), {"s": sym}).fetchone()
    print(f"  快照：{br}")

print("\n" + "=" * 118)
print("B. 周期股口径：stock_industry 是否可查 + 甬金/金牛 行业")
print("=" * 118)
for t in ("stock_industry", "stocks", "stock_info"):
    try:
        rows = db.execute(text(f"SELECT * FROM {t} LIMIT 1")).fetchall()
        cols = db.execute(text(f"PRAGMA table_info({t})")).fetchall()
        print(f"  {t}: cols={[c[1] for c in cols]}")
    except Exception as e:
        print(f"  {t}: {str(e)[:80]}")
PY
