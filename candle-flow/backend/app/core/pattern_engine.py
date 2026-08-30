from datetime import datetime
from typing import List

from app.core.candle import Candle, PatternResult
from app.core.nison_patterns import NISON_STRATEGIES
from app.core.pattern_strategies import DEFAULT_STRATEGIES, PatternStrategy
from app.core.rare_patterns import RARE_STRATEGIES


class PatternEngine:
    def __init__(self, strategies: List[PatternStrategy] | None = None, min_score: float = 50.0):
        # 只扫蜡烛形态；金叉/死叉是西方工具，只在汇聚里确认，不当形态触发
        self.strategies = strategies or [
            *DEFAULT_STRATEGIES,
            *NISON_STRATEGIES,
            *RARE_STRATEGIES,
        ]
        self.min_score = min_score

    def scan(self, candles: List[Candle]) -> List[PatternResult]:
        results: List[PatternResult] = []
        seen: set[tuple[str, int]] = set()
        for strategy in self.strategies:
            ws = strategy.window_size()
            for i in range(ws - 1, len(candles)):
                result = strategy.identify(candles, i)
                if result and result.score >= self.min_score:
                    key = (result.pattern_name, result.candle_index)
                    if key not in seen:
                        seen.add(key)
                        results.append(result)
        results.sort(key=lambda r: (r.candle_index, -r.score))
        return results


def kline_to_candles(klines) -> List[Candle]:
    candles = []
    for k in klines:
        ts = k.date if isinstance(k.date, datetime) else datetime.combine(k.date, datetime.min.time())
        candles.append(
            Candle(
                open=float(k.open),
                high=float(k.high),
                low=float(k.low),
                close=float(k.close),
                volume=float(k.volume),
                timestamp=ts,
            )
        )
    return candles
