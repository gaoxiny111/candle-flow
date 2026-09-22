"""重大风险事件：公告关键词 + 质押率；区分生存级 / 观察级。"""

from __future__ import annotations

import logging
import re
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
        # 说明：中文标题的语序多变，**连续子串**匹配会大面积漏判。实测 15 种
        # 真实写法中，下面这 4 个严格关键词只命中 5 种（漏 67%），例如
        # 「关于控股股东一致行动人减持股份计划公告」（立霸股份 2026-08-25）
        # 含「减持股份计划」但既不含「减持计划」（非连续）也不含「股份减持」
        # （顺序相反）→ 零命中。故改用 all_of 语义共现（见 match 逻辑）。
        "keywords": ("减持计划", "股份减持", "拟减持", "大宗交易减持"),
        "all_of": (("减持",), ("计划", "预披露", "进展", "减持股份")),
    },
]

# 说明：``all_of`` 是「组间必须各命中 ≥1」的语义共现判据，用来替代脆弱的
# 连续子串匹配。仅用于语序多变的标题族（如减持）。命中任一 ``keywords``
# **或** 满足全部 ``all_of`` 组即算命中（OR），避免收紧既有召回。

# 风险释放关键词：减持完毕、承诺不减持等 → 观察级风险可释放
RISK_RELEASE_KEYWORDS = (
    "减持完毕", "减持完成", "减持计划实施完毕", "减持计划届满",
    "承诺不减持", "自愿承诺锁定", "延长锁定期", "承诺延长",
)

# 组合释放规则：标题同时含「减持」与「届满/到期」即视为减持计划已结束。
# 枚举式关键词穷举不了标题写法——同一语义在实务中至少有「减持计划届满」
# 「减持计划期限届满」「减持期限届满」「减持计划时间届满」等多种表述。
# 曾因此漏判格力电器 2026-06-22「关于大股东减持计划期限届满暨减持结果的
# 公告」：计划已到期结束，却仍按新增观察级风险扣 8 分。注意组合条件必须
# 同时满足，避免「限售期届满」这类不含减持语义的标题被误释放。
_RELEASE_SUBJECT_HINTS = ("减持",)
_RELEASE_END_HINTS = ("届满", "到期")

# ── 生存级风险的解除通道 ────────────────────────────────────────────────
# 生存级（一票否决）事件过去没有释放路径，只能被越叠越重：一旦命中过
# 「可能被终止上市」「无法表示意见」，即使后续风险已正式消除也永远锁死。
# 新潮能源（600777）即典型：2026-04-24 披露《关于2024年度内部控制和财务
# 报表审计报告无法表示意见涉及事项影响已消除的专项说明》并申请撤销退市
# 风险警示，2026-06-19 收到《关于撤销退市风险警示及其他风险警示暨停牌的
# 公告》，证券简称由「*ST新潮」恢复为「新潮能源」；但系统仍按生存级事件
# 强制 E 级、估值锁 28 分，与事实相反。
#
# 口径（严格，宁可不释放）：
#   ① 标题必须同时含「主语线索」与「解除线索」；
#   ② face_delist 排除「申请撤销」阶段（那是申请，不是交易所决定）；
#   ③ 解除公告日必须不早于风险公告日（同规则内，迟到的风险公告会重新激活）；
#   ④ face_delist 额外要求当前证券简称不含 ST（见 current_name_hint）。
SURVIVAL_RELEASE_RULES: tuple[tuple[str, tuple[str, ...], tuple[str, ...]], ...] = (
    # (rule_id, 主语线索, 解除线索)
    ("face_delist", ("退市风险警示", "其他风险警示"), ("撤销", "取消", "解除")),
    (
        "audit_nonstd",
        ("审计", "无法表示意见", "非标"),
        ("影响已消除", "已消除", "整改完成", "已整改完毕", "出具标准无保留"),
    ),
    ("investigation", ("立案调查", "立案告知"), ("结案", "不予处罚", "终止调查", "撤销立案")),
    ("pre_reorg", ("重整",), ("重整计划执行完毕", "重整完成", "终结重整程序")),
)
# 仅处于「申请」阶段的公告不构成解除
_RELEASE_APPLICATION_HINTS = ("申请撤销", "拟申请撤销", "申请解除")


