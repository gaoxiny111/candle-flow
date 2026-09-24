from __future__ import annotations

from typing import Any

import pandas as pd

from app.analysis.base import AnalysisLevel, BaseAnalyzer, IndicatorResult, ModuleResult, format_report_period, score_to_level
from app.analysis.config.company_profiles import get_company_profile, get_merger_consolidation


# ── 补丁二：基数校验（净利3年CAGR > 50% 时触发）──────────────────────────
# 核心逻辑：CAGR 高不代表可持续成长。若「基期」是亏损或极低值，则增速的
# 绝大部分来自「从坑里爬出来」，属扭亏/低基数失真，须对成长性降权。
# 与 engine._peg_base_check 的分工：那条只拦 PEG 分母（PEG→None），本函数
# 在成长模块分上做实质扣减，两者判据互补、互不叠加。
GROWTH_BASE_CAGR_TRIGGER = 50.0          # 触发线：净利3年CAGR > 50%
GROWTH_BASE_LOW_ABS = 5_000_000.0        # 基期绝对值低于 500 万 = 极低值
GROWTH_BASE_PENALTY_EXTREME = 20.0       # CAGR > 100 → 扣 20
GROWTH_BASE_PENALTY_HIGH = 15.0          # CAGR > 80  → 扣 15
GROWTH_BASE_PENALTY_BASE = 10.0          # 其余      → 扣 10
GROWTH_BASE_DEDUCT_MIN = 0.0             # 扣分下限
GROWTH_BASE_RAW_FLOOR = 20.0             # 扣减后 raw 下限（防击穿到负分）


def check_growth_base_quality(*, profit_cagr: float | None, base_net_profit: float | None) -> dict[str, Any]:
    """基数校验：净利3年CAGR > 50% 时，检查基期是否为亏损/极低值。

    参数
    ----
    profit_cagr : 净利润3年CAGR（百分数，如 95.92 表示 95.92%）
    base_net_profit : **CAGR 实际使用的基期净利润**（元）。3年CAGR 时即
        ``net_profit.dropna().iloc[-4]``；跨度退化时应传 ``iloc[0]``。

    返回
    ----
    ``{"passed", "deduction", "reason", "triggered", "base_net_profit", "cagr"}``

    缺数据一律放行：只有 CAGR 与基期净利**都**拿到，且 CAGR > 50 时才判定。
    """
    out: dict[str, Any] = {
        "passed": True,
        "deduction": 0.0,
        "reason": "",
        "triggered": False,
        "base_net_profit": None,
        "cagr": None,
    }
    if profit_cagr is None or base_net_profit is None:
        out["reason"] = "基数校验数据缺失，不判定"
        return out
    try:
        cagr = float(profit_cagr)
        base = float(base_net_profit)
    except (TypeError, ValueError):
        out["reason"] = "基数校验数据非数值，不判定"
        return out
    out["cagr"] = cagr
    out["base_net_profit"] = base
    if cagr <= GROWTH_BASE_CAGR_TRIGGER:
        out["reason"] = ""
        return out

    out["triggered"] = True
    anomaly = ""
    if base <= 0:
        anomaly = "基期亏损，同比增速失真"
    elif abs(base) < GROWTH_BASE_LOW_ABS:
        anomaly = f"基期净利润仅{base / 1e4:.1f}万元，极低基数推高增速"
    if not anomaly:
        out["reason"] = "成长基数正常"
        return out

    if cagr > 100:
        deduction = GROWTH_BASE_PENALTY_EXTREME
    elif cagr > 80:
        deduction = GROWTH_BASE_PENALTY_HIGH
    else:
        deduction = GROWTH_BASE_PENALTY_BASE
    out["passed"] = False
    out["deduction"] = float(deduction)
    out["reason"] = f"基数校验不通过：{anomaly}，成长性降权{deduction:.0f}分"
    return out


# ── 补丁二-B：扭亏型伪成长（同比口径，独立于 CAGR 闸门）─────────────────
# 为什么必须另起一条链路：`_calc_cagr` 在 `start <= 0` 时**直接返回 0**，
# 所以「上年亏损、今年扭亏」的票根本进不了 CAGR>50% 的判定 —— 补丁二那
# 条 CAGR 闸门对「扭亏型」天然免疫（全市场实测 0 触发）。真正能拦住扭亏
# 的是**单期同比**：同比的分母就是上年同期，上年亏损时同比本身失真。
#
# 与既有判据的分工（铁律3 防叠加）：
#   · 补丁二  check_growth_base_quality —— CAGR 口径，基期 = 倒数第4期
#   · 补丁二-B 本函数 —— 同比口径，基期 = 上年同期（含同期扣非）
#   · profit_illusion —— 扣非为负，**当期**口径
# 三者口径互不重叠；本函数只对 同比>100% 的票生效，且与 profit_illusion 重叠时
# 只取更严者（不累加），避免同一只票被同一事实扣两次。
TURNAROUND_YOY_TRIGGER = 100.0           # 触发线：最新报告期净利同比 > 100%
TURNAROUND_YOY_EXTREME = 200.0           # 极端扭亏线
TURNAROUND_LOW_ABS = 5_000_000.0         # 上年同期绝对值 < 500 万 = 极低值
TURNAROUND_PENALTY_LOSS = 20.0           # 上年同期亏损
TURNAROUND_PENALTY_EXTREME_EXTRA = 10.0  # 同比>200% 且上年亏损 → 额外
TURNAROUND_PENALTY_LOW = 15.0            # 上年同期极低值
TURNAROUND_PENALTY_DEDUCTED = 10.0       # 上年扣非 <=0（主营未改善）；低于「上年亏损」档


