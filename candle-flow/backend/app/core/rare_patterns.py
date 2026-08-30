"""Less common Nison patterns: kicker, unique three river, concealing baby swallow,
Tasuki, breakaway, downside gap side-by-side white.
"""

from typing import List, Optional

from app.core.candle import Candle, PatternResult
from app.core.indicators import (
    avg_body,
    evaluate_trend,
    is_downtrend,
    is_uptrend,
    position_score,
    score_to_level,
)
from app.core.pattern_strategies import PatternStrategy


def _near(a: float, b: float, tol: float) -> bool:
    if b == 0:
        return abs(a - b) <= tol
    return abs(a - b) / abs(b) <= tol


def _marubozu_black(c: Candle) -> bool:
    if c.is_bullish or c.range <= 0 or c.body < c.range * 0.78:
        return False
    return c.upper_shadow <= c.range * 0.12 and c.lower_shadow <= c.range * 0.12


class BullishKickerStrategy(PatternStrategy):
    """看涨脱离线：大阴后次日向上跳空长阳，两实体不重叠。"""

    def window_size(self) -> int:
        return 2

    def identify(self, candles: List[Candle], index: int) -> Optional[PatternResult]:
        if index < 1:
            return None
        c1, c2 = candles[index - 1], candles[index]
        avg_b = avg_body(candles, index, 5)
        if c1.is_bullish or not c2.is_bullish:
            return None
        if c1.body < avg_b * 0.9 or c2.body < avg_b * 1.1:
            return None
        if min(c2.open, c2.close) < max(c1.open, c1.close):
            return None
        if not is_downtrend(candles, index - 1):
            return None
        score = min(100.0, 58.0 + evaluate_trend(candles, index) + 8.0)
        return PatternResult("看涨脱离线", "bullish", score, index, score_to_level(score))


class BearishKickerStrategy(PatternStrategy):
    """看跌脱离线：大阳后次日向下跳空长阴，两实体不重叠。"""

    def window_size(self) -> int:
        return 2

    def identify(self, candles: List[Candle], index: int) -> Optional[PatternResult]:
        if index < 1:
            return None
        c1, c2 = candles[index - 1], candles[index]
        avg_b = avg_body(candles, index, 5)
        if not c1.is_bullish or c2.is_bullish:
            return None
        if c1.body < avg_b * 0.9 or c2.body < avg_b * 1.1:
            return None
        if max(c2.open, c2.close) > min(c1.open, c1.close):
            return None
        if not is_uptrend(candles, index - 1):
            return None
        score = min(100.0, 58.0 + evaluate_trend(candles, index) + 8.0)
        return PatternResult("看跌脱离线", "bearish", score, index, score_to_level(score))


class UniqueThreeRiverStrategy(PatternStrategy):
    """独特三川底部：长阴 + 创新低的锤形孕线 + 低于前日收盘的小阳。"""

    def window_size(self) -> int:
        return 3

    def identify(self, candles: List[Candle], index: int) -> Optional[PatternResult]:
        if index < 2:
            return None
        c1, c2, c3 = candles[index - 2], candles[index - 1], candles[index]
        avg_b = avg_body(candles, index - 2, 5)
        if c1.is_bullish or c1.body < avg_b * 1.2:
            return None
        if c2.is_bullish or c2.body <= 0:
            return None
        if c2.low >= c1.low:
            return None
        if c2.lower_shadow < c2.body * 1.5:
            return None
        inner_high = max(c2.open, c2.close)
        inner_low = min(c2.open, c2.close)
        if inner_high >= c1.open or inner_low <= c1.close:
            return None
        if not c3.is_bullish or c3.body > c1.body * 0.45:
            return None
        if c3.close >= c2.close:
            return None
        if not is_downtrend(candles, index - 2):
            return None
        score = min(100.0, 56.0 + evaluate_trend(candles, index) + position_score(candles, index))
        return PatternResult("独特三川底部", "bullish", score, index, score_to_level(score))


class ConcealingBabySwallowStrategy(PatternStrategy):
    """藏婴吞没：两根光脚阴线后，第三根探入前实体、第四根完全包住第三根（含影线）。"""

    def window_size(self) -> int:
        return 4

    def identify(self, candles: List[Candle], index: int) -> Optional[PatternResult]:
        if index < 3:
            return None
        c1, c2, c3, c4 = candles[index - 3], candles[index - 2], candles[index - 1], candles[index]
        if not _marubozu_black(c1) or not _marubozu_black(c2):
            return None
        if c3.is_bullish or c4.is_bullish:
            return None
        if c3.open >= c2.close:
            return None
        if c3.high <= min(c2.open, c2.close):
            return None
        if c3.range == 0 or c3.close > c3.low + c3.range * 0.25:
            return None
        if c4.open < c3.high or c4.close > c3.low:
            return None
        if not is_downtrend(candles, index - 3):
            return None
        score = min(100.0, 60.0 + evaluate_trend(candles, index) + 8.0)
        return PatternResult("藏婴吞没", "bullish", score, index, score_to_level(score))


