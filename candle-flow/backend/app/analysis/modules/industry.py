from __future__ import annotations

import pandas as pd

from app.analysis.base import AnalysisLevel, BaseAnalyzer, IndicatorResult, ModuleResult, score_to_level


class IndustryAnalyzer(BaseAnalyzer):
    """与同行业中位数对比（宽松区间，避免误杀周期股）。"""

    def analyze(self, financial_data: pd.DataFrame, **kwargs) -> ModuleResult:
        indicators: list[IndicatorResult] = []
        industry_avg = kwargs.get("industry_avg") or {}
        if not industry_avg:
            return ModuleResult("行业对比", 55, AnalysisLevel.NEUTRAL, warnings=["暂无足够同业样本"])

        roe = kwargs.get("symbol_roe")
        if roe is not None and "roe" in industry_avg:
            diff = float(roe) - float(industry_avg["roe"])
            # 落后 15pct 才到低分，持平附近给中性偏上
            score = self._linear_score(diff, -15, 10)
            indicators.append(
                IndicatorResult(
                    name="ROE vs 行业",
                    value=round(float(roe), 2),
                    score=score,
                    level=score_to_level(score),
                    industry_avg=round(float(industry_avg["roe"]), 2),
                    comment=f"较行业中位数{'高' if diff > 0 else '低'} {abs(diff):.1f} pct",
                    weight=2.0,
                )
            )

        rev_yoy = kwargs.get("revenue_yoy")
        if rev_yoy is not None and "revenue_yoy" in industry_avg:
            diff = float(rev_yoy) - float(industry_avg["revenue_yoy"])
            score = self._linear_score(diff, -20, 25)
            indicators.append(
                IndicatorResult(
                    name="营收增速 vs 行业",
                    value=round(float(rev_yoy), 2),
                    score=score,
                    level=score_to_level(score),
                    industry_avg=round(float(industry_avg["revenue_yoy"]), 2),
                    weight=2.0,
                )
            )

        module_score = self._weighted_score(indicators) if indicators else 55.0
        return ModuleResult(
            module_name="行业对比",
            score=round(module_score, 1),
            level=score_to_level(module_score),
            indicators=indicators,
        )
