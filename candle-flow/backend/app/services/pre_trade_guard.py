"""前置排雷层（一票否决）。

设计原则（与全站口径铁律一致）：

1. **排雷的是「不可救药的生存级缺陷」，不是「估值贵」**。
   估值贵有专门的处置通道（估值模块打分 + 分类准入闸门封顶档位），
   两者强度不同：闸门只封顶「观察」，排雷是直接出局。把「PE 高」放进排雷层
   会与闸门重复计数（铁律 3），且实测 `PE>80` 全库命中 20.4%、与
   `PE>行业均值×2` 并集中达 30.4% —— 砍掉三分之一市场，不是排雷是清场。

2. **每条判据必须能拿到真实数据**（铁律 13）。
   本模块只启用有数据源的条件；缺数据的条件显式记为 unavailable 并留痕，
   不静默返回 False（静默返回 False 会把「没查」伪装成「查过且没问题」）。

3. **保留区分度，不做单一指标一票否决**（铁律 3）。
   资金链一条必须「高杠杆 AND 现金流失血」双条件；单看负债率会误杀
   银行/券商（常熟银行 91.7%、申万宏源 82.9%，负债率是业务模型不是风险）。

4. **金融业豁免是结构性的**（与 market_scan.ADMISSION_FINANCIAL_INDUSTRY_KW 同源）。
   银行赚息差、券商赚佣金，经营现金流天然为负（放贷即现金流出），
   负债率天然 90%+。用制造业尺子量金融会拦掉整个金融板块。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

# 金融业结构性豁免：这些行业的「高负债 + 经营现金流为负」是业务模型而非风险。
# 与 market_scan.ADMISSION_FINANCIAL_INDUSTRY_KW 保持同源（铁律 5：同一判定不两处各算）。
FINANCIAL_INDUSTRY_KW: tuple[str, ...] = (
    "银行",
    "保险",
    "证券",
    "多元金融",
    "信托",
    "期货",
)

# ── 资金链极度紧张：高杠杆 AND 现金流失血 ──
DEBT_RATIO_MAX = 70.0            # 资产负债率上限（%）
OCF_NEGATIVE_YEARS_MIN = 2       # 经营现金流连续为负年数

# ── 减持：观察窗与比例门槛 ──
REDUCE_LOOKBACK_DAYS = 30        # 近 1 个月
REDUCE_PCT_MIN = 0.5             # 减持比例门槛（%）

# ── 估值极端泡沫 ──
# 只在「绝对高 AND 相对高 AND 盈利支撑弱」三者同时成立时触发，
# 避免把「高估值 + 高增长」的真成长股误杀（半导体真成长 PE 80~150 常见）。
PE_ABSOLUTE_EXTREME = 150.0      # 绝对 PE 极端档
PE_PCTL_EXTREME = 90.0           # 历史分位极端档
PE_PREMIUM_EXTREME = 200.0       # 相对行业溢价极端档（%）

# ── 处罚类合规风险（investigation 已覆盖「立案调查」，这里补「处罚/警示」）──
# 注意：处罚的严重度差异极大（警示函 vs 行政处罚），故只做观察级不进排雷，
# 真正的生存级合规风险走 major_risk_events 的 survival 规则。
COMPLIANCE_PENALTY_KW: tuple[str, ...] = (
    "行政处罚",
    "责令改正",
    "监管函",
    "警示函",
    "通报批评",
    "公开谴责",
    "纪律处分",
)

_PCT_RE = re.compile(r"([\d.]+)\s*%")
_SHARES_RE = re.compile(r"(?:不超过|合计不超过)?\s*([\d,，]{4,})\s*股")


@dataclass
class MineResult:
    """排雷结果。

    ``blocked`` 为 True 时该票不应参与后续打分。
    ``unavailable`` 记录因缺数据源而**未能**执行的条件（必须对用户可见，
    不能伪装成「已检查且通过」）。
    """

    blocked: bool = False
    reasons: list[str] = field(default_factory=list)
    labels: list[str] = field(default_factory=list)
    unavailable: list[str] = field(default_factory=list)
    details: dict[str, Any] = field(default_factory=dict)


def _is_financial(industry: str | None) -> bool:
    if not industry:
        return False
    return any(k in industry for k in FINANCIAL_INDUSTRY_KW)


def _parse_reduce_pct(body_text: str) -> float | None:
    """从减持公告正文抽取「占公司总股本 X%」或「不超过总股本的 X%」。

    实测东财正文（``np-cnotice-stock`` 的 ``notice_content``）含：
        「本次拟减持的股份数量：不超过 7,989,834 股」+「占公司总股本的 4.43%」
    优先取「占总股本」语义的百分数；取不到则用股数 ÷ 总股本无法算（无总股本字段）→ None。
    """
    if not body_text:
        return None
    txt = re.sub(r"\s+", " ", re.sub(r"<[^>]+>", "", body_text))
    # ① 「不超过公司股份总数的 X%」（计划上限，最稳）
    m = re.search(r"不超过\s*(?:公司)?(?:股份)?总数?(?:的)?\s*([\d.]+)\s*%", txt)
    if m:
        try:
            return float(m.group(1))
        except ValueError:
            pass
    # ② 「占公司总股本的 X%」
    m = re.search(r"占\s*(?:公司)?(?:总)?股本\s*(?:的)?\s*([\d.]+)\s*%", txt)
    if m:
        try:
            return float(m.group(1))
        except ValueError:
            pass
    return None


def _check_reduce(
    observe_events: list[dict[str, Any]],
    *,
    today: str,
) -> tuple[bool, str | None, dict[str, Any]]:
    """条件1：近 30 日大股东/董监高减持，且比例超门槛。

    比例来源：公告正文（``notice_content``）。正文取不到比例时
    **不视为通过**，而是降级为「未验证」并留痕 —— 静默放行会让用户
    以为查过了。
    """
    from datetime import date, timedelta

    cutoff = (date.fromisoformat(today) - timedelta(days=REDUCE_LOOKBACK_DAYS)).isoformat()
    recent = [
        e for e in observe_events
        if e.get("rule_id") == "reduce_hold"
        and str(e.get("notice_date") or "")[:10] >= cutoff
    ]
    if not recent:
        return False, None, {"recent_count": 0}

    # 有近 1 月减持事件；比例需从正文取，取不到即按「有减持公告」保守记录，
    # 但只有「取到比例且超门槛」才升级为排雷（避免单凭标题一票否决，铁律 3）。
    pcts: list[float] = []
    for e in recent[:3]:
        art = _art_code_from_url(str(e.get("url") or ""))
        body = _fetch_notice_body(art) if art else ""
        pct = _parse_reduce_pct(body)
        if pct is not None:
            pcts.append(pct)
    detail = {
        "recent_count": len(recent),
        "titles": [str(e.get("title") or "") for e in recent[:3]],
        "pcts": pcts,
    }
    if not pcts:
        return False, None, {**detail, "pct_unverified": True}
    top = max(pcts)
    if top > REDUCE_PCT_MIN:
        return (
            True,
            "大股东/董监高减持",
            {**detail, "max_pct": top},
        )
    return False, None, detail


def _art_code_from_url(url: str) -> str | None:
    m = re.search(r"/detail/[^/]+/(AN\d+)\.html", url or "")
    if m:
        return m.group(1)
    m = re.search(r"(AN\d{10,})", url or "")
    return m.group(1) if m else None


def _fetch_notice_body(art_code: str, *, timeout: int = 12) -> str:
    """抓公告正文（东财 np-cnotice-stock）。失败返回空串（调用方按「未验证」处理）。"""
    try:
        import requests
    except Exception:  # pragma: no cover
        return ""
    from app.services.major_risk_events import _HEADERS

    try:
        r = requests.get(
            "https://np-cnotice-stock.eastmoney.com/api/content/ann",
            params={"art_code": art_code, "client_source": "web"},
            headers=_HEADERS,
            timeout=timeout,
        )
        if not r.ok:
            return ""
        return str(((r.json() or {}).get("data") or {}).get("notice_content") or "")
    except Exception:
        return ""


def _check_solvency(
    *,
    debt_ratio: float | None,
    ocf_ps: float | None,
    ocf_negative_years: int | None,
    industry: str | None,
) -> tuple[bool, str | None, dict[str, Any]]:
    """条件3：高杠杆 AND 经营现金流连续为负。

    **必须双条件**（铁律 3）。单看负债率会误杀整个金融业：
    实测常熟银行 91.7%、苏农银行 91.8%、申万宏源 82.9%、
    兴业银行 91.8% —— 银行吸存放贷、券商两融，负债率与「经营现金流为负」
    都是业务模型本身，不是资金链紧张。
    """
    detail: dict[str, Any] = {
        "debt_ratio": debt_ratio,
        "ocf_ps": ocf_ps,
        "ocf_negative_years": ocf_negative_years,
        "financial_exempt": _is_financial(industry),
    }
    if _is_financial(industry):
        return False, None, {**detail, "skip_reason": "金融业结构性豁免"}
    if debt_ratio is None or ocf_negative_years is None:
        return False, None, {**detail, "unavailable": True}
    if float(debt_ratio) > DEBT_RATIO_MAX and int(ocf_negative_years) >= OCF_NEGATIVE_YEARS_MIN:
        return (
            True,
            "资金链紧张",
            {**detail, "trigger": f"负债率{float(debt_ratio):.1f}% 且 经营现金流连续{int(ocf_negative_years)}年为负"},
        )
    return False, None, detail


def _check_valuation_bubble(
    *,
    pe: float | None,
    pe_pctl: float | None,
    pe_premium_pct: float | None,
    profit_yoy: float | None,
    is_strong_cyclical: bool,
) -> tuple[bool, str | None, dict[str, Any]]:
    """条件2：估值极端泡沫。

    **不采用「PE>80」或「PE>行业均值×2」做一票否决**（实测会清场）：
    - `PE>80` 全库命中 20.4%，其中 TOP50 命中的 6 只**全部**是系统判定的成长股；
      半导体真成长（澜起科技 PE80.4/分位50、百济神州分位4）会被一起砍掉。
    - `PE>行业均值×2` 命中 27.8%，与上者并集 **30.4%**；
      且「行业均值极小」时形同虚设（银行 2x=11.08、环境治理 2x=18.48），
      而用户点名的立霸股份 PE 23.16 **低于**行业均值 39.53（溢价 -41.4%），
      这条规则拦不住它自己的靶点。

    改用**三条件同时成立**（绝对贵 AND 历史分位高 AND 相对行业贵或盈利不支撑）：
    这样既拦住金牛化工（PE 193.5/分位 89.8）式双高，又不误伤真成长。
    """
    detail: dict[str, Any] = {
        "pe": pe, "pe_pctl": pe_pctl, "pe_premium_pct": pe_premium_pct,
        "profit_yoy": profit_yoy, "is_strong_cyclical": is_strong_cyclical,
    }
    if pe is None or float(pe) <= PE_ABSOLUTE_EXTREME:
        return False, None, detail
    if is_strong_cyclical:
        # 周期股 PE 在盈利谷底天然巨大，绝对 PE 无意义（估值模块另有周期陷阱判据）
        return False, None, {**detail, "skip_reason": "强周期股 PE 失真"}
    if pe_pctl is None:
        return False, None, {**detail, "unavailable": True}
    if float(pe_pctl) < PE_PCTL_EXTREME:
        return False, None, detail
    relative_expensive = pe_premium_pct is not None and float(pe_premium_pct) >= PE_PREMIUM_EXTREME
    weak_earnings = profit_yoy is None or float(profit_yoy) <= 0
    if relative_expensive or weak_earnings:
        return (
            True,
            "估值极端泡沫",
            {
                **detail,
                "trigger": (
                    f"PE {float(pe):.0f}x 且历史分位 {float(pe_pctl):.0f}%"
                    + (f"、相对行业溢价 {float(pe_premium_pct):.0f}%" if relative_expensive else "")
                    + ("、净利同比未正增长" if weak_earnings else "")
                ),
            },
        )
    return False, None, detail


def evaluate_pre_trade_guard(
    *,
    symbol: str = "",
    name: str = "",
    industry: str | None = None,
    pe: float | None = None,
    pe_pctl: float | None = None,
    pe_premium_pct: float | None = None,
    profit_yoy: float | None = None,
    debt_ratio: float | None = None,
    ocf_ps: float | None = None,
    ocf_negative_years: int | None = None,
    observe_events: list[dict[str, Any]] | None = None,
    fatal: bool = False,
    is_strong_cyclical: bool = False,
    today: str | None = None,
) -> dict[str, Any]:
    """前置排雷总入口。返回 dict（含 ``blocked`` / ``reasons`` / ``unavailable``）。

    参数默认全 None → 除 ST 与已有 survival 事件外不拦任何票（安全默认）。
    """
    from datetime import date

    today = today or date.today().isoformat()
    res = MineResult()
    observe_events = observe_events or []
    res.details["industry"] = industry

    # ── 条件4a：ST/*ST（名称即事实，无需数据源）──
    if "ST" in (name or "").upper():
        res.blocked = True
        res.reasons.append("当前为 ST/*ST 状态")
        res.labels.append("合规风险")

    # ── 条件4b：立案调查 / 退市风险 / 重整（survival 级，已由 major_risk_events 判定）──
    if fatal:
        res.blocked = True
        res.reasons.append("命中生存级风险事件（立案调查/退市风险/重整等）")
        res.labels.append("合规风险")

    # ── 条件1：减持 ──
    try:
        b, why, d = _check_reduce(observe_events, today=today)
        res.details["reduce"] = d
        if b:
            res.blocked = True
            res.reasons.append(why or "大股东减持")
            res.labels.append("股东动向")
        elif d.get("pct_unverified"):
            res.unavailable.append("减持比例（公告正文未取到，仅确认为「有减持公告」）")
    except Exception:  # pragma: no cover
        res.unavailable.append("减持比例（取数异常）")

    # ── 条件2：估值极端泡沫 ──
    try:
        b, why, d = _check_valuation_bubble(
            pe=pe, pe_pctl=pe_pctl, pe_premium_pct=pe_premium_pct,
            profit_yoy=profit_yoy, is_strong_cyclical=is_strong_cyclical,
        )
        res.details["valuation"] = d
        if b:
            res.blocked = True
            res.reasons.append(why or "估值极端泡沫")
            res.labels.append("估值泡沫")
    except Exception:  # pragma: no cover
        res.unavailable.append("估值泡沫（取数异常）")

    # ── 条件3：资金链 ──
    try:
        b, why, d = _check_solvency(
            debt_ratio=debt_ratio, ocf_ps=ocf_ps,
            ocf_negative_years=ocf_negative_years, industry=industry,
        )
        res.details["solvency"] = d
        if b:
            res.blocked = True
            res.reasons.append(why or "资金链紧张")
            res.labels.append("财务异常")
        elif d.get("unavailable"):
            res.unavailable.append("经营现金流连续为负年数（快照无年度序列）")
    except Exception:  # pragma: no cover
        res.unavailable.append("资金链（取数异常）")

    return {
        "symbol": symbol,
        "blocked": res.blocked,
        "reasons": res.reasons,
        "labels": res.labels,
        "unavailable": sorted(set(res.unavailable)),
        "details": res.details,
    }
