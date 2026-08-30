from typing import List

from app.core.candle import Candle


def calc_ma(candles: List[Candle], period: int, end_index: int | None = None) -> float:
    if end_index is None:
        end_index = len(candles) - 1
    if end_index < period - 1:
        return candles[end_index].close
    closes = [c.close for c in candles[end_index - period + 1 : end_index + 1]]
    return sum(closes) / len(closes)


def calc_atr(candles: List[Candle], period: int = 14, end_index: int | None = None) -> float:
    if end_index is None:
        end_index = len(candles) - 1
    if end_index < 1:
        return candles[0].range if candles else 0.0
    start = max(1, end_index - period + 1)
    trs = []
    for i in range(start, end_index + 1):
        tr = max(
            candles[i].high - candles[i].low,
            abs(candles[i].high - candles[i - 1].close),
            abs(candles[i].low - candles[i - 1].close),
        )
        trs.append(tr)
    return sum(trs) / len(trs) if trs else 0.0


def avg_body(candles: List[Candle], end_index: int, lookback: int = 5) -> float:
    start = max(0, end_index - lookback + 1)
    bodies = [c.body for c in candles[start : end_index + 1]]
    return sum(bodies) / len(bodies) if bodies else 0.0


def evaluate_trend(candles: List[Candle], end_index: int, ma_periods: list[int] | None = None) -> float:
    ma_periods = ma_periods or [5, 10, 20]
    if end_index < max(ma_periods):
        return 12.0
    mas = {p: calc_ma(candles, p, end_index) for p in ma_periods}
    atr = calc_atr(candles, 14, end_index)
    if mas[5] > mas[10] > mas[20]:
        return 20.0
    if mas[5] < mas[10] < mas[20]:
        return 20.0
    if abs(mas[5] - mas[10]) < atr * 0.2:
        return 8.0
    return 12.0


def _confirmed_pivots(candles: List[Candle], end_index: int, lookback: int = 30, wing: int = 2):
    """Swing highs/lows that already have `wing` bars on both sides."""
    if end_index < wing * 2 + 1:
        return [], []
    start = max(wing, end_index - lookback)
    last = end_index - wing
    highs: list[tuple[int, float]] = []
    lows: list[tuple[int, float]] = []
    for i in range(start, last + 1):
        h = float(candles[i].high)
        l = float(candles[i].low)
        if all(h >= float(candles[j].high) for j in range(i - wing, i + wing + 1)):
            highs.append((i, h))
        if all(l <= float(candles[j].low) for j in range(i - wing, i + wing + 1)):
            lows.append((i, l))
    return highs, lows


def _directional_move(candles: List[Candle], end_index: int, look: int = 15) -> str:
    """Visible rally/decline when pivots are scarce (monotonic runs)."""
    look = min(look, end_index)
    if look < 8:
        return "none"
    start_i = end_index - look
    start_c = float(candles[start_i].close)
    end_c = float(candles[end_index].close)
    if abs(end_c - start_c) / max(abs(start_c), 0.01) < 0.05:
        return "none"
    mid = start_i + look // 2
    first = sum(float(c.close) for c in candles[start_i:mid]) / max(mid - start_i, 1)
    second = sum(float(c.close) for c in candles[mid : end_index + 1]) / max(end_index + 1 - mid, 1)
    if end_c >= start_c * 1.05 and second > first * 1.01:
        return "up"
    if end_c <= start_c * 0.95 and second < first * 0.99:
        return "down"
    return "none"


def is_uptrend(candles: List[Candle], end_index: int) -> bool:
    """Nison: a visible rally (higher highs and higher lows), not just MA alignment."""
    if end_index < 8:
        return False
    highs, lows = _confirmed_pivots(candles, end_index)
    if len(highs) >= 2 and len(lows) >= 2:
        hh = highs[-1][1] > highs[-2][1]
        hl = lows[-1][1] > lows[-2][1]
        lh = highs[-1][1] < highs[-2][1]
        ll = lows[-1][1] < lows[-2][1]
        if hh and hl:
            return True
        if lh and ll:
            return False
    return _directional_move(candles, end_index) == "up"


def is_downtrend(candles: List[Candle], end_index: int) -> bool:
    """Nison: a visible decline (lower highs and lower lows)."""
    if end_index < 8:
        return False
    highs, lows = _confirmed_pivots(candles, end_index)
    if len(highs) >= 2 and len(lows) >= 2:
        hh = highs[-1][1] > highs[-2][1]
        hl = lows[-1][1] > lows[-2][1]
        lh = highs[-1][1] < highs[-2][1]
        ll = lows[-1][1] < lows[-2][1]
        if lh and ll:
            return True
        if hh and hl:
            return False
    return _directional_move(candles, end_index) == "down"


def position_score(candles: List[Candle], end_index: int, lookback: int = 20) -> float:
    start = max(0, end_index - lookback + 1)
    window = candles[start : end_index + 1]
    if not window:
        return 0.0
    lows = [c.low for c in window]
    highs = [c.high for c in window]
    current = candles[end_index]
    min_low = min(lows)
    max_high = max(highs)
    near_low = abs(current.low - min_low) / min_low <= 0.01 if min_low else False
    near_high = abs(current.high - max_high) / max_high <= 0.01 if max_high else False
    if near_low or near_high:
        return 20.0
    return 8.0


def confirmation_score(candles: List[Candle], index: int, bullish: bool) -> float:
    if index + 1 >= len(candles):
        return 10.0
    signal = candles[index]
    nxt = candles[index + 1]
    if bullish:
        if nxt.close > signal.body_mid:
            return 20.0
        if nxt.close > signal.open:
            return 12.0
    else:
        if nxt.close < signal.body_mid:
            return 20.0
        if nxt.close < signal.open:
            return 12.0
    return 0.0


def score_to_level(score: float) -> str:
    if score >= 80:
        return "HIGH"
    if score >= 60:
        return "MEDIUM"
    return "LOW"
