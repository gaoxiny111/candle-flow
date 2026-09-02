from __future__ import annotations

import pandas as pd

from app.analysis.base import AnalysisLevel, BaseAnalyzer, IndicatorResult, ModuleResult, score_to_level


class SolvencyAnalyzer(BaseAnalyzer):
    """资产负债率、流动比率、利息保障。"""

    def analyze(self, financial_data: pd.DataFrame, **kwargs) -> ModuleResult:
        indicators: list[IndicatorResult] = []
        warnings: list[str] = []
        debt_ratio = kwargs.get("debt_ratio")
        if debt_ratio is not None:
            dr = float(debt_ratio)
            score, level = self._score_by_range(dr, (0, 40), (40, 55), (55, 70))
            if dr > 75:
                score, level = 25.0, AnalysisLevel.DANGER
                warnings.append(f"资产负债率 {dr:.1f}% 偏高，偿债压力较大")
            indicators.append(
                IndicatorResult(
                    name="资产负债率(%)",
                    value=round(dr, 2),
                    score=score,
                    level=level,
                    weight=3.0,
                    comment="估算值，仅供参考" if kwargs.get("debt_ratio_estimated") else "",
                )
            )

        if not financial_data.empty and "current_ratio" in financial_data.columns:
            cr = float(financial_data["current_ratio"].iloc[-1])
            score2 = self._linear_score(cr, 0.8, 2.5)
            indicators.append(
                IndicatorResult(
                    name="流动比率",
                    value=round(cr, 2),
                    score=score2,
                    level=score_to_level(score2),
                    trend=self._calc_trend(financial_data["current_ratio"]),
                    weight=2.0,
                )
            )

        if not indicators:
            return ModuleResult("偿债能力", 50, AnalysisLevel.NEUTRAL, warnings=["暂无资产负债数据"])

        module_score = self._weighted_score(indicators)
        return ModuleResult(
            module_name="偿债能力",
            score=round(module_score, 1),
            level=score_to_level(module_score),
            indicators=indicators,
            warnings=warnings,
        )
