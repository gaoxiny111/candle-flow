"""立霸 603519：新浪中报(2026Q2)应收/存货/利润，验证 203.66% 与 77%。"""
import json
import sys

sys.path.insert(0, ".")

import requests

CODE = "603519"
H = {"User-Agent": "Mozilla/5.0"}

# 新浪资产负债表
bs = requests.get(
    f"https://money.finance.sina.com.cn/corp/go.php/vFD_BalanceSheet/stockid/{CODE}/ctrl/2026/displaytype/4.phtml",
    headers=H,
    timeout=20,
)
print("BS status:", bs.status_code, len(bs.text))

# 新浪现金流量表
cf = requests.get(
    f"https://money.finance.sina.com.cn/corp/go.php/vFD_CashFlow/stockid/{CODE}/ctrl/2026/displaytype/4.phtml",
    headers=H,
    timeout=20,
)
print("CF status:", cf.status_code, len(cf.text))

import re

def grab(html, keys):
    out = {}
    for k in keys:
        m = re.search(re.escape(k) + r"</th>\s*<td[^>]*>([^<]*)</td>", html)
        if not m:
            m = re.search(re.escape(k) + r"[\s\S]{0,200}?<td[^>]*>([\d\.,\-]+)</td>", html)
        if m:
            out[k] = m.group(1).strip()
    return out

print("\n=== BS ===")
for k, v in grab(bs.text, ["应收账款", "存货", "总资产", "货币资金", "预收款项", "合同负债"]).items():
    print(f"  {k} = {v}")
print("\n=== CF ===")
for k, v in grab(cf.text, ["经营活动产生的现金流量净额", "销售商品、提供劳务收到的现金", "投资活动产生的现金流量净额"]).items():
    print(f"  {k} = {v}")

# 中报利润表
isr = requests.get(
    f"https://money.finance.sina.com.cn/corp/go.php/vFD_ProfitStatement/stockid/{CODE}/ctrl/2026/displaytype/4.phtml",
    headers=H,
    timeout=20,
)
print("\n=== IS ===")
for k, v in grab(isr.text, ["营业总收入", "营业收入", "净利润", "归属于母公司所有者的净利润", "投资收益", "营业利润"]).items():
    print(f"  {k} = {v}")

# 落盘原文片段供人工核对
with open("_diag_liba_raw_bs.txt", "w", encoding="utf-8") as f:
    f.write(bs.text[:60000])
