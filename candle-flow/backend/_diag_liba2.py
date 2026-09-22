"""立霸 603519：查利润表原始项（投资收益/营业利润/扣非）+ 2026 中报三表。"""
import json
import sys

sys.path.insert(0, ".")

import requests

SYM_CODE = "603519"


def _sina(url, params):
    r = requests.get(url, params=params, headers={"User-Agent": "Mozilla/5.0"}, timeout=15)
    return r.json()


# 新浪利润表（合并报表）
print("=== SINA 利润表 ===")
try:
    j = _sina(
        "https://money.finance.sina.com.cn/corp/go.php/vDOWNLOAD_FinanceReport/stockid/%s/type/income.phtml" % SYM_CODE,
        {},
    )
    print(json.dumps(j, ensure_ascii=False)[:500])
except Exception as e:
    print("sina download fail:", e)

# 东财 datacenter 利润表关键项
for rpt, cols in (
    ("RPT_DMSK_FN_INCOME", "SECUCODE,REPORT_DATE,TOTAL_OPERATE_INCOME,OPERATE_PROFIT,PARENT_NETPROFIT,DEDUCT_PARENT_NETPROFIT,INVEST_INCOME,FAIRVALUE_CHANGE_INCOME,NONBUSINESS_INCOME,NONBUSINESS_EXPENSE"),
    ("RPT_F10_FINANCE_GINCOME", "SECUCODE,REPORT_DATE,INVEST_INCOME,OPERATE_PROFIT,PARENT_NETPROFIT"),
):
    print(f"\n=== {rpt} ===")
    try:
        r = requests.get(
            "https://datacenter-web.eastmoney.com/api/data/v1/get",
            params={
                "reportName": rpt,
                "columns": cols,
                "filter": f'(SECUCODE="{SYM_CODE}.SH")',
                "pageNumber": "1",
                "pageSize": "12",
                "sortColumns": "REPORT_DATE",
                "sortTypes": "-1",
                "source": "WEB",
                "client": "WEB",
            },
            headers={"User-Agent": "Mozilla/5.0", "Referer": "https://data.eastmoney.com/"},
            timeout=15,
        )
        d = r.json()
        rows = ((d.get("result") or {}).get("data")) or []
        if not rows:
            print("no rows:", json.dumps(d, ensure_ascii=False)[:300])
        for row in rows:
            print(json.dumps({k: row.get(k) for k in cols.split(",")}, ensure_ascii=False))
    except Exception as e:
        print("fail:", type(e).__name__, e)
