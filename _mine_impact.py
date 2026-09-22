# -*- coding: utf-8 -*-
"""排雷四条全库影响实测。

注意：条件是「AND」还是「OR」在用户原文里是分列的独立条件（任一触发即拦截），
这里逐条测，并测组合后的总拦截量。
"""
import json
import sqlite3
import sys

sys.path.insert(0, "/opt/candle-flow/backend")
from app.analysis.engine import STRONG_CYCLICAL_INDUSTRIES  # noqa

conn = sqlite3.connect("/opt/candle-flow/backend/data/candle_flow.db")
conn.row_factory = sqlite3.Row

D = []
for r in conn.execute("select symbol, composite_score, pe_ttm, payload from factor_snapshots"):
    p = json.loads(r["payload"])
    mk = p.get("market") or {}
    mr = p.get("major_risks") or {}
    mods = p.get("modules") or {}
    rel = ((p.get("valuation") or {}).get("relative")) or {}

    def ind(mod, pred):
        for it in ((mods.get(mod) or {}).get("indicators") or []):
            if pred(str(it.get("name") or "")):
                return it.get("value")
        return None

    D.append({
        "sym": r["symbol"], "name": p.get("name") or "", "ind": p.get("industry") or "",
        "comp": r["composite_score"] or 0,
        "pe": r["pe_ttm"] if r["pe_ttm"] is not None else mk.get("pe_ttm"),
        "pe_med": (rel.get("PE_TTM") or {}).get("industry_median"),
        "pe_prem": (rel.get("PE_TTM") or {}).get("industry_premium_pct"),
        "debt": ind("solvency", lambda n: n.startswith("资产负债率")),
        "ocf_ps": ind("cashflow", lambda n: "每股经营现金流" in n),
        "obs": mr.get("observe_events") or [],
        "evts": mr.get("events") or [],
        "comp_veto": p.get("compliance_veto"),
        "fatal": mr.get("fatal"),
    })

N = len(D)
print("全库 =", N)


def rate(label, h):
    print("  %-56s %5d  %6.2f%%" % (label, len(h), 100.0 * len(h) / N))


print()
print("=" * 84)
print("条件1 大股东减持（过去1个月，比例>0.5%）")
print("=" * 84)
import datetime
CUT = "2026-08-22"
red = [d for d in D if any(e.get("rule_id") == "reduce_hold" for e in d["obs"])]
rate("有减持观察事件（不限时间窗）", red)
red1m = [d for d in D if any(e.get("rule_id") == "reduce_hold"
                            and str(e.get("notice_date") or "")[:10] >= CUT for e in d["obs"])]
rate("近1个月内有减持事件", red1m)
print("  近1个月内有减持的（按综合分降序，前 15）：")
for d in sorted(red1m, key=lambda x: -x["comp"])[:15]:
    e = [x for x in d["obs"] if x.get("rule_id") == "reduce_hold"][0]
    print("     %-11s %-8s comp=%-5.1f %s" % (d["sym"], d["name"][:8], d["comp"], str(e.get("title"))[:40]))
print()
print("  ⚠ 比例% 不在快照里，需另抓公告正文（实测可行，见 _mine_extract.py）")

print()
print("=" * 84)
print("条件2 估值极端泡沫：PE > 80  或  PE > 行业均值 x2")
print("=" * 84)
p80 = [d for d in D if d["pe"] is not None and d["pe"] > 80]
rate("PE > 80", p80)
med = [d for d in D if d["pe"] is not None and d["pe_med"] is not None
       and float(d["pe_med"]) > 0 and d["pe"] > 2 * float(d["pe_med"])]
print("  PE > 行业均值x2 的（行业均值字段有值 %d 只）:" % sum(1 for d in D if d["pe_med"] is not None))
rate("  其中 PE > 行业均值x2", med)
union = {d["sym"] for d in p80} | {d["sym"] for d in med}
print("  两者并集 = %d (%.2f%%)" % (len(union), 100.0 * len(union) / N))
hi = [d for d in D if d["comp"] >= 70]
print("  其中 comp>=70（B级及以上）的 = %d / %d 只" %
      (len([d for d in D if d["sym"] in union and d["comp"] >= 70]), len(hi)))

print()
print("=" * 84)
print("条件3 资金链极度紧张：资产负债率>70% 且 经营现金流连续2年为负")
print("=" * 84)
d70 = [d for d in D if d["debt"] is not None and d["debt"] > 70]
rate("资产负债率 > 70%", d70)
ocfneg = [d for d in D if d["ocf_ps"] is not None and d["ocf_ps"] < 0]
rate("每股经营现金流 < 0（当期代理）", ocfneg)
both = [d for d in d70 if d["ocf_ps"] is not None and d["ocf_ps"] < 0]
rate("两者同时成立", both)
for d in sorted(both, key=lambda x: -x["comp"])[:12]:
    print("     %-11s %-8s comp=%-5.1f 负债率=%.1f%% 每股OCF=%s"
          % (d["sym"], d["name"][:8], d["comp"], d["debt"], d["ocf_ps"]))
print("  注：快照只有当期每股 OCF，**没有「连续2年为负」的年度序列** →")
print("      「连续2年」需从 financials 年度序列取，属快照外字段。")

print()
print("=" * 84)
print("条件4 合规风险：ST/*ST 或 立案调查/处罚")
print("=" * 84)
inv = [d for d in D if any(e.get("rule_id") == "investigation" for e in d["evts"])]
rate("命中立案调查（survival 级）", inv)
st = [d for d in D if "ST" in d["name"].upper()]
rate("名称含 ST/*ST", st)
print("  注：ST 已被榜单 filters.exclude_st 过滤；")
print("      立案调查 investigation 规则**早已存在**且 severity=survival（强制 E）。")
print("      缺的是「处罚」类（行政处罚/警示函）关键词。")
