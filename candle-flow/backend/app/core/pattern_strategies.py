from abc import ABC, abstractmethod
from typing import Dict, List, Optional

from app.core.candle import Candle, PatternResult
from app.core.indicators import (
    avg_body,
    calc_atr,
    confirmation_score,
    evaluate_trend,
    is_downtrend,
    is_uptrend,
    position_score,
    score_to_level,
)


def next_close_confirms(candles: List[Candle], index: int, bullish: bool, level: float) -> bool:
    """No next bar → still list the pattern. Next bar exists → close must confirm."""
    if index + 1 >= len(candles):
        return True
    nxt = candles[index + 1]
    return nxt.close > level if bullish else nxt.close < level


class PatternStrategy(ABC):
    @abstractmethod
    def identify(self, candles: List[Candle], index: int) -> Optional[PatternResult]:
        pass

    @abstractmethod
    def window_size(self) -> int:
        pass


class HammerStrategy(PatternStrategy):
    def window_size(self) -> int:
        return 1

    def identify(self, candles: List[Candle], index: int) -> Optional[PatternResult]:
        c = candles[index]
        if c.range == 0:
            return None
        body = c.body
        if body == 0:
            body = c.range * 0.01
        if c.lower_shadow < body * 2.0:
            return None
        if c.upper_shadow > body * 0.5:
            return None
        if body > c.range * 0.3:
            return None
        if not is_downtrend(candles, index):
            return None
        # 尼森：锤子线须下一根收盘高于实体上沿，才当作看涨反转
        confirmed = False
        body_high = max(c.open, c.close)
        if index + 1 < len(candles):
            nxt = candles[index + 1]
            if nxt.close > body_high:
                confirmed = True
            else:
                return None
        base = 40.0
        extra = min(8.0, ((c.lower_shadow / body) - 2.0) / 0.5 * 2.0)
        trend = evaluate_trend(candles, index)
        pos = position_score(candles, index)
        confirm = confirmation_score(candles, index, bullish=True) if confirmed else 6.0
        score = min(100.0, base + extra + trend + pos + confirm)
        return PatternResult(
            "锤子线",
            "bullish",
            score,
            index,
            score_to_level(score),
            details={"needs_confirmation": not confirmed, "confirmed": confirmed},
        )

    def calculate_score(self, candle: Candle, context: Dict) -> float:
        return context.get("score", 0.0)


class HangingManStrategy(PatternStrategy):
    def window_size(self) -> int:
        return 1

    def identify(self, candles: List[Candle], index: int) -> Optional[PatternResult]:
        c = candles[index]
        if c.range == 0:
            return None
        body = c.body or c.range * 0.01
        if c.lower_shadow < body * 2.0 or c.upper_shadow > body * 0.5 or body > c.range * 0.3:
            return None
        if not is_uptrend(candles, index):
            return None
        # 尼森：上吊线必须有下一根收在实体之下的确认，否则不当作反转
        confirmed = False
        if index + 1 < len(candles):
            nxt = candles[index + 1]
            body_low = min(c.open, c.close)
            if nxt.close < body_low:
                confirmed = True
            else:
                return None
        base = 40.0
        trend = evaluate_trend(candles, index)
        pos = position_score(candles, index)
        confirm = confirmation_score(candles, index, bullish=False) if confirmed else 6.0
        score = min(100.0, base + trend + pos + confirm)
        return PatternResult(
            "上吊线",
            "bearish",
            score,
            index,
            score_to_level(score),
            details={"needs_confirmation": not confirmed, "confirmed": confirmed},
        )

    def calculate_score(self, candle: Candle, context: Dict) -> float:
        return context.get("score", 0.0)


class ShootingStarStrategy(PatternStrategy):
    def window_size(self) -> int:
        return 1

    def identify(self, candles: List[Candle], index: int) -> Optional[PatternResult]:
        c = candles[index]
        if c.range == 0:
            return None
        body = c.body or c.range * 0.01
        if c.upper_shadow < body * 2.0 or c.lower_shadow > body * 0.5 or body > c.range * 0.3:
            return None
        if not is_uptrend(candles, index):
            return None
        confirmed = False
        body_low = min(c.open, c.close)
        if index + 1 < len(candles):
            nxt = candles[index + 1]
            if nxt.close < body_low:
                confirmed = True
            else:
                return None
        base = 40.0
        trend = evaluate_trend(candles, index)
        pos = position_score(candles, index)
        confirm = confirmation_score(candles, index, bullish=False) if confirmed else 6.0
        score = min(100.0, base + trend + pos + confirm)
        return PatternResult(
            "流星线",
            "bearish",
            score,
            index,
            score_to_level(score),
            details={"needs_confirmation": not confirmed, "confirmed": confirmed},
        )

    def calculate_score(self, candle: Candle, context: Dict) -> float:
        return context.get("score", 0.0)


