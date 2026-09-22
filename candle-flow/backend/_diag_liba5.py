"""核实两个数据源：(1) 减持公告能否解析起止日期 (2) 大宗交易折价率接口是否存在。"""
import json
import re
import sys

sys.path.insert(0, ".")

import requests

CODE = "603519"
H = {"User-Agent": "Mozilla/5.0", "Referer": "https://data.eastmoney.com/"}

print("=== 1. 公告原文（含减持窗口期）===")
try:
    r = requests.get(
        "https://np-anotice-stock.eastmoney.com/api/security/ann",
        params={
            "sr": "-1",
            "page_size": "50",
            "page_index": "1",
            "ann_type": "A",
            "client_source": "web",
            "stock_list": CODE,
        },
        headers=H,
        timeout=15,
    )
    d = r.json()
    rows = (d.get("data") or {}).get("list") or []
    print("公告数:", len(rows))
    for it in rows:
        t = it.get("title") or ""
        if "减持" in t or "大宗" in t:
            print(f"  [{it.get('notice_date','')[:10]}] {t}")
            print(f"      art_code={it.get('art_code')}")
except Exception as e:
    print("fail:", type(e).__name__, e)

print("\n=== 2. 公告正文（试抓取减持公告全文，看能否解析日期）===")
try:
    art = None
    for it in rows:
        if "减持" in (it.get("title") or ""):
            art = it.get("art_code")
            break
    if art:
        r2 = requests.get(
            "https://np-cnotice-stock.eastmoney.com/api/content/ann",
            params={"art_code": art, "client_source": "web", "page_index": "1"},
            headers=H,
            timeout=15,
        )
        body = (r2.json().get("data") or {}).get("notice_content") or ""
        print("正文长度:", len(body))
        # 找日期区间
        pats = re.findall(r"(20\d{2}\s*年\s*\d{1,2}\s*月\s*\d{1,2}\s*日)[^。]{0,30}?(20\d{2}\s*年\s*\d{1,2}\s*月\s*\d{1,2}\s*日)", body)
        print("日期区间候选:", pats[:5])
        idx = body.find("减持")
        print("片段:", body[max(0, idx - 100): idx + 400].replace("\n", " "))
except Exception as e:
    print("fail:", type(e).__name__, e)

print("\n=== 3. 大宗交易接口探测 ===")
for rpt in ("RPT_DATA_BLOCKTRADE", "RPT_BLOCKTRADE_DETAIL", "RPT_DMSK_BLOCKTRADE"):
    try:
        r3 = requests.get(
            "https://datacenter-web.eastmoney.com/api/data/v1/get",
            params={
                "reportName": rpt,
                "columns": "ALL",
                "filter": f'(SECURITY_CODE="{CODE}")',
                "pageNumber": "1",
                "pageSize": "5",
                "sortColumns": "TRADE_DATE",
                "sortTypes": "-1",
                "source": "WEB",
                "client": "WEB",
            },
            headers=H,
            timeout=15,
        )
        j = r3.json()
        data = ((j.get("result") or {}).get("data")) or []
        if data:
            print(f"  {rpt}: OK, {len(data)} 行")
            print("   keys:", sorted(data[0].keys())[:25])
            print("   sample:", json.dumps({k: v for k, v in list(data[0].items())[:12]}, ensure_ascii=False))
        else:
            print(f"  {rpt}: 无数据 / {j.get('message')}")
    except Exception as e:
        print(f"  {rpt}: fail {type(e).__name__} {e}")
