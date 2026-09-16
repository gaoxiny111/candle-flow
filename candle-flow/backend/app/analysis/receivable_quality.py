"""客户质量 / 强现金流过滤：弱化「回款极其困难」类表述。"""

from __future__ import annotations

from typing import Any

# 央企/能源龙头：应收上升多为结算周期，非赊销危机
_SOE_NAME_KEYS = ("神华", "国家能源", "华能", "大唐", "华电", "国电", "中石油", "中石化", "中海油", "中国移动", "中国电信", "中国联通", "工商银行", "建设银行", "农业银行", "中国银行")
_SOE_INDUSTRY_KEYS = ("煤炭", "焦炭", "电力", "石油", "天然气", "开采", "银行", "保险", "铁路")


def is_quality_receivable_context(
    *,
    name: str = "",
    industry: str = "",
    ocf: float | None = None,
    net_profit: float | None = None,
    is_dividend_asset: bool = False,
    cash_ratio: float | None = None,
) -> bool:
    """
    具备央企/能源长协客户结构，或经营现金流显著覆盖净利时，
    应收周转下降仅作效率观察，不作回款危机定性。
    """
    name_u = name or ""
    ind = industry or ""
    if any(k in name_u for k in _SOE_NAME_KEYS):
        return True
    if any(k in ind for k in _SOE_INDUSTRY_KEYS):
        return True
    if is_dividend_asset:
        return True
    try:
        if cash_ratio is not None and float(cash_ratio) >= 1.2:
            return True
        if (
            ocf is not None
            and net_profit is not None
            and float(net_profit) > 0
            and float(ocf) / float(net_profit) >= 1.2
        ):
            return True
    except (TypeError, ValueError, ZeroDivisionError):
        pass
    return False


def ar_turnover_warning(
    *,
    quality_context: bool,
    severe_wc_spike: bool = False,
) -> tuple[str, float]:
    """
    返回 (提示文案, 风险扣分)。
    quality_context 下仅效率提示、轻扣或不扣。
    """
    if severe_wc_spike:
        if quality_context:
            return (
                "应收账款与营运资本占用上升，需关注大客户结算周期变化（非赊销危机定性）",
                5.0,
            )
        return (
            "应收账款与存货激增，营运资本占用严重，需警惕下游需求放缓带来的坏账与减值风险",
            15.0,
        )
    if quality_context:
        return (
            "应收账款周转效率边际下降，回款节奏放缓，需关注大客户结算周期变化",
            3.0,
        )
    return ("应收账款周转恶化，回款极其困难", 15.0)


def format_ar_note(ctx: dict[str, Any] | None = None) -> str:
    return "质量客户/强现金流口径：应收上升按结算效率观察，不升格为回款危机"
