"""高股息 / 红利资产识别：用于 WACC、ROIC 对比与估值口径切换。"""

from __future__ import annotations

from typing import Any

# 中国十年期国债收益率近似（百分点）；作股息利差锚，可日后改为实时抓取
CN_10Y_BOND_YIELD_PCT = 2.0

HIGH_DIV_YIELD_PCT = 5.0
SOFT_DIV_YIELD_PCT = 4.0
# 股息率接近无风险利率溢价带即可纳入红利观察（神华约 4.2%~4.7%）
DIVIDEND_OBSERVE_YIELD_PCT = 3.8
HIGH_PAYOUT_PCT = 60.0
# 红利资产资本成本（百分点）：无风险利率 + 极低股权风险溢价
DIVIDEND_ASSET_WACC_PCT = 4.5


def estimate_payout_pct(dividend_yield_pct: float | None, pe_ttm: float | None) -> float | None:
    """
    粗估分红率(%) ≈ 股息率 × PE。
    例：股息率 6%、PE 12 → 分红率约 72%。
    """
    if dividend_yield_pct is None or pe_ttm is None:
        return None
    try:
        dy = float(dividend_yield_pct)
        pe = float(pe_ttm)
    except (TypeError, ValueError):
        return None
    if dy <= 0 or pe <= 0 or pe > 80:
        return None
    return round(dy * pe, 1)


def classify_dividend_asset(
    *,
    dividend_yield_pct: float | None,
    pe_ttm: float | None = None,
    payout_ratio_pct: float | None = None,
) -> dict[str, Any]:
    """
    高股息/红利资产：股息率高且分红慷慨。
    满足时 ROIC/WACC 用红利口径，估值以股息利差为主而非 PEG/历史分位。
    """
    dy = float(dividend_yield_pct) if dividend_yield_pct is not None else None
    payout = payout_ratio_pct
    if payout is None:
        payout = estimate_payout_pct(dy, pe_ttm)

    qualified = False
    tier = ""
    if dy is not None and dy >= HIGH_DIV_YIELD_PCT and (payout is None or payout >= HIGH_PAYOUT_PCT):
        qualified = True
        tier = "high"
    elif (
        dy is not None
        and dy >= SOFT_DIV_YIELD_PCT
        and payout is not None
        and payout >= HIGH_PAYOUT_PCT
    ):
        qualified = True
        tier = "soft"
    elif dy is not None and dy >= DIVIDEND_OBSERVE_YIELD_PCT and (
        (payout is not None and payout >= HIGH_PAYOUT_PCT) or (pe_ttm is not None and float(pe_ttm) <= 25)
    ):
        # 4% 附近 + 高分红或中低 PE：仍按红利资产定价（避免神华被 PE 分位误杀）
        qualified = True
        tier = "observe"

    spread = None
    if dy is not None:
        spread = round(dy - CN_10Y_BOND_YIELD_PCT, 2)

    return {
        "is_dividend_asset": qualified,
        "tier": tier,
        "dividend_yield_pct": round(dy, 2) if dy is not None else None,
        "payout_ratio_pct": payout,
        "bond_yield_pct": CN_10Y_BOND_YIELD_PCT,
        "div_bond_spread_pct": spread,
        "wacc_pct": DIVIDEND_ASSET_WACC_PCT if qualified else None,
    }


def dividend_spread_signal(spread_pct: float | None) -> tuple[str, float]:
    """股息率相对十年国债利差 → (信号, 评分参考分)。"""
    if spread_pct is None:
        return "—", 55.0
    if spread_pct >= 3.0:
        return "低估", 88.0
    if spread_pct >= 2.0:
        return "合理", 76.0
    if spread_pct >= 1.5:
        return "合理", 70.0
    if spread_pct >= 1.0:
        return "合理", 62.0
    return "偏贵", 42.0


def dividend_valuation_note(
    *,
    dividend_yield_pct: float | None,
    payout_ratio_pct: float | None,
    pe_percentile: float | None = None,
) -> str:
    """红利资产估值叙事：历史分位偏高 ≠ 泡沫。"""
    parts = ["相对历史分位可能偏高，但相对股息率与分红确定性仍有支撑"]
    if dividend_yield_pct is not None:
        parts.append(f"股息率约 {float(dividend_yield_pct):.1f}%")
    if payout_ratio_pct is not None:
        parts.append(f"估分红率约 {float(payout_ratio_pct):.0f}%")
    if pe_percentile is not None and float(pe_percentile) >= 75:
        parts.append(f"PE分位 {float(pe_percentile):.0f}% 反映确定性溢价而非单纯泡沫")
    return "；".join(parts)
