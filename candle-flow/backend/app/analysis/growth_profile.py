"""成长股 / 科创板 / 周期底部反转识别。

与 classify_dividend_asset 对等的另一套估值框架：
- 红利股：看股息率利差、FCF覆盖分红、PE历史分位
- 成长股：看PS/PEG、赛道景气度、周期位置、技术稀缺性代理（毛利率）
"""

from __future__ import annotations

from typing import Any


def classify_growth_stock(
    *,
    symbol: str | None = None,
    gross_margin_pct: float | None = None,
    revenue_yoy: float | None = None,
    profit_yoy: float | None = None,
    profit_cagr_3y: float | None = None,
    pe_ttm: float | None = None,
    is_v_shape: bool = False,
    is_marginal_recovery: bool = False,
    is_high_growth_quality: bool = False,
    is_dividend_asset: bool = False,
) -> dict[str, Any]:
    """
    判定是否为「成长/科创板估值框架」适用标的。
    触发条件（任一）：
    - 科创板 688 开头
    - 毛利率 ≥ 40%（技术壁垒代理）+ 最新营收同比 ≥ 15%（景气度代理）
    - 3年净利CAGR为负但最新同比转正（周期底部反转）+ 成长模块已识别 V型拐点/边际改善
    """
    is_growth = False
    tier = ""
    reasons: list[str] = []

    # 已识别为红利资产的，不再按成长股处理（双轨制互斥）
    if is_dividend_asset:
        return {
            "is_growth_stock": False,
            "tier": "",
            "reasons": ["dividend_asset_excluded"],
        }

    sym = (symbol or "").upper()
    if sym.startswith("688"):
        is_growth = True
        tier = "star"
        reasons.append("科创板(688)")

    if gross_margin_pct is not None and revenue_yoy is not None:
        if gross_margin_pct >= 40 and revenue_yoy >= 15:
            if not is_growth:
                is_growth = True
                tier = "high_growth"
            reasons.append(f"高毛利({gross_margin_pct:.0f}%)+高增长({revenue_yoy:.0f}%)")

    # 周期底部反转：CAGR为负但最新转正 + 成长模块已识别拐点
    if (
        profit_cagr_3y is not None
        and profit_cagr_3y < 0
        and (is_v_shape or is_marginal_recovery)
    ):
        if not is_growth:
            is_growth = True
            tier = "turnaround"
        reasons.append("周期底部反转(CAGR负+拐点确认)")

    # 高成长质量兜底
    if is_high_growth_quality and not is_growth:
        is_growth = True
        tier = "hq_growth"
        reasons.append("高成长质量")

    # PE 极端失真（>80x）几乎只在成长股/周期底部出现
    if pe_ttm is not None and pe_ttm > 80 and not is_growth:
        is_growth = True
        tier = "pe_distorted"
        reasons.append(f"PE {pe_ttm:.0f}x 极端失真(周期底部特征)")

    return {
        "is_growth_stock": is_growth,
        "tier": tier,
        "reasons": reasons,
    }
