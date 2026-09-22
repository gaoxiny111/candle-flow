#!/bin/bash
cd /opt/candle-flow/backend || exit 1
.venv/bin/python -u - <<'PY'
import json
from collections import Counter, defaultdict
from sqlalchemy import text
from app.database import SessionLocal
from app.services.kline_service import KlineService
from app.services.market_scan import (
    _verdict, resonance_index_build, _reso_cache, VERDICT_MIN_FUND,
    VERDICT_MIN_TECH, VERDICT_CORE_SCORE, VERDICT_VETO_SCORE,
)
from app.services.market_confluence_service import _detect_buy_signal, KLINE_LIMIT

db = SessionLocal()
print("### 构建/读取共振索引 ...", flush=True)
res = resonance_index_build(force=False)
print("索引:", {k: res.get(k) for k in ("total", "tech_needed", "tech_computed", "tech_failed")}, flush=True)
items = _reso_cache["items"]
print("items =", len(items))

# ---------- A. 决策 × 买点信号 交叉表 ----------
print("\n" + "=" * 120)
print("A. 决策档位 × 买点信号 交叉表（共振视图全量）")
byv = defaultdict(Counter)
for it in items:
    fund = it.get("composite_score")
    tech = it.get("tech_score")
    v, _ = _verdict(fund, tech, min_fund=VERDICT_MIN_FUND, min_tech=VERDICT_MIN_TECH,
                    core=VERDICT_CORE_SCORE, veto=VERDICT_VETO_SCORE)
    byv[v][it.get("buy_signal") or "(None)"] += 1
for v in ("core", "candidate", "watch", "eliminated"):
    c = byv.get(v)
    if not c:
        continue
    tot = sum(c.values())
    print(f"  {v:<11} n={tot:<5} " + "  ".join(f"{k}={n}({n/tot*100:.0f}%)" for k, n in c.most_common()))

print("\n  买入候选里 buy_signal=neutral 的票，其 buy_reasons（前 12）：")
n_show = 0
for it in items:
    fund = it.get("composite_score"); tech = it.get("tech_score")
    v, _ = _verdict(fund, tech, min_fund=VERDICT_MIN_FUND, min_tech=VERDICT_MIN_TECH,
                    core=VERDICT_CORE_SCORE, veto=VERDICT_VETO_SCORE)
    if v == "candidate" and (it.get("buy_signal") == "neutral"):
        print(f"    {it.get('symbol')} {it.get('name')} fund={fund} tech={tech} "
              f"| {' / '.join(it.get('buy_reasons') or [])}")
        n_show += 1
        if n_show >= 12:
            break

# ---------- B. 用户点名的 5 只票 ----------
print("\n" + "=" * 120)
print("B. 用户点名标的的实况（形态 / 共振 / 均线 / 量能 / 买点）")
NAMED = [("002371.SZ", "北方华创"), ("603456.SH", "九洲药业"), ("001359.SZ", "平安电工"),
         ("002409.SZ", "雅克科技"), ("603496.SH", "恒为科技")]
idx = {it.get("symbol"): it for it in items}
for sym, nm in NAMED:
    it = idx.get(sym)
    if not it:
        print(f"  {sym} {nm}: 不在共振索引（基本面 < 淘汰线 或 无 K 线）")
        continue
    kl, _ = KlineService(db).get_recent_klines(sym, limit=KLINE_LIMIT)
    ma = {}
    if len(kl) >= 60:
        c = [float(k.close) for k in kl]
        v_ = [float(k.volume) for k in kl]
        ma = {
            "close": c[-1],
            "ma5": sum(c[-5:]) / 5, "ma10": sum(c[-10:]) / 10,
            "ma20": sum(c[-20:]) / 20, "ma60": sum(c[-60:]) / 60,
            "vol_ratio": v_[-1] / (sum(v_[-21:-1]) / 20) if sum(v_[-21:-1]) > 0 else None,
        }
    fund = it.get("composite_score"); tech = it.get("tech_score")
    v, vr = _verdict(fund, tech, min_fund=VERDICT_MIN_FUND, min_tech=VERDICT_MIN_TECH,
                     core=VERDICT_CORE_SCORE, veto=VERDICT_VETO_SCORE)
    print(f"\n  [{sym}] {nm}  fund={fund} tech={tech} 决策={v}  买点={it.get('buy_label')}({it.get('buy_signal')})")
    print(f"    形态={it.get('pattern_name')} pattern_score={it.get('pattern_score')} "
          f"有效共振={it.get('confluence_effective')} combined={it.get('combined_score')}")
    if ma:
        arr = ("空头" if ma["ma5"] < ma["ma10"] < ma["ma20"] else
               "多头" if ma["ma5"] > ma["ma10"] > ma["ma20"] else "缠绕")
        ab60 = "站上MA60" if ma["close"] >= ma["ma60"] else "在MA60下方"
        ab20 = "站上MA20" if ma["close"] >= ma["ma20"] else "在MA20下方"
        print(f"    收盘={ma['close']:.2f} MA5={ma['ma5']:.2f} MA10={ma['ma10']:.2f} "
              f"MA20={ma['ma20']:.2f} MA60={ma['ma60']:.2f}  排列={arr} {ab20} {ab60}")
        print(f"    当日量比(对20日均量)={ma['vol_ratio']:.2f}" if ma["vol_ratio"] else "    量比不可算")
    print(f"    共振命中：{it.get('confluence_hits')}")
    if it.get("buy_reasons"):
        print(f"    买点理由：{' / '.join(it['buy_reasons'])}")

# ---------- C. 买点信号闸门统计（全量） ----------
print("\n" + "=" * 120)
print("C. buy_signal 分布 + neutral 的闸门归因（全量索引）")
c_dist = Counter(it.get("buy_signal") for it in items)
for k, n in c_dist.most_common():
    print(f"  {k:<20}{n:>6}  ({n/len(items)*100:.1f}%)")
gate = Counter()
for it in items:
    if it.get("buy_signal") != "neutral":
        continue
    reasons = " / ".join(it.get("buy_reasons") or [])
    if "基本面" in reasons and "< 80" in reasons:
        gate["① 基本面 < 80（非 PEG 问题）"] += 1
    elif "PEG 缺失" in reasons or "PEG缺失" in reasons:
        gate["② PEG 缺失"] += 1
    else:
        gate["③ 其他（均线/量能未达）"] += 1
for k, n in gate.most_common():
    print(f"  {k:<28}{n:>6}")
PY
