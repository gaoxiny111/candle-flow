"""立霸 603519：2023 投资收益 vs 扣非净利；分红现金 vs 自由现金流；减持窗口。"""
import json
import sys

sys.path.insert(0, ".")

import requests

SYM_CODE = "603519"


def em(report, cols, filt, size="10", sort="REPORT_DATE"):
    r = requests.get(
        "https://datacenter-web.eastmoney.com/api/data/v1/get",
        params={
            "reportName": report,
            "columns": cols,
            "filter": filt,
            "pageNumber": "1",
            "pageSize": size,
            "sortColumns": sort,
            "sortTypes": "-1",
            "source": "WEB",
            "client": "WEB",
        },
        headers={"User-Agent": "Mozilla/5.0", "Referer": "https://data.eastmoney.com/"},
        timeout=15,
    )
    d = r.json()
    return ((d.get("result") or {}).get("data")) or d.get("message")


print("=== 利润表全字段探测 ===")
try:
    r = requests.get(
        "https://datacenter-web.eastmoney.com/api/data/v1/get",
        params={
            "reportName": "RPT_F10_FINANCE_GINCOME",
            "columns": "ALL",
            "filter": f'(SECUCODE="{SYM_CODE}.SH")',
            "pageNumber": "1",
            "pageSize": "3",
            "sortColumns": "REPORT_DATE",
            "sortTypes": "-1",
            "source": "WEB",
            "client": "WEB",
        },
        headers={"User-Agent": "Mozilla/5.0", "Referer": "https://data.eastmoney.com/"},
        timeout=15,
    )
    rows = ((r.json().get("result") or {}).get("data")) or []
    if rows:
        keys = sorted(rows[0].keys())
        print("ALL columns:", keys)
        for row in rows:
            print("\n---", row.get("REPORT_DATE"))
            for k in keys:
                v = row.get(k)
                if isinstance(v, (int, float)) and v:
                    print(f"  {k} = {v}")
except Exception as e:
    print("fail:", e)

print("\n=== 分红明细 RPT_SHAREBONUS_DET ===")
print(json.dumps(em("RPT_SHAREBONUS_DET", "ALL", f'(SECURITY_CODE="{SYM_CODE}")', size="8"), ensure_ascii=False, indent=1)[:2500])
