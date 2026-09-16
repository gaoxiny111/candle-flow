"""高毛利 / 高成长赛道：用于 WACC 下调、ROIC 误杀豁免与成长包容度。"""

from __future__ import annotations

from typing import Any

HIGH_GROSS_MARGIN_PCT = 40.0
HIGH_PROFIT_YOY_PCT = 50.0
# 扩张期科技龙头资本成本（百分点）：低于默认 10%
HIGH_GROWTH_WACC_PCT = 7.0


def classify_high_growth_quality(
    *,
    gross_margin_pct: float | None,
    profit_yoy_pct: float | None,
) -> dict[str, Any]:
    """
    高毛利 + 高净利增速 → 高成长赛道质量画像。
    用于下调 WACC，避免扩张期科技龙头被 ROIC<10% WACC 误杀。
    """
    gm = float(gross_margin_pct) if gross_margin_pct is not None else None
    yoy = float(profit_yoy_pct) if profit_yoy_pct is not None else None
    qualified = (
        gm is not None
        and yoy is not None
        and gm >= HIGH_GROSS_MARGIN_PCT
        and yoy >= HIGH_PROFIT_YOY_PCT
    )
    return {
        "is_high_growth_quality": qualified,
        "gross_margin_pct": round(gm, 2) if gm is not None else None,
        "profit_yoy_pct": round(yoy, 2) if yoy is not None else None,
        "wacc_pct": HIGH_GROWTH_WACC_PCT if qualified else None,
    }