class InvertedHammerStrategy(PatternStrategy):
    def window_size(self) -> int:
        return 1

    def identify(self, candles: List[Candle], index: int) -> Optional[PatternResult]:
        c = candles[index]
        if c.range == 0:
            return None
        body = c.body or c.range * 0.01
        if c.upper_shadow < body * 2.0 or c.lower_shadow > body * 0.5 or body > c.range * 0.3:
            return None
        if not is_downtrend(candles, index):
            return None
        confirmed = False
        body_high = max(c.open, c.close)
        if index + 1 < len(candles):
            nxt = candles[index + 1]
            if nxt.close > body_high:
                confirmed = True
            else:
                return None
        base = 40.0
        trend = evaluate_trend(candles, index)
        pos = position_score(candles, index)
        confirm = confirmation_score(candles, index, bullish=True) if confirmed else 6.0
        score = min(100.0, base + trend + pos + confirm)
        return PatternResult(
            "倒锤子线",
            "bullish",
            score,
            index,
            score_to_level(score),
            details={"needs_confirmation": not confirmed, "confirmed": confirmed},
        )

    def calculate_score(self, candle: Candle, context: Dict) -> float:
        return context.get("score", 0.0)


class DojiStrategy(PatternStrategy):
    def window_size(self) -> int:
        return 1

    def identify(self, candles: List[Candle], index: int) -> Optional[PatternResult]:
        c = candles[index]
        if c.range == 0:
            return None
        name = "十字线"
        direction = "neutral"
        if c.body < c.range * 0.05:
            if c.lower_shadow >= c.range * 0.70 and c.upper_shadow < c.range * 0.10:
                name = "蜻蜓十字线"
                direction = "bullish" if is_downtrend(candles, index) else "neutral"
            elif c.upper_shadow >= c.range * 0.70 and c.lower_shadow < c.range * 0.10:
                name = "墓碑十字线"
                direction = "bearish" if is_uptrend(candles, index) else "neutral"
            elif not is_uptrend(candles, index) and not is_downtrend(candles, index):
                return None
        else:
            return None
        score = min(100.0, 35.0 + evaluate_trend(candles, index) + position_score(candles, index))
        return PatternResult(name, direction, score, index, score_to_level(score))

    def calculate_score(self, candle: Candle, context: Dict) -> float:
        return context.get("score", 0.0)


class BullishEngulfingStrategy(PatternStrategy):
    def window_size(self) -> int:
        return 2

    def identify(self, candles: List[Candle], index: int) -> Optional[PatternResult]:
        if index < 1:
            return None
        c1, c2 = candles[index - 1], candles[index]
        if c1.is_bullish or not c2.is_bullish:
            return None
        if not (c2.open <= c1.close and c2.close >= c1.open):
            return None
        if not is_downtrend(candles, index):
            return None
        if not next_close_confirms(candles, index, True, max(c2.open, c2.close)):
            return None
        base = 40.0
        if c2.body > c1.body * 1.5:
            base += 10.0
        vol_bonus = 15.0 if c2.volume > c1.volume * 1.2 else 5.0
        score = min(100.0, base + evaluate_trend(candles, index) + vol_bonus + 10.0)
        return PatternResult("看涨吞没", "bullish", score, index, score_to_level(score))

    def calculate_score(self, candle: Candle, context: Dict) -> float:
        return context.get("score", 0.0)


class BearishEngulfingStrategy(PatternStrategy):
    def window_size(self) -> int:
        return 2

    def identify(self, candles: List[Candle], index: int) -> Optional[PatternResult]:
        if index < 1:
            return None
        c1, c2 = candles[index - 1], candles[index]
        if not c1.is_bullish or c2.is_bullish:
            return None
        if not (c2.open >= c1.close and c2.close <= c1.open):
            return None
        if not is_uptrend(candles, index):
            return None
        if not next_close_confirms(candles, index, False, min(c2.open, c2.close)):
            return None
        base = 40.0
        if c2.body > c1.body * 1.5:
            base += 10.0
        vol_bonus = 15.0 if c2.volume > c1.volume * 1.2 else 5.0
        score = min(100.0, base + evaluate_trend(candles, index) + vol_bonus + 10.0)
        return PatternResult("看跌吞没", "bearish", score, index, score_to_level(score))

    def calculate_score(self, candle: Candle, context: Dict) -> float:
        return context.get("score", 0.0)


