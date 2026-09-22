#!/bin/bash
# 读层验证：左侧抄底防守 + 买点信号阶段化（不依赖快照 .4 重建完成）
cd /opt/candle-flow/backend || exit 1
.venv/bin/python -u - <<'PY'
from collections import Counter, defaultdict
from sqlalchemy import text
from app.database import SessionLocal
from app.services.kline_service import KlineService
from app.services.market_scan import (
    _verdict, resonance_index_build, _reso_cache, VERDICT_MIN_FUND,
    VERDICT_MIN_TECH, VERDICT_CORE_SCORE, VERDICT_VETO_SCORE,
)
from app.services.market_confluence_service import KLINE_LIMIT

db = SessionLocal()
print("### 构建共振索引（force=True，读层改动即时生效，不等快照重建）...", flush=True)
res = resonance_index_build(force=True)
print("索引:", {k: res.get(k) for k in ("total", "tech_needed", "tech_computed", "tech_failed")}, flush=True)
items = _reso_cache["items"]

print("\n" + "=" * 116)
print("A. 决策档位 × 买点信号 交叉表（共振视图全量）——重点看「候选 + 趋势未确认」是否为 0")
print("=" * 116)
byv = defaultdict(Counter)
for it in items:
    v, _ = _verdict(it.get("composite_score"), it.get("tech_score"),
                    min_fund=VERDICT_MIN_FUND, min_tech=VERDICT_MIN_TECH,
                    core=VERDICT_CORE_SCORE, veto=VERDICT_VETO_SCORE,
                    tech_blocker=it.get("tech_blocker"))
    byv[v][it.get("buy_signal") or "(None)"] += 1
for v in ("core", "candidate", "watch", "eliminated"):
    c = byv.get(v)
    if not c:
        continue
    tot = sum(c.values())
    print(f"  {v:<11} n={tot:<5} " + "  ".join(f"{k}={n}({n/tot*100:.0f}%)" for k, n in c.most_common()))

bad = [it for it in items
       if _verdict(it.get("composite_score"), it.get("tech_score"),
                   min_fund=VERDICT_MIN_FUND, min_tech=VERDICT_MIN_TECH,
                   core=VERDICT_CORE_SCORE, veto=VERDICT_VETO_SCORE)[0] == "candidate"
       and it.get("buy_signal") == "neutral"]
print(f"\n  ★ 「买入候选 + 买点=趋势未确认」的票数 = {len(bad)}（应为 0）")

print("\n" + "=" * 116)
print("B. 左侧抄底防守：被它否决的票（tech_blocker 非空）")
print("=" * 116)
blocked = [it for it in items if it.get("tech_blocker")]
print(f"  共 {len(blocked)} 只技术面被左侧防守否决")
print(f"  {'代码':<11}{'名称':<10}{'基本面':>7}{'决策':>7}  买点信号 / 形态")
for it in sorted(blocked, key=lambda x: -(x.get("composite_score") or 0))[:25]:
    v, _ = _verdict(it.get("composite_score"), None,
                    min_fund=VERDICT_MIN_FUND, min_tech=VERDICT_MIN_TECH,
                    core=VERDICT_CORE_SCORE, veto=VERDICT_VETO_SCORE)
    print(f"  {it.get('symbol'):<11}{(it.get('name') or '')[:8]:<10}"
          f"{(it.get('composite_score') or 0):>7.1f}{v:>7}  "
          f"{it.get('buy_signal')} / {it.get('pattern_name')}")

print("\n" + "=" * 116)
print("C. 用户点名 5 只票的现状（改前全是「买入候选 + 中性」或技术 90~100）")
print("=" * 116)
NAMED = [("002371.SZ", "北方华创"), ("603456.SH", "九洲药业"), ("001359.SZ", "平安电工"),
         ("002409.SZ", "雅克科技"), ("603496.SH", "恒为科技"), ("603995.SH", "甬金股份")]
idx = {it.get("symbol"): it for it in items}
for sym, nm in NAMED:
    it = idx.get(sym)
    if not it:
        print(f"\n  [{sym}] {nm}: 不在共振索引（基本面 < 淘汰线 或 无 K 线）")
        continue
    kl, _ = KlineService(db).get_recent_klines(sym, limit=KLINE_LIMIT)
    c = [float(k.close) for k in kl]
    ma60 = sum(c[-60:]) / 60 if len(c) >= 60 else None
    v, vr = _verdict(it.get("composite_score"), it.get("tech_score"),
                     min_fund=VERDICT_MIN_FUND, min_tech=VERDICT_MIN_TECH,
                     core=VERDICT_CORE_SCORE, veto=VERDICT_VETO_SCORE,
                     tech_blocker=it.get("tech_blocker"))
    print(f"\n  [{sym}] {nm}  基本面={it.get('composite_score')} 技术面={it.get('tech_score')} "
          f"决策={v}  买点={it.get('buy_label')}({it.get('buy_signal')})")
    print(f"    形态={it.get('pattern_name')} 形态分={it.get('pattern_score')} "
          f"combined={it.get('combined_score')} 收盘={ma60 and c[-1]:.2f} MA60={ma60 and ma60:.2f}")
    print(f"    决策理由：{' / '.join(vr)}")
    if it.get("buy_reasons"):
        print(f"    买点理由：{' / '.join(it['buy_reasons'])}")
    if it.get("tech_blocker"):
        print(f"    ⛔ 技术面否决：{it['tech_blocker']}")

print("\n" + "=" * 116)
print("D. buy_signal 全量分布")
print("=" * 116)
c_dist = Counter(it.get("buy_signal") for it in items)
for k, n in c_dist.most_common():
    print(f"  {str(k):<22}{n:>6}  ({n/len(items)*100:.1f}%)")
PY
