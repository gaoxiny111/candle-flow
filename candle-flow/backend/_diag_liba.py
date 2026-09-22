"""立霸股份 603519 数据体检：验证用户提出的四个数字。"""
import json
import sys

sys.path.insert(0, ".")

from app.analysis.financials import build_financial_dataframe  # noqa

SYM = "603519.SH"

fd, meta = build_financial_dataframe(SYM)

print("=== meta keys ===")
print(sorted(meta.keys()))

print("\n=== dividend ===")
print(json.dumps(meta.get("dividend"), ensure_ascii=False, indent=2))

print("\n=== payout_ratio_pct ===", meta.get("payout_ratio_pct"))
print("=== ar_metrics ===")
print(json.dumps(meta.get("ar_metrics"), ensure_ascii=False, indent=2))
print("=== latest_cash_ratio ===", meta.get("latest_cash_ratio"))
print("=== latest_cash_ratio_source ===", meta.get("latest_cash_ratio_source"))
print("=== latest_period_net_profit ===", meta.get("latest_period_net_profit"))
print("=== latest_operating_cashflow ===", meta.get("latest_operating_cashflow"))
print("=== latest_fcf ===", meta.get("latest_fcf"))
print("=== deducted_yoy_pct ===", meta.get("deducted_yoy_pct"))
print("=== single_quarter ===")
print(json.dumps(meta.get("single_quarter"), ensure_ascii=False, indent=2))
print("=== interim_balance_sheet ===")
print(json.dumps(meta.get("interim_balance_sheet"), ensure_ascii=False, indent=2))

print("\n=== fd columns ===")
print(list(fd.columns))
print("\n=== fd index ===")
print([str(i) for i in fd.index])

print("\n=== fd 关键列 tail ===")
cols = [c for c in ["revenue", "net_profit", "operating_cashflow", "capital_expenditure", "accounts_receivable", "inventory", "total_assets", "monetary_funds", "advance_receipts"] if c in fd.columns]
print(fd[cols].tail(8).to_string())
