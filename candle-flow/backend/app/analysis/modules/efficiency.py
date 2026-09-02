from __future__ import annotations

import pandas as pd

from app.analysis.base import AnalysisLevel, BaseAnalyzer, IndicatorResult, ModuleResult, score_to_level
from app.analysis.config import CAPITAL_HEAVY_INDUSTRY_KEYWORDS


class EfficiencyAnalyzer(BaseAnalyzer):
    """总资产周转率（按行业类型调整评分，避免误杀重资产股）。"""

    def analyze(self, financial_data: pd.DataFrame, **kwargs) -> ModuleResult:
        indicators: list[IndicatorResult] = []
        if financial_data.empty or "total_assets" not in financial_data:
            return ModuleResult("营运效率", 50, AnalysisLevel.NEUTRAL)

        industry = str(kwargs.get("industry") or "")
        heavy = any(k in industry for k in CAPITAL_HEAVY_INDUSTRY_KEYWORDS)

        fd = financial_data
        ta = fd["total_assets"].replace(0, pd.NA)
        rev = fd["revenue"]
        turnover = (rev / ta).dropna()
        if len(turnover):
            tv = float(turnover.iloc[-1])
            # 重资产：0.15~0.45 为中性偏好；轻资产仍用 0.3~1.2
            if heavy:
                score = self._linear_score(tv, 0.10, 0.50)
                comment = "重资产行业周转率天然偏低，已按行业标准评分"
            else:
                score = self._linear_score(tv, 0.3, 1.2)
                comment = ""
            # 地板抬高到 40，避免单项拖垮观感（本模块不参与综合加权）
            score = max(40.0, score) if heavy else score
            indicators.append(
                IndicatorResult(
                    name="总资产周转率",
                    value=round(tv, 3),
                    score=score,
                    level=score_to_level(score),
                    trend=self._calc_trend(turnover),
                    weight=2.5,
                    comment=comment,
                )
            )

        module_score = self._weighted_score(indicators) if indicators else 50.0
        return ModuleResult(
            module_name="营运效率",
            score=round(module_score, 1),
            level=score_to_level(module_score),
            indicators=indicators,
            metadata={"capital_heavy": heavy},
        )
