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
    "十字启明星": 3,
    "十字黄昏星": 3,
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
    "破低反涨": 1,
    "破高反跌": 1,
    "圆形顶部": 12,
    "平底锅底部": 12,
    "三山形态": 25,
    "北方十字": 1,
    "长腿十字线": 1,
    "黄包车夫": 1,
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
    "北方十字",
    "长腿十字线",
    "黄包车夫",
}

# 尼森：红三兵若已处高位，应等回调到支撑再买，不可追涨
NO_CHASE = {"红三兵"}

# 左侧（超跌反转）看涨形态。
# 定义上都是「下跌途中在低位形成的反转」——它们天然出现在均线系统还没转多的时候，
# 属左侧抄底。与之相对的「右侧/延续」形态（红三兵、上升三法、上升窗口、
# 升窗回测、看涨突破缺口、向上跳空肩带、跳空并列阳线、看涨脱离线）
# 本身就把趋势已在当作前提，不需要额外的趋势确认闸门。
# 仅由 PatternEngine 真实产出的 bullish 形态名构成（非同义词扩表）：
# 名单来自 nison_patterns / pattern_strategies / rare_patterns 的 PatternResult。
LEFT_SIDE_BULLISH = frozenset(
    {
        "刺透",
        "看涨吞没",
        "看涨孕线",
        "看涨捉腰带",
        "看涨反击线",
        "平头底部",
        "启明星",
        "十字启明星",
        "看涨弃婴",
        "锤子线",
        "倒锤子线",
        "平底锅底部",
        "破低反涨",
        "塔形底部",
        "独特三川底部",
        "藏婴吞没",
        "看涨分手线",
    }
)

MIN_RISK_REWARD = 1.5
ATR_STOP_MULT = 1.5


def pattern_bar_count(name: str) -> int:
    return PATTERN_BARS.get(name, 1)


def _atr_at(klines: Sequence, index: int, period: int = 14) -> Optional[float]:
    if index < period or index >= len(klines):
        return None
    trs: list[float] = []
    for i in range(index - period + 1, index + 1):
        high = float(klines[i].high)
        low = float(klines[i].low)
        prev_close = float(klines[i - 1].close) if i > 0 else float(klines[i].close)
        tr = max(high - low, abs(high - prev_close), abs(low - prev_close))
        trs.append(tr)
    return sum(trs) / len(trs) if trs else None


def atr_stop(
    klines: Sequence,
    index: int,
    entry: float,
    direction: str,
    mult: float = ATR_STOP_MULT,
) -> Optional[float]:
    """入场价 ± ATR×倍数 的动态风控止损。"""
    atr = _atr_at(klines, index)
    if atr is None or atr <= 0 or entry <= 0:
        return None
    if direction == "bullish":
        return round(entry - mult * atr, 4)
    return round(entry + mult * atr, 4)


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


def pattern_invalidation(
    klines: Sequence,
    end_index: int,
    direction: str,
    name: str,
) -> Optional[float]:
    """形态否定价：收盘穿越即形态失效，应退出观望（可严于风控止损）。

    窗口类优先用缺口边沿；其余取形态窗口极值（不加缓冲）。
    """
    try:
        from app.core.windows import active_windows

        for z in reversed(active_windows(klines, end_index)):
            if direction == "bullish" and z.kind == "rising":
                return round(z.bottom, 4)
            if direction == "bearish" and z.kind == "falling":
                return round(z.top, 4)
    except Exception:
        pass

    n = pattern_bar_count(name)
    start = max(0, end_index - n + 1)
    window = klines[start : end_index + 1]
    if not window:
        return None
    if direction == "bullish":
        return round(float(min(k.low for k in window)), 4)
    return round(float(max(k.high for k in window)), 4)


def resolve_trading_stop(
    klines: Sequence,
    index: int,
    direction: str,
    name: str,
    entry: float,
) -> tuple[Optional[float], str]:
    """形态止损与 ATR×1.5 止损取离入场更近者（更紧风控）。"""
    candidates: list[tuple[float, str]] = []
    p = pattern_stop(klines, index, direction, name)
    a = atr_stop(klines, index, entry, direction)
    if p is not None:
        if direction == "bullish" and p < entry:
            candidates.append((p, "形态止损"))
        elif direction == "bearish" and p > entry:
            candidates.append((p, "形态止损"))
    if a is not None:
        if direction == "bullish" and a < entry:
            candidates.append((a, f"ATR×{ATR_STOP_MULT:g}"))
        elif direction == "bearish" and a > entry:
            candidates.append((a, f"ATR×{ATR_STOP_MULT:g}"))
    if not candidates:
        return p or a, "形态止损" if p is not None else (f"ATR×{ATR_STOP_MULT:g}" if a else "")
    best_price, best_src = min(candidates, key=lambda x: abs(entry - x[0]))
    if len(candidates) > 1:
        other = next(c for c in candidates if c[0] != best_price or c[1] != best_src)
        detail = f"{best_src} {_fmt(best_price)}（较{other[1]} {_fmt(other[0])} 更近入场）"
    else:
        detail = f"{best_src} {_fmt(best_price)}"
    return best_price, detail


def _fmt(v: float) -> str:
    return f"{v:.4f}".rstrip("0").rstrip(".")


def is_extended_high(klines: Sequence, index: int, lookback: int = 20) -> bool:
    start = max(0, index - lookback + 1)
    window = klines[start : index + 1]
    if not window:
        return False
    period_high = max(float(k.high) for k in window)
    close = float(klines[index].close)
    return period_high > 0 and close >= period_high * 0.98
