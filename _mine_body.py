# -*- coding: utf-8 -*-
"""探查：减持公告正文能否拿到减持比例（%）。"""
import re
import sys

sys.path.insert(0, "/opt/candle-flow/backend")
import requests

from app.services.major_risk_events import NOTICE_URL, _HEADERS, _code6

code = _code6("603519.SH")
params = {"sr": "-1", "page_size": "50", "page_index": "1", "ann_type": "A",
          "client_source": "web", "stock_list": code, "f_node": "0", "s_node": "0"}
r = requests.get(NOTICE_URL, params=params, headers=_HEADERS, timeout=12)
items = ((r.json() or {}).get("data") or {}).get("list") or []
print("公告条数 =", len(items))
target = None
for it in items:
    if "减持股份计划" in str(it.get("title") or ""):
        target = it
        break
if target:
    print("\n命中目标公告：")
    print("  title =", target.get("title"))
    print("  keys  =", sorted(target.keys()))
    for k, v in target.items():
        if k in ("title", "notice_date"):
            continue
        print("  %-14s = %s" % (k, str(v)[:120]))
    art = target.get("art_code")
    print("\n  art_code =", art)
    if art:
        url = "https://np-cnotice-stock.eastmoney.com/api/content/ann"
        for p2 in ({"art_code": art, "client_source": "web", "page_index": "1"},
                   {"art_code": art, "client_source": "web"}):
            try:
                rr = requests.get(url, params=p2, headers=_HEADERS, timeout=12)
                print("  正文 API status=%s len=%s" % (rr.status_code, len(rr.text)))
                if rr.ok:
                    j = rr.json() or {}
                    d = j.get("data") or {}
                    print("  正文 data keys =", sorted(d.keys()))
                    body = d.get("notice_content") or d.get("content") or ""
                    if body:
                        txt = re.sub(r"<[^>]+>", "", str(body))
                        print("  正文前 600 字：")
                        print("   ", txt[:600].replace("\n", " "))
                        pct = re.findall(r"([\d.]+)\s*%", txt[:3000])
                        print("  正文前3000字里的百分数 =", pct[:25])
                    break
            except Exception as e:
                print("  正文失败:", e)
else:
    print("未找到减持公告")