class MorningStarStrategy(PatternStrategy):
    def window_size(self) -> int:
        return 3

    def identify(self, candles: List[Candle], index: int) -> Optional[PatternResult]:
        if index < 2:
            return None
        c1, c2, c3 = candles[index - 2], candles[index - 1], candles[index]
        avg_b = avg_body(candles, index - 2, 5)
        if c1.is_bullish or c1.body < avg_b * 1.2:
            return None
        if c2.body >= avg_body(candles, index - 1, 3) * 0.3:
            return None
        if not c3.is_bullish:
            return None
        if c2.high >= min(c1.open, c1.close):
            return None
        penetration = c1.open - c1.close
        if c3.close <= c1.close + penetration * 0.5:
            return None
        if not is_downtrend(candles, index - 2):
            return None
        gap_window = c2.high < c1.low
        score = min(100.0, 50.0 + (10.0 if gap_window else 0.0) + evaluate_trend(candles, index) + 10.0)
        return PatternResult("启明星", "bullish", score, index, score_to_level(score))

    def calculate_score(self, candle: Candle, context: Dict) -> float:
        return context.get("score", 0.0)


class EveningStarStrategy(PatternStrategy):
    def window_size(self) -> int:
        return 3

    def identify(self, candles: List[Candle], index: int) -> Optional[PatternResult]:
        if index < 2:
            return None
        c1, c2, c3 = candles[index - 2], candles[index - 1], candles[index]
        avg_b = avg_body(candles, index - 2, 5)
        if not c1.is_bullish or c1.body < avg_b * 1.2:
            return None
        if c2.body >= avg_body(candles, index - 1, 3) * 0.3:
            return None
        if c3.is_bullish:
            return None
        if c2.low <= max(c1.open, c1.close):
            return None
        penetration = c1.close - c1.open
        if c3.close >= c1.open - penetration * 0.5:
            return None
        if not is_uptrend(candles, index - 2):
            return None
        gap_window = c2.low > c1.high
        score = min(100.0, 50.0 + (10.0 if gap_window else 0.0) + evaluate_trend(candles, index) + 10.0)
        return PatternResult("黄昏星", "bearish", score, index, score_to_level(score))

    def calculate_score(self, candle: Candle, context: Dict) -> float:
        return context.get("score", 0.0)


class ThreeWhiteSoldiersStrategy(PatternStrategy):
    def window_size(self) -> int:
        return 3

    def identify(self, candles: List[Candle], index: int) -> Optional[PatternResult]:
        if index < 2:
            return None
        c1, c2, c3 = candles[index - 2], candles[index - 1], candles[index]
        seq = [c1, c2, c3]
        if not all(c.is_bullish for c in seq):
            return None
        if not (c2.close > c1.close and c3.close > c2.close):
            return None
        if not (c2.body >= c1.body * 0.8 and c3.body >= c2.body * 0.8):
            return None
        # 尼森：第三根收盘接近最高价；若已处高位则属超买警告而非追涨
        near_high = all(c.close >= c.high - c.range * 0.15 or c.range == 0 for c in seq)
        lookback = candles[max(0, index - 19) : index + 1]
        period_high = max(x.high for x in lookback)
        extended = c3.close >= period_high * 0.98
        base = 40.0 + (8.0 if near_high else 0.0)
        if extended:
            base -= 18.0
        score = min(100.0, max(0.0, base + evaluate_trend(candles, index) + 15.0))
        return PatternResult(
            "红三兵",
            "bullish",
            score,
            index,
            score_to_level(score),
            details={"extended": extended, "wait_pullback": extended},
        )

    def calculate_score(self, candle: Candle, context: Dict) -> float:
        return context.get("score", 0.0)


class ThreeBlackCrowsStrategy(PatternStrategy):
    def window_size(self) -> int:
        return 3

    def identify(self, candles: List[Candle], index: int) -> Optional[PatternResult]:
        if index < 2:
            return None
        c1, c2, c3 = candles[index - 2], candles[index - 1], candles[index]
        seq = [c1, c2, c3]
        if any(c.is_bullish for c in seq):
            return None
        if not (c2.close < c1.close and c3.close < c2.close):
            return None
        if not (c2.body >= c1.body * 0.8 and c3.body >= c2.body * 0.8):
            return None
        score = min(100.0, 40.0 + evaluate_trend(candles, index) + 15.0)
        return PatternResult("三只乌鸦", "bearish", score, index, score_to_level(score))

    def calculate_score(self, candle: Candle, context: Dict) -> float:
        return context.get("score", 0.0)


DEFAULT_STRATEGIES: List[PatternStrategy] = [
    HammerStrategy(),
    HangingManStrategy(),
    ShootingStarStrategy(),
    InvertedHammerStrategy(),
    DojiStrategy(),
    BullishEngulfingStrategy(),
    BearishEngulfingStrategy(),
    MorningStarStrategy(),
    EveningStarStrategy(),
    ThreeWhiteSoldiersStrategy(),
    ThreeBlackCrowsStrategy(),
]
