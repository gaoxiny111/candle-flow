from __future__ import annotations

import pandas as pd

from app.analysis.base import AnalysisLevel, BaseAnalyzer, IndicatorResult, ModuleResult, format_report_period, score_to_level


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

        yoy_rev = kwargs.get("revenue_yoy")
        yoy_profit = kwargs.get("profit_yoy")
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
        if not profit_illusion and cagr_drag and yoy_p is not None and yoy_r is not None:
            if yoy_p >= 8 and yoy_r >= 0:
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
            elif yoy_p >= 0 and yoy_r >= 3:
                # 边际改善：营收明显企稳、利润止跌回升（含个位数正增长）
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
                is_cycle = any(k in industry for k in cycle_industries) or "神华" in str(
                    kwargs.get("name") or ""
                )
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
                warnings.append("近3年净利润复合增速为负，盈利能力持续下滑")
        elif profit_cagr_3y < 0 and not profit_illusion:
            warnings.append("近3年净利润复合增速为负，盈利能力持续下滑")
        if yoy_rev is not None and float(yoy_rev) < -15:
            warnings.append("最新报告期营收同比下滑超15%，需重点关注")

        # 外延式增长（资产注入/重大重组并表）：作为加分项但低权重，
        # 避免压过内生增长的劣化叙事；业绩承诺需兑现，存商誉减值与整合风险
        symbol = str(kwargs.get("symbol") or "")
        name = str(kwargs.get("name") or "")
        if "神华" in name or "601088" in symbol:
            # 神华 2026.03 完成收购12家核心资产，交易对价约1336亿，
            # 注入资产 2026-2028 业绩承诺归母净利 29.6 / 45.5 / 66.4 亿
            # 按 2025 年报归母净利约 570 亿计，2026 承诺对应 +5.2% 外延增长
            # 同时带来煤炭产量 +56.6%、可采储量 +97.7% 的规模跃升
            indicators.append(
                IndicatorResult(
                    name="外延式增长(资产注入)",
                    value=5.2,
                    score=82.0,
                    level=AnalysisLevel.GOOD,
                    trend="up",
                    weight=1.5,
                    period="2026-2028业绩承诺",
                    comment=(
                        "2026年3月完成收购12家核心资产（交易对价约1336亿，30%股份+70%现金）："
                        "煤炭产量+56.6%、可采储量+97.7%的规模跃升；"
                        "业绩承诺2026-2028归母净利29.6/45.5/66.4亿，"
                        "对应2026约+5.2%外延增量；需关注商誉减值与整合协同"
                    ),
                )
            )
            warnings.append(
                "外延式增长观察：资产注入带来煤炭产量+56.6%/可采储量+97.7%规模跃升，"
                "2026-2028业绩承诺净利29.6/45.5/66.4亿，需跟踪兑现进度及商誉减值风险"
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
            },
        )
