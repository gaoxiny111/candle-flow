"""核实：efficiency 模块能否拿到中报期应收/存货/营收数据。"""
import sys

sys.path.insert(0, ".")

from app.analysis.financials import build_financial_dataframe  # noqa

for sym in ("603519.SH",):
    fd, meta = build_financial_dataframe(sym)
    print(f"=== {sym} ===")
    print("fd.index:", [str(i) for i in fd.index])
    print("latest_report:", meta.get("latest_report"))
    print("report_dates:", meta.get("report_dates"))
    print("\nsingle_quarter:", meta.get("single_quarter"))
    print("\nfd cols 有应收/存货?:", "accounts_receivable" in fd.columns, "inventory" in fd.columns)
    # 关键：fd 是否含中报期行？
    for idx in fd.index:
        print(f"  {idx}: rev={fd.at[idx,'revenue']:.0f} ar={fd.at[idx,'accounts_receivable']:.0f} inv={fd.at[idx,'inventory']:.0f}")
