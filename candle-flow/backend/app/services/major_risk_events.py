"""重大风险事件：抓取个股公告标题，按关键词命中合规/生存风险。"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Any

import requests

from app.utils.symbol import SymbolError, normalize_symbol, parse_symbol

logger = logging.getLogger(__name__)

NOTICE_URL = "https://np-anotice-stock.eastmoney.com/api/security/ann"
PLEDGE_URL = "https://datacenter-web.eastmoney.com/api/data/v1/get"
CACHE_TTL_SEC = 3 * 3600
LOOKBACK_DAYS = 540  # ~18 个月
MAX_PAGES = 3
PAGE_SIZE = 50
# 控股股东累计质押占持股比例超过此阈值 → 质押危机
PLEDGE_FATAL_RATIO = 0.80

COMPLIANCE_VETO_MESSAGE = "命中重大风险事件，财务打分不适用；请优先关注合规与生存风险"

_HEADERS = {
    "User-Agent": "Mozilla/5.0",
    "Referer": "https://data.eastmoney.com/notices/",
}

# 命中任一即视为致命合规/生存风险（顶部红灯 + 评级强制 E）
RISK_RULES: list[dict[str, Any]] = [
    {
        "id": "pre_reorg",
        "label": "法院预重整/破产重整",
        "keywords": ("预重整", "破产重整", "重整申请", "被法院裁定受理重整", "进入重整程序"),
    },
    {
        "id": "audit_nonstd",
        "label": "非标审计/持续经营不确定性",
        "keywords": (
            "无法表示意见",
            "保留意见",
            "否定意见",
            "持续经营重大不确定性",
            "带有强调事项段",
            "带强调事项段",
            "非标准无保留",
            "非标审计",
        ),
    },
    {
        "id": "investigation",
        "label": "立案调查",
        "keywords": ("立案调查", "证监会立案", "被中国证监会立案", "被立案告知"),
    },
    {
        "id": "share_freeze",
        "label": "股份司法冻结",
        "keywords": ("司法冻结", "股权冻结", "股份冻结", "轮候冻结", "冻结股份", "司法轮候冻结"),
    },
    {
        "id": "face_delist",
        "label": "面值/重大违法退市风险",
        "keywords": (
            "面值退市",
            "可能被终止上市",
            "终止上市风险警示",
            "重大违法强制退市",
        ),
    },
    {
        "id": "pledge_crisis",
        "label": "质押平仓/强制平仓风险",
        "keywords": ("质押平仓", "强制平仓", "质押违约", "触及平仓线", "高比例质押"),
    },
]

_cache: dict[str, tuple[float, dict[str, Any]]] = {}


@dataclass
class RiskHit:
    rule_id: str
    label: str
    keyword: str
    title: str
    notice_date: str = ""
    url: str = ""


def _code6(symbol: str) -> str | None:
    raw = (symbol or "").strip()
    try:
        code, _ = parse_symbol(normalize_symbol(raw))
        return code
    except SymbolError:
        pass
    try:
        code, _ = parse_symbol(raw)
        return code
    except SymbolError:
        pass
    digits = "".join(ch for ch in raw if ch.isdigit())
    if len(digits) >= 6:
        return digits[-6:]
    return None


def _parse_notice_date(raw: Any) -> str:
    if not raw:
        return ""
    s = str(raw)
    if "T" in s:
        s = s.split("T", 1)[0]
    return s[:10]


def _notice_detail_url(code: str, art_code: str) -> str:
    if not art_code:
        return ""
    return f"https://data.eastmoney.com/notices/detail/{code}/{art_code}.html"


def fetch_stock_notices(symbol: str, *, max_pages: int = MAX_PAGES) -> list[dict[str, str]]:
    """拉取个股近期公告标题列表（东财公告 API）。"""
    code = _code6(symbol)
    if not code:
        return []
    cutoff = date.today() - timedelta(days=LOOKBACK_DAYS)
    out: list[dict[str, str]] = []
    try:
        for page in range(1, max_pages + 1):
            params = {
                "sr": "-1",
                "page_size": str(PAGE_SIZE),
                "page_index": str(page),
                "ann_type": "A",
                "client_source": "web",
                "stock_list": code,
                "f_node": "0",
                "s_node": "0",
            }
            r = requests.get(NOTICE_URL, params=params, headers=_HEADERS, timeout=12)
            if not r.ok:
                break
            payload = r.json() or {}
            items = ((payload.get("data") or {}).get("list")) or []
            if not items:
                break
            for item in items:
                title = str(item.get("title") or "").strip()
                if not title:
                    continue
                nd = _parse_notice_date(item.get("notice_date"))
                if nd:
                    try:
                        if datetime.strptime(nd, "%Y-%m-%d").date() < cutoff:
                            continue
                    except ValueError:
                        pass
                art = str(item.get("art_code") or "")
                out.append(
                    {
                        "title": title,
                        "notice_date": nd,
                        "url": _notice_detail_url(code, art),
                    }
                )
    except Exception as e:
        logger.warning("fetch notices failed for %s: %s", symbol, e)
    return out


def scan_notice_titles(notices: list[dict[str, str]]) -> list[RiskHit]:
    """对公告标题做关键词扫描（轻量 NLP：规则命中）。"""
    hits: list[RiskHit] = []
    seen: set[tuple[str, str]] = set()
    for n in notices:
        title = n.get("title") or ""
        for rule in RISK_RULES:
            for kw in rule["keywords"]:
                if kw in title:
                    key = (rule["id"], title)
                    if key in seen:
                        continue
                    seen.add(key)
                    hits.append(
                        RiskHit(
                            rule_id=str(rule["id"]),
                            label=str(rule["label"]),
                            keyword=kw,
                            title=title,
                            notice_date=n.get("notice_date") or "",
                            url=n.get("url") or "",
                        )
                    )
                    break
    return hits


def fetch_controller_pledge_ratio(symbol: str) -> float | None:
    """
    东财重要股东质押明细（RPTA_APP_ACCUMDETAILS）：
    取近 LOOKBACK_DAYS 内「占所持股份比例」最大值。
    """
    code = _code6(symbol)
    if not code:
        return None
    try:
        params = {
            "sortColumns": "NOTICE_DATE",
            "sortTypes": "-1",
            "pageSize": "50",
            "pageNumber": "1",
            "reportName": "RPTA_APP_ACCUMDETAILS",
            "columns": "ALL",
            "source": "WEB",
            "client": "WEB",
            "filter": f'(SECURITY_CODE="{code}")',
        }
        r = requests.get(
            PLEDGE_URL,
            params=params,
            headers={**_HEADERS, "Referer": "https://data.eastmoney.com/gpzy/pledgeDetail.aspx"},
            timeout=10,
        )
        if not r.ok:
            return None
        rows = ((r.json() or {}).get("result") or {}).get("data") or []
        cutoff = date.today() - timedelta(days=LOOKBACK_DAYS)
        best: float | None = None
        for row in rows:
            nd = _parse_notice_date(row.get("NOTICE_DATE"))
            if nd:
                try:
                    if datetime.strptime(nd, "%Y-%m-%d").date() < cutoff:
                        continue
                except ValueError:
                    pass
            raw = row.get("PF_HOLD_RATIO")
            if raw is None:
                continue
            try:
                v = float(raw)
            except (TypeError, ValueError):
                continue
            if v > 1.5:
                v = v / 100.0
            if v <= 0:
                continue
            best = v if best is None else max(best, v)
        return best
    except Exception as e:
        logger.debug("pledge ratio fetch failed for %s: %s", symbol, e)
        return None


def detect_major_risk_events(symbol: str) -> dict[str, Any]:
    """
    返回重大风险事件扫描结果。
    fatal=True 时引擎应顶部红灯并强制评级 E。
    """
    sym = symbol
    try:
        sym = normalize_symbol(symbol)
    except SymbolError:
        pass

    now = time.time()
    cached = _cache.get(sym.upper())
    if cached and now - cached[0] < CACHE_TTL_SEC:
        return dict(cached[1])

    notices = fetch_stock_notices(sym)
    hits = scan_notice_titles(notices)

    events = [
        {
            "rule_id": h.rule_id,
            "label": h.label,
            "keyword": h.keyword,
            "title": h.title,
            "notice_date": h.notice_date,
            "url": h.url,
            "source": "notice",
        }
        for h in hits
    ]

    pledge_ratio = fetch_controller_pledge_ratio(sym)
    if pledge_ratio is not None and pledge_ratio >= PLEDGE_FATAL_RATIO:
        events.append(
            {
                "rule_id": "pledge_crisis",
                "label": "控股股东高比例质押",
                "keyword": f"质押率{pledge_ratio:.1%}",
                "title": f"控股股东/重要股东累计质押占所持股份约 {pledge_ratio:.1%}（≥{PLEDGE_FATAL_RATIO:.0%}）",
                "notice_date": "",
                "url": "",
                "source": "pledge",
            }
        )

    # 每个规则只保留最新一条公告，避免红灯列表刷屏
    deduped: list[dict[str, Any]] = []
    seen_rules: set[str] = set()
    for ev in events:
        rid = str(ev.get("rule_id") or "")
        if rid in seen_rules:
            continue
        seen_rules.add(rid)
        deduped.append(ev)
    events = deduped

    audit_opinion = None
    for ev in events:
        if ev.get("rule_id") == "audit_nonstd":
            audit_opinion = str(ev.get("title") or "")[:80]
            break

    result = {
        "fatal": bool(events),
        "events": events,
        "event_count": len(events),
        "notice_scanned": len(notices),
        "pledge_ratio": pledge_ratio,
        "message": COMPLIANCE_VETO_MESSAGE if events else "",
        "audit_opinion_hint": audit_opinion,
        "labels": sorted({str(e["label"]) for e in events}),
    }
    _cache[sym.upper()] = (now, result)
    return dict(result)


def clear_major_risk_cache() -> None:
    _cache.clear()
