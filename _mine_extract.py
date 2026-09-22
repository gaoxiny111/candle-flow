# -*- coding: utf-8 -*-
"""减持比例结构化提取可行性：多票抽样 + 耗时测量。"""
import re
import sys
import time

sys.path.insert(0, "/opt/candle-flow/backend")
import requests

from app.services.major_risk_events import NOTICE_URL, _HEADERS, _code6

BODY_URL = "https://np-cnotice-stock.eastmoney.com/api/content/ann"


def fetch_body(art_code):
    r = requests.get(BODY_URL, params={"art_code": art_code, "client_source": "web"},
                     headers=_HEADERS, timeout=12)
    if not r.ok:
        return ""
    j = r.json() or {}
    return str((j.get("data") or {}).get("notice_content") or "")


def strip_html(s):
    return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", "", s))


# 抽取「占总股本 X%」与「不超过 N 股」
PAT_TOTAL_PCT = re.compile(r"占\s*(?:公司)?(?:总)?股本\s*(?:的)?\s*([\d.]+)\s*%")
PAT_SHARES = re.compile(r"(?:不超过|合计不超过|拟减持)[^。；]{0,40}?([\d,，]{4,})\s*股")
PAT_MAX_PCT = re.compile(r"不超过\s*(?:公司)?(?:股份)?总数?的?\s*([\d.]+)\s*%")


def extract(body):
    txt = strip_html(body)
    out = {}
    m = PAT_MAX_PCT.search(txt)
    if m:
        out["cap_pct"] = float(m.group(1))
    m = PAT_SHARES.search(txt)
    if m:
        out["shares"] = m.group(1).replace(",", "").replace("，", "")
    m = PAT_TOTAL_PCT.search(txt)
    if m:
        out["holding_pct"] = float(m.group(1))
    return out


SAMPLES = ["603519.SH", "603099.SH", "600722.SH", "002107.SZ", "300033.SZ"]

t0 = time.time()
for sym in SAMPLES:
    code = _code6(sym)
    try:
        r = requests.get(NOTICE_URL, params={
            "sr": "-1", "page_size": "50", "page_index": "1", "ann_type": "A",
            "client_source": "web", "stock_list": code, "f_node": "0", "s_node": "0"},
            headers=_HEADERS, timeout=12)
        items = ((r.json() or {}).get("data") or {}).get("list") or []
    except Exception as e:
        print("%s 列表失败 %s" % (sym, e))
        continue
    red = [it for it in items if "减持" in str(it.get("title") or "")]
    print("=" * 78)
    print("%s  公告 %d 条，含「减持」%d 条" % (sym, len(items), len(red)))
    for it in red[:3]:
        t = str(it.get("title") or "")
        body = fetch_body(it.get("art_code"))
        ex = extract(body)
        print("   %s  %s" % (it.get("notice_date"), t[:52]))
        print("      body_len=%d  extract=%s" % (len(body), ex))
        if body:
            txt = strip_html(body)
            seg = txt[:100]
            print("      head: %s" % seg[:96])
print()
print("总耗时 %.1fs" % (time.time() - t0))
