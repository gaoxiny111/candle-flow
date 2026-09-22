"""验证减持窗口期 + 大宗交易折价率。"""
import json
import sys

sys.path.insert(0, ".")

from app.services.major_risk_events import (  # noqa
    detect_major_risk_events,
    parse_reduce_window,
    fetch_recent_block_trades,
)

print("=== parse_reduce_window 单测 ===")
samples = [
    "减持期间为2026年9月16日至2026年12月15日，通过集中竞价交易减持",
    "自2026 年 9 月 16 日 至 2026 年 12 月 15 日",
    "减持期间2025年3月1日至2025年6月30日",
    "无日期区间",
]
for s in samples:
    print(f"  {s[:40]:42s} -> {parse_reduce_window(s)}")

for sym in ("603519.SH",):
    print(f"\n=== {sym} detect_major_risk_events ===")
    r = detect_major_risk_events(sym)
    print("in_reduce_window:", r.get("in_reduce_window"))
    print("reduce_remaining_days:", r.get("reduce_remaining_days"))
    print("reduce_window:", json.dumps(r.get("reduce_window"), ensure_ascii=False))
    print("observe_labels:", r.get("observe_labels"))
    print("latest_block_trade:", json.dumps(r.get("latest_block_trade"), ensure_ascii=False))
    print("block_trades count:", len(r.get("block_trades") or []))
    for ev in r.get("observe_events") or []:
        if ev.get("rule_id") == "reduce_hold":
            print("  reduce ev:", json.dumps(ev, ensure_ascii=False, indent=1))
