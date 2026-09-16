"""周期股识别。与 classify_dividend_asset / classify_growth_stock 三轨互斥。

周期股特征：
- PE < 15（低估值）
- 行业含 化工/有色/煤炭/钢铁/航运/港口/开采/化肥/磷
- 3年净利CAGR为负但最新YoY转正（周期底部）
- 毛利率波动大（标准差 > 5%）
- 存货/总资产 > 25%（重资产，存货变现强）

三轨优先级（互斥）：
  1. is_dividend_asset=True → 红利股
  2. is_cyclical=True → 周期股
  3. is_growth_stock=True → 成长股
  4. 否则 → 普通股
"""

from __future__ import annotations

from typing import Any


CYCLE_KEYWORDS = (
    "化工", "有色", "煤炭", "钢铁", "航运", "港口", "开采",
    "化肥", "磷", "矿", "石油", "天然气", "金属", "能源",
)


def classify_cyclical_stock(
    *,
    symbol: str | None = None,
    industry: str | None = None,
    pe_ttm: float | None = None,
    profit_cagr_3y: float | None = None,
    profit_yoy: float | None = None,
    gross_margin_std: float | None = None,
    inventory_ratio: float | None = None,
    is_dividend_asset: bool = False,
    is_growth_stock: bool = False,
) -> dict[str, Any]:
    is_cyclical = False
    tier = ""
    reasons: list[str] = []

    # 已被分类为红利或成长的，不再标记为周期股
    if is_dividend_asset or is_growth_stock:
        return {
            "is_cyclical": False,
            "tier": "",
            "reasons": ["already_classified"],
        }

    # 行业关键词匹配
    ind = (industry or "").lower()
    has_cycle_industry = any(kw.lower() in ind for kw in CYCLE_KEYWORDS)

    # 触发条件：行业匹配 + 任一周期特征
    if has_cycle_industry:
        triggers = 0
        if pe_ttm is not None and 0 < float(pe_ttm) < 15:
            triggers += 1
            reasons.append(f"PE {float(pe_ttm):.1f}<15")
        if profit_cagr_3y is not None and profit_yoy is not None:
            if float(profit_cagr_3y) < 0 and float(profit_yoy) > 0:
                triggers += 1
                reasons.append("CAGR负+YoY转正(底部复苏)")
        if gross_margin_std is not None and float(gross_margin_std) > 5:
            triggers += 1
            reasons.append(f"毛利率波动大(σ={float(gross_margin_std):.1f}%)")
        if inventory_ratio is not None and float(inventory_ratio) > 0.25:
            triggers += 1
            reasons.append(f"存货/总资产{float(inventory_ratio)*100:.0f}%>25%")

        if triggers >= 1:
            is_cyclical = True
            if triggers >= 3:
                tier = "strong_cycle"
            else:
                tier = "weak_cycle"
            reasons.insert(0, f"行业={industry}")

    return {
        "is_cyclical": is_cyclical,
        "tier": tier,
        "reasons": reasons,
    }
