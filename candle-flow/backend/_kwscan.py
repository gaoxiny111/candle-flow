# -*- coding: utf-8 -*-
"""对比旧/新关键词集合在 500 只样本真实公告标题上的召回差异。"""
import sys, collections
sys.path.insert(0, "/opt/candle-flow/backend")
from app.services.major_risk_events import (
    fetch_stock_notices, scan_notice_titles, RISK_RULES,
)

OLD_KW = ("减持计划", "股份减持", "拟减持", "大宗交易减持")

def old_hit(title):
    return any(k in title for k in OLD_KW)

import sqlite3, json
con = sqlite3.connect("/opt/candle-flow/backend/data/candle_flow.db")
syms = [r[0] for r in con.execute(
    "SELECT symbol FROM factor_snapshots ORDER BY RANDOM() LIMIT 260")]
con.close()

only_old = only_new = both = 0
samples_new = []
reduce_titles = 0
for s in syms:
    try:
        ns = fetch_stock_notices(s)
    except Exception:
        continue
    for n in ns:
        t = n.get("title") or ""
        if "减持" not in t:
            continue
        reduce_titles += 1
        new = any(h.rule_id == "reduce_hold" for h in scan_notice_titles([n]))
        o = old_hit(t)
        if new and not o:
            only_new += 1
            if len(samples_new) < 12:
                samples_new.append((s, n.get("notice_date"), t))
        elif o and not new:
            only_old += 1
        elif new:
            both += 1

print(f"扫描 {len(syms)} 只，含「减持」的公告 {reduce_titles} 条")
print(f"  仅旧规则命中: {both}  (严格子串本来就覆盖的)")
print(f"  新规则新增召回: {only_new}")
print(f"  仅旧命中(新规则丢失): {only_old}   <- 必须为 0")
print("\n--- 新规则新增召回的样本 ---")
for s, d, t in samples_new:
    print(f"  {s} {d} {t[:70]}")