def check_turnaround_growth(
    *,
    profit_yoy: float | None,
    last_year_net_profit: float | None,
    deducted_net_profit: float | None = None,
    last_year_deducted_net_profit: float | None = None,
) -> dict[str, Any]:
    """扭亏型伪成长检测（同比口径）。

    参数
    ----
    profit_yoy : 最新报告期归母净利同比（百分数，如 137.9）
    last_year_net_profit : **上年同期**归母净利（元）
    deducted_net_profit : 当期扣非归母净利（元，用于交叉印证，可为 None）
    last_year_deducted_net_profit : 上年同期扣非归母净利（元）

    返回
    ----
    ``{"triggered", "deduction", "reason", "penalty_loss", "penalty_low",
       "penalty_deducted", "penalty_extreme"}``

    缺数据一律放行。扣分按「档位取严 + 极端叠加」：基础档（亏损20 / 极低15）
    二者互斥，另加两条**独立**扣减 —— 极端扭亏 +10、上年扣非<=0 +15，各只计一次。
    """
    out: dict[str, Any] = {
        "triggered": False,
        "deduction": 0.0,
        "reason": "",
        "penalty_loss": 0.0,
        "penalty_low": 0.0,
        "penalty_deducted": 0.0,
        "penalty_extreme": 0.0,
        "profit_yoy": None,
        "last_year_net_profit": None,
    }
    if profit_yoy is None or last_year_net_profit is None:
        out["reason"] = "扭亏检测数据缺失，不判定"
        return out
    try:
        yoy = float(profit_yoy)
        ly_np = float(last_year_net_profit)
    except (TypeError, ValueError):
        out["reason"] = "扭亏检测数据非数值，不判定"
        return out
    out["profit_yoy"] = yoy
    out["last_year_net_profit"] = ly_np
    if yoy <= TURNAROUND_YOY_TRIGGER:
        return out

    out["triggered"] = True
    reasons: list[str] = []

    # 基础档：亏损 与 极低值 互斥（elif），不叠加
    if ly_np <= 0:
        out["penalty_loss"] = TURNAROUND_PENALTY_LOSS
        reasons.append(f"上年同期亏损（{ly_np / 1e8:.2f}亿），同比增速失真")
        if yoy > TURNAROUND_YOY_EXTREME:
            out["penalty_extreme"] = TURNAROUND_PENALTY_EXTREME_EXTRA
            reasons.append("极端扭亏（同比>200%），额外扣减")
    elif abs(ly_np) < TURNAROUND_LOW_ABS:
        out["penalty_low"] = TURNAROUND_PENALTY_LOW
        reasons.append(f"上年同期净利仅{ly_np / 1e4:.1f}万元，极低基数推高增速")

    # 独立判据：上年扣非<=0 → 主营本身不赚钱（与上面两档并存）
    if last_year_deducted_net_profit is not None:
        try:
            ly_ded = float(last_year_deducted_net_profit)
        except (TypeError, ValueError):
            ly_ded = None
        if ly_ded is not None and ly_ded <= 0 and ly_np > 0:
            out["penalty_deducted"] = TURNAROUND_PENALTY_DEDUCTED
            reasons.append("上年同期扣非<=0，主营未改善")
    _ = deducted_net_profit  # 当期扣非由 profit_illusion 单独处理，此处不重复计

    total = (
        out["penalty_loss"]
        + out["penalty_low"]
        + out["penalty_extreme"]
        + out["penalty_deducted"]
    )
    if not reasons or total <= 0:
        out["triggered"] = False
        out["reason"] = "同比高增但上年基数正常，不判定"
        return out
    out["deduction"] = float(total)
    out["reason"] = f"扭亏型伪成长检测：{'；'.join(reasons)}，成长性扣减{total:.0f}分"
    return out


