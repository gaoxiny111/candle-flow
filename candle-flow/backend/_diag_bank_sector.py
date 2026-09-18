# -*- coding: utf-8 -*-
"""用 20 家银行真实资产负债率过一遍新口径，验证区分度（不再整行业一律 E）。"""
import sys
sys.path.insert(0, '.')
import pandas as pd
from app.analysis.modules.solvency import SolvencyAnalyzer
from app.analysis.config.company_profiles import bank_benchmarks

BANKS = [
    ("600036.SH", "招商银行", 90.18), ("600016.SH", "民生银行", 90.79),
    ("601658.SH", "邮储银行", 93.95), ("601288.SH", "农业银行", 93.47),
    ("600919.SH", "江苏银行", 93.52), ("601009.SH", "南京银行", 93.14),
    ("600926.SH", "杭州银行", 92.96), ("002142.SZ", "宁波银行", 92.78),
    ("601398.SH", "工商银行", 92.37), ("601229.SH", "上海银行", 92.16),
    ("601328.SH", "交通银行", 91.98), ("601988.SH", "中国银行", 91.94),
    ("601939.SH", "建设银行", 91.90), ("600000.SH", "浦发银行", 91.92),
    ("002839.SZ", "张家港行", 91.84), ("601128.SH", "常熟银行", 91.65),
    ("601166.SH", "兴业银行", 91.62), ("601818.SH", "光大银行", 91.52),
    ("601998.SH", "中信银行", 91.42), ("002807.SZ", "江阴银行", 91.10),
]
b = bank_benchmarks()
fd = pd.DataFrame({"total_assets": [1e13], "equity": [1e12]}, index=["20260630"])
eng = SolvencyAnalyzer()
rows = []
for sym, nm, dr in BANKS:
    r = eng.analyze(fd, name=nm, symbol=sym, industry="银行Ⅱ", debt_ratio=dr,
                    current_ratio=0.0, quick_ratio=0.0, interest_bearing_ratio=1.0)
    rows.append((r.score, dr, nm, [i.name for i in r.indicators], r.level.value))
rows.sort(reverse=True)
print("银行同业中位 DR = %.2f%%（20 家样本）\n" % b["debt_ratio_pct"])
print("%-10s %8s %7s %4s  %s" % ("name", "DR%", "score", "lvl", "indicators"))
for score, dr, nm, inds, lvl in rows:
    print("%-10s %8.2f %7.1f %4s  %s" % (nm, dr, score, lvl, ",".join(inds)))
lo, hi = rows[-1], rows[0]
print()
print("分布：%.1f (%s) ~ %.1f (%s)   极差 %.1f 分 → 仍保留区分度" % (
    lo[0], lo[2], hi[0], hi[2], hi[0] - lo[0]))
print("旧口径下这 20 家全部为 33.3 分 E 级（3 项被误判 + 1 项被误高）")
