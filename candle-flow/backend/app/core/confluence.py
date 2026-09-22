"""Nison confluence: candlestick + Western tools (MA, MACD, RSI, volume, location)."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Literal, Sequence

from app.core.ma_cross import ma_cross_kind
from app.core.nison_rules import LEFT_SIDE_BULLISH
from app.core.pattern_engine import kline_to_candles
from app.core.timeframe import weekly_trend_at
from app.core.windows import active_windows

MIN_HITS = 2.0

# 正交维度：同一维度内仅保留权重最高的一项
Dimension = Literal["trend", "momentum", "volatility", "volume", "structure"]

DIMENSION_BY_NAME: dict[str, Dimension] = {
    "周线趋势": "trend",
    "均线转多": "trend",
    "均线转空": "trend",
    "金叉": "trend",
    "死叉": "trend",
    "上升趋势线": "trend",
    "下降趋势线": "trend",
    "MACD": "momentum",
    "RSI": "momentum",
    "随机指标": "momentum",
    "RSI背离": "momentum",
    "MACD背离": "momentum",
    "布林": "volatility",
    "波动率拐点": "volatility",
    "放量": "volume",
    "缩量回撤": "volume",
    "均线支撑": "structure",
    "均线阻力": "structure",
    "低点": "structure",
    "高点": "structure",
    "窗口支撑": "structure",
    "窗口阻力": "structure",
    "极性支撑": "structure",
    "极性阻力": "structure",
    "回撤支撑": "structure",
    "回撤阻力": "structure",
    "形态互证": "structure",
}

# 金叉/死叉新鲜度权重
CROSS_WEIGHT_BY_AGE = {0: 1.0, 1: 0.8, 2: 0.8, 3: 0.8, 4: 0.5, 5: 0.5}

# 同维度同权重时的优先级（越大越优先保留）
HIT_PRIORITY: dict[str, int] = {
    "周线趋势": 50,
    "金叉": 48,
    "死叉": 48,
    "上升趋势线": 42,
    "下降趋势线": 42,
    "均线转多": 35,
    "均线转空": 35,
    "MACD背离": 45,
    "RSI背离": 44,
    "MACD": 40,
    "RSI": 38,
    "随机指标": 36,
    "波动率拐点": 48,
    "布林": 40,
    "放量": 45,
    "缩量回撤": 42,
    "形态互证": 46,
}

SoftConflictKind = Literal["emotion_extreme", "structure_flaw", "low_momentum"]


@dataclass
class ConfluenceHit:
    name: str
    detail: str
    weight: float = 1.0
    dimension: str = ""

    def __post_init__(self) -> None:
        if not self.dimension:
            self.dimension = _hit_dimension(self.name)


@dataclass
class SoftConflict:
    kind: SoftConflictKind
    message: str
    position_factor: float = 1.0


@dataclass
class ConfluenceResult:
    hits: list[ConfluenceHit] = field(default_factory=list)
    conflicts: list[str] = field(default_factory=list)
    soft_conflict_items: list[SoftConflict] = field(default_factory=list)
    _candidates: list[ConfluenceHit] = field(default_factory=list, repr=False)
    _penalties: list[ConfluenceHit] = field(default_factory=list, repr=False)
    _finalized: bool = field(default=False, repr=False)

    @property
    def position_factor(self) -> float:
        factors = [sc.position_factor for sc in self.soft_conflict_items if sc.position_factor < 1]
        return min(factors) if factors else 1.0

    @property
    def effective_count(self) -> float:
        self._ensure_finalized()
        return sum(h.weight for h in self.hits)

    @property
    def count(self) -> int:
        """整型汇聚项数（四舍五入），供 DB 与列表展示。"""
        return int(round(self.effective_count))

    @property
    def hard_conflicts(self) -> list[str]:
        return [c for c in self.conflicts if c.startswith("周线") or c.startswith("极端")]

    @property
    def soft_conflicts(self) -> list[str]:
        return [sc.message for sc in self.soft_conflict_items]

    @property
    def blocked(self) -> bool:
        return bool(self.hard_conflicts)

    @property
    def ok(self) -> bool:
        return not self.blocked and self.effective_count >= MIN_HITS

    @property
    def label(self) -> str:
        self._ensure_finalized()
        return ",".join(h.name for h in self.hits)

    @property
    def details_json(self) -> str:
        self._ensure_finalized()
        rows = [
            {
                "name": h.name,
                "detail": h.detail,
                "weight": round(h.weight, 2),
                "dimension": h.dimension,
            }
            for h in self.hits
        ]
        for p in self._penalties:
            rows.append(
                {
                    "name": p.name,
                    "detail": p.detail,
                    "weight": round(-abs(p.weight), 2),
                    "dimension": p.dimension,
                    "penalty": True,
                }
            )
        return json.dumps(rows, ensure_ascii=False)

    def _ensure_finalized(self) -> None:
        if not self._finalized:
            self.finalize()

    def add(self, name: str, detail: str, weight: float = 1.0) -> None:
        if any(h.name == name for h in self._candidates):
            return
        self._candidates.append(
            ConfluenceHit(name, detail, weight=weight, dimension=_hit_dimension(name))
        )
        self._finalized = False

    def add_soft(self, kind: SoftConflictKind, message: str, position_factor: float = 1.0) -> None:
        existing = next((sc for sc in self.soft_conflict_items if sc.message == message), None)
        if existing:
            existing.position_factor = min(existing.position_factor, position_factor)
            return
        self.soft_conflict_items.append(
            SoftConflict(kind, message, position_factor=position_factor)
        )
        if message not in self.conflicts:
            self.conflicts.append(message)

    def add_penalty(self, name: str, detail: str, weight: float = 1.0) -> None:
        if any(h.name == name for h in self._penalties):
            return
        self._penalties.append(ConfluenceHit(name, detail, weight=weight, dimension="momentum"))

    def finalize(self) -> None:
        """按维度正交：每维度仅保留权重×优先级最高的一项。"""
        best: dict[str, ConfluenceHit] = {}
        for hit in self._candidates:
            dim = hit.dimension
            prev = best.get(dim)
            if prev is None or _hit_rank(hit) > _hit_rank(prev):
                best[dim] = hit
        self.hits = sorted(best.values(), key=lambda h: (-h.weight, h.name))
        self._finalized = True


def _hit_dimension(name: str) -> str:
    return DIMENSION_BY_NAME.get(name, "structure")


def _hit_priority(name: str) -> int:
    return HIT_PRIORITY.get(name, 20)


def _hit_rank(hit: ConfluenceHit) -> tuple[float, int]:
    return (hit.weight, _hit_priority(hit.name))


def _close(k) -> float:
    return float(k.close)


def _high(k) -> float:
    return float(k.high)


def _low(k) -> float:
    return float(k.low)


def _vol(k) -> float:
    return float(getattr(k, "volume", 0) or 0)


def _bar_as_of(klines: Sequence, index: int) -> str:
    raw = getattr(klines[index], "date", None)
    if raw is None:
        return ""
    return str(raw)[:10]


def _stamp(as_of: str, text: str) -> str:
    return f"截至 {as_of}：{text}" if as_of else text


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


def _volume_cv(vols: list[float]) -> float | None:
    """近 20 日量能的变异系数（不含今日）。用于判断「几倍量才算异常」。

    量能基线本身越平稳（CV 小），同样倍数越罕见 → 阈值应下调；
    波动越大（CV 大），同倍数越常见 → 阈值应上调。
    """
    base = vols[:-1]
    if len(base) < 10:
        return None
    mean = sum(base) / len(base)
    if mean <= 0:
        return None
    var = sum((v - mean) ** 2 for v in base) / len(base)
    return (var**0.5) / mean


# 量能阈值自适应系数：CV=0.35 视为「常态波动」，对应 k=1.0（即原固定阈值）。
# 限幅 0.85~1.25，避免极端行情把阈值推到不可解释的区间。
_VOL_CV_BASE = 0.35
_VOL_K_MIN = 0.85
_VOL_K_MAX = 1.25
VOL_STRONG_RATIO = 1.5
VOL_MILD_RATIO = 1.2
VOL_DRY_RATIO = 0.85  # 回调段缩量线

# 左侧超跌形态的趋势确认闸门：收盘在 MA60 下方即为「中期趋势仍向下」，
# 此时当日量比（对 20 日均量）低于该线就认定为「无量确认」。
# 与 VOL_MILD_RATIO 同值，但语义独立（那里是「温和放量」的判定线），
# 刻意分成两个名字，避免将来调其中一个时误改另一个。
VOL_CONFIRM_MIN = 1.2
VOL_SHRINK_RATIO = 0.78  # 单日缩量线


def _vol_scale(vols: list[float]) -> float:
    """量能阈值自适应系数 k；CV 不可得时返回 1.0（等于原固定阈值）。"""
    cv = _volume_cv(vols)
    if cv is None:
        return 1.0
    return max(_VOL_K_MIN, min(_VOL_K_MAX, cv / _VOL_CV_BASE))


def _avg_vol_ratio(klines: Sequence, index: int, lookback: int = 20) -> float | None:
    """当日量能 / 前 ``lookback`` 日均量（不含当日）。

    与 `_detect_buy_signal` 的「近5日均量 vs 近20日均量」不是同一口径，这里
    刻意用**单日**量比对基线：左侧抄底防守问的是「今天这根形态K线有没有被
    资金确认」，用 5 日均量会把某一天的天量摊薄掉，反而漏掉真正的放量突破。
    样本不足或基线为 0 时返回 None（调用方按「无量」处理）。
    """
    if index < 2 or index >= len(klines):
        return None
    start = max(0, index - lookback)
    base_slice = klines[start:index]
    if not base_slice:
        return None
    base = sum(_vol(k) for k in base_slice) / len(base_slice)
    if base <= 0:
        return None
    return _vol(klines[index]) / base


def _vol_cv_note(vols: list[float]) -> str:
    """阈值自证文案：让「为什么这次 1.3 倍算放量」可被复核。"""
    cv = _volume_cv(vols)
    if cv is None:
        return ""
    return f"，动态阈值基于量能波动 CV={cv:.2f}"


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


def _boll_bandwidth(closes: list[float], index: int, period: int = 20) -> float | None:
    bb = _bollinger(closes, index, period)
    if bb is None or bb[0] <= 0:
        return None
    mid, upper, lower = bb
    return (upper - lower) / mid


def _atr_at(klines: Sequence, index: int, period: int = 14) -> float | None:
    if index < period:
        return None
    trs: list[float] = []
    for i in range(index - period + 1, index + 1):
        high = _high(klines[i])
        low = _low(klines[i])
        prev_close = _close(klines[i - 1]) if i > 0 else _close(klines[i])
        tr = max(high - low, abs(high - prev_close), abs(low - prev_close))
        trs.append(tr)
    return sum(trs) / len(trs)


def _ma_cross_with_freshness(candles, index: int) -> tuple[str | None, str, float]:
    """Return (kind, detail, weight) with T+0=1.0, T-1~3=0.8, T-4~5=0.5."""
    for back in range(0, 6):
        idx = index - back
        if idx < 20:
            break
        kind, detail = ma_cross_kind(candles, idx)
        if kind:
            weight = CROSS_WEIGHT_BY_AGE.get(back, 0.0)
            if back == 0:
                age_note = "当日"
            elif back <= 3:
                age_note = f"T-{back}"
            else:
                age_note = f"T-{back}（弱化）"
            full_detail = f"{detail or ('均线黄金交叉' if kind == 'golden' else '均线死亡交叉')}（{age_note}）"
            return kind, full_detail, weight
    return None, "", 0.0


def _volatility_confluence(
    klines: Sequence,
    closes: list[float],
    index: int,
    bullish: bool,
) -> tuple[str, str] | None:
    """布林带带宽收窄后扩张 + 价格触轨，或 ATR 压缩后突破。"""
    bb = _bollinger(closes, index)
    if bb is None or index < 25:
        return None
    mid, upper, lower = bb
    close = closes[index]
    bw_today = _boll_bandwidth(closes, index)
    if bw_today is None:
        return None
    bws = [_boll_bandwidth(closes, i) for i in range(index - 4, index)]
    bws = [b for b in bws if b is not None]
    if len(bws) < 3:
        return None
    avg_prev = sum(bws[:-1]) / max(len(bws) - 1, 1)
    narrowing = avg_prev > 0 and bw_today <= avg_prev * 0.98
    expanding = bw_today > bws[-2] if len(bws) >= 2 else False
    atr = _atr_at(klines, index)
    atr_prev = _atr_at(klines, index - 1) if index > 0 else None
    atr_avg = sum(x for x in (_atr_at(klines, i) for i in range(index - 4, index + 1)) if x) / 5
    compressed = atr is not None and atr_avg > 0 and atr <= atr_avg * 0.88
    atr_expanding = atr is not None and atr_prev is not None and atr > atr_prev * 1.05

    if bullish and close <= lower * 1.015 and (narrowing and expanding or compressed and atr_expanding):
        pct = bw_today * 100
        return (
            "波动率拐点",
            f"触及下轨 {_px(lower)}；带宽 {pct:.2f}% "
            + ("收窄后扩张" if narrowing and expanding else "ATR 压缩后抬升"),
        )
    if not bullish and close >= upper * 0.985 and (narrowing and expanding or compressed and atr_expanding):
        pct = bw_today * 100
        return (
            "波动率拐点",
            f"触及上轨 {_px(upper)}；带宽 {pct:.2f}% "
            + ("收窄后向下扩张" if narrowing and expanding else "ATR 压缩后放大"),
        )
    return None


def evaluate_confluence(
    klines: Sequence,
    index: int,
    direction: str,
    pattern_name: str | None = None,
    decision_index: int | None = None,
) -> ConfluenceResult:
    """Western-tool agreement for a bullish/bearish candle signal at `index`.

    ``pattern_name`` 为该信号对应的形态名（``PatternResult.pattern_name``），
    仅供「左侧超跌形态防守」识别形态类别使用；不传时该闸门自动跳过
    （历史调用方与单元测试保持原行为）。

    ``decision_index`` 为「决策日」所在下标，只影响左侧超跌形态防守的判定基准：
    榜单/信号页是**今天**要不要买的决策，用户看到的也是今天的价与量，因此该
    闸门按决策日（扫描层传 ``len(klines)-1``）而非形态发生日判定。缺省 ``None``
    → 退回 ``index``（形态日口径，历史调用方与单测原行为）。
    """
    result = ConfluenceResult()
    if index < 20 or index >= len(klines):
        return result
    bullish = direction == "bullish"
    as_of = _bar_as_of(klines, index)
    closes = [_close(k) for k in klines]
    close = closes[index]
    ma20 = _sma(closes, 20, index)
    ma5 = _sma(closes, 5, index)
    ma10 = _sma(closes, 10, index)
    ma60 = _sma(closes, 60, index)
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
            result.add_soft(
                "structure_flaw",
                f"上影刺破 MA20 {_px(ma20)}，收盘 {_px(close)} 未站上，按收盘价不算突破",
            )
        elif not bullish and wick_false_dn and not crossed_dn:
            result.add_soft(
                "structure_flaw",
                f"下影刺破 MA20 {_px(ma20)}，收盘 {_px(close)} 未跌破，按收盘价不算失守",
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
        # 趋势一致性守卫：标准空头/多头排列里的反向形态 = 接刀/摸顶，不入选。
        # （尼森：逆短线趋势的反转形态需先突破 MA20 确认；此处按收盘口径拦截）
        if bullish and ma5 < ma10 < ma20:
            result.add_soft(
                "structure_flaw",
                f"日线均线空头排列：MA5 {_px(ma5)} < MA10 {_px(ma10)} < MA20 {_px(ma20)}，"
                "看涨形态逆短线趋势，等待放量站回 MA20 再确认",
            )
        elif not bullish and ma5 > ma10 > ma20:
            result.add_soft(
                "structure_flaw",
                f"日线均线多头排列：MA5 {_px(ma5)} > MA10 {_px(ma10)} > MA20 {_px(ma20)}，"
                "看跌形态逆短线趋势，等待收盘跌破 MA20 再确认",
            )

    # ── 左侧超跌形态防守（决策日口径）──────────────────────────────────
    # 上面那条空头排列守卫只拦 MA5<MA10<MA20（短线也还在下行）。
    # 真实缺口在「MA5 已上翘但价格仍在 MA60 下方」：下跌途中的反弹刚起步，
    # 形态分可以打满，共振却是「无量 + 中期趋势向下」，是最典型的下跌中继。
    #
    # 判定基准是**决策日**（decision_index，扫描层传最新一根），不是形态发生日。
    # 这条曾经写错，代价很直接（2026-09-21 线上实测）：形态日通常伴随放量，
    # 按形态日判定等于闸门永不触发 —— 002371 北方华创 09-18 平底锅底部当日
    # 量比 1.48（放行），到 09-21 已缩回 0.87 且仍收在 MA60 724.61 下方，
    # 榜单上却还挂着「买入候选 · 技术 105」。002409 雅克科技同型（1.28 → 0.97）。
    # 用户看的是今天的票，所以按今天的价与量判：左侧形态必须等「放量站上 MA60」
    # 才算右侧确认，无量则按 structure_flaw 淘汰（与空头排列同一处置强度，
    # 不额外叠加惩罚）。
    if bullish and pattern_name in LEFT_SIDE_BULLISH:
        d = decision_index if decision_index is not None else index
        if 0 <= d < len(klines):
            d_close = closes[d]
            d_ma60 = _sma(closes, 60, d)
            if d_ma60 is not None and d_close < d_ma60:
                d_vr = _avg_vol_ratio(klines, d)
                if d_vr is None or d_vr < VOL_CONFIRM_MIN:
                    _vr = f"量比 {d_vr:.2f}" if d_vr is not None else "量比不可算"
                    _when = "" if d == index else f"（形态发生在 {as_of}）"
                    result.add_soft(
                        "structure_flaw",
                        f"左侧超跌形态（{pattern_name}）{_when}：决策日收盘 "
                        f"{_px(d_close)} 仍在 MA60 {_px(d_ma60)} 下方，{_vr} < "
                        f"{VOL_CONFIRM_MIN:g}（无放量确认），中期趋势仍向下，"
                        "等待放量站上 MA60 再确认",
                    )

    candles = kline_to_candles(klines)
    cross_kind, cross_detail, cross_weight = _ma_cross_with_freshness(candles, index)
    if cross_kind == "golden" and bullish and cross_weight > 0:
        result.add("金叉", cross_detail or "均线黄金交叉", weight=cross_weight)
    elif cross_kind == "death" and not bullish and cross_weight > 0:
        result.add("死叉", cross_detail or "均线死亡交叉", weight=cross_weight)

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
        # 只有 DIF 已在 DEA 正确一侧才算动能支持；
        # 「绿柱缩短/红柱回落」未过死叉/金叉前不作为反向证据（空头动能未衰竭≠转多）。
        if bullish and dif >= dea:
            bits = [f"DIF {_px(dif)} 在 DEA {_px(dea)} 之上"]
            if turning_up:
                bits.append(f"柱 {_px(hist)} 较前值 {_px(prev_hist)} 抬升")
            result.add("MACD", _stamp(as_of, "；".join(bits)))
        elif not bullish and dif <= dea:
            bits = [f"DIF {_px(dif)} 在 DEA {_px(dea)} 之下"]
            if turning_down:
                bits.append(f"柱 {_px(hist)} 较前值 {_px(prev_hist)} 回落")
            result.add("MACD", _stamp(as_of, "；".join(bits)))

    rsi = rsi_at(closes, index)
    if rsi is not None:
        # RSI 的语义不是「越超卖越看多」，而是「未超买且方向未破」。
        # 上界从 48 放宽到 60：强势股回调往往不破 50~60，原区间会把整段
        # 强势回调买点漏掉；下界保持 28 —— 超卖(<28)仍不作为做多证据，
        # 反转证据走「RSI背离」（002545 案例结论）。
        # 分档权重：中轴区满权，弱势区 0.6（动能未确认，不足以单独撑起双证）。
        if bullish and 28.0 <= rsi <= 60.0:
            strong_zone = rsi >= 45.0
            zone = (
                f"RSI(14)={rsi:.1f}，45~60 强势回调区（未超买），支持做多"
                if strong_zone
                else f"RSI(14)={rsi:.1f}，28~45 弱势区（未超卖但动能未确认），弱支持做多"
            )
            result.add("RSI", _stamp(as_of, zone), weight=1.0 if strong_zone else 0.6)
        elif not bullish and 40.0 <= rsi <= 72.0:
            strong_zone = rsi <= 55.0
            zone = (
                f"RSI(14)={rsi:.1f}，40~55 空头动能区（未超卖），支持做空"
                if strong_zone
                else f"RSI(14)={rsi:.1f}，55~72 高位钝化区（动能未确认），弱支持做空"
            )
            result.add("RSI", _stamp(as_of, zone), weight=1.0 if strong_zone else 0.6)

    from app.core.oscillators import (
        bearish_divergence,
        bullish_divergence,
        rsi_series,
        stochastic_at,
    )

    stoch = stochastic_at(klines, index)
    if stoch:
        k_val, d_val = stoch
        # 超卖/超买本身不作为证据，需已金叉/死叉（%K 在 %D 正确一侧）才算动能配合
        if bullish and k_val > d_val and k_val < 40:
            result.add(
                "随机指标",
                _stamp(as_of, f"%K={k_val:.1f} %D={d_val:.1f}，低位金叉，支持做多"),
            )
        elif not bullish and k_val < d_val and k_val > 60:
            result.add(
                "随机指标",
                _stamp(as_of, f"%K={k_val:.1f} %D={d_val:.1f}，高位死叉，支持做空"),
            )
        if bullish and k_val >= 90:
            msg = _stamp(
                as_of,
                f"随机指标超买：%K={k_val:.1f} %D={d_val:.1f}，仓位×0.7；等待%K回落至80下方再入场",
            )
            result.add_soft("emotion_extreme", msg, position_factor=0.7)
            result.add_penalty("随机超买", msg)
        elif not bullish and k_val <= 10:
            msg = _stamp(
                as_of,
                f"随机指标超卖：%K={k_val:.1f} %D={d_val:.1f}，仓位×0.7；等待%K反弹至20上方再入场",
            )
            result.add_soft("emotion_extreme", msg, position_factor=0.7)
            result.add_penalty("随机超卖", msg)

    rsi_line = rsi_series(closes)
    if bullish and bullish_divergence(closes, rsi_line, index):
        result.add("RSI背离", "价格创新低而 RSI 未创新低（看涨背离）")
    elif not bullish and bearish_divergence(closes, rsi_line, index):
        result.add("RSI背离", "价格创新高而 RSI 未创新高（看跌背离）")

    if macd:
        # Build MACD hist over a short lookback for divergence (avoid full O(n²))
        hist_line: list[float | None] = [None] * len(closes)
        start_h = max(30, index - 40)
        for i in range(start_h, index + 1):
            m = macd_at(closes, i)
            if m:
                hist_line[i] = m[2]
        if bullish and bullish_divergence(closes, hist_line, index):
            result.add("MACD背离", "价格创新低而 MACD 柱未创新低（看涨背离）")
        elif not bullish and bearish_divergence(closes, hist_line, index):
            result.add("MACD背离", "价格创新高而 MACD 柱未创新高（看跌背离）")

    # Ch.15 volume: breakout thrust 需显著放量。阈值按量能波动率自适应：
    # 量能基线平稳的缩量行情里 1.3~1.4 倍已是有效突破，高波动票则需更大倍数
    # 才算「异常」。CV 不可得时 k=1.0，退回原固定阈值 1.5× / 1.2×。
    vols = [_vol(k) for k in klines[max(0, index - 19) : index + 1]]
    if len(vols) >= 5:
        avg_vol = sum(vols[:-1]) / max(len(vols) - 1, 1)
        today_vol = _vol(klines[index])
        prev_vol = _vol(klines[index - 1]) if index > 0 else 0.0
        k = _vol_scale(vols)
        thr_strong = VOL_STRONG_RATIO * k
        thr_mild = VOL_MILD_RATIO * k
        cv_note = _vol_cv_note(vols)
        if avg_vol > 0:
            ratio = today_vol / avg_vol
            breaking_high = prior and close > prior_high
            breaking_low = prior and close < prior_low
            if ratio >= thr_strong:
                if bullish:
                    result.add(
                        "放量",
                        f"确认放量 {_vol_zh(today_vol)}≈均量 {ratio:.2f} 倍"
                        + ("，收盘越过前高" if breaking_high else "")
                        + f"（阈值 {thr_strong:.2f}×{cv_note}）",
                    )
                else:
                    result.add(
                        "放量",
                        f"确认放量 {_vol_zh(today_vol)}≈均量 {ratio:.2f} 倍"
                        + ("，收盘跌破前低" if breaking_low else "")
                        + f"（阈值 {thr_strong:.2f}×{cv_note}）",
                    )
            elif thr_mild <= ratio < thr_strong:
                result.add_soft(
                    "low_momentum",
                    f"量能一般：约均量 {ratio:.2f} 倍"
                    f"（{thr_mild:.2f}~{thr_strong:.2f}），突破确认偏弱，信号降权",
                )
        # 缩量回撤：近几日量能低于均量后今日转强/转弱（缩量线同样随波动率自适应）
        if avg_vol > 0 and index >= 3:
            dry_line = avg_vol * (VOL_DRY_RATIO / k)
            recent3 = [_vol(klines[i]) for i in range(index - 3, index)]
            dry = sum(1 for v in recent3 if v < dry_line) >= 2
            if bullish and dry and today_vol >= prev_vol and close > float(klines[index].open):
                result.add(
                    "缩量回撤",
                    f"回调段缩量（低于均量），今日量能回升并收阳，量价配合看涨",
                )
            elif not bullish and dry and today_vol >= prev_vol and close < float(klines[index].open):
                result.add(
                    "缩量回撤",
                    f"反弹段缩量（低于均量），今日量能回升并收阴，量价配合看跌",
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
        result.add_soft(
            "structure_flaw",
            f"上影刺破前高 {_px(prior_high)}，收盘 {_px(close)} 未越过，不算突破",
        )
    if prior and not bullish and low < prior_low and close > prior_low:
        result.add_soft(
            "structure_flaw",
            f"下影刺破前低 {_px(prior_low)}，收盘 {_px(close)} 未跌破，不算失守",
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

    vol_hit = _volatility_confluence(klines, closes, index, bullish)
    if vol_hit:
        result.add(vol_hit[0], vol_hit[1])

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

    from app.core.western_levels import (
        polarity_levels,
        retracement_hit,
        trendline_proximity,
    )

    # Ch.11 polarity + trendline
    for role, price, detail in polarity_levels(klines, index):
        near = abs(close - price) / max(price, 1e-9) <= 0.015
        if bullish and role == "support" and near:
            result.add("极性支撑", detail + f"；现价 {_px(close)} 回测该位")
        elif not bullish and role == "resistance" and near:
            result.add("极性阻力", detail + f"；现价 {_px(close)} 回测该位")

    tl = trendline_proximity(klines, index, "bullish" if bullish else "bearish")
    if tl:
        result.add(tl.name, tl.detail)

    # Ch.12 percentage retracements
    rh = retracement_hit(klines, index, "bullish" if bullish else "bearish")
    if rh:
        result.add(rh.name, rh.detail)

    near_high = period_high > 0 and close >= period_high * 0.98
    near_low = period_low > 0 and close <= period_low * 1.02
    if bullish and near_high and rsi is not None and rsi >= 72 and macd and macd[2] > 0 and macd[2] >= macd[3]:
        if rsi >= 85:
            result.conflicts.append(
                f"极端超买硬否决：RSI(14)={rsi:.1f}≥85，高位追涨风险过大"
            )
        else:
            result.add_soft(
                "emotion_extreme",
                f"高位超买追涨：收盘贴近 20 日高点，RSI(14)={rsi:.1f}，MACD 柱仍未回落",
            )
    if not bullish and near_low and rsi is not None and rsi <= 28 and macd and macd[2] < 0 and macd[2] <= macd[3]:
        if rsi <= 15:
            result.conflicts.append(
                f"极端超卖硬否决：RSI(14)={rsi:.1f}≤15，低位杀跌风险过大"
            )
        else:
            result.add_soft(
                "emotion_extreme",
                f"低位超卖杀跌：收盘贴近 20 日低点，RSI(14)={rsi:.1f}，MACD 柱仍未抬升",
            )

    # 缩量反弹/缩量下跌：动能不足，轻度扣分（缩量线同随量能波动率自适应）
    if len(vols) >= 5:
        avg_vol = sum(vols[:-1]) / max(len(vols) - 1, 1)
        today_vol = _vol(klines[index])
        prev_vol = _vol(klines[index - 1]) if index > 0 else 0.0
        shrink_line = avg_vol * (VOL_SHRINK_RATIO / _vol_scale(vols))
        if avg_vol > 0:
            if (
                bullish
                and close > float(klines[index].open)
                and today_vol < shrink_line
                and today_vol <= prev_vol * 0.95
            ):
                result.add_soft(
                    "low_momentum",
                    f"缩量反弹：量 {_vol_zh(today_vol)} 低于均量 {_vol_zh(avg_vol)}，上攻动能偏弱",
                )
            elif (
                not bullish
                and close < float(klines[index].open)
                and today_vol < shrink_line
                and today_vol <= prev_vol * 0.95
            ):
                result.add_soft(
                    "low_momentum",
                    f"缩量下跌：量 {_vol_zh(today_vol)} 低于均量 {_vol_zh(avg_vol)}，下杀动能偏弱",
                )

    result.finalize()
    return result
