from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from enum import Enum
from typing import Optional

import pandas as pd


class AnalysisLevel(str, Enum):
    EXCELLENT = "A"
    GOOD = "B"
    NEUTRAL = "C"
    POOR = "D"
    DANGER = "E"


@dataclass
class IndicatorResult:
    name: str
    value: float
    score: float
    level: AnalysisLevel
    trend: str = "flat"
    industry_avg: Optional[float] = None
    percentile: Optional[float] = None
    weight: float = 1.0
    comment: str = ""


@dataclass
class ModuleResult:
    module_name: str
    score: float
    level: AnalysisLevel
    indicators: list[IndicatorResult] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    metadata: dict = field(default_factory=dict)


def score_to_level(score: float) -> AnalysisLevel:
    if score >= 85:
        return AnalysisLevel.EXCELLENT
    if score >= 70:
        return AnalysisLevel.GOOD
    if score >= 55:
        return AnalysisLevel.NEUTRAL
    if score >= 35:
        return AnalysisLevel.POOR
    return AnalysisLevel.DANGER


class BaseAnalyzer(ABC):
    def __init__(self, config: dict | None = None):
        self.config = config or {}

    @abstractmethod
    def analyze(self, financial_data: pd.DataFrame, **kwargs) -> ModuleResult:
        pass

    def _calc_trend(self, values: pd.Series, periods: int = 3) -> str:
        clean = values.dropna()
        if len(clean) < periods:
            return "flat"
        recent = clean.iloc[-periods:]
        slope = (recent.iloc[-1] - recent.iloc[0]) / max(periods - 1, 1)
        threshold = abs(recent.mean()) * 0.02 if recent.mean() else 0.01
        if slope > threshold:
            return "up"
        if slope < -threshold:
            return "down"
        return "flat"

    def _score_by_range(
        self,
        value: float,
        excellent: tuple[float, float],
        good: tuple[float, float],
        neutral: tuple[float, float],
    ) -> tuple[float, AnalysisLevel]:
        if excellent[0] <= value <= excellent[1]:
            return 90.0, AnalysisLevel.EXCELLENT
        if good[0] <= value <= good[1]:
            return 75.0, AnalysisLevel.GOOD
        if neutral[0] <= value <= neutral[1]:
            return 55.0, AnalysisLevel.NEUTRAL
        if value < neutral[0]:
            return max(10.0, 40.0 - (neutral[0] - value) * 5), AnalysisLevel.POOR
        return 40.0, AnalysisLevel.NEUTRAL

    def _linear_score(self, value: float, low: float, high: float) -> float:
        if high <= low:
            return 50.0
        return max(0.0, min(100.0, (value - low) / (high - low) * 100))

    def _weighted_score(self, indicators: list[IndicatorResult]) -> float:
        if not indicators:
            return 0.0
        total = sum(i.weight for i in indicators)
        return sum(i.score * i.weight for i in indicators) / total
