from __future__ import annotations

import pandas as pd

from app.analysis.base import AnalysisLevel, BaseAnalyzer, IndicatorResult, ModuleResult, format_report_period, score_to_level
from app.analysis.config.company_profiles import get_company_profile


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
                        f"= 综合{combined:.0f}分{accel_note}{cyclical_note}"
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
                                ded_note = f"；扣非同比+{float(ded_yoy):.1f}%，主业修复更强"
                            else:
                                ded_note = "；扣非净利同步改善，主业盈利韧性增强"
                    except (TypeError, ValueError):
                        pass
                cycle_industries = ("煤炭", "焦炭", "有色", "钢铁", "化工", "航运", "港口", "开采")
                industry = str(kwargs.get("industry") or "")
                is_cycle = any(k in industry for k in cycle_industries) or _profile.get("cyclical", False)
                if is_cycle:
                    comment = (
                        f"周期回落后出现边际修复：营收同比+{yoy_r:.1f}%、净利同比+{yoy_p:.1f}%"
                        f"{ded_note}；资产注入与多业务板块改善增强盈利韧性"
                        "（非高成长，但是底部修复）"
                    )
                    score_m = 78.0 if yoy_p >= 3 else 74.0
                else:
                    comment = (
                        f"年报CAGR为负，但最新报告期营收同比+{yoy_r:.1f}%、"
                        f"净利同比+{yoy_p:.1f}%，出现边际修复迹象{ded_note}"
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
                # 单季业绩加速（Q2 环比/同比大幅改善 → 修复力度超预期）
                sq = kwargs.get("single_quarter")
                if sq and sq.get("net_profit") is not None:
                    sq_yoy = sq.get("yoy_pct")
                    sq_qoq = sq.get("qoq_pct")
                    if sq_yoy is not None and sq_qoq is not None:
                        # 修复力度超预期 → 加分项
                        if float(sq_yoy) >= 30 or float(sq_qoq) >= 50:
                            sq_score = 86.0
                            sq_comment = (
                                f"Q2单季净利环比+{float(sq_qoq):.0f}%、同比+{float(sq_yoy):.0f}%，"
                                f"修复力度超预期"
                            )
                        elif float(sq_yoy) >= 15 or float(sq_qoq) >= 30:
                            sq_score = 82.0
                            sq_comment = (
                                f"Q2单季净利环比+{float(sq_qoq):.0f}%、同比+{float(sq_yoy):.0f}%，"
                                f"修复力度较强"
                            )
                        else:
                            sq_score = 75.0
                            sq_comment = (
                                f"Q2单季净利环比+{float(sq_qoq):.0f}%、同比+{float(sq_yoy):.0f}%"
                            )
                        # 扣非增速补充
                        ded_yoy = kwargs.get("deducted_yoy_pct")
                        if ded_yoy is not None and float(ded_yoy) > 0:
                            sq_comment += f"；扣非同比+{float(ded_yoy):.1f}%，主业修复更强"
                        indicators.append(
                            IndicatorResult(
                                name="单季业绩加速",
                                value=round(float(sq_yoy), 2),
                                score=sq_score,
                                level=AnalysisLevel.GOOD,
                                trend="up",
                                weight=2.8,
                                period=sq.get("label") or yoy_period,
                                comment=sq_comment,
                            )
                        )
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
        # 扣非否决：成长性强制压至 D 档（≤40）
        if profit_illusion:
            module_score = min(module_score, 38.0)
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
            },
        )
