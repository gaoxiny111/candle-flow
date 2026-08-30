"""Nison confluence: candlestick + Western tools (MA, MACD, RSI, volume, location)."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Sequence

from app.core.ma_cross import ma_cross_kind
from app.core.pattern_engine import kline_to_candles
from app.core.timeframe import weekly_trend_at
from app.core.windows import active_windows


MIN_HITS = 2


@dataclass
class ConfluenceHit:
    name: str
    detail: str


@dataclass
class ConfluenceResult:
    hits: list[ConfluenceHit] = field(default_factory=list)
    conflicts: list[str] = field(default_factory=list)

    @property
    def count(self) -> int:
        return len(self.hits)

    @property
    def blocked(self) -> bool:
        return bool(self.conflicts)

    @property
    def ok(self) -> bool:
        return not self.blocked and self.count >= MIN_HITS

    @property
    def label(self) -> str:
        return ",".join(h.name for h in self.hits)

    @property
    def details_json(self) -> str:
        return json.dumps(
            [{"name": h.name, "detail": h.detail} for h in self.hits],
            ensure_ascii=False,
        )

    def add(self, name: str, detail: str) -> None:
        if any(h.name == name for h in self.hits):
            return
        self.hits.append(ConfluenceHit(name, detail))


def _close(k) -> float:
    return float(k.close)


def _high(k) -> float:
    return float(k.high)


def _low(k) -> float:
    return float(k.low)


def _vol(k) -> float:
    return float(getattr(k, "volume", 0) or 0)


def _px(v: float) -> str:
    if abs(v) >= 100:
        return f"{v:.2f}"
    if abs(v) >= 1:
        return f"{v:.3f}".rstrip("0").rstrip(".")
    return f"{v:.4f}"


def _pct(ratio: float) -> str:
    sign = "+" if ratio > 0 else ""
    return f"{sign}{ratio * 100:.2f}%"


def _vol_zh(v: float) -> str:
    if v >= 1e8:
        return f" {v / 1e8:.2f}亿".strip()
    if v >= 1e4:
        return f"{v / 1e4:.1f}万"
    return str(int(round(v)))


def _sma(values: list[float], period: int, end: int) -> float | None:
    if end < period - 1:
        return None
    window = values[end - period + 1 : end + 1]
    return sum(window) / period


def _ema_series(values: list[float], period: int) -> list[float | None]:
    out: list[float | None] = [None] * len(values)
    if len(values) < period:
        return out
    prev = sum(values[:period]) / period
    out[period - 1] = prev
    k = 2 / (period + 1)
    for i in range(period, len(values)):
        prev = values[i] * k + prev * (1 - k)
        out[i] = prev
    return out


def macd_at(closes: list[float], index: int) -> tuple[float, float, float, float] | None:
    if index < 26 + 8:
        return None
    ema12 = _ema_series(closes, 12)
    ema26 = _ema_series(closes, 26)
    dif: list[float] = []
    idx: list[int] = []
    for i, (a, b) in enumerate(zip(ema12, ema26)):
        if a is None or b is None:
            continue
        dif.append(a - b)
        idx.append(i)
    dea = _ema_series(dif, 9)
    if index not in idx:
        return None
    j = idx.index(index)
    if dea[j] is None:
        return None
    hist = 2 * (dif[j] - dea[j])
    prev_hist = 2 * (dif[j - 1] - dea[j - 1]) if j > 0 and dea[j - 1] is not None else hist
    return dif[j], dea[j], hist, prev_hist


def rsi_at(closes: list[float], index: int, period: int = 14) -> float | None:
    if index < period:
        return None
    gains = 0.0
    losses = 0.0
    for i in range(1, period + 1):
        ch = closes[i] - closes[i - 1]
        if ch >= 0:
            gains += ch
        else:
            losses -= ch
    avg_gain = gains / period
    avg_loss = losses / period
    for i in range(period + 1, index + 1):
        ch = closes[i] - closes[i - 1]
        gain = ch if ch > 0 else 0.0
        loss = -ch if ch < 0 else 0.0
        avg_gain = (avg_gain * (period - 1) + gain) / period
        avg_loss = (avg_loss * (period - 1) + loss) / period
    if avg_loss == 0:
        return 100.0
    return 100.0 - 100.0 / (1.0 + avg_gain / avg_loss)


def _bollinger(closes: list[float], index: int, period: int = 20, k: float = 2.0):
    ma = _sma(closes, period, index)
    if ma is None:
        return None
    window = closes[index - period + 1 : index + 1]
    var = sum((x - ma) ** 2 for x in window) / period
    std = var ** 0.5
    return ma, ma + k * std, ma - k * std


def evaluate_confluence(klines: Sequence, index: int, direction: str) -> ConfluenceResult:
    """Western-tool agreement for a bullish/bearish candle signal at `index`."""
    result = ConfluenceResult()
    if index < 20 or index >= len(klines):
        return result
    bullish = direction == "bullish"
    closes = [_close(k) for k in klines]
    close = closes[index]
    ma20 = _sma(closes, 20, index)
    ma5 = _sma(closes, 5, index)
    ma10 = _sma(closes, 10, index)
    lookback = klines[max(0, index - 19) : index + 1]
    prior = klines[max(0, index - 19) : index]
    period_high = max(_high(k) for k in lookback)
    period_low = min(_low(k) for k in lookback)
    prior_high = max(_high(k) for k in prior) if prior else period_high
    prior_low = min(_low(k) for k in prior) if prior else period_low
    high = _high(klines[index])
    low = _low(klines[index])

    if ma20:
        dev = (close - ma20) / ma20 if ma20 else 0.0
        crossed_up = closes[index - 1] < ma20 <= close
        crossed_dn = closes[index - 1] > ma20 >= close
        wick_false_up = high > ma20 > close
        wick_false_dn = low < ma20 < close
        if bullish and wick_false_up and not crossed_up:
            result.conflicts.append(
                f"上影刺破 MA20 {_px(ma20)}，收盘 {_px(close)} 未站上，按收盘价不算突破"
            )
        elif not bullish and wick_false_dn and not crossed_dn:
            result.conflicts.append(
                f"下影刺破 MA20 {_px(ma20)}，收盘 {_px(close)} 未跌破，按收盘价不算失守"
            )
        if bullish and (close >= ma20 * 0.985 or crossed_up) and not wick_false_up:
            if crossed_up:
                detail = f"收盘 {_px(close)} 上穿 MA20 {_px(ma20)}"
            elif close >= ma20:
                detail = f"收盘 {_px(close)} 站上 MA20 {_px(ma20)}，偏离 {_pct(dev)}"
            else:
                detail = f"收盘 {_px(close)} 贴近 MA20 {_px(ma20)} 支撑，偏离 {_pct(dev)}"
            result.add("均线支撑", detail)
        elif not bullish and (close <= ma20 * 1.015 or crossed_dn) and not wick_false_dn:
            if crossed_dn:
                detail = f"收盘 {_px(close)} 跌破 MA20 {_px(ma20)}"
            elif close <= ma20:
                detail = f"收盘 {_px(close)} 压在 MA20 {_px(ma20)} 下方，偏离 {_pct(dev)}"
            else:
                detail = f"收盘 {_px(close)} 贴近 MA20 {_px(ma20)} 阻力，偏离 {_pct(dev)}"
            result.add("均线阻力", detail)

    if ma5 and ma10 and ma20:
        if bullish and ma5 >= ma10:
            result.add(
                "均线转多",
                f"MA5 {_px(ma5)} ≥ MA10 {_px(ma10)}，短线转多（MA20 {_px(ma20)}）",
            )
        elif not bullish and ma5 <= ma10:
            result.add(
                "均线转空",
                f"MA5 {_px(ma5)} ≤ MA10 {_px(ma10)}，短线转空（MA20 {_px(ma20)}）",
            )

    candles = kline_to_candles(klines)
    kind, cross_detail = ma_cross_kind(candles, index)
    recent_kind = kind
    recent_detail = cross_detail
    if recent_kind is None:
        for back in range(1, 4):
            if index - back < 20:
                break
            k2, d2 = ma_cross_kind(candles, index - back)
            if k2:
                recent_kind, recent_detail = k2, d2
                break
    if recent_kind == "golden" and bullish:
        result.add("金叉", recent_detail or "均线黄金交叉")
    elif recent_kind == "death" and not bullish:
        result.add("死叉", recent_detail or "均线死亡交叉")

    weekly = weekly_trend_at(klines, index)
    if weekly == "up" and bullish:
        result.add("周线趋势", "周线波段向上，日线做多与主趋势同向")
    elif weekly == "down" and not bullish:
        result.add("周线趋势", "周线波段向下，日线做空与主趋势同向")
    elif weekly == "down" and bullish:
        result.conflicts.append("周线波段向下，日线做多逆大趋势，按尼森先看周线，不做")
    elif weekly == "up" and not bullish:
        result.conflicts.append("周线波段向上，日线做空逆大趋势，按尼森先看周线，不做")

    macd = macd_at(closes, index)
    if macd:
        dif, dea, hist, prev_hist = macd
        turning_up = hist > prev_hist
        turning_down = hist < prev_hist
        if bullish and (dif >= dea or turning_up):
            bits = []
            if dif >= dea:
                bits.append(f"DIF {_px(dif)} 在 DEA {_px(dea)} 之上")
            else:
                bits.append(f"DIF {_px(dif)} 仍低于 DEA {_px(dea)}")
            if turning_up:
                bits.append(f"柱 {_px(hist)} 较前值 {_px(prev_hist)} 抬升")
            result.add("MACD", "；".join(bits))
        elif not bullish and (dif <= dea or turning_down):
            bits = []
            if dif <= dea:
                bits.append(f"DIF {_px(dif)} 在 DEA {_px(dea)} 之下")
            else:
                bits.append(f"DIF {_px(dif)} 仍高于 DEA {_px(dea)}")
            if turning_down:
                bits.append(f"柱 {_px(hist)} 较前值 {_px(prev_hist)} 回落")
            result.add("MACD", "；".join(bits))

    rsi = rsi_at(closes, index)
    if rsi is not None:
        if bullish and rsi <= 48:
            result.add("RSI", f"RSI(14)={rsi:.1f}，低于 48，未超买，支持做多")
        elif not bullish and rsi >= 52:
            result.add("RSI", f"RSI(14)={rsi:.1f}，高于 52，未超卖，支持做空")

    vols = [_vol(k) for k in klines[max(0, index - 19) : index + 1]]
    if len(vols) >= 5:
        avg_vol = sum(vols[:-1]) / max(len(vols) - 1, 1)
        today_vol = _vol(klines[index])
        if avg_vol > 0 and today_vol >= avg_vol:
            ratio = today_vol / avg_vol
            result.add(
                "放量",
                f"当日量 {_vol_zh(today_vol)}，约为近 20 日均量 {_vol_zh(avg_vol)} 的 {ratio:.2f} 倍",
            )

    if period_low > 0 and bullish and (close - period_low) / period_low <= 0.03:
        dist = (close - period_low) / period_low
        result.add(
            "低点",
            f"收盘 {_px(close)} 距 20 日低点 {_px(period_low)} 仅 {dist * 100:.2f}%",
        )
    if period_high > 0 and not bullish and (period_high - close) / period_high <= 0.03:
        dist = (period_high - close) / period_high
        result.add(
            "高点",
            f"收盘 {_px(close)} 距 20 日高点 {_px(period_high)} 仅 {dist * 100:.2f}%",
        )

    if prior and bullish and high > prior_high and close < prior_high:
        result.conflicts.append(
            f"上影刺破前高 {_px(prior_high)}，收盘 {_px(close)} 未越过，不算突破"
        )
    if prior and not bullish and low < prior_low and close > prior_low:
        result.conflicts.append(
            f"下影刺破前低 {_px(prior_low)}，收盘 {_px(close)} 未跌破，不算失守"
        )

    bb = _bollinger(closes, index)
    if bb:
        mid, upper, lower = bb
        if bullish and close <= lower * 1.01:
            result.add("布林", f"收盘 {_px(close)} 贴近下轨 {_px(lower)}，中轨 {_px(mid)}")
        elif bullish and close >= mid and closes[index - 1] < mid:
            result.add("布林", f"收盘 {_px(close)} 站上中轨 {_px(mid)}")
        elif not bullish and close >= upper * 0.99:
            result.add("布林", f"收盘 {_px(close)} 贴近上轨 {_px(upper)}，中轨 {_px(mid)}")
        elif not bullish and close <= mid and closes[index - 1] > mid:
            result.add("布林", f"收盘 {_px(close)} 跌破中轨 {_px(mid)}")

    for z in active_windows(klines, index):
        in_zone = z.bottom * 0.995 <= close <= z.top * 1.005
        near_rising = z.kind == "rising" and abs(close - z.bottom) / max(z.bottom, 0.01) <= 0.012
        near_falling = z.kind == "falling" and abs(close - z.top) / max(z.top, 0.01) <= 0.012
        if bullish and z.kind == "rising" and (in_zone or near_rising):
            result.add(
                "窗口支撑",
                f"收盘 {_px(close)} 回测未回补升窗 {_px(z.bottom)}–{_px(z.top)}（仅收盘填补才失效）",
            )
        elif not bullish and z.kind == "falling" and (in_zone or near_falling):
            result.add(
                "窗口阻力",
                f"收盘 {_px(close)} 回测未回补降窗 {_px(z.bottom)}–{_px(z.top)}（仅收盘填补才失效）",
            )

    near_high = period_high > 0 and close >= period_high * 0.98
    near_low = period_low > 0 and close <= period_low * 1.02
    if bullish and near_high and rsi is not None and rsi >= 72 and macd and macd[2] > 0 and macd[2] >= macd[3]:
        result.conflicts.append(
            f"高位超买追涨：收盘贴近 20 日高点，RSI(14)={rsi:.1f}，MACD 柱仍未回落"
        )
    if not bullish and near_low and rsi is not None and rsi <= 28 and macd and macd[2] < 0 and macd[2] <= macd[3]:
        result.conflicts.append(
            f"低位超卖杀跌：收盘贴近 20 日低点，RSI(14)={rsi:.1f}，MACD 柱仍未抬升"
        )

    return result
