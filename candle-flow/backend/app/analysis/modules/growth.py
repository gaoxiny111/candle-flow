from __future__ import annotations

import pandas as pd

from app.analysis.base import AnalysisLevel, BaseAnalyzer, IndicatorResult, ModuleResult, score_to_level


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
            if start <= 0 or end <= 0:
                return 0.0
            return ((end / start) ** (1 / span) - 1) * 100
        start = clean.iloc[-(years + 1)]
        end = clean.iloc[-1]
        if start <= 0 or end <= 0:
            return 0.0
        return ((end / start) ** (1 / years) - 1) * 100

    def analyze(self, financial_data: pd.DataFrame, **kwargs) -> ModuleResult:
        indicators: list[IndicatorResult] = []
        warnings: list[str] = []
        if financial_data.empty or "revenue" not in financial_data:
            return ModuleResult("成长性", 0, AnalysisLevel.DANGER, warnings=["暂无营收数据"])

        revenue = financial_data["revenue"]
        profit = financial_data["net_profit"]

        rev_cagr_3y = self._calc_cagr(revenue, 3)
        # 周期股负增长不直接打到地板：中性区扩到 -15~0
        score, level = self._score_by_range(rev_cagr_3y, (15, 200), (5, 15), (-15, 5))
        indicators.append(
            IndicatorResult(
                name="营收3年CAGR(%)",
                value=round(rev_cagr_3y, 2),
                score=score,
                level=level,
                trend=self._calc_trend(revenue.pct_change(fill_method=None) * 100),
                weight=2.0,
            )
        )

        profit_cagr_3y = self._calc_cagr(profit, 3)
        score2, level2 = self._score_by_range(profit_cagr_3y, (20, 300), (5, 20), (-20, 5))
        indicators.append(
            IndicatorResult(
                name="净利润3年CAGR(%)",
                value=round(profit_cagr_3y, 2),
                score=score2,
                level=level2,
                trend=self._calc_trend(profit.pct_change(fill_method=None) * 100),
                weight=2.0,
            )
        )

        yoy_rev = kwargs.get("revenue_yoy")
        yoy_profit = kwargs.get("profit_yoy")
        # 最新同比权重更高：捕捉边际拐点
        if yoy_rev is not None:
            yoy_val = float(yoy_rev)
            ls = self._linear_score(yoy_val, -20, 40)
            indicators.append(
                IndicatorResult(
                    name="最新报告期营收同比(%)",
                    value=round(yoy_val, 2),
                    score=ls,
                    level=score_to_level(ls),
                    trend="up" if yoy_val > 5 else ("down" if yoy_val < -5 else "flat"),
                    weight=3.0,
                )
            )

        if yoy_profit is not None:
            yp = float(yoy_profit)
            ls2 = self._linear_score(min(yp, 150), -30, 80)
            indicators.append(
                IndicatorResult(
                    name="最新报告期净利同比(%)",
                    value=round(yp, 2),
                    score=ls2,
                    level=score_to_level(ls2),
                    trend="up" if yp > 5 else ("down" if yp < -5 else "flat"),
                    weight=3.0,
                )
            )

        # V 型反转：历史 CAGR 为负，但最新同比显著转正
        v_shape = (
            profit_cagr_3y < 0
            and yoy_profit is not None
            and float(yoy_profit) >= 20
            and yoy_rev is not None
            and float(yoy_rev) >= 0
        )
        if v_shape:
            indicators.append(
                IndicatorResult(
                    name="成长拐点",
                    value=round(float(yoy_profit), 2),
                    score=78.0,
                    level=AnalysisLevel.GOOD,
                    trend="up",
                    weight=2.5,
                    comment="历史复合增速为负但最新同比强劲反弹（V型拐点）",
                )
            )
            warnings.append("近3年净利润复合增速为负，但最新报告期已现拐点，需观察持续性")
        elif profit_cagr_3y < 0:
            warnings.append("近3年净利润复合增速为负，盈利能力持续下滑")
        if yoy_rev is not None and float(yoy_rev) < -15:
            warnings.append("最新报告期营收同比下滑超15%，需重点关注")

        module_score = self._weighted_score(indicators)
        return ModuleResult(
            module_name="成长性",
            score=round(module_score, 1),
            level=score_to_level(module_score),
            indicators=indicators,
            warnings=warnings,
            metadata={"v_shape": v_shape},
        )
