"""成长股 / 科创板 / 周期底部反转识别。

与 classify_dividend_asset 对等的另一套估值框架：
- 红利股：看股息率利差、FCF覆盖分红、PE历史分位
- 成长股：看PS/PEG、赛道景气度、周期位置、技术稀缺性代理（毛利率）
"""

from __future__ import annotations

from typing import Any


# 周期底部反转的估值前提：市场必须已经把盈利下滑定价进去。
# 用 PB 而非 PE 判定 —— 周期底部 E→0 时 PE 天然巨大（PE 在高位恰恰是底部特征），
# 拿 PE 当门槛会把真底部误杀；PB 在周期中相对稳定，是「市场是否已定价衰退」的可靠代理。
# 实测（2026-09-21，5254 只快照）：
#   真周期底部   万华化学 PB 2.01 / 璞泰来 2.23 / 中电港 3.29 → 应保留
#   蹭豁免的高估值 金牛化工 PB 8.49 / 中国卫星 11.03 / 长裕集团 11.15 → 应排除
# 缺失 PB（None）时不做判定，保持既有行为（不因缺数据而改变分类）。
CYCLE_TROUGH_PB_MAX = 5.0


def classify_growth_stock(
    *,
    symbol: str | None = None,
    gross_margin_pct: float | None = None,
    revenue_yoy: float | None = None,
    profit_yoy: float | None = None,
    profit_cagr_3y: float | None = None,
    pe_ttm: float | None = None,
    pb: float | None = None,
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
      + **PB 不处于高位**（见 CYCLE_TROUGH_PB_MAX 说明）
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

    # 周期底部反转：CAGR为负但最新转正 + 成长模块已识别拐点 + PB 不在高位
    # PB 前提说明见 CYCLE_TROUGH_PB_MAX。缺 PB 时不判定（保持原行为）。
    if profit_cagr_3y is not None and profit_cagr_3y < 0 and (is_v_shape or is_marginal_recovery):
        pb_known = pb is not None
        pb_trough_ok = (not pb_known) or float(pb) <= CYCLE_TROUGH_PB_MAX
        if pb_trough_ok:
            if not is_growth:
                is_growth = True
                tier = "turnaround"
            reasons.append("周期底部反转(CAGR负+拐点确认)")
            if not pb_known:
                reasons.append("PB缺失，未做底部估值前提校验")
        else:
            # 不判周期底部：盈利下滑是真的，但市场并未给出底部估值，
            # 此时套用「PE/PB 极端值不判高估」会与「周期底部」的定义自相矛盾。
            reasons.append(
                f"业绩拐点但 PB {float(pb):.1f} > {CYCLE_TROUGH_PB_MAX:g}，"
                "未获底部估值，不适用周期底部口径"
            )

    # 高成长质量兜底
    if is_high_growth_quality and not is_growth:
        is_growth = True
        tier = "hq_growth"
        reasons.append("高成长质量")

    # 【已移除】原「PE 极端失真(>80x) → 判成长股」规则。
    # 该规则是自证循环：高 PE 本身成为「免于高 PE 惩罚」的通行证，
    # 使估值模块对越贵的票越宽松。实测（2026-09-21）：
    #   PE>80 的 1076 只中 1068 只（99.3%）命中该规则，
    #   pe_distorted 组（653 只）PE 中位 152.4，估值分中位 41.0，
    #   反而高于正常口径组（PE 中位 25.2，估值分中位 28.0）——高 PE 得了高分。
    #   PE 高不构成「成长股」的证据；真成长股由 688 / 高毛利+高增长 两条路径认定。
    # 若确需识别「PE 因周期底部而失真」，应由 PB/ROE 等与 PE 无关的证据支撑，
    # 不能由 PE 自身支撑（见 CYCLE_TROUGH_PB_MAX）。

    return {
        "is_growth_stock": is_growth,
        "tier": tier,
        "reasons": reasons,
    }
