"""重大风险事件：公告关键词 + 质押率；区分生存级 / 观察级。"""

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
# 观察级：占所持股份比例达到此阈值才提示（不作一票否决）
PLEDGE_OBSERVE_RATIO = 0.50
# 超过 100% 视为抓取异常，丢弃并标记需复核
PLEDGE_MAX_SANE = 1.0

COMPLIANCE_VETO_MESSAGE = "命中生存级重大风险事件，财务打分不适用；请优先关注合规与生存风险"
OBSERVE_RISK_MESSAGE = "命中观察级风险，警惕情绪杀跌；不等于公司生存危机"

_HEADERS = {
    "User-Agent": "Mozilla/5.0",
    "Referer": "https://data.eastmoney.com/notices/",
}

# severity: survival=一票否决；observe=扣分提示、不熔断
RISK_RULES: list[dict[str, Any]] = [
    {
        "id": "pre_reorg",
        "label": "法院预重整/破产重整",
        "severity": "survival",
        "keywords": ("预重整", "破产重整", "重整申请", "被法院裁定受理重整", "进入重整程序"),
    },
    {
        "id": "audit_nonstd",
        "label": "非标审计/持续经营不确定性",
        "severity": "survival",
        "keywords": (
            "无法表示意见",
            "保留意见",
            "否定意见",
            "持续经营重大不确定性",
            "带有强调事项段",
            "带强调事项段",
            "非标准无保留",
            "非标审计",
            "非标准审计",
            "非标准审计意见",
            "非标准审计报告",
        ),
    },
    {
        "id": "investigation",
        "label": "立案调查",
        "severity": "survival",
        "keywords": ("立案调查", "证监会立案", "被中国证监会立案", "被立案告知"),
    },
    {
        "id": "face_delist",
        "label": "面值/重大违法退市风险",
        "severity": "survival",
        "keywords": (
            "面值退市",
            "可能被终止上市",
            "终止上市风险警示",
            "重大违法强制退市",
        ),
    },
    {
        "id": "share_freeze",
        "label": "股份司法冻结",
        "severity": "observe",
        "keywords": ("司法冻结", "股权冻结", "股份冻结", "轮候冻结", "冻结股份", "司法轮候冻结"),
    },
    {
        "id": "pledge_crisis",
        "label": "质押平仓风险",
        "severity": "observe",
        "keywords": ("质押平仓", "强制平仓", "质押违约", "触及平仓线"),
    },
    {
        "id": "policy_geo",
        "label": "政策/地缘风险传闻",
        "severity": "observe",
        "keywords": (
            "被列入实体清单",
            "实体清单",
            "出口管制",
            "贸易制裁",
            "FCC",
            "禁止销售",
            "关税制裁",
        ),
    },
    {
        "id": "macro_policy",
        "label": "宏观与消费政策风险",
        "severity": "observe",
        "keywords": (
            "消费税",
            "消费税改革",
            "禁酒令",
            "公务接待",
            "中央八项规定",
            "限制酒类消费",
            "高端消费限制",
        ),
    },
    {
        "id": "reduce_hold",
        "label": "实控人/大股东减持",
        "severity": "observe",
        "keywords": ("减持计划", "股份减持", "拟减持", "大宗交易减持"),
    },
]

# 风险释放关键词：减持完毕、承诺不减持等 → 观察级风险可释放
RISK_RELEASE_KEYWORDS = (
    "减持完毕", "减持完成", "减持计划实施完毕", "减持计划届满",
    "承诺不减持", "自愿承诺锁定", "延长锁定期", "承诺延长",
)

SEVERITY_BY_ID = {str(r["id"]): str(r["severity"]) for r in RISK_RULES}

_cache: dict[str, tuple[float, dict[str, Any]]] = {}


@dataclass
class RiskHit:
    rule_id: str
    label: str
    keyword: str
    title: str
    notice_date: str = ""
    url: str = ""
    severity: str = "observe"


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


def normalize_pledge_hold_ratio(raw: Any) -> float | None:
    """
    东财 PF_HOLD_RATIO 为「占所持股份比例」的百分数（如 3.45 表示 3.45%）。
    统一转为 0~1 小数；>100% 视为脏数据返回 None。
    """
    if raw is None:
        return None
    try:
        v = float(raw)
    except (TypeError, ValueError):
        return None
    if v < 0:
        return None
    # API 恒为百分数；切勿把 1.5（1.5%）当成小数 150%
    ratio = v / 100.0
    if ratio > PLEDGE_MAX_SANE + 1e-9:
        return None
    if ratio <= 0:
        return None
    return ratio


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
                            severity=str(rule.get("severity") or "observe"),
                        )
                    )
                    break
    return hits


