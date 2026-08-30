"""Nison trading-rule helpers used when converting patterns into orders."""

from typing import Optional, Sequence

# 形态占用的 K 线根数，止损取该窗口内的最低/最高价
PATTERN_BARS: dict[str, int] = {
    "刺透": 2,
    "乌云盖顶": 2,
    "看涨吞没": 2,
    "看跌吞没": 2,
    "看涨孕线": 2,
    "看跌孕线": 2,
    "看涨十字孕线": 2,
    "看跌十字孕线": 2,
    "看涨反击线": 2,
    "看跌反击线": 2,
    "平头底部": 2,
    "平头顶部": 2,
    "上升窗口": 2,
    "下降窗口": 2,
    "启明星": 3,
    "黄昏星": 3,
    "看涨弃婴": 3,
    "看跌弃婴": 3,
    "红三兵": 3,
    "三只乌鸦": 3,
    "上升三法": 5,
    "下降三法": 5,
    "看涨分手线": 2,
    "看跌分手线": 2,
    "跳空并列阳线": 3,
    "跳空并列阴线": 3,
    "两只乌鸦": 3,
    "三星": 3,
    "塔形底部": 5,
    "塔形顶部": 5,
    "前进受阻": 3,
    "停顿形态": 3,
    "升窗回测": 1,
    "降窗回测": 1,
    "看涨脱离线": 2,
    "看跌脱离线": 2,
    "独特三川底部": 3,
    "藏婴吞没": 4,
    "向上跳空肩带": 3,
    "向下跳空肩带": 3,
    "看涨突破缺口": 5,
    "看跌突破缺口": 5,
    "下跌跳空并列阳线": 3,
}

# 均线金叉/死叉不是蜡烛形态，不能单独当买点/卖点
WESTERN_NOT_CANDLES = {"黄金交叉", "死亡交叉", "Golden Cross", "Death Cross"}

# 尼森：这些形态要下一根收盘确认才可当作交易信号
NEEDS_NEXT_CONFIRM = {
    "锤子线",
    "上吊线",
    "倒锤子线",
    "流星线",
    "看涨孕线",
    "看跌孕线",
    "看涨十字孕线",
    "看跌十字孕线",
    "平头底部",
    "平头顶部",
    "看涨吞没",
    "看跌吞没",
    "刺透",
    "乌云盖顶",
}

# 尼森：红三兵若已处高位，应等回调到支撑再买，不可追涨
NO_CHASE = {"红三兵"}

MIN_RISK_REWARD = 1.5


def pattern_bar_count(name: str) -> int:
    return PATTERN_BARS.get(name, 1)


def pattern_stop(klines: Sequence, end_index: int, direction: str, name: str) -> Optional[float]:
    n = pattern_bar_count(name)
    start = max(0, end_index - n + 1)
    window = klines[start : end_index + 1]
    if not window:
        return None
    if direction == "bullish":
        extreme = float(min(k.low for k in window))
        return round(extreme * 0.998, 4)
    extreme = float(max(k.high for k in window))
    return round(extreme * 1.002, 4)


def is_extended_high(klines: Sequence, index: int, lookback: int = 20) -> bool:
    start = max(0, index - lookback + 1)
    window = klines[start : index + 1]
    if not window:
        return False
    period_high = max(float(k.high) for k in window)
    close = float(klines[index].close)
    return period_high > 0 and close >= period_high * 0.98