class UpsideTasukiGapStrategy(PatternStrategy):
    """向上跳空肩带：升势中两阳跳空，第三根阴线回补缺口但未填满。"""

    def window_size(self) -> int:
        return 3

    def identify(self, candles: List[Candle], index: int) -> Optional[PatternResult]:
        if index < 2:
            return None
        c1, c2, c3 = candles[index - 2], candles[index - 1], candles[index]
        if not c1.is_bullish or not c2.is_bullish or c3.is_bullish:
            return None
        if c2.low <= c1.high:
            return None
        body_lo, body_hi = min(c2.open, c2.close), max(c2.open, c2.close)
        if not (body_lo < c3.open < body_hi):
            return None
        if c3.close >= c2.low or c3.close <= c1.high:
            return None
        if not is_uptrend(candles, index - 2):
            return None
        score = min(100.0, 52.0 + evaluate_trend(candles, index) + 8.0)
        return PatternResult("向上跳空肩带", "bullish", score, index, score_to_level(score))


class DownsideTasukiGapStrategy(PatternStrategy):
    """向下跳空肩带：降势中两阴跳空，第三根阳线回补缺口但未填满。"""

    def window_size(self) -> int:
        return 3

    def identify(self, candles: List[Candle], index: int) -> Optional[PatternResult]:
        if index < 2:
            return None
        c1, c2, c3 = candles[index - 2], candles[index - 1], candles[index]
        if c1.is_bullish or c2.is_bullish or not c3.is_bullish:
            return None
        if c2.high >= c1.low:
            return None
        body_lo, body_hi = min(c2.open, c2.close), max(c2.open, c2.close)
        if not (body_lo < c3.open < body_hi):
            return None
        if c3.close <= c2.high or c3.close >= c1.low:
            return None
        if not is_downtrend(candles, index - 2):
            return None
        score = min(100.0, 52.0 + evaluate_trend(candles, index) + 8.0)
        return PatternResult("向下跳空肩带", "bearish", score, index, score_to_level(score))


class BullishBreakawayStrategy(PatternStrategy):
    """看涨突破缺口：长阴跳空下行数根小实体后，长阳收进缺口。"""

    def window_size(self) -> int:
        return 5

    def identify(self, candles: List[Candle], index: int) -> Optional[PatternResult]:
        if index < 4:
            return None
        c0, c1, c2, c3, c4 = (
            candles[index - 4],
            candles[index - 3],
            candles[index - 2],
            candles[index - 1],
            candles[index],
        )
        avg_b = avg_body(candles, index - 4, 5)
        if c0.is_bullish or c0.body < avg_b * 1.15:
            return None
        if c1.high >= c0.low:
            return None
        mids = [c1, c2, c3]
        if any(m.body >= c0.body * 0.7 for m in mids):
            return None
        if min(c3.close, c3.low) >= min(c1.close, c1.low):
            return None
        if not c4.is_bullish or c4.body < avg_b * 1.05:
            return None
        if c4.close <= c1.high or c4.close >= c0.low:
            return None
        if not is_downtrend(candles, index - 4):
            return None
        score = min(100.0, 54.0 + evaluate_trend(candles, index) + 8.0)
        return PatternResult("看涨突破缺口", "bullish", score, index, score_to_level(score))


class BearishBreakawayStrategy(PatternStrategy):
    """看跌突破缺口：长阳跳空上行数根小实体后，长阴收进缺口。"""

    def window_size(self) -> int:
        return 5

    def identify(self, candles: List[Candle], index: int) -> Optional[PatternResult]:
        if index < 4:
            return None
        c0, c1, c2, c3, c4 = (
            candles[index - 4],
            candles[index - 3],
            candles[index - 2],
            candles[index - 1],
            candles[index],
        )
        avg_b = avg_body(candles, index - 4, 5)
        if not c0.is_bullish or c0.body < avg_b * 1.15:
            return None
        if c1.low <= c0.high:
            return None
        mids = [c1, c2, c3]
        if any(m.body >= c0.body * 0.7 for m in mids):
            return None
        if max(c3.close, c3.high) <= max(c1.close, c1.high):
            return None
        if c4.is_bullish or c4.body < avg_b * 1.05:
            return None
        if c4.close >= c1.low or c4.close <= c0.high:
            return None
        if not is_uptrend(candles, index - 4):
            return None
        score = min(100.0, 54.0 + evaluate_trend(candles, index) + 8.0)
        return PatternResult("看跌突破缺口", "bearish", score, index, score_to_level(score))


class DownsideGapSideBySideWhiteStrategy(PatternStrategy):
    """下跌跳空并列阳线：下降窗口后两根相近阳线，空头中继而非反转。"""

    def window_size(self) -> int:
        return 3

    def identify(self, candles: List[Candle], index: int) -> Optional[PatternResult]:
        if index < 2:
            return None
        c0, c1, c2 = candles[index - 2], candles[index - 1], candles[index]
        if not c1.is_bullish or not c2.is_bullish:
            return None
        if c1.high >= c0.low:
            return None
        if not _near(c2.open, c1.open, 0.008):
            return None
        if not _near(c2.body, c1.body, 0.4) and c2.body < c1.body * 0.5:
            return None
        if not is_downtrend(candles, index - 2):
            return None
        score = min(100.0, 50.0 + evaluate_trend(candles, index) + 8.0)
        return PatternResult("下跌跳空并列阳线", "bearish", score, index, score_to_level(score))


RARE_STRATEGIES: List[PatternStrategy] = [
    BullishKickerStrategy(),
    BearishKickerStrategy(),
    UniqueThreeRiverStrategy(),
    ConcealingBabySwallowStrategy(),
    UpsideTasukiGapStrategy(),
    DownsideTasukiGapStrategy(),
    BullishBreakawayStrategy(),
    BearishBreakawayStrategy(),
    DownsideGapSideBySideWhiteStrategy(),
]