def scan_risk_release(notices: list[dict[str, str]]) -> dict[str, Any]:
    """扫描风险释放信号：减持完毕、承诺不减持等。

    返回 {released: bool, release_type: str, release_date: str, lock_commitment: bool}
    """
    released = False
    release_type = ""
    release_date = ""
    lock_commitment = False

    for n in notices:
        title = n.get("title") or ""
        for kw in RISK_RELEASE_KEYWORDS:
            if kw in title:
                released = True
                release_date = n.get("notice_date") or ""
                if "承诺" in title or "锁定" in title:
                    lock_commitment = True
                    release_type = "commitment"
                elif not release_type:
                    release_type = "completed"
                break
        if released and lock_commitment:
            break  # 已找到最强信号

    return {
        "released": released,
        "release_type": release_type,
        "release_date": release_date,
        "lock_commitment": lock_commitment,
    }


def fetch_controller_pledge_ratio(symbol: str) -> dict[str, Any]:
    """
    东财重要股东质押明细：取近窗内控股股东（优先）单笔「占所持股份比例」的合理最大值。
    返回 {ratio, invalid, source, holder}；ratio 为 0~1 或 None。
    """
    empty = {"ratio": None, "invalid": False, "source": "", "holder": ""}
    code = _code6(symbol)
    if not code:
        return empty
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
            return empty
        rows = ((r.json() or {}).get("result") or {}).get("data") or []
        cutoff = date.today() - timedelta(days=LOOKBACK_DAYS)

        ctrl_latest: float | None = None
        any_best: float | None = None
        ctrl_holder = ""
        any_holder = ""
        saw_invalid = False

        for row in rows:
            nd = _parse_notice_date(row.get("NOTICE_DATE"))
            if nd:
                try:
                    if datetime.strptime(nd, "%Y-%m-%d").date() < cutoff:
                        continue
                except ValueError:
                    pass

            raw = row.get("PF_HOLD_RATIO")
            pf_num = row.get("PF_NUM")
            hold_num = row.get("HOLD_NUM")
            ratio: float | None = None
            try:
                # 优先用官方「占所持股份比例」百分数（避免股/万股单位混淆）
                ratio = normalize_pledge_hold_ratio(raw)
                # 可选交叉校验：质押股数 / 持股数；持股数常为「万股」
                if pf_num is not None and hold_num is not None:
                    try:
                        pf = float(pf_num)
                        hold = float(hold_num)
                    except (TypeError, ValueError):
                        pf, hold = 0.0, 0.0
                    if hold > 0 and pf > 0:
                        # HOLD_NUM 过小多半是万股
                        if pf / hold > PLEDGE_MAX_SANE and hold * 10000 >= pf:
                            hold *= 10000.0
                        share_ratio = pf / hold
                        if share_ratio > PLEDGE_MAX_SANE + 1e-9:
                            saw_invalid = True
                        elif ratio is None:
                            ratio = share_ratio
                        elif abs(share_ratio - ratio) > 0.05 and share_ratio <= PLEDGE_MAX_SANE:
                            ratio = share_ratio
                if raw is not None:
                    try:
                        raw_f = float(raw)
                        if raw_f / 100.0 > PLEDGE_MAX_SANE + 1e-9:
                            saw_invalid = True
                    except (TypeError, ValueError):
                        pass
            except (TypeError, ValueError):
                continue
            if ratio is None:
                continue

            holder = str(row.get("HOLDER_NAME") or "")
            is_ctrl = str(row.get("IS_CONTROL_SHAREHOLDER") or "") in ("1", "是", "Y", "true", "True")
            # 列表已按公告日降序：控股股东取最近一笔，避免历史峰值夸大
            if is_ctrl and ctrl_latest is None:
                ctrl_latest = ratio
                ctrl_holder = holder
            if any_best is None or ratio > any_best:
                any_best = ratio
                any_holder = holder

        if ctrl_latest is not None:
            return {
                "ratio": ctrl_latest,
                "invalid": False,
                "source": "control_pf_hold_ratio",
                "holder": ctrl_holder,
            }
        if any_best is not None:
            return {
                "ratio": any_best,
                "invalid": False,
                "source": "max_pf_hold_ratio",
                "holder": any_holder,
            }
        return {
            "ratio": None,
            "invalid": saw_invalid,
            "source": "invalid_over_100pct" if saw_invalid else "",
            "holder": "",
        }
    except Exception as e:
        logger.debug("pledge ratio fetch failed for %s: %s", symbol, e)
        return empty


