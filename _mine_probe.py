# -*- coding: utf-8 -*-
"""排雷四条的数据源可得性核查。"""
import json
import sqlite3
import sys

sys.path.insert(0, "/opt/candle-flow/backend")

conn = sqlite3.connect("/opt/candle-flow/backend/data/candle_flow.db")
conn.row_factory = sqlite3.Row


def one(sym):
    r = conn.execute("select payload from factor_snapshots where symbol=?", (sym,)).fetchone()
    return json.loads(r["payload"]) if r else None


print("=" * 84)
print("条件4 合规风险：ST / 立案调查 —— 看 major_risks 与 compliance_veto 真结构")
print("=" * 84)
p = one("603519.SH")
mr = p.get("major_risks") or {}
print("major_risks keys =", sorted(mr.keys()))
print("compliance_veto =", json.dumps(p.get("compliance_veto"), ensure_ascii=False)[:400])
print("risk_level_label =", p.get("risk_level_label"))
print("warnings =", json.dumps(p.get("warnings"), ensure_ascii=False)[:400])
print()

print("=" * 84)
print("条件1 减持比例：能否从公告标题拿到 % ？")
print("=" * 84)
from app.services.major_risk_events import fetch_stock_notices, RISK_RULES

try:
    ns = fetch_stock_notices("603519.SH")
    print("603519 公告条数 =", len(ns))
    for n in ns[:12]:
        print("   %s  %s" % (n.get("notice_date"), n.get("title")))
except Exception as e:
    print("fetch failed:", e)
print()
print("RISK_RULES 各规则命中方式（有无比例字段）：")
for r in RISK_RULES:
    print("  %-18s sev=%-8s kw=%s all_of=%s" % (
        r["id"], r.get("severity"), r.get("keywords"), r.get("all_of")))

print()
print("=" * 84)
print("条件2 估值：PE-TTM / 行业5年均值 —— 实际有什么")
print("=" * 84)
p = one("603519.SH")
mk = p.get("market") or {}
val = p.get("valuation") or {}
rel = val.get("relative") or {}
print("market keys =", sorted(mk.keys()))
print("valuation.relative keys =", sorted(rel.keys()))
print("PE_TTM entry =", json.dumps(rel.get("PE_TTM"), ensure_ascii=False)[:500])
print("comps 里有行业均值吗 =", json.dumps(val.get("comps"), ensure_ascii=False)[:400])
print()

print("=" * 84)
print("条件3 资产负债率>70% 且 经营现金流连续2年为负")
print("=" * 84)
mods = p.get("modules") or {}
print("solvency indicators =", json.dumps(
    [(i.get("name"), i.get("value")) for i in ((mods.get("solvency") or {}).get("indicators") or [])],
    ensure_ascii=False))
print("cashflow indicators =", json.dumps(
    [(i.get("name"), i.get("value")) for i in ((mods.get("cashflow") or {}).get("indicators") or [])],
    ensure_ascii=False)[:700])
print()
print("report_dates =", json.dumps(p.get("report_dates"), ensure_ascii=False))
print("latest_report =", json.dumps(p.get("latest_report"), ensure_ascii=False)[:300])
