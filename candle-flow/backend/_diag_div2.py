"""验证分红来源拆解字段。"""
import json
import sys

sys.path.insert(0, ".")

from app.analysis.financials import build_financial_dataframe, fetch_deducted_series  # noqa

for sym in ("603519.SH", "600585.SH", "601088.SH"):
    print(f"\n{'='*20} {sym} {'='*20}")
    try:
        fd, meta = build_financial_dataframe(sym)
        d = meta.get("dividend") or {}
        for k in (
            "fy", "cash_total", "payout_ratio_pct", "consecutive_years",
            "fcf_coverage_3y", "fcf_sum_3y", "fcf_window_3y",
            "nonrecurring_3y", "nonrecurring_latest", "nonrecurring_window_3y",
        ):
            print(f"  {k} = {d.get(k)}")
    except Exception as e:
        print("FAIL:", type(e).__name__, e)

print("\n=== 东财扣非序列 603519 ===")
s = fetch_deducted_series("603519.SH")
for k in sorted(s, reverse=True)[:8]:
    print(f"  {k}: parent={s[k][0]} deducted={s[k][1]}")