def detect_major_risk_events(symbol: str) -> dict[str, Any]:
    """
    返回重大风险事件扫描结果。
    fatal=True 仅当命中生存级事件；观察级只进 observe_events。
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
    # 风险释放检测：减持完毕、承诺不减持等
    risk_release = scan_risk_release(notices)

    events: list[dict[str, Any]] = [
        {
            "rule_id": h.rule_id,
            "label": h.label,
            "keyword": h.keyword,
            "title": h.title,
            "notice_date": h.notice_date,
            "url": h.url,
            "source": "notice",
            "severity": h.severity,
        }
        for h in hits
    ]

    pledge_info = fetch_controller_pledge_ratio(sym)
    pledge_ratio = pledge_info.get("ratio")
    pledge_invalid = bool(pledge_info.get("invalid"))
    if pledge_ratio is not None and pledge_ratio >= PLEDGE_OBSERVE_RATIO:
        holder = pledge_info.get("holder") or "控股/重要股东"
        events.append(
            {
                "rule_id": "pledge_high",
                "label": "大股东高比例质押（观察）",
                "keyword": f"质押率{pledge_ratio:.1%}",
                "title": (
                    f"{holder}单笔质押占所持股份约 {pledge_ratio:.1%}"
                    f"（≥{PLEDGE_OBSERVE_RATIO:.0%}，属个人财务安排观察项，非公司生存危机）"
                ),
                "notice_date": "",
                "url": "",
                "source": "pledge",
                "severity": "observe",
            }
        )
    elif pledge_invalid:
        events.append(
            {
                "rule_id": "pledge_data_error",
                "label": "质押率数据异常（需复核）",
                "keyword": ">100%",
                "title": "抓取到质押率超过100%，疑似字段误用，已忽略该数值、不触发熔断",
                "notice_date": "",
                "url": "",
                "source": "pledge",
                "severity": "observe",
            }
        )

    # 每规则保留最新一条
    deduped: list[dict[str, Any]] = []
    seen_rules: set[str] = set()
    for ev in events:
        rid = str(ev.get("rule_id") or "")
        if rid in seen_rules:
            continue
        seen_rules.add(rid)
        deduped.append(ev)
    events = deduped

    survival = [e for e in events if e.get("severity") == "survival"]
    observe = [e for e in events if e.get("severity") != "survival"]

    audit_opinion = None
    for ev in survival:
        if ev.get("rule_id") == "audit_nonstd":
            audit_opinion = str(ev.get("title") or "")[:80]
            break

    fatal = bool(survival)
    # 观察级风险释放：减持完毕+承诺不减持时，减持类观察事件可释放
    if risk_release.get("released"):
        released_ids = {"reduce_hold"}
        for ev in observe:
            if ev.get("rule_id") in released_ids:
                ev["released"] = True
                ev["release_type"] = risk_release.get("release_type", "")
    # 过滤已释放的事件，不计入观察级扣分
    observe_active = [e for e in observe if not e.get("released")]
    observe_released = [e for e in observe if e.get("released")]

    result = {
        "fatal": fatal,
        "events": survival,  # 红灯只列生存级
        "observe_events": observe_active,  # 未释放的观察级
        "observe_events_released": observe_released,  # 已释放的观察级
        "risk_released": risk_release.get("released", False),
        "risk_release_type": risk_release.get("release_type", ""),
        "risk_release_date": risk_release.get("release_date", ""),
        "risk_lock_commitment": risk_release.get("lock_commitment", False),
        "event_count": len(survival),
        "observe_count": len(observe_active),  # 只计未释放的
        "notice_scanned": len(notices),
        "pledge_ratio": pledge_ratio,
        "pledge_invalid": pledge_invalid,
        "pledge_holder": pledge_info.get("holder") or "",
        "message": COMPLIANCE_VETO_MESSAGE if fatal else "",
        "observe_message": OBSERVE_RISK_MESSAGE if observe_active and not fatal else "",
        "audit_opinion_hint": audit_opinion,
        "labels": sorted({str(e["label"]) for e in survival}),
        "observe_labels": sorted({str(e["label"]) for e in observe_active}),
    }
    _cache[sym.upper()] = (now, result)
    return dict(result)


def clear_major_risk_cache() -> None:
    _cache.clear()