class GrowthAnalyzer(BaseAnalyzer):
    """营收/利润 CAGR + 最新同比；识别 V 型拐点。"""

    def _calc_cagr(self, series: pd.Series, years: int) -> float:
        clean = series.dropna()
        if len(clean) < years + 1:
            # 数据不足时用最长可用跨度
            if len(clean) < 2:
                return 0.0
            span = len(clean) - 1
            start, end = clean.iloc[0], clean.iloc[-1]
            if float(start) <= 0 or float(end) <= 0:
                return 0.0
            return float(((end / start) ** (1 / span) - 1) * 100)
        start = clean.iloc[-(years + 1)]
        end = clean.iloc[-1]
        if float(start) <= 0 or float(end) <= 0:
            return 0.0
        return float(((end / start) ** (1 / years) - 1) * 100)

    def analyze(self, financial_data: pd.DataFrame, **kwargs) -> ModuleResult:
        indicators: list[IndicatorResult] = []
        warnings: list[str] = []
        if financial_data.empty or "revenue" not in financial_data:
            return ModuleResult("成长性", 0, AnalysisLevel.DANGER, warnings=["暂无营收数据"])

        # 公司画像：从配置加载个股特殊因子（替代硬编码 if 判断）
        _profile = get_company_profile(
            kwargs.get("name") or "", kwargs.get("symbol") or ""
        )

        revenue = financial_data["revenue"]
        profit = financial_data["net_profit"]
        annual_dates = [str(x) for x in (kwargs.get("annual_dates") or list(financial_data.index))]
        cagr_period = ""
        if len(annual_dates) >= 2:
            a0 = format_report_period(annual_dates[max(0, len(annual_dates) - 4)]) or annual_dates[0]
            a1 = format_report_period(annual_dates[-1]) or annual_dates[-1]
            cagr_period = f"{a0}–{a1}"
        elif annual_dates:
            cagr_period = format_report_period(annual_dates[-1])
        yoy_period = format_report_period(kwargs.get("latest_report")) or "最新报告期"

        rev_cagr_3y = self._calc_cagr(revenue, 3)
        # 成熟蓝筹：小幅负 CAGR 仍属稳健，不按成长股标准打地板
        score, level = self._score_by_range(rev_cagr_3y, (10, 200), (-10, 10), (-25, -10))
        indicators.append(
            IndicatorResult(
                name="营收3年CAGR(%)",
                value=round(rev_cagr_3y, 2),
                score=score,
                level=level,
                trend=self._calc_trend(revenue.pct_change(fill_method=None) * 100),
                weight=1.8,
                period=cagr_period,
                comment="年报序列复合增速",
            )
        )

        profit_cagr_3y = self._calc_cagr(profit, 3)
        score2, level2 = self._score_by_range(profit_cagr_3y, (12, 300), (-12, 12), (-30, -12))
        indicators.append(
            IndicatorResult(
                name="净利润3年CAGR(%)",
                value=round(profit_cagr_3y, 2),
                score=score2,
                level=level2,
                trend=self._calc_trend(profit.pct_change(fill_method=None) * 100),
                weight=1.8,
                period=cagr_period,
                comment="年报序列复合增速",
            )
        )

        # ── 前瞻成长性：历史40% + 前瞻60%（无机构预期时用最新同比代理） ──
        yoy_rev = kwargs.get("revenue_yoy")
        yoy_profit = kwargs.get("profit_yoy")
        fwd_growth = None
        fwd_source = ""
        # 优先机构一致预期（当前无数据源，预留接口）
        consensus_cagr = kwargs.get("consensus_net_profit_cagr_3y")
        if consensus_cagr is not None and float(consensus_cagr) > 0:
            fwd_growth = float(consensus_cagr)
            fwd_source = "机构一致预期未来3年CAGR"
        elif yoy_profit is not None:
            fwd_growth = float(yoy_profit)
            fwd_source = f"最新报告期净利同比（{yoy_period}）代理前瞻"

        if fwd_growth is not None:
            # 评分标准：>15%=A, 10-15%=B+, 5-10%=B, 0-5%=C, <0%=D
            def _growth_score(cagr: float) -> tuple[float, AnalysisLevel]:
                if cagr > 15:
                    return 88.0, AnalysisLevel.EXCELLENT
                elif cagr > 10:
                    return 80.0, AnalysisLevel.GOOD
                elif cagr > 5:
                    return 72.0, AnalysisLevel.GOOD
                elif cagr > 0:
                    return 55.0, AnalysisLevel.NEUTRAL
                else:
                    return 35.0, AnalysisLevel.POOR

            hist_score, _ = _growth_score(profit_cagr_3y)
            fwd_score, fwd_level = _growth_score(fwd_growth)
            # 加权：历史40% + 前瞻60%
            combined = hist_score * 0.4 + fwd_score * 0.6

            # ===== 周期底部修正：识别非经常性损益扰动 + 主动收缩 =====
            cyclical_note = ""
            deducted_yoy = kwargs.get("deducted_yoy_pct")
            sq = kwargs.get("single_quarter") or {}
            q2_qoq = sq.get("qoq_pct")
            rev_yoy_val = kwargs.get("revenue_yoy")
            # 毛利率环比变化
            gm_qoq_val = None
            if "gross_margin" in financial_data.columns:
                gm_series = financial_data["gross_margin"].dropna()
                if len(gm_series) >= 2:
                    gm_qoq_val = float(gm_series.iloc[-1]) - float(gm_series.iloc[-2])

            is_cyclical_recovery = (
                fwd_growth < 0  # 归母同比为负
                and deducted_yoy is not None  # 有扣非数据
                and abs(float(deducted_yoy) - fwd_growth) > 10  # 扣非与归母差距>10pct
                and q2_qoq is not None and float(q2_qoq) > 0  # Q2环比为正
                and rev_yoy_val is not None and float(rev_yoy_val) < 0  # 营收下滑
            )

            if is_cyclical_recovery:
                # 1) 用扣非增速替代归母增速（权重 70/30）
                adjusted_fwd = float(deducted_yoy) * 0.7 + fwd_growth * 0.3
                adj_fwd_score, _ = _growth_score(adjusted_fwd)
                combined = hist_score * 0.4 + adj_fwd_score * 0.6
                # 2) Q2 环比改善加分（边际修复奖励，最多+10）
                q2_bonus = min(float(q2_qoq) * 0.5, 10.0)
                combined = min(95.0, combined + q2_bonus)
                # 3) 主动收缩优化结构加分（营收降但毛利率升，+5）
                quality_bonus = 0.0
                if gm_qoq_val is not None and gm_qoq_val > 0.01:
                    quality_bonus = 5.0
                    combined = min(95.0, combined + quality_bonus)
                cyclical_note = (
                    f"；周期底部修正：扣非{float(deducted_yoy):.1f}%与归母{fwd_growth:.1f}%差距>10pct，"
                    f"用扣非替代归母（权重70/30），Q2环比+{float(q2_qoq):.0f}%奖励+{q2_bonus:.1f}"
                    + (f"，主动收缩毛利率升奖励+{quality_bonus:.0f}" if quality_bonus > 0 else "")
                )

            # 加速因子：前瞻 > 历史×1.5，加5分
            acceleration_bonus = 0.0
            if fwd_growth > profit_cagr_3y * 1.5 and profit_cagr_3y > 0:
                acceleration_bonus = 5.0
                combined = min(95.0, combined + acceleration_bonus)
            combined = round(combined, 1)
            combined_level = (
                AnalysisLevel.EXCELLENT if combined >= 85
                else AnalysisLevel.GOOD if combined >= 70
                else AnalysisLevel.NEUTRAL if combined >= 55
                else AnalysisLevel.POOR
            )
            accel_note = (
                f"；加速因子+{acceleration_bonus:.0f}（前瞻{fwd_growth:.1f}%>历史{profit_cagr_3y:.1f}%×1.5）"
                if acceleration_bonus > 0 else ""
            )
            # 极端同比（基期接近0 / 并表 / 非经常性损益）只影响计分上限，不改写展示值
            extreme_note = ""
            if abs(float(fwd_growth)) > 300:
                extreme_note = "；⚠️ 属极端增速，仅按±300%上限计分，不可线性外推"
                warnings.append(
                    f"净利同比{fwd_growth:.1f}% 为极端值（低基数/并表/非经常性损益），"
                    f"评分已按 300% 上限计分，不可按此线性外推下一年"
                )
            indicators.append(
                IndicatorResult(
                    name="前瞻成长性(%)",
                    value=round(fwd_growth, 2),
                    score=combined,
                    level=combined_level,
                    trend="up" if fwd_growth > profit_cagr_3y else "flat",
                    weight=1.5,
                    period=yoy_period,
                    comment=(
                        f"历史3年CAGR {profit_cagr_3y:.1f}%（权重40%）+"
                        f"前瞻 {fwd_growth:.1f}%（权重60%，{fwd_source}）"
                        f"= 综合{combined:.0f}分{accel_note}{cyclical_note}{extreme_note}"
                    ),
                )
            )
        # 最新同比权重高于 CAGR，避免周期回落掩盖边际企稳
        yoy_weight = 3.5
        # 蓝筹温和复苏（0%~+10%）给良好档，不要求高增长
        if yoy_rev is not None:
            yoy_val = float(yoy_rev)
            ls, yoy_lv = self._score_by_range(yoy_val, (12, 200), (0, 12), (-10, 0))
            indicators.append(
                IndicatorResult(
                    name="最新报告期营收同比(%)",
                    value=round(yoy_val, 2),
                    score=ls,
                    level=yoy_lv,
                    trend="up" if yoy_val > 5 else ("down" if yoy_val < -5 else "flat"),
                    weight=yoy_weight,
                    period=yoy_period,
                    comment=f"同比口径：{yoy_period}（非年报CAGR）",
                )
            )

        if yoy_profit is not None:
            yp = float(yoy_profit)
            ls2, yp_lv = self._score_by_range(min(yp, 150), (15, 300), (0, 15), (-12, 0))
            indicators.append(
                IndicatorResult(
                    name="最新报告期净利同比(%)",
                    value=round(yp, 2),
                    score=ls2,
                    level=yp_lv,
                    trend="up" if yp > 5 else ("down" if yp < -5 else "flat"),
                    weight=yoy_weight,
                    period=yoy_period,
                    comment=f"同比口径：{yoy_period}（非年报CAGR）",
                )
            )

        # 扣非一票否决：归母净利同比高增（或已转正）但扣非仍为负 → 利润幻增
        deducted = kwargs.get("deducted_net_profit")
        parent_np = kwargs.get("parent_net_profit")
        profit_illusion = False
        if deducted is not None and float(deducted) < 0:
            parent_pos = parent_np is not None and float(parent_np) > 0
            yoy_strong = yoy_profit is not None and float(yoy_profit) >= 15
            if parent_pos or yoy_strong:
                profit_illusion = True
                ded_wan = float(deducted) / 1e4
                indicators.append(
                    IndicatorResult(
                        name="扣非净利润(万元)",
                        value=round(ded_wan, 2),
                        score=25.0,
                        level=AnalysisLevel.POOR,
                        weight=4.0,
                        period=yoy_period,
                        comment="扣非仍为负：主业未恢复，表观盈利依赖非经常性损益",
                    )
                )
                warnings.append(
                    "主业造血能力未恢复，利润依赖非经常性损益（扣非净利润仍为负）"
                )

        # 业绩拐点 / 边际改善：历史 CAGR 为负，但最新同比已转正（含温和企稳）
        # 神华等：净利同比仅 +4% 也应捕捉，不宜要求 ≥8% 才给拐点
        yoy_p = float(yoy_profit) if yoy_profit is not None else None
        yoy_r = float(yoy_rev) if yoy_rev is not None else None
        cagr_drag = profit_cagr_3y < 0 or rev_cagr_3y < 0
        v_shape = False
        marginal_recovery = False
        # 提前计算毛利率（供“主动收缩低毛利业务”检测用）
        gross_margin_val = kwargs.get("latest_gross_margin")
        if gross_margin_val is None and "gross_margin" in financial_data.columns:
            gms = financial_data["gross_margin"].dropna()
            if len(gms):
                gross_margin_val = float(gms.iloc[-1])
        elif gross_margin_val is None and "cogs" in financial_data.columns and financial_data["cogs"].notna().any():
            rev_s = financial_data["revenue"].replace(0, pd.NA)
            gms = ((financial_data["revenue"] - financial_data["cogs"]) / rev_s * 100).dropna()
            if len(gms):
                gross_margin_val = float(gms.iloc[-1])
        # 周期股识别：营收同比放宽（主动收缩贸易≠衰退）
        cycle_industries = ("煤炭", "焦炭", "有色", "钢铁", "化工", "航运", "港口", "开采", "化肥", "磷", "矿", "农化", "农药")
        industry_str = str(kwargs.get("industry") or "")
        is_cycle = any(k in industry_str for k in cycle_industries)
        if not profit_illusion and cagr_drag and yoy_p is not None and yoy_r is not None:
            # 周期股：营收同比允许为负（主动收缩低毛利业务），只要求净利转正
            if is_cycle:
                if yoy_p >= 5 and yoy_r >= -15:
                    v_shape = True
                    strong = yoy_p >= 15
                    indicators.append(
                        IndicatorResult(
                            name="成长拐点",
                            value=round(yoy_p, 2),
                            score=82.0 if strong else 76.0,
                            level=AnalysisLevel.GOOD,
                            trend="up",
                            weight=3.0,
                            period=yoy_period,
                            comment=(
                                "周期底部复苏：历史CAGR为负但净利同比转正"
                                f"（营收同比{yoy_r:+.1f}%为主动收缩低毛利贸易/周期特征）"
                            ),
                        )
                    )
                    warnings.append(
                        "近3年净利润复合增速为负，但最新报告期已现拐点，需观察持续性"
                    )
                    # 下调 CAGR 指标权重，避免下行期拖垮底部复苏叙事
                    for ind in indicators:
                        if "CAGR" in ind.name:
                            ind.weight = 1.2
            # 非周期股原逻辑
            elif yoy_p >= 8 and yoy_r >= 0:
                v_shape = True
                strong = yoy_p >= 20
                indicators.append(
                    IndicatorResult(
                        name="成长拐点",
                        value=round(yoy_p, 2),
                        score=82.0 if strong else 76.0,
                        level=AnalysisLevel.GOOD,
                        trend="up",
                        weight=3.0,
                        period=yoy_period,
                        comment=(
                            "历史复合增速为负但最新同比强劲反弹（V型拐点）"
                            if strong
                            else "历史复合增速为负但最新同比已转正（复苏拐点）"
                        ),
                    )
                )
                warnings.append(
                    "近3年净利润复合增速为负，但最新报告期已现拐点，需观察持续性"
                )
            # 边际改善
            if not v_shape:
                # 周期股：营收同比放宽至 ≥-10%（允许小幅收缩）
                if is_cycle and yoy_p >= 0 and yoy_r >= -10:
                    marginal_recovery = True
                elif not is_cycle and yoy_p >= 0 and yoy_r >= 3:
                    marginal_recovery = True
                ded_note = ""
                # 扣非改善：主业修复信号（神华中报扣非强于归母时常出现）
                ded_yoy = kwargs.get("deducted_yoy_pct")
                if deducted is not None and parent_np is not None:
                    try:
                        if float(deducted) > 0 and float(parent_np) > 0:
                            if ded_yoy is not None and float(ded_yoy) > 0:
                                ded_note = f"；扣非同比{float(ded_yoy):+.1f}%，主业修复更强"
                            else:
                                ded_note = "；扣非净利同步改善，主业盈利韧性增强"
                    except (TypeError, ValueError):
                        pass
                cycle_industries = ("煤炭", "焦炭", "有色", "钢铁", "化工", "航运", "港口", "开采")
                industry = str(kwargs.get("industry") or "")
                is_cycle = any(k in industry for k in cycle_industries) or _profile.get("cyclical", False)
                # 只有 marginal_recovery 成立（净利同比转正，周期股放宽营收）才输出
                # 「业绩边际改善」。旧逻辑无条件追加，导致新潮能源净利同比 -65.6%、
                # 营收同比 -21.5% 时仍给出「边际改善 74 分（权重 3.2）」+「单季加速
                # 82 分（权重 2.8）」，两项合计权重 6.0/16.9 把成长分从衰退口径的
                # 约 30 分抬到 44.1 —— 指标名与事实相反，且与
                # metadata.marginal_recovery=False 自相矛盾。降幅收窄但同比未转正的，
                # 只出提示、不计分。
                if marginal_recovery:
                    if is_cycle:
                        comment = (
                            f"周期回落后出现边际修复：营收同比{yoy_r:+.1f}%、净利同比{yoy_p:+.1f}%"
                            f"{ded_note}；资产注入与多业务板块改善增强盈利韧性"
                            "（非高成长，但是底部修复）"
                        )
                        score_m = 78.0 if yoy_p >= 3 else 74.0
                    else:
                        comment = (
                            f"年报CAGR为负，但最新报告期营收同比{yoy_r:+.1f}%、"
                            f"净利同比{yoy_p:+.1f}%，出现边际修复迹象{ded_note}"
                        )
                        score_m = 74.0 if yoy_p >= 3 else 70.0
                    indicators.append(
                        IndicatorResult(
                            name="业绩边际改善",
                            value=round(yoy_p, 2),
                            score=score_m,
                            level=AnalysisLevel.GOOD,
                            trend="up",
                            weight=3.2,
                            period=yoy_period,
                            comment=comment,
                        )
                    )
                    # 下调 CAGR 指标权重，避免「仍在衰退」叙事压过边际改善
                    for ind in indicators:
                        if "CAGR" in ind.name:
                            ind.weight = 1.2
                else:
                    warnings.append(
                        f"最新报告期净利同比{yoy_p:+.1f}%、营收同比{yoy_r:+.1f}%，"
                        "同比未转正，未构成边际改善；降幅收窄不等于趋势反转，需后续季度验证"
                    )
                # 单季业绩加速（Q2 环比/同比大幅改善 → 修复力度超预期）
                # 口径：单季"加速"必须有同比支撑；纯靠环比改善（同比仍下滑）不计分，
                # 否则会出现「环比+31%、同比-49%，修复力度较强」这类自相矛盾的结论。
                sq = kwargs.get("single_quarter")
                if sq and sq.get("net_profit") is not None:
                    sq_yoy = sq.get("yoy_pct")
                    sq_qoq = sq.get("qoq_pct")
                    if sq_yoy is not None and sq_qoq is not None:
                        sq_yoy_f = float(sq_yoy)
                        sq_qoq_f = float(sq_qoq)
                        sq_score: float | None = None
                        sq_comment = ""
                        # 修复力度超预期 → 加分项
                        if sq_yoy_f >= 30 or (sq_qoq_f >= 50 and sq_yoy_f >= 0):
                            sq_score = 86.0
                            sq_comment = (
                                f"Q2单季净利环比{sq_qoq_f:+.0f}%、同比{sq_yoy_f:+.0f}%，"
                                f"修复力度超预期"
                            )
                        elif sq_yoy_f >= 15 or (sq_qoq_f >= 30 and sq_yoy_f >= 0):
                            sq_score = 82.0
                            sq_comment = (
                                f"Q2单季净利环比{sq_qoq_f:+.0f}%、同比{sq_yoy_f:+.0f}%，"
                                f"修复力度较强"
                            )
                        elif sq_yoy_f >= 0:
                            sq_score = 75.0
                            sq_comment = (
                                f"Q2单季净利环比{sq_qoq_f:+.0f}%、同比{sq_yoy_f:+.0f}%"
                            )
                        else:
                            warnings.append(
                                f"Q2单季净利环比{sq_qoq_f:+.0f}%、同比{sq_yoy_f:+.0f}%，"
                                "环比改善但同比仍下滑，修复未获同比验证，不计入成长加分"
                            )
                        if sq_score is not None:
                            # 扣非增速补充
                            ded_yoy = kwargs.get("deducted_yoy_pct")
                            if ded_yoy is not None and float(ded_yoy) > 0:
                                sq_comment += f"；扣非同比{float(ded_yoy):+.1f}%，主业修复更强"
                            indicators.append(
                                IndicatorResult(
                                    name="单季业绩加速",
                                    value=round(sq_yoy_f, 2),
                                    score=sq_score,
                                    level=AnalysisLevel.GOOD,
                                    trend="up",
                                    weight=2.8,
                                    period=sq.get("label") or yoy_period,
                                    comment=sq_comment,
                                )
                            )
                if marginal_recovery:
                    warnings.append(
                        "近3年复合增速为负，但最新报告期已现边际修复"
                        "（周期企稳+主业/多业务改善），非纯粹衰退通道"
                    )
            elif profit_cagr_3y < 0:
                # 周期底部反转已确认时，不再重复输出“盈利能力下滑”
                if not v_shape and not marginal_recovery:
                    warnings.append("近3年净利润复合增速为负，盈利能力持续下滑")
        elif profit_cagr_3y < 0 and not profit_illusion:
            warnings.append("近3年净利润复合增速为负，盈利能力持续下滑")
        # ── 营收下滑但主动收缩低毛利业务：不判负面 ───────────────────
        # 当营收同比为负但毛利率同比提升时，说明公司在主动收缩低毛利业务
        if (yoy_r is not None and yoy_r < 0 and gross_margin_val is not None
                and not profit_illusion):
            gm_trend = self._calc_trend(
                financial_data.get("gross_margin", pd.Series(dtype=float)).dropna()
            ) if "gross_margin" in financial_data.columns else "flat"
            # 回退：用 cogs 计算毛利率趋势
            if gm_trend == "flat" and "cogs" in financial_data.columns:
                rev_s = financial_data["revenue"].replace(0, pd.NA)
                gm_s = ((financial_data["revenue"] - financial_data["cogs"]) / rev_s * 100).dropna()
                if len(gm_s) >= 2:
                    gm_trend = self._calc_trend(gm_s)
            if gm_trend == "up":
                indicators.append(
                    IndicatorResult(
                        name="主动收缩低毛利业务",
                        value=round(yoy_r, 2),
                        score=72.0,
                        level=AnalysisLevel.GOOD,
                        trend="up",
                        weight=2.0,
                        period=yoy_period,
                        comment=(
                            f"营收同比{yoy_r:+.1f}%但毛利率趋势向上，"
                            f"主因主动收缩低毛利商贸/贸易业务，非主业萎缩；"
                            f"核心业务盈利质量改善"
                        ),
                    )
                )
                # 下调营收CAGR权重，避免主动收缩误判为衰退
                for ind in indicators:
                    if ind.name == "营收3年CAGR(%)":
                        ind.weight = min(ind.weight, 1.0)
        if yoy_rev is not None and float(yoy_rev) < -15:
            # 主动收缩低毛利业务时不报营收下滑警告
            _is_active_contraction = (
                yoy_r is not None and yoy_r < 0
                and gross_margin_val is not None
                and "gross_margin" in financial_data.columns
            )
            if not _is_active_contraction:
                warnings.append("最新报告期营收同比下滑超15%，需重点关注")

        # 外延式增长（资产注入/重大重组并表）：配置驱动，不再硬编码公司名
        symbol = str(kwargs.get("symbol") or "")
        name = str(kwargs.get("name") or "")
        _asset_inj = _profile.get("asset_injection")
        if _asset_inj:
            _level_map = {
                "excellent": AnalysisLevel.EXCELLENT,
                "good": AnalysisLevel.GOOD,
                "neutral": AnalysisLevel.NEUTRAL,
                "danger": AnalysisLevel.DANGER,
            }
            for _ai in _asset_inj.get("indicators", []):
                indicators.append(
                    IndicatorResult(
                        name=_ai["name"],
                        value=_ai["value"],
                        score=float(_ai["score"]),
                        level=_level_map.get(_ai.get("level", "good"), AnalysisLevel.GOOD),
                        trend=_ai.get("trend", "up"),
                        weight=float(_ai.get("weight", 1.5)),
                        period=_ai.get("period", ""),
                        comment=_ai.get("comment", ""),
                    )
                )
            for _aw in _asset_inj.get("warnings", []):
                warnings.append(_aw)

        # ── 并购并表调整因子：区分「有机增长」与「并表增长」 ──────────────
        # 增速里含并表贡献时，直接按披露同比给成长性高分，会把一次性规模跃升
        # 当成可持续成长。配置了 consolidation_contribution_pct 就折减出有机增速；
        # 未配置则不臆造数字，只做定性标注 + 单独风险提示（可审计）。
        _merger = get_merger_consolidation(name, symbol)
        if _merger and not profit_illusion and yoy_p is not None:
            _target = str(_merger.get("target") or "被并表标的")
            _since = str(_merger.get("since") or "本期")
            _contrib = _merger.get("consolidation_contribution_pct")
            _organic = None
            if _contrib is not None:
                _c = max(0.0, min(90.0, float(_contrib)))
                _organic = round(yoy_p * (1 - _c / 100.0), 2)
                _m_score, _m_level = self._score_by_range(_organic, (15, 300), (0, 15), (-12, 0))
                _m_value = _organic
                _m_comment = (
                    f"并表标的：{_target}（{_since} 起纳入合并）；"
                    f"披露净利同比{yoy_p:+.1f}%，按并表贡献{_c:.0f}%折减后"
                    f"有机增速约{_organic:+.1f}%；成长性按有机增速计分"
                )
            else:
                _m_score, _m_level = 68.0, AnalysisLevel.NEUTRAL
                _m_value = yoy_p
                _m_comment = (
                    f"并表标的：{_target}（{_since} 起纳入合并）；"
                    f"披露净利同比{yoy_p:+.1f}% 中含并表贡献但未拆分，"
                    f"按中性计分，待公司披露有机/并表口径后再校准"
                )
            indicators.append(
                IndicatorResult(
                    name="并购并表调整因子",
                    value=round(float(_m_value), 2),
                    score=_m_score,
                    level=_m_level,
                    trend="flat",
                    weight=1.5,
                    period=f"{_since} 起并表",
                    comment=_m_comment,
                )
            )
            warnings.append(
                f"增速含并表贡献：{_target} 自 {_since} 年起纳入合并报表，"
                f"{'已按并表贡献折减' if _organic is not None else '并表与有机贡献未拆分'}，"
                f"非纯有机增长，需跟踪并表后内生增速与原业务协同"
            )

        # 业务结构转型 / 成长包容度：高毛利 + 利润高增 → 新品类放量窗口加分
        from app.analysis.growth_quality import classify_high_growth_quality

        gm = kwargs.get("latest_gross_margin")
        if gm is None and "gross_margin" in financial_data.columns:
            gms = financial_data["gross_margin"].dropna()
            if len(gms):
                gm = float(gms.iloc[-1])
        elif gm is None and "cogs" in financial_data.columns and financial_data["cogs"].notna().any():
            rev = financial_data["revenue"].replace(0, pd.NA)
            gms = ((financial_data["revenue"] - financial_data["cogs"]) / rev * 100).dropna()
            if len(gms):
                gm = float(gms.iloc[-1])
        hq = classify_high_growth_quality(
            gross_margin_pct=float(gm) if gm is not None else None,
            profit_yoy_pct=float(yoy_profit) if yoy_profit is not None else None,
        )
        business_transform = False
        if hq.get("is_high_growth_quality") and not profit_illusion:
            business_transform = True
            indicators.append(
                IndicatorResult(
                    name="业务结构转型",
                    value=round(float(hq.get("profit_yoy_pct") or 0), 2),
                    score=86.0,
                    level=AnalysisLevel.EXCELLENT,
                    trend="up",
                    weight=2.5,
                    period=yoy_period,
                    comment=(
                        f"高毛利({hq.get('gross_margin_pct')}%)+净利高增"
                        f"({hq.get('profit_yoy_pct')}%)：新品类/品类扩张放量窗口，给予成长包容度"
                    ),
                )
            )

        # ── 修复1：周期位置识别因子 ─────────────────────────────────────
        # 区分"周期底部复苏"vs"周期顶部衰退"：底部复苏给成长溢价，顶部衰退给折价
        # 核心逻辑：CAGR为负但最新同比已转正 + 单季环比强劲 → 底部复苏
        # 注意：is_cycle 可能在边际改善块内被重定义为更窄口径，
        # 这里用独立变量保存宽口径周期判断（含化肥/磷/矿）
        is_cycle_broad = any(k in industry_str for k in cycle_industries)
        cyclical_position = {}
        sq_data = kwargs.get("single_quarter") or {}
        if (is_cycle_broad and cagr_drag and not profit_illusion
                and yoy_p is not None and yoy_p > 0):
            sq_np = sq_data.get("net_profit")
            sq_qoq_v = sq_data.get("qoq_pct")
            sq_yoy_v = sq_data.get("yoy_pct")
            # 单季环比大幅正增长 → 景气复苏初期（非顶部回落）
            if sq_qoq_v is not None and float(sq_qoq_v) > 50:
                cyclical_position = {
                    "phase": "bottom_recovery",
                    "label": "周期底部复苏",
                    "qoq": float(sq_qoq_v),
                    "yoy": float(sq_yoy_v) if sq_yoy_v is not None else yoy_p,
                    "score": 86.0,
                    "bonus": 8,
                }
            elif sq_yoy_v is not None and float(sq_yoy_v) > 10:
                cyclical_position = {
                    "phase": "bottom_recovery",
                    "label": "周期底部复苏",
                    "qoq": float(sq_qoq_v) if sq_qoq_v is not None else None,
                    "yoy": float(sq_yoy_v),
                    "score": 80.0,
                    "bonus": 5,
                }
            elif (sq_qoq_v is not None and float(sq_qoq_v) > 0
                    and sq_yoy_v is not None and float(sq_yoy_v) > 0):
                cyclical_position = {
                    "phase": "early_recovery",
                    "label": "复苏初期",
                    "qoq": float(sq_qoq_v),
                    "yoy": float(sq_yoy_v),
                    "score": 76.0,
                    "bonus": 5,
                }

        if cyclical_position:
            cp = cyclical_position
            qoq_s = f"环比+{cp['qoq']:.0f}%" if cp['qoq'] is not None else ""
            yoy_s = f"同比+{cp['yoy']:.1f}%" if cp['yoy'] is not None else ""
            momentum = "、".join(filter(None, [yoy_s, qoq_s]))
            indicators.append(
                IndicatorResult(
                    name="周期位置识别",
                    value=cp["score"],
                    score=cp["score"],
                    level=AnalysisLevel.GOOD if cp["score"] < 85 else AnalysisLevel.EXCELLENT,
                    trend="up",
                    weight=3.0,
                    period=sq_data.get("label") or yoy_period,
                    comment=(
                        f"{cp['label']}：{momentum}；"
                        f"历史CAGR下行期不代表当前景气位置，"
                        f"单季加速确认底部反转，给予成长溢价+{cp['bonus']}分"
                    ),
                )
            )
            warnings.append(
                f"周期位置：{cp['label']}（{momentum}），"
                f"不宜用下行期CAGR直接判定低成长"
            )

        # ── 修复2：第二曲线弹性因子 ─────────────────────────────────────
        # 评估新业务/第二增长曲线的产能释放与成长贡献
        # 支持 kwargs 传入显式数据，或公司特征自动识别
        second_curve = kwargs.get("second_curve")
        if second_curve is None:
            second_curve = _profile.get("second_curve")

        if second_curve and isinstance(second_curve, dict):
            sc_score = float(second_curve.get("score", 80.0))
            sc_bonus = second_curve.get("bonus", 8)
            sc_status = second_curve.get("status", "")
            status_label = {
                "capacity_ramp": "产能爬坡期",
                "full_production": "满产满销",
                "planning": "规划阶段",
            }.get(sc_status, sc_status)
            indicators.append(
                IndicatorResult(
                    name="第二曲线弹性",
                    value=sc_score,
                    score=sc_score,
                    level=AnalysisLevel.EXCELLENT if sc_score >= 85 else AnalysisLevel.GOOD,
                    trend="up",
                    weight=4.0,
                    period="中期成长窗口",
                    comment=(
                        f"{second_curve.get('name', '新业务')}({status_label})："
                        f"{second_curve.get('detail', '')}；"
                        f"第二曲线明确，成长弹性+{sc_bonus}分"
                    ),
                )
            )
            warnings.append(
                f"第二曲线：{second_curve.get('name', '新业务')}"
                f"{status_label}，关注产能释放节奏与客户导入进度"
            )

        # ── 修复3：资源注入预期因子 ─────────────────────────────────────
        # 量化资源壁垒强化：集团资产注入带来的储量/成本优势跃升
        # 支持 kwargs 传入显式数据，或公司特征自动识别
        resource_injection = kwargs.get("resource_injection")
        if resource_injection is None:
            _ri_profile = _profile.get("resource_injection")
            if _ri_profile:
                # 行业匹配检查（部分资源注入需行业条件才生效）
                _ri_industry_req = _ri_profile.get("industry_required")
                if not _ri_industry_req or any(k in industry_str for k in _ri_industry_req):
                    resource_injection = _ri_profile

        if resource_injection and isinstance(resource_injection, dict):
            ri_score = float(resource_injection.get("score", 78.0))
            ri_bonus = resource_injection.get("bonus", 3)
            reserve = resource_injection.get("reserve_billion_tons")
            reserve_s = f"{reserve}亿吨" if reserve else ""
            indicators.append(
                IndicatorResult(
                    name="资源注入预期",
                    value=ri_score,
                    score=ri_score,
                    level=AnalysisLevel.GOOD if ri_score < 85 else AnalysisLevel.EXCELLENT,
                    trend="up",
                    weight=3.0,
                    period="中长期",
                    comment=(
                        f"{resource_injection.get('name', '资源注入')}"
                        f"({reserve_s})："
                        f"现有{resource_injection.get('existing_reserve', '')}+"
                        f"注入后{resource_injection.get('total_after_injection', '')}，"
                        f"占国内{resource_injection.get('domestic_share', '')}；"
                        f"自采{resource_injection.get('cost_self', '')} vs "
                        f"外购{resource_injection.get('cost_market', '')}，"
                        f"成本优势{resource_injection.get('cost_advantage', '')}；"
                        f"{resource_injection.get('commitment', '')}；"
                        f"资源壁垒强化+{ri_bonus}分"
                    ),
                )
            )
            warnings.append(
                f"资源注入：{resource_injection.get('name', '')}"
                f"({reserve_s})，关注采矿证取得进度与注入时间表"
            )

        module_score = self._weighted_score(indicators)

        # ── 补丁二：基数校验（净利3年CAGR > 50% 且基期亏损/极低 → 成长性降权）──
        # 基期口径必须与 _calc_cagr 实际使用的一致：3年CAGR 用倒数第4期；
        # 跨度不足 4 期时 _calc_cagr 退化为 clean.iloc[0]，此处同步退化。
        _profit_clean = profit.dropna()
        if len(_profit_clean) >= 4:
            _base_np = float(_profit_clean.iloc[-4])
        elif len(_profit_clean) >= 2:
            _base_np = float(_profit_clean.iloc[0])
        else:
            _base_np = None
        base_check = check_growth_base_quality(
            profit_cagr=profit_cagr_3y, base_net_profit=_base_np
        )
        growth_base_penalty = float(base_check["deduction"] or 0.0)

        # ── 补丁二-B：扭亏型伪成长（同比口径，与 CAGR 闸门平行）──────────────
        # 同比的分母是上年同期，上年亏损时同比本身失真。
        turnaround_check = check_turnaround_growth(
            profit_yoy=yoy_profit,
            last_year_net_profit=kwargs.get("last_year_net_profit"),
            deducted_net_profit=deducted,
            last_year_deducted_net_profit=kwargs.get("last_year_deducted_net_profit"),
        )
        turnaround_penalty = float(turnaround_check["deduction"] or 0.0)

        # 两条闸门扣减**取更严者**，不与 profit_illusion 叠加（铁律3）。
        # 关键：扭亏型票在上年亏损时，当期扣非常常也为负 → 必然同时命中
        # profit_illusion（11/32）。若先扣分再压 38 上限，ST西王会掉到 8 分，
        # 属同一事实被惩罚两次。正确做法是三条判据统一成「候选分取最小」。
        _gate_penalty = max(growth_base_penalty, turnaround_penalty)
        _candidates = [module_score]
        if _gate_penalty > 0:
            _candidates.append(module_score - _gate_penalty)
        if profit_illusion:
            _candidates.append(min(module_score, 38.0))
        _final = max(GROWTH_BASE_RAW_FLOOR, min(_candidates))

        growth_base_applied = 0.0
        turnaround_applied = 0.0
        if _final < module_score:
            # 只在「闸门真的比 profit_illusion 更严」时才把差额记到闸门名下，
            # 否则留痕会虚报扣分（铁律：展示值/得分同口径）。
            _gap = round(module_score - _final, 1)
            if _gate_penalty > 0 and growth_base_penalty >= turnaround_penalty:
                growth_base_applied = _gap
            elif _gate_penalty > 0:
                turnaround_applied = _gap
        module_score = _final
        if growth_base_applied > 0:
            warnings.append(base_check["reason"])
        if turnaround_applied > 0:
            warnings.append(turnaround_check["reason"])

        return ModuleResult(
            module_name="成长性",
            score=round(module_score, 1),
            level=score_to_level(module_score),
            indicators=indicators,
            warnings=warnings,
            metadata={
                "v_shape": bool(v_shape),
                "marginal_recovery": bool(marginal_recovery),
                "profit_illusion": bool(profit_illusion),
                "deducted_net_profit": float(deducted) if deducted is not None else None,
                "business_transform": business_transform,
                "growth_quality": hq,
                "cyclical_position": cyclical_position,
                "second_curve": second_curve,
                "resource_injection": resource_injection,
                "growth_base_check": base_check,
                "growth_base_penalty": growth_base_applied,
                "turnaround_check": turnaround_check,
                "turnaround_penalty": turnaround_applied,
            },
        )
