"""核实新浪利润表「扣除非经常性损益后的净利润」行的真实标签。"""
import re
import sys

sys.path.insert(0, ".")

import requests

CODE = "603519"
H = {"User-Agent": "Mozilla/5.0"}

r = requests.get(
    f"https://money.finance.sina.com.cn/corp/go.php/vFD_ProfitStatement/stockid/{CODE}/ctrl/2025/displaytype/4.phtml",
    headers=H,
    timeout=20,
)
html = r.text
print("len:", len(html))

# 找所有含「扣」的表格行标签
for m in re.finditer(r"<th[^>]*>([^<]*扣[^<]*)</th>", html):
    print("TH:", repr(m.group(1)))

for m in re.finditer(r"扣[^<]{0,30}", html):
    tag = m.group(0)
    if len(tag) > 3:
        print("HIT:", repr(tag))

print("\n--- 所有 IS 关键标签核对 ---")
for key in ["一、营业总收入", "三、营业利润", "五、净利润", "归属于母公司所有者的净利润", "基本每股收益"]:
    print(f"  {key}: {'FOUND' if key in html else 'MISSING'}")

# 打印含「净利润」的所有 th
for m in re.finditer(r"<th[^>]*>([^<]*净利润[^<]*)</th>", html):
    print("NP-TH:", repr(m.group(1)))