def _survival_release_kind(title: str) -> str | None:
    """生存级解除公告 → 返回对应 rule_id；否则 None。"""
    if any(h in title for h in _RELEASE_APPLICATION_HINTS):
        return None
    for rule_id, subjects, releases in SURVIVAL_RELEASE_RULES:
        if any(s in title for s in subjects) and any(r in title for r in releases):
            return rule_id
    return None


def scan_survival_release(notices: list[dict[str, str]]) -> dict[str, dict[str, str]]:
    """扫描生存级风险的解除公告（同规则取最新一条）。

    返回 {rule_id: {"date", "title"}}；日期比较在 detect_major_risk_events 内完成。
    """
    out: dict[str, dict[str, str]] = {}
    for n in notices:
        title = n.get("title") or ""
        rule_id = _survival_release_kind(title)
        if not rule_id:
            continue
        d = n.get("notice_date") or ""
        prev = out.get(rule_id)
        if prev and prev.get("date", "") >= d:
            continue
        out[rule_id] = {"date": d, "title": title}
    return out


def current_name_hint(notices: list[dict[str, str]]) -> str:
    """从公告标题前缀推断当前证券简称（东财标题格式「简称:正文」）。

    公告前缀反映该公告披露时的证券简称，因此最新一条的前缀即当前简称——
    这是判断 ST/*ST 是否仍存续的事实性依据（比关键词推断更可靠）。
    """
    for n in notices:
        title = (n.get("title") or "").strip()
        for sep in (":", "："):
            if sep in title:
                prefix = title.split(sep, 1)[0].strip()
                # 过滤「关于XXX」这类非简称前缀
                if prefix and not prefix.startswith("关于") and len(prefix) <= 12:
                    return prefix
                break
    return ""


