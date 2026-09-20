# -*- coding: utf-8 -*-
"""dump 宁德时代 ROIC 计算的分子/分母，验证 91.74% 是否为口径错误。"""
import sys

sys.path.insert(0, ".")
import pandas as pd

from app.analysis.financials import build_financial_dataframe

pd.set_option("display.width", 220)


def fmt(series):
    out = []
    for v in series:
        try:
            out.append("%.1f亿" % (float(v) / 1e8) if pd.notna(v) else "NA")
        except Exception:
            out.append(str(v))
    return out


fd, meta = build_financial_dataframe(sys.argv[1] if len(sys.argv) > 1 else "300750.SZ", years=5)

print("index:", list(fd.index))
print()
cols = [
    "monetary_funds", "equity", "interest_bearing_debt", "total_assets",
    "current_liabilities", "operating_profit", "operating_profit_estimated",
    "net_profit", "total_revenue", "inventory", "receivables",
]
for c in cols:
    if c in fd.columns:
        print("%-26s" % c, fmt(pd.to_numeric(fd[c], errors="coerce")))
    else:
        print("%-26s MISSING" % c)

print()
# ── 复刻 _invested_capital_series ──
equity = pd.to_numeric(fd["equity"], errors="coerce")
ibd = pd.to_numeric(fd["interest_bearing_debt"], errors="coerce")
cash = pd.to_numeric(fd["monetary_funds"], errors="coerce") if "monetary_funds" in fd.columns else pd.Series(0.0, index=fd.index)
invested = equity + ibd.fillna(0) - cash.fillna(0)
ta = pd.to_numeric(fd["total_assets"], errors="coerce")
cl = pd.to_numeric(fd["current_liabilities"], errors="coerce") if "current_liabilities" in fd.columns else pd.Series(0.0, index=fd.index)
fallback = ta - cl.fillna(0)
used = invested.where(invested.notna() & (invested > 0), fallback)

print("invested = equity+ibd-cash:", fmt(invested))
print("fallback  = ta-cl        :", fmt(fallback))
print("used                      :", fmt(used))
print()

op = pd.to_numeric(fd["operating_profit"], errors="coerce")
nopat = op * 0.75
roic_series = (nopat / used * 100).dropna()
print("NOPAT = op×0.75           :", fmt(nopat))
print("ROIC series               :", [round(v, 2) for v in roic_series])
if len(roic_series):
    last_op = float(op.iloc[-1])
    last_inv = float(used.iloc[-1])
    print("latest ROIC (未年化)      : %.2f%%" % float(roic_series.iloc[-1]))
    if last_op > 0 and last_inv > 0:
        print("若最新期为半年报(NOPAT×2) : %.2f%%" % (last_op * 0.75 * 2 / last_inv * 100))

print()
print("meta 中相关字段:")
for k, v in (meta or {}).items():
    lk = k.lower()
    if any(t in lk for t in ("wacc", "roic", "cash", "monetary", "report", "period")):
        print("  %-28s %s" % (k, v))
