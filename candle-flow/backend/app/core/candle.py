from dataclasses import dataclass, field
from datetime import datetime
from typing import Any


@dataclass
class Candle:
    open: float
    high: float
    low: float
    close: float
    volume: float
    timestamp: datetime

    @property
    def body(self) -> float:
        return abs(self.close - self.open)

    @property
    def upper_shadow(self) -> float:
        return self.high - max(self.open, self.close)

    @property
    def lower_shadow(self) -> float:
        return min(self.open, self.close) - self.low

    @property
    def range(self) -> float:
        return self.high - self.low

    @property
    def is_bullish(self) -> bool:
        return self.close > self.open

    @property
    def body_mid(self) -> float:
        return (self.open + self.close) / 2


@dataclass
class PatternResult:
    pattern_name: str
    direction: str  # bullish / bearish / neutral
    score: float
    candle_index: int
    confidence_level: str  # HIGH / MEDIUM / LOW
    details: dict[str, Any] = field(default_factory=dict)
