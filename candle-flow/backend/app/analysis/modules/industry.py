from __future__ import annotations

import pandas as pd

from app.analysis.base import AnalysisLevel, BaseAnalyzer, IndicatorResult, ModuleResult, score_to_level

# 同业样本不足时行业对比分与 PE 分位均应显示 N/A，避免假精确
MIN_INDUSTRY_PEERS = 5


class IndustryAnalyzer(BaseAnalyzer):
    """与同行业中位数对比（宽松区间，避免误杀周期股）。"""

    def analyze(self, financial_data: pd.DataFrame, **kwargs) -> ModuleResult:
        indicators: list[IndicatorResult] = []
        industry_avg = kwargs.get("industry_avg") or {}
        peer_count = int(float(industry_avg.get("peer_count") or 0))
        if not industry_avg or peer_count < MIN_INDUSTRY_PEERS:
            return ModuleResult(
                module_name="行业对比",
                score=0.0,
                level=AnalysisLevel.NEUTRAL,
                warnings=["N/A（样本不足）"],
                metadata={
                    "insufficient_sample": True,
                    "peer_count": peer_count,
                    "display": "N/A（样本不足）",
                },
            )

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

        if not indicators:
            return ModuleResult(
                module_name="行业对比",
                score=0.0,
                level=AnalysisLevel.NEUTRAL,
                warnings=["N/A（样本不足）"],
                metadata={
                    "insufficient_sample": True,
                    "peer_count": peer_count,
                    "display": "N/A（样本不足）",
                },
            )

        module_score = self._weighted_score(indicators)
        return ModuleResult(
            module_name="行业对比",
            score=round(module_score, 1),
            level=score_to_level(module_score),
            indicators=indicators,
            metadata={"peer_count": peer_count, "insufficient_sample": False},
        )
