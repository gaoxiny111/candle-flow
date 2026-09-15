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
                weight=2.0,
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
                weight=2.0,
                period=cagr_period,
                comment="年报序列复合增速",
            )
        )

        yoy_rev = kwargs.get("revenue_yoy")
        yoy_profit = kwargs.get("profit_yoy")
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
                    weight=3.0,
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
                    weight=3.0,
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

        # V 型反转：历史 CAGR 为负，但最新同比显著转正（含温和复苏）
        # 扣非仍为负时不给 V 型高分，避免「非经常性损益」制造假拐点
        v_shape = (
            not profit_illusion
            and profit_cagr_3y < 0
            and yoy_profit is not None
            and float(yoy_profit) >= 8
            and yoy_rev is not None
            and float(yoy_rev) >= 0
        )
        if v_shape:
            strong = float(yoy_profit) >= 20
            indicators.append(
                IndicatorResult(
                    name="成长拐点",
                    value=round(float(yoy_profit), 2),
                    score=78.0 if strong else 70.0,
                    level=AnalysisLevel.GOOD,
                    trend="up",
                    weight=2.5,
                    period=yoy_period,
                    comment=(
                        "历史复合增速为负但最新同比强劲反弹（V型拐点）"
                        if strong
                        else "历史复合增速为负但最新同比已转正（复苏拐点）"
                    ),
                )
            )
            warnings.append("近3年净利润复合增速为负，但最新报告期已现拐点，需观察持续性")
        elif profit_cagr_3y < 0 and not profit_illusion:
            warnings.append("近3年净利润复合增速为负，盈利能力持续下滑")
        if yoy_rev is not None and float(yoy_rev) < -15:
            warnings.append("最新报告期营收同比下滑超15%，需重点关注")

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
                "profit_illusion": bool(profit_illusion),
                "deducted_net_profit": float(deducted) if deducted is not None else None,
            },
        )