def _is_release_title(title: str) -> bool:
    """标题是否构成风险释放信号（减持完毕/计划届满、承诺不减持等）。"""
    if any(kw in title for kw in RISK_RELEASE_KEYWORDS):
        return True
    return any(s in title for s in _RELEASE_SUBJECT_HINTS) and any(
        e in title for e in _RELEASE_END_HINTS
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


def _match_rule(title: str, rule: dict[str, Any]) -> str | None:
    """返回命中的关键词（用于留痕），未命中返回 None。

    两级判据（OR）：
    ① ``keywords`` —— 连续子串，精确；
    ② ``all_of``  —— 语义共现，每组须至少命中一个词。

    ② 用于语序多变的标题族。中文标题里「减持股份计划」「减持公司股份计划」
    这类写法，用连续子串必然漏判（实测 15 种写法漏 10 种）。
    """
    for kw in rule.get("keywords") or ():
        if kw in title:
            return str(kw)
    groups = rule.get("all_of")
    if groups:
        matched: list[str] = []
        for group in groups:
            got = next((w for w in group if w in title), None)
            if got is None:
                break
            matched.append(got)
        else:
            return "+".join(matched)
    return None


# 减持类标题中「计划尚在进行」与「计划已结束」的语义区分。
# 「减持计划届满暨减持结果」是**结束**信号，不该被当成新增减持风险。
_REDUCE_END_HINTS = ("届满", "到期", "实施完毕", "期限届满")


def scan_notice_titles(notices: list[dict[str, str]]) -> list[RiskHit]:
    """对公告标题做规则扫描（轻量 NLP：关键词 + 语义共现）。"""
    hits: list[RiskHit] = []
    seen: set[tuple[str, str]] = set()
    for n in notices:
        title = n.get("title") or ""
        for rule in RISK_RULES:
            kw = _match_rule(title, rule)
            if kw is None:
                continue
            # 减持类：标题语义为「计划已结束」时不记为新增风险，
            # 交给 scan_risk_release 走释放通道（避免自相矛盾）。
            if rule["id"] == "reduce_hold" and any(
                h in title for h in _REDUCE_END_HINTS
            ):
                continue
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
    另带 ``notice``：触发释放的原始公告（供调用方留痕）。
    """
    released = False
    release_type = ""
    release_date = ""
    lock_commitment = False
    release_notice: dict[str, str] | None = None

    for n in notices:
        title = n.get("title") or ""
        if not _is_release_title(title):
            continue
        if not released or (("承诺" in title or "锁定" in title) and not lock_commitment):
            release_notice = n
        released = True
        release_date = n.get("notice_date") or ""
        if "承诺" in title or "锁定" in title:
            lock_commitment = True
            release_type = "commitment"
        elif not release_type:
            release_type = "completed"
        if released and lock_commitment:
            break  # 已找到最强信号

    return {
        "released": released,
        "release_type": release_type,
        "release_date": release_date,
        "lock_commitment": lock_commitment,
        "notice": release_notice or {},
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
    # 生存级风险的解除检测（撤销退市风险警示、审计非标影响已消除等）
    survival_release = scan_survival_release(notices)
    name_hint = current_name_hint(notices)
    name_has_st = "ST" in name_hint.upper()

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

    # 每规则保留最新一条。
    # **必须先按日期降序排序**：上游接口虽然通常 newest-first，但依赖输入顺序
    # 会让「保留最新」在乱序输入下退化成「保留第一条」，进而使后文的
    # 「释放公告只释放更早事件」判据失效（新建的风险被旧释放记录抵消）。
    events.sort(key=lambda e: str(e.get("notice_date") or ""), reverse=True)
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

    # 生存级风险的解除判定：解除公告必须不早于风险公告；face_delist 还需
    # 当前简称已去掉 ST（避免"撤销申请已提交但尚未获准"被当成解除）。
    active_survival: list[dict[str, Any]] = []
    released_survival: list[dict[str, Any]] = []
    for ev in survival:
        rid = str(ev.get("rule_id") or "")
        rel = survival_release.get(rid)
        ev_date = str(ev.get("notice_date") or "")
        releasable = (
            rel is not None
            and bool(rel.get("date"))
            and bool(ev_date)
            and rel["date"] >= ev_date
            and not (rid == "face_delist" and name_has_st)
        )
        if releasable:
            released_survival.append(
                {
                    **ev,
                    "released": True,
                    "release_date": rel["date"],
                    "release_title": rel["title"],
                }
            )
        else:
            active_survival.append(ev)
    survival = active_survival

    audit_opinion = None
    for ev in survival:
        if ev.get("rule_id") == "audit_nonstd":
            audit_opinion = str(ev.get("title") or "")[:80]
            break

    fatal = bool(survival)
    # 观察级风险释放：减持完毕+承诺不减持时，减持类观察事件可释放。
    # **必须带日期比较**：释放公告只能释放早于它的减持事件。否则一个
    # 2026-02 的「减持计划届满」会把 2026-08 的**新增**减持计划一并洗白
    # （立霸股份 2026-08-25 新减持公告就处在 2026-01 解除质押的时间窗内）。
    if risk_release.get("released"):
        released_ids = {"reduce_hold"}
        _rel_date = str(risk_release.get("release_date") or "")
        _matched_any = False
        for ev in observe:
            if ev.get("rule_id") not in released_ids:
                continue
            _ev_date = str(ev.get("notice_date") or "")
            if _rel_date and _ev_date and _ev_date > _rel_date:
                continue  # 事件晚于释放公告 → 是新增风险，不释放
            ev["released"] = True
            ev["release_type"] = risk_release.get("release_type", "")
            _matched_any = True
        # 若释放公告本身即「减持计划届满」类（不构成新增风险、故未进 observe），
        # 仍补一条已释放记录留痕，保证审计链完整、可追溯到具体公告。
        if not _matched_any:
            _rn = risk_release.get("notice") or {}
            observe.append(
                {
                    "rule_id": "reduce_hold",
                    "label": "实控人/大股东减持",
                    "title": _rn.get("title") or "",
                    "notice_date": _rel_date,
                    "url": _rn.get("url") or "",
                    "severity": "observe",
                    "released": True,
                    "release_type": risk_release.get("release_type", ""),
                }
            )
    # 过滤已释放的事件，不计入观察级扣分
    observe_active = [e for e in observe if not e.get("released")]
    observe_released = [e for e in observe if e.get("released")]

    # ── 减持窗口期倒计时 + 大宗交易折价率（仅未释放的减持事件）──────────
    # 数据来源：减持公告正文解析窗口期；东财 RPT_DATA_BLOCKTRADE 取折价率。
    # 二者都只**报告事实**，评分交给读层（market_scan 封顶信号档位）与
    # 风险模块，避免在事件层再造一套扣分逻辑（铁律 10）。
    reduce_window: dict[str, Any] | None = None
    reduce_notice = next(
        (e for e in observe_active if e.get("rule_id") == "reduce_hold"), None
    )
    if reduce_notice:
        # art_code 形如 AN202608241828367755，位于 URL 末段。
        # 不能用 rsplit("/detail/")[-1].split(".")[0]：那会先取到路径里的
        # 代码段（603519），因为 URL 形如 /notices/detail/603519/ANxxx.html。
        _art = ""
        _m = re.search(r"/(AN[0-9A-Za-z]+)\.html", str(reduce_notice.get("url") or ""))
        if _m:
            _art = _m.group(1)
        if not _art:
            # 回退：标题里常含公告编号，但更可靠的是直接从 notices 原始项取
            _art = str(reduce_notice.get("art_code") or "")
        if _art:
            try:
                reduce_window = fetch_reduce_window(
                    _art, str(reduce_notice.get("notice_date") or "")
                )
            except Exception as e:
                logger.warning("reduce window parse failed for %s: %s", sym, e)
        if reduce_window:
            reduce_notice["window_start"] = reduce_window["start"]
            reduce_notice["window_end"] = reduce_window["end"]
            reduce_notice["in_window"] = reduce_window["in_window"]
            reduce_notice["remaining_days"] = reduce_window["remaining_days"]

    block_trades: list[dict[str, Any]] = []
    try:
        block_trades = fetch_recent_block_trades(sym)
    except Exception as e:
        logger.warning("block trade fetch failed for %s: %s", sym, e)
    # 最近一笔（含折价率）挂到结果顶层，供前端与读层直接消费
    latest_block = block_trades[0] if block_trades else None

    result = {
        "fatal": fatal,
        "events": survival,  # 红灯只列存活（未解除）的生存级事件
        "events_released": released_survival,  # 已解除的生存级事件（留痕，不否决）
        "survival_released_count": len(released_survival),
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
        "released_labels": sorted({str(e["label"]) for e in released_survival}),
        "current_name_hint": name_hint,
        "name_has_st": name_has_st,
        # ── 减持窗口期 / 大宗交易（新增，只报事实不评分）──
        "reduce_window": reduce_window,
        "in_reduce_window": bool(reduce_window and reduce_window.get("in_window")),
        "reduce_remaining_days": (
            int(reduce_window["remaining_days"]) if reduce_window else None
        ),
        "block_trades": block_trades,
        "latest_block_trade": latest_block,
    }
    _cache[sym.upper()] = (now, result)
    return dict(result)


def clear_major_risk_cache() -> None:
    _cache.clear()


# ── 减持窗口期倒计时 ────────────────────────────────────────────────────
# 减持计划公告的**正文**里含明确的起止日期（窗口期），标题里没有。
# 实测（立霸股份 603519，公告号 2026-039）正文：「自本公告披露之日起 15 个
# 交易日后的 3 个月内…减持期间为 2026 年 9 月 16 日至 2026 年 12 月 15 日」。
# 窗口期对短期股价的压制是**时间性**的：窗口开启期间抛压持续存在，
# 窗口结束后该压制消失。而既有的减持事件检测只记「有一条减持公告」，
# 无法表达「还要压制多少天」，故这里补上窗口期解析。
WINDOW_RE = re.compile(
    r"(20\d{2})\s*年\s*(\d{1,2})\s*月\s*(\d{1,2})\s*日"
    r"[^。；;0-9]{0,20}?"
    r"(20\d{2})\s*年\s*(\d{1,2})\s*月\s*(\d{1,2})\s*日"
)
# 分隔符实测有「至 / 到 / — / - / ～ / ~ / 至次」等多种写法：立霸 603519
# 公告正文用的是全角波浪号「2026 年 9 月 16 日～2026 年 12 月 15 日」，
# 只列「至/-」会漏判。故不枚举分隔符，而是用 `[^。；;0-9]{0,20}?` 宽松匹配
# 两段日期之间的非数字间隔（禁止跨句号/分号，禁止吞掉数字），
# 既能覆盖全部写法，又不会把「两个不相干的日期」误配成区间。


def parse_reduce_window(text: str) -> dict[str, str] | None:
    """
    从减持公告正文解析「减持期间」起止日期。
    返回 {"start": "YYYY-MM-DD", "end": "YYYY-MM-DD"}；解析不到返回 None。
    """
    if not text:
        return None
    m = WINDOW_RE.search(text)
    if not m:
        return None
    y1, mo1, d1, y2, mo2, d2 = (int(x) for x in m.groups())
    try:
        start = date(y1, mo1, d1)
        end = date(y2, mo2, d2)
    except ValueError:
        return None
    if end < start:
        return None
    return {"start": start.isoformat(), "end": end.isoformat()}


def fetch_notice_content(art_code: str) -> str:
    """抓取公告正文（纯文本）。失败返回空串。"""
    if not art_code:
        return ""
    try:
        r = requests.get(
            "https://np-cnotice-stock.eastmoney.com/api/content/ann",
            params={"art_code": art_code, "client_source": "web", "page_index": "1"},
            headers=_HEADERS,
            timeout=15,
        )
        if not r.ok:
            return ""
        data = (r.json() or {}).get("data") or {}
        return str(data.get("notice_content") or "")
    except Exception:
        return ""


def fetch_reduce_window(art_code: str, notice_date: str = "") -> dict[str, Any] | None:
    """
    解析单条减持公告的窗口期，并算出「剩余天数 / 是否在窗口内」。

    倒计时以**当日**（date.today）为基准：remaining_days 为距窗口结束的自然日数，
    窗口已过则为 0 且 in_window=False。只报事实，不在此处做任何评分。
    """
    text = fetch_notice_content(art_code)
    win = parse_reduce_window(text)
    if not win:
        return None
    try:
        end = date.fromisoformat(win["end"])
        start = date.fromisoformat(win["start"])
    except ValueError:
        return None
    today = date.today()
    in_window = start <= today <= end
    remaining = (end - today).days if in_window else 0
    return {
        "start": win["start"],
        "end": win["end"],
        "in_window": in_window,
        "remaining_days": max(0, remaining),
        "notice_date": notice_date or "",
    }


# ── 大宗交易折价率 ──────────────────────────────────────────────────────
# 东财 RPT_DATA_BLOCKTRADE：DISCOUNT_RATIO / PREMIUM_RATIO 已由接口算好。
# 实测（立霸 603519 2026-09-16）：DEAL_PRICE 13.47、PREMIUM_RATIO -0.0917
# （即折价 9.17%）。折价率反映承接方要求的风险补偿，折价越深短期抛压信号越强。
BLOCKTRADE_URL = "https://datacenter-web.eastmoney.com/api/data/v1/get"
BLOCKTRADE_LOOKBACK_DAYS = 30


def fetch_recent_block_trades(symbol: str, *, lookback_days: int = BLOCKTRADE_LOOKBACK_DAYS) -> list[dict[str, Any]]:
    """
    近 N 日大宗交易记录。
    返回 [{trade_date, deal_price, premium_ratio, discount_pct, deal_amt, volume, buyer, seller}]
    按日期降序；无数据返回 []。
    """
    code = _code6(symbol)
    if not code:
        return []
    cutoff = date.today() - timedelta(days=lookback_days)
    try:
        r = requests.get(
            BLOCKTRADE_URL,
            params={
                "reportName": "RPT_DATA_BLOCKTRADE",
                "columns": (
                    "SECURITY_CODE,TRADE_DATE,DEAL_PRICE,PREMIUM_RATIO,"
                    "DEAL_VOLUME,DEAL_AMT,BUYER_NAME,SELLER_NAME"
                ),
                "filter": f'(SECURITY_CODE="{code}")',
                "pageNumber": "1",
                "pageSize": "30",
                "sortColumns": "TRADE_DATE",
                "sortTypes": "-1",
                "source": "WEB",
                "client": "WEB",
            },
            headers=_HEADERS,
            timeout=15,
        )
        if not r.ok:
            return []
        rows = ((r.json() or {}).get("result") or {}).get("data") or []
    except Exception:
        return []

    out: list[dict[str, Any]] = []
    for row in rows:
        td = _parse_notice_date(row.get("TRADE_DATE"))
        if not td:
            continue
        try:
            d = date.fromisoformat(td)
        except ValueError:
            continue
        if d < cutoff:
            continue
        prem = row.get("PREMIUM_RATIO")
        out.append(
            {
                "trade_date": td,
                "deal_price": float(row["DEAL_PRICE"]) if row.get("DEAL_PRICE") is not None else None,
                # premium_ratio：接口原值（负=折价）。discount_pct 为正的折价幅度。
                "premium_ratio": float(prem) if prem is not None else None,
                "discount_pct": round(-float(prem) * 100, 2) if prem is not None else None,
                "deal_amt": float(row["DEAL_AMT"]) if row.get("DEAL_AMT") is not None else None,
                "volume": float(row["DEAL_VOLUME"]) if row.get("DEAL_VOLUME") is not None else None,
                "buyer": row.get("BUYER_NAME") or "",
                "seller": row.get("SELLER_NAME") or "",
            }
        )
    return out

