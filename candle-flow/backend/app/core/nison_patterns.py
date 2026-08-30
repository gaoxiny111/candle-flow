"""Steve Nison Japanese candlestick patterns not covered by the core set.

Rules follow 《日本蜡烛图教程》(Steve Nison / 何平林译):
- Prior trend is required for reversal patterns.
- Piercing / dark-cloud need close beyond the prior body's midpoint.
- Harami is the inside-day cousin of engulfing.
- Belt-hold opens at the extreme of the session.
- Tweezers share matching highs or lows.
- Counterattack (meeting lines) closes at the prior close, not through it.
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
from app.core.pattern_strategies import PatternStrategy, next_close_confirms
from app.core.windows import active_windows


def _near(a: float, b: float, tol: float) -> bool:
    if b == 0:
        return abs(a - b) <= tol
    return abs(a - b) / abs(b) <= tol


class PiercingStrategy(PatternStrategy):
    """刺透：下跌后大阴线，次日低开并收于前日实体中点之上。"""

    def window_size(self) -> int:
        return 2

    def identify(self, candles: List[Candle], index: int) -> Optional[PatternResult]:
        if index < 1:
            return None
        c1, c2 = candles[index - 1], candles[index]
        if c1.is_bullish or not c2.is_bullish:
            return None
        if c1.body <= 0:
            return None
        if c2.open >= c1.low:
            return None
        if c2.close <= c1.body_mid:
            return None
        if c2.close >= c1.open:
            return None
        if not is_downtrend(candles, index):
            return None
        if not next_close_confirms(candles, index, True, max(c2.open, c2.close)):
            return None
        penetration = (c2.close - c1.close) / c1.body
        base = 45.0 + min(15.0, penetration * 20.0)
        score = min(100.0, base + evaluate_trend(candles, index) + position_score(candles, index))
        return PatternResult("刺透", "bullish", score, index, score_to_level(score))


class DarkCloudCoverStrategy(PatternStrategy):
    """乌云盖顶：上涨后大阳线，次日高开并收于前日实体中点之下。"""

    def window_size(self) -> int:
        return 2

    def identify(self, candles: List[Candle], index: int) -> Optional[PatternResult]:
        if index < 1:
            return None
        c1, c2 = candles[index - 1], candles[index]
        if not c1.is_bullish or c2.is_bullish:
            return None
        if c1.body <= 0:
            return None
        if c2.open <= c1.high:
            return None
        if c2.close >= c1.body_mid:
            return None
        if c2.close <= c1.open:
            return None
        if not is_uptrend(candles, index):
            return None
        if not next_close_confirms(candles, index, False, min(c2.open, c2.close)):
            return None
        penetration = (c1.close - c2.close) / c1.body
        base = 45.0 + min(15.0, penetration * 20.0)
        score = min(100.0, base + evaluate_trend(candles, index) + position_score(candles, index))
        return PatternResult("乌云盖顶", "bearish", score, index, score_to_level(score))


class BullishHaramiStrategy(PatternStrategy):
    """看涨孕线：大阴线后包进一根小实体。"""

    def window_size(self) -> int:
        return 2

    def identify(self, candles: List[Candle], index: int) -> Optional[PatternResult]:
        if index < 1:
            return None
        c1, c2 = candles[index - 1], candles[index]
        if c1.is_bullish or c1.body <= 0:
            return None
        inner_high = max(c2.open, c2.close)
        inner_low = min(c2.open, c2.close)
        if not (c2.body < c1.body * 0.6 and inner_high < c1.open and inner_low > c1.close):
            return None
        if not is_downtrend(candles, index):
            return None
        if not next_close_confirms(candles, index, True, max(c2.open, c2.close)):
            return None
        score = min(100.0, 42.0 + evaluate_trend(candles, index) + position_score(candles, index))
        return PatternResult("看涨孕线", "bullish", score, index, score_to_level(score))


class BearishHaramiStrategy(PatternStrategy):
    """看跌孕线：大阳线后包进一根小实体。"""

    def window_size(self) -> int:
        return 2

    def identify(self, candles: List[Candle], index: int) -> Optional[PatternResult]:
        if index < 1:
            return None
        c1, c2 = candles[index - 1], candles[index]
        if not c1.is_bullish or c1.body <= 0:
            return None
        inner_high = max(c2.open, c2.close)
        inner_low = min(c2.open, c2.close)
        if not (c2.body < c1.body * 0.6 and inner_high < c1.close and inner_low > c1.open):
            return None
        if not is_uptrend(candles, index):
            return None
        if not next_close_confirms(candles, index, False, min(c2.open, c2.close)):
            return None
        score = min(100.0, 42.0 + evaluate_trend(candles, index) + position_score(candles, index))
        return PatternResult("看跌孕线", "bearish", score, index, score_to_level(score))


class HaramiCrossStrategy(PatternStrategy):
    """十字孕线：第二根为十字，力度强于普通孕线。"""

    def window_size(self) -> int:
        return 2

    def identify(self, candles: List[Candle], index: int) -> Optional[PatternResult]:
        if index < 1:
            return None
        c1, c2 = candles[index - 1], candles[index]
        if c1.body <= 0 or c1.range == 0:
            return None
        if c2.body > c1.range * 0.05:
            return None
        inner_high = max(c2.open, c2.close)
        inner_low = min(c2.open, c2.close)
        body_high, body_low = max(c1.open, c1.close), min(c1.open, c1.close)
        if not (inner_high <= body_high and inner_low >= body_low):
            return None
        if is_downtrend(candles, index) and not c1.is_bullish:
            direction, name = "bullish", "看涨十字孕线"
        elif is_uptrend(candles, index) and c1.is_bullish:
            direction, name = "bearish", "看跌十字孕线"
        else:
            return None
        level = max(c2.open, c2.close) if direction == "bullish" else min(c2.open, c2.close)
        if not next_close_confirms(candles, index, direction == "bullish", level):
            return None
        score = min(100.0, 50.0 + evaluate_trend(candles, index) + position_score(candles, index))
        return PatternResult(name, direction, score, index, score_to_level(score))


class BullishBeltHoldStrategy(PatternStrategy):
    """看涨捉腰带：下跌后几乎以最低价开盘的长阳线。"""

    def window_size(self) -> int:
        return 1

    def identify(self, candles: List[Candle], index: int) -> Optional[PatternResult]:
        c = candles[index]
        if not c.is_bullish or c.range == 0:
            return None
        avg_b = avg_body(candles, index, 5)
        if c.body < avg_b * 1.2:
            return None
        if c.lower_shadow > c.range * 0.08:
            return None
        if c.body < c.range * 0.6:
            return None
        if not is_downtrend(candles, index):
            return None
        score = min(100.0, 45.0 + evaluate_trend(candles, index) + position_score(candles, index))
        return PatternResult("看涨捉腰带", "bullish", score, index, score_to_level(score))


class BearishBeltHoldStrategy(PatternStrategy):
    """看跌捉腰带：上涨后几乎以最高价开盘的长阴线。"""

    def window_size(self) -> int:
        return 1

    def identify(self, candles: List[Candle], index: int) -> Optional[PatternResult]:
        c = candles[index]
        if c.is_bullish or c.range == 0:
            return None
        avg_b = avg_body(candles, index, 5)
        if c.body < avg_b * 1.2:
            return None
        if c.upper_shadow > c.range * 0.08:
            return None
        if c.body < c.range * 0.6:
            return None
        if not is_uptrend(candles, index):
            return None
        score = min(100.0, 45.0 + evaluate_trend(candles, index) + position_score(candles, index))
        return PatternResult("看跌捉腰带", "bearish", score, index, score_to_level(score))


class BullishCounterattackStrategy(PatternStrategy):
    """看涨反击线：大阴后跳空低开，收在前日收盘附近（未刺透）。"""

    def window_size(self) -> int:
        return 2

    def identify(self, candles: List[Candle], index: int) -> Optional[PatternResult]:
        if index < 1:
            return None
        c1, c2 = candles[index - 1], candles[index]
        if c1.is_bullish or not c2.is_bullish:
            return None
        if c2.open >= c1.close:
            return None
        if not _near(c2.close, c1.close, 0.003):
            return None
        if not is_downtrend(candles, index):
            return None
        score = min(100.0, 40.0 + evaluate_trend(candles, index) + 8.0)
        return PatternResult("看涨反击线", "bullish", score, index, score_to_level(score))


class BearishCounterattackStrategy(PatternStrategy):
    """看跌反击线：大阳后跳空高开，收在前日收盘附近（弱于乌云盖顶）。"""

    def window_size(self) -> int:
        return 2

    def identify(self, candles: List[Candle], index: int) -> Optional[PatternResult]:
        if index < 1:
            return None
        c1, c2 = candles[index - 1], candles[index]
        if not c1.is_bullish or c2.is_bullish:
            return None
        if c2.open <= c1.close:
            return None
        if not _near(c2.close, c1.close, 0.003):
            return None
        if not is_uptrend(candles, index):
            return None
        score = min(100.0, 40.0 + evaluate_trend(candles, index) + 8.0)
        return PatternResult("看跌反击线", "bearish", score, index, score_to_level(score))


class TweezerBottomStrategy(PatternStrategy):
    """平头底部：连续两根相近最低价。"""

    def window_size(self) -> int:
        return 2

    def identify(self, candles: List[Candle], index: int) -> Optional[PatternResult]:
        if index < 1:
            return None
        c1, c2 = candles[index - 1], candles[index]
        if not _near(c1.low, c2.low, 0.002):
            return None
        if not is_downtrend(candles, index):
            return None
        lookback = candles[max(0, index - 19) : index + 1]
        period_low = min(x.low for x in lookback)
        if min(c1.low, c2.low) > period_low * 1.003:
            return None
        if not next_close_confirms(candles, index, True, max(c2.open, c2.close)):
            return None
        score = min(100.0, 42.0 + evaluate_trend(candles, index) + position_score(candles, index))
        return PatternResult("平头底部", "bullish", score, index, score_to_level(score))


class TweezerTopStrategy(PatternStrategy):
    """平头顶部：连续两根相近最高价。"""

    def window_size(self) -> int:
        return 2

    def identify(self, candles: List[Candle], index: int) -> Optional[PatternResult]:
        if index < 1:
            return None
        c1, c2 = candles[index - 1], candles[index]
        if not _near(c1.high, c2.high, 0.002):
            return None
        if not is_uptrend(candles, index):
            return None
        lookback = candles[max(0, index - 19) : index + 1]
        period_high = max(x.high for x in lookback)
        if max(c1.high, c2.high) < period_high * 0.997:
            return None
        if not next_close_confirms(candles, index, False, min(c2.open, c2.close)):
            return None
        score = min(100.0, 42.0 + evaluate_trend(candles, index) + position_score(candles, index))
        return PatternResult("平头顶部", "bearish", score, index, score_to_level(score))


class RisingWindowStrategy(PatternStrategy):
    """上升窗口：跳空高开缺口，持续看涨，缺口下沿为支撑。"""

    def window_size(self) -> int:
        return 2

    def identify(self, candles: List[Candle], index: int) -> Optional[PatternResult]:
        if index < 1:
            return None
        c1, c2 = candles[index - 1], candles[index]
        if c2.low <= c1.high:
            return None
        gap = c2.low - c1.high
        if gap / max(c1.close, 0.01) < 0.003:
            return None
        if not is_uptrend(candles, index):
            return None
        score = min(100.0, 48.0 + evaluate_trend(candles, index))
        return PatternResult(
            "上升窗口",
            "bullish",
            score,
            index,
            score_to_level(score),
            details={"support": c1.high, "window_bottom": c1.high, "window_top": c2.low},
        )


class FallingWindowStrategy(PatternStrategy):
    """下降窗口：跳空低开缺口，持续看跌，缺口上沿为阻力。"""

    def window_size(self) -> int:
        return 2

    def identify(self, candles: List[Candle], index: int) -> Optional[PatternResult]:
        if index < 1:
            return None
        c1, c2 = candles[index - 1], candles[index]
        if c2.high >= c1.low:
            return None
        gap = c1.low - c2.high
        if gap / max(c1.close, 0.01) < 0.003:
            return None
        if not is_downtrend(candles, index):
            return None
        score = min(100.0, 48.0 + evaluate_trend(candles, index))
        return PatternResult(
            "下降窗口",
            "bearish",
            score,
            index,
            score_to_level(score),
            details={"resistance": c1.low, "window_top": c1.low, "window_bottom": c2.high},
        )


class BullishAbandonedBabyStrategy(PatternStrategy):
    """看涨弃婴：启明星的窗口加强版，星线与前后实体均不重叠。"""

    def window_size(self) -> int:
        return 3

    def identify(self, candles: List[Candle], index: int) -> Optional[PatternResult]:
        if index < 2:
            return None
        c1, c2, c3 = candles[index - 2], candles[index - 1], candles[index]
        avg_b = avg_body(candles, index - 2, 5)
        if c1.is_bullish or c1.body < avg_b * 1.1:
            return None
        if c2.body >= avg_body(candles, index - 1, 3) * 0.3:
            return None
        if c2.high >= c1.low:
            return None
        if not c3.is_bullish:
            return None
        if c3.low <= c2.high:
            return None
        penetration = c1.open - c1.close
        if penetration <= 0 or c3.close <= c1.close + penetration * 0.5:
            return None
        if not is_downtrend(candles, index - 2):
            return None
        score = min(100.0, 62.0 + evaluate_trend(candles, index) + 8.0)
        return PatternResult("看涨弃婴", "bullish", score, index, score_to_level(score))


class BearishAbandonedBabyStrategy(PatternStrategy):
    """看跌弃婴：黄昏星的窗口加强版，星线与前后实体均不重叠。"""

    def window_size(self) -> int:
        return 3

    def identify(self, candles: List[Candle], index: int) -> Optional[PatternResult]:
        if index < 2:
            return None
        c1, c2, c3 = candles[index - 2], candles[index - 1], candles[index]
        avg_b = avg_body(candles, index - 2, 5)
        if not c1.is_bullish or c1.body < avg_b * 1.1:
            return None
        if c2.body >= avg_body(candles, index - 1, 3) * 0.3:
            return None
        if c2.low <= c1.high:
            return None
        if c3.is_bullish:
            return None
        if c3.high >= c2.low:
            return None
        penetration = c1.close - c1.open
        if penetration <= 0 or c3.close >= c1.open - penetration * 0.5:
            return None
        if not is_uptrend(candles, index - 2):
            return None
        score = min(100.0, 62.0 + evaluate_trend(candles, index) + 8.0)
        return PatternResult("看跌弃婴", "bearish", score, index, score_to_level(score))


class RisingThreeMethodsStrategy(PatternStrategy):
    """上升三法：长阳后三根小阴回落仍在其区间内，再一根长阳创新高。"""

    def window_size(self) -> int:
        return 5

    def identify(self, candles: List[Candle], index: int) -> Optional[PatternResult]:
        if index < 4:
            return None
        c0, m1, m2, m3, c4 = candles[index - 4], candles[index - 3], candles[index - 2], candles[index - 1], candles[index]
        avg_b = avg_body(candles, index - 4, 5)
        if not c0.is_bullish or c0.body < avg_b * 1.2:
            return None
        mids = [m1, m2, m3]
        if any(m.body >= c0.body * 0.55 for m in mids):
            return None
        if min(m.low for m in mids) < c0.low:
            return None
        if max(m.high for m in mids) > c0.high:
            return None
        if m3.close >= c0.close:
            return None
        if not c4.is_bullish or c4.body < avg_b:
            return None
        if c4.close <= c0.close:
            return None
        if not is_uptrend(candles, index - 4):
            return None
        score = min(100.0, 52.0 + evaluate_trend(candles, index) + 10.0)
        return PatternResult("上升三法", "bullish", score, index, score_to_level(score))


class FallingThreeMethodsStrategy(PatternStrategy):
    """下降三法：长阴后三根小阳反弹仍在其区间内，再一根长阴创新低。"""

    def window_size(self) -> int:
        return 5

    def identify(self, candles: List[Candle], index: int) -> Optional[PatternResult]:
        if index < 4:
            return None
        c0, m1, m2, m3, c4 = candles[index - 4], candles[index - 3], candles[index - 2], candles[index - 1], candles[index]
        avg_b = avg_body(candles, index - 4, 5)
        if c0.is_bullish or c0.body < avg_b * 1.2:
            return None
        mids = [m1, m2, m3]
        if any(m.body >= c0.body * 0.55 for m in mids):
            return None
        if max(m.high for m in mids) > c0.high:
            return None
        if min(m.low for m in mids) < c0.low:
            return None
        if m3.close <= c0.close:
            return None
        if c4.is_bullish or c4.body < avg_b:
            return None
        if c4.close >= c0.close:
            return None
        if not is_downtrend(candles, index - 4):
            return None
        score = min(100.0, 52.0 + evaluate_trend(candles, index) + 10.0)
        return PatternResult("下降三法", "bearish", score, index, score_to_level(score))


class BullishSeparatingLinesStrategy(PatternStrategy):
    """看涨分手线：上升途中阴线后，次日几乎同价开盘的长阳，沿原趋势继续。"""

    def window_size(self) -> int:
        return 2

    def identify(self, candles: List[Candle], index: int) -> Optional[PatternResult]:
        if index < 1:
            return None
        c1, c2 = candles[index - 1], candles[index]
        if c1.is_bullish or not c2.is_bullish:
            return None
        if not _near(c2.open, c1.open, 0.004):
            return None
        if c2.body < avg_body(candles, index, 5) * 1.1:
            return None
        if not is_uptrend(candles, index - 1):
            return None
        score = min(100.0, 48.0 + evaluate_trend(candles, index) + 8.0)
        return PatternResult("看涨分手线", "bullish", score, index, score_to_level(score))


class BearishSeparatingLinesStrategy(PatternStrategy):
    """看跌分手线：下降途中阳线后，次日几乎同价开盘的长阴。"""

    def window_size(self) -> int:
        return 2

    def identify(self, candles: List[Candle], index: int) -> Optional[PatternResult]:
        if index < 1:
            return None
        c1, c2 = candles[index - 1], candles[index]
        if not c1.is_bullish or c2.is_bullish:
            return None
        if not _near(c2.open, c1.open, 0.004):
            return None
        if c2.body < avg_body(candles, index, 5) * 1.1:
            return None
        if not is_downtrend(candles, index - 1):
            return None
        score = min(100.0, 48.0 + evaluate_trend(candles, index) + 8.0)
        return PatternResult("看跌分手线", "bearish", score, index, score_to_level(score))


class SideBySideWhiteStrategy(PatternStrategy):
    """跳空并列阳线：上升窗口后两根相近阳线，窗口继续有效。"""

    def window_size(self) -> int:
        return 3

    def identify(self, candles: List[Candle], index: int) -> Optional[PatternResult]:
        if index < 2:
            return None
        c0, c1, c2 = candles[index - 2], candles[index - 1], candles[index]
        if not c1.is_bullish or not c2.is_bullish:
            return None
        if c1.low <= c0.high:
            return None
        if not _near(c2.open, c1.open, 0.008):
            return None
        if not _near(c2.body, c1.body, 0.35) and c2.body < c1.body * 0.5:
            return None
        if not is_uptrend(candles, index - 2):
            return None
        score = min(100.0, 50.0 + evaluate_trend(candles, index) + 8.0)
        return PatternResult("跳空并列阳线", "bullish", score, index, score_to_level(score))


class SideBySideBlackStrategy(PatternStrategy):
    """跳空并列阴线：下降窗口后两根相近阴线。"""

    def window_size(self) -> int:
        return 3

    def identify(self, candles: List[Candle], index: int) -> Optional[PatternResult]:
        if index < 2:
            return None
        c0, c1, c2 = candles[index - 2], candles[index - 1], candles[index]
        if c1.is_bullish or c2.is_bullish:
            return None
        if c1.high >= c0.low:
            return None
        if not _near(c2.open, c1.open, 0.008):
            return None
        if not is_downtrend(candles, index - 2):
            return None
        score = min(100.0, 50.0 + evaluate_trend(candles, index) + 8.0)
        return PatternResult("跳空并列阴线", "bearish", score, index, score_to_level(score))


class TwoCrowsStrategy(PatternStrategy):
    """两只乌鸦：大阳后跳空阴线，第三根阴线从第二根实体内开盘并收进第一根实体。"""

    def window_size(self) -> int:
        return 3

    def identify(self, candles: List[Candle], index: int) -> Optional[PatternResult]:
        if index < 2:
            return None
        c1, c2, c3 = candles[index - 2], candles[index - 1], candles[index]
        avg_b = avg_body(candles, index - 2, 5)
        if not c1.is_bullish or c1.body < avg_b * 1.2:
            return None
        if c2.is_bullish or c3.is_bullish:
            return None
        if c2.open <= c1.close:
            return None
        if not (min(c2.open, c2.close) < c3.open < max(c2.open, c2.close)):
            return None
        if c3.close >= c1.close or c3.close <= c1.open:
            return None
        if not is_uptrend(candles, index - 2):
            return None
        score = min(100.0, 52.0 + evaluate_trend(candles, index) + 8.0)
        return PatternResult("两只乌鸦", "bearish", score, index, score_to_level(score))


class TriStarStrategy(PatternStrategy):
    """三星：连续三根十字，中间一根与前后脱离，出现在趋势末端。"""

    def window_size(self) -> int:
        return 3

    def identify(self, candles: List[Candle], index: int) -> Optional[PatternResult]:
        if index < 2:
            return None
        c1, c2, c3 = candles[index - 2], candles[index - 1], candles[index]
        if any(x.range == 0 or x.body > x.range * 0.08 for x in (c1, c2, c3)):
            return None
        if is_downtrend(candles, index - 2) and c2.high < min(c1.open, c1.close):
            score = min(100.0, 55.0 + evaluate_trend(candles, index))
            return PatternResult("三星", "bullish", score, index, score_to_level(score))
        if is_uptrend(candles, index - 2) and c2.low > max(c1.open, c1.close):
            score = min(100.0, 55.0 + evaluate_trend(candles, index))
            return PatternResult("三星", "bearish", score, index, score_to_level(score))
        return None


class TowerBottomStrategy(PatternStrategy):
    """塔形底部：长阴后一小段横盘小实体，再一根长阳收复。"""

    def window_size(self) -> int:
        return 5

    def identify(self, candles: List[Candle], index: int) -> Optional[PatternResult]:
        if index < 4:
            return None
        c0, m1, m2, m3, c4 = candles[index - 4], candles[index - 3], candles[index - 2], candles[index - 1], candles[index]
        avg_b = avg_body(candles, index - 4, 5)
        if c0.is_bullish or c0.body < avg_b * 1.3:
            return None
        mids = [m1, m2, m3]
        if any(m.body >= c0.body * 0.6 for m in mids):
            return None
        if not c4.is_bullish or c4.body < avg_b * 1.1:
            return None
        if c4.close <= (c0.open + c0.close) / 2:
            return None
        if not is_downtrend(candles, index - 4):
            return None
        score = min(100.0, 50.0 + evaluate_trend(candles, index) + position_score(candles, index))
        return PatternResult("塔形底部", "bullish", score, index, score_to_level(score))


class TowerTopStrategy(PatternStrategy):
    """塔形顶部：长阳后一小段横盘小实体，再一根长阴压回。"""

    def window_size(self) -> int:
        return 5

    def identify(self, candles: List[Candle], index: int) -> Optional[PatternResult]:
        if index < 4:
            return None
        c0, m1, m2, m3, c4 = candles[index - 4], candles[index - 3], candles[index - 2], candles[index - 1], candles[index]
        avg_b = avg_body(candles, index - 4, 5)
        if not c0.is_bullish or c0.body < avg_b * 1.3:
            return None
        mids = [m1, m2, m3]
        if any(m.body >= c0.body * 0.6 for m in mids):
            return None
        if c4.is_bullish or c4.body < avg_b * 1.1:
            return None
        if c4.close >= (c0.open + c0.close) / 2:
            return None
        if not is_uptrend(candles, index - 4):
            return None
        score = min(100.0, 50.0 + evaluate_trend(candles, index) + position_score(candles, index))
        return PatternResult("塔形顶部", "bearish", score, index, score_to_level(score))


class AdvanceBlockStrategy(PatternStrategy):
    """前进受阻：红三兵变形，实体缩短、上影加长，上涨乏力。"""

    def window_size(self) -> int:
        return 3

    def identify(self, candles: List[Candle], index: int) -> Optional[PatternResult]:
        if index < 2:
            return None
        c1, c2, c3 = candles[index - 2], candles[index - 1], candles[index]
        if not all(c.is_bullish for c in (c1, c2, c3)):
            return None
        if not (c2.close > c1.close and c3.close > c2.close):
            return None
        if not (c2.body <= c1.body * 0.9 and c3.body <= c2.body * 0.9):
            return None
        if c3.range == 0 or c3.upper_shadow < c3.body * 0.5:
            return None
        if not is_uptrend(candles, index):
            return None
        score = min(100.0, 48.0 + evaluate_trend(candles, index) + 8.0)
        return PatternResult("前进受阻", "bearish", score, index, score_to_level(score))


class StalledPatternStrategy(PatternStrategy):
    """停顿形态：两根长阳后一根小实体贴在高位，上涨停顿。"""

    def window_size(self) -> int:
        return 3

    def identify(self, candles: List[Candle], index: int) -> Optional[PatternResult]:
        if index < 2:
            return None
        c1, c2, c3 = candles[index - 2], candles[index - 1], candles[index]
        avg_b = avg_body(candles, index - 2, 5)
        if not c1.is_bullish or not c2.is_bullish:
            return None
        if c1.body < avg_b * 1.1 or c2.body < avg_b * 1.1:
            return None
        if c3.body > min(c1.body, c2.body) * 0.45:
            return None
        if c3.close < c2.close * 0.997:
            return None
        if not is_uptrend(candles, index):
            return None
        score = min(100.0, 48.0 + evaluate_trend(candles, index) + 8.0)
        return PatternResult("停顿形态", "bearish", score, index, score_to_level(score))


class RisingWindowRetestStrategy(PatternStrategy):
    """升窗回测：价格回到未回补升窗区内，收盘仍站在窗口下沿之上。"""

    def window_size(self) -> int:
        return 1

    def identify(self, candles: List[Candle], index: int) -> Optional[PatternResult]:
        if index < 10:
            return None
        c = candles[index]
        for z in active_windows(candles, index):
            if z.kind != "rising":
                continue
            if c.low > z.top:
                continue
            if c.close < z.bottom:
                continue
            score = min(100.0, 54.0 + evaluate_trend(candles, index) + 8.0)
            return PatternResult(
                "升窗回测",
                "bullish",
                score,
                index,
                score_to_level(score),
                details={"window_bottom": z.bottom, "window_top": z.top},
            )
        return None


class FallingWindowRetestStrategy(PatternStrategy):
    """降窗回测：价格回到未回补降窗区内，收盘仍压在窗口上沿之下。"""

    def window_size(self) -> int:
        return 1

    def identify(self, candles: List[Candle], index: int) -> Optional[PatternResult]:
        if index < 10:
            return None
        c = candles[index]
        for z in active_windows(candles, index):
            if z.kind != "falling":
                continue
            in_zone = c.high >= z.bottom and c.close <= z.top
            if not in_zone:
                continue
            if c.close >= z.top:
                continue
            score = min(100.0, 54.0 + evaluate_trend(candles, index) + 8.0)
            return PatternResult(
                "降窗回测",
                "bearish",
                score,
                index,
                score_to_level(score),
                details={"window_bottom": z.bottom, "window_top": z.top},
            )
        return None


NISON_STRATEGIES: List[PatternStrategy] = [
    HaramiCrossStrategy(),
    PiercingStrategy(),
    DarkCloudCoverStrategy(),
    BullishHaramiStrategy(),
    BearishHaramiStrategy(),
    BullishBeltHoldStrategy(),
    BearishBeltHoldStrategy(),
    BullishCounterattackStrategy(),
    BearishCounterattackStrategy(),
    TweezerBottomStrategy(),
    TweezerTopStrategy(),
    RisingWindowStrategy(),
    FallingWindowStrategy(),
    RisingWindowRetestStrategy(),
    FallingWindowRetestStrategy(),
    BullishAbandonedBabyStrategy(),
    BearishAbandonedBabyStrategy(),
    RisingThreeMethodsStrategy(),
    FallingThreeMethodsStrategy(),
    BullishSeparatingLinesStrategy(),
    BearishSeparatingLinesStrategy(),
    SideBySideWhiteStrategy(),
    SideBySideBlackStrategy(),
    TwoCrowsStrategy(),
    TriStarStrategy(),
    TowerBottomStrategy(),
    TowerTopStrategy(),
    AdvanceBlockStrategy(),
    StalledPatternStrategy(),
]
