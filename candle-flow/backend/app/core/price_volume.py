"""价量策略引擎：趋势确认（3 个）+ 反转捕捉（2 个）。

**只读**：本模块不写库、不改写任何分数（``composite_score`` / ``qv_score`` /
技术面得分均不受影响），只把「价格与成交量的配合关系」如实算出来供展示与筛选。

## 口径与坑（实现时逐条规避，勿简化回去）

1. **前复权**：价格必须前复权（本项目 kline 源统一 ``qfq``），否则 N 日新高、
   均线、回撤全部失真。
2. **均量基线一律 ``shift(1)``（不含当日）**：若把当日量算进均量，放量当日的
   分母被自身抬高，放大倍数被系统性低估（常见伪代码写法 ``rolling(M).mean()``
   就是含当日的，会漏掉一半以上的放量信号）。
3. **突破用「昨日为止的 N 日最高」**，避免「自己突破自己」。
4. **「长上影」判据不可写成 ``(high-close) > 2*(close-open)``**：阴线时右式为负，
   任何阴线都会命中。改用实体绝对值 ``upper_shadow > 2*body`` 且
   ``upper_shadow > 0.5*range``。
5. **量纲**：``volume`` 单位是「手」，跨票不可直接比大小 → 一律用「相对自身均量」
   的倍数 / 标准化偏离。
6. **T+1**：t 日收盘出信号 ⇒ **t+1 开盘**才能成交（``exec_hint``）；一字涨停买不进、
   一字跌停卖不出，须标记 ``blocked``。日频信号当日的「收盘价成交」是回测作弊。
7. **PVT 不可跨票比较绝对值**（累积量与成交量单位、价格水平相关）→ 只用它做
   **自身序列**的斜率与背离判断。
8. 数据不足（次新股 / 长期停牌）→ 返回 ``insufficient`` 并说明原因，不猜。
"""

from __future__ import annotations

import math
import warnings
from dataclasses import dataclass, field
from typing import Any, Sequence

import numpy as np

# ── 参数（集中在模块顶部，便于按偏好调整）────────────────────────────
# 趋势 ① 量价突破
BREAKOUT_N = 20            # 突破周期：昨日为止的 N 日最高
BREAKOUT_VOL_MA = 5        # 放量基准：昨日为止的 M 日均量
BREAKOUT_VOL_K = 1.5       # 放量倍数
# 趋势 ② 价量共振
RESO_PRICE_MA = 50         # 价格中期均线
RESO_PRICE_LAG = 3         # 价能：MA50 相对 3 日前
RESO_VOL_SHORT = 5
RESO_VOL_LONG = 100
# 注意：伪代码给的是「价能 × 量能 > 1.15」。实测 MA50 三日斜率几乎不可能到 15%
# （MA50 变化慢），直接照搬会得到 0 个信号。这里拆成两个条件：价能 > 1（均线上行）、
# 量能 > 1（短期活跃度高于长期），再要求乘积 > 阈值。
RESO_PRODUCT_MIN = 1.05
# 趋势 ③ 价量趋势 PVT
PVT_WINDOW = 60            # 背离比较窗口
# 反转 ④ 极端成交量
DRY_VOL_RATIO = 0.5        # 地量：不足 20 日均量的一半
DRY_NO_NEW_LOW = 10        # 止跌：不再创 10 日新低
HEAVY_VOL_K = 3.0          # 天量：3 倍 5 日均量（不含当日）
HEAVY_SHADOW_BODY = 2.0    # 长上影：上影 > 2 倍实体
HEAVY_SHADOW_RANGE = 0.5   # 且上影 > 半个振幅
# 反转 ⑤ 价量融合（马氏距离 + 极坐标）
PV_WINDOW = 60             # 滚动统计窗口
PV_HISTORY = 250           # 自身历史分位比较长度
# 分位基准要**剔除最近 PV_EXCLUDE 根**：若把当前这段极端行情算进基准，
# 连续极端（价量齐升 8 天）会把 95 分位抬到自己的水平，导致「极端」永远测不出来。
PV_EXCLUDE = 10
PV_HIGH_PCT = 0.95         # 距离的极端阈值（**只取高分位**，见下）
# ★ 语义更正：马氏距离是「离常态多远」，**距离小 = 常态**，不是「极端低位」。
#   因此只对「距离极端大」出信号，方向由极角决定：
#     距离 ≥ p95 且极角偏正（价量齐升）→ 过热，防反转下行（看空）
#     距离 ≥ p95 且极角偏负（价量齐跌）→ 恐慌，关注反弹（看多）
PV_MIN_DISTANCE = 2.0      # 绝对下限：距离 < 2 仍在常态椭圆内，不构成极端
# 趋势背离的上下文门槛（避免横盘里「平价即新高」的假背离）
PVT_DIV_TREND_PCT = 3.0    # 相对 20 日前至少涨/跌 3% 才算处在趋势尾部
PVT_DIV_SLOPE = 0.2        # PVT 斜率必须明确反向（排除 -0.00 级噪声）
# 环境过滤
ADX_PERIOD = 14
ADX_TREND = 25.0           # ≥ 视为强趋势（趋势策略更可信）
ADX_RANGE = 20.0           # ≤ 视为震荡（反转策略更可信）
MIN_BARS = 120             # 少于该根数不出信号（指标未热身）

# ── 信号优先级仲裁 + ADX 环境分流（用户补丁一 / 补丁二）──────────────
# 补丁一：同一只票同时触发多条信号时，**只留一条**，按优先级裁决；
#   ≥ PRIORITY_AVOID 的「风险类（看空）」直接给 AVOID（规避，不进买入列表）。
#   ★ 优先级不按「名字」硬编码，而是按 SIGNAL_META 的方向补齐（见 _priority_of）：
#   用户伪代码只列了 6 个信号，本项目实际有 9 个，漏配的 3 个会 fallback 到 0，
#   表现为「PVT顶背离 被当无关信号忽略」——比不实现更糟。
PRIORITY_RISK = 5          # 卖出/规避类
PRIORITY_TREND = 3         # 趋势买入类
PRIORITY_REVERSAL = 2      # 反转/抄底类（优先级最低）
PRIORITY_AVOID = 5         # ≥ 该值 = 规避
SIGNAL_PRIORITY: dict[str, int] = {
    # 风险类（看空）——不论什么环境都必须生效，见 arbitrate 的口径说明
    "pv_overheat": PRIORITY_RISK,
    "vol_heavy_reversal": PRIORITY_RISK,
    "pvt_top_divergence": PRIORITY_RISK,      # 扩展：伪代码未列，方向同为看空
    # 趋势（看多）类
    "vol_breakout": PRIORITY_TREND,
    "pv_resonance": PRIORITY_TREND,
    "pvt_trend": PRIORITY_TREND,
    # 反转（看多）类
    "vol_dry_reversal": PRIORITY_REVERSAL,
    "pvt_bottom_divergence": PRIORITY_REVERSAL,  # 扩展：伪代码未列，性质同「抄底」
    "pv_capitulation": PRIORITY_REVERSAL,        # 扩展：伪代码未列，性质同「抄底」
}

# 补丁二：ADX 环境 → 策略分流。阈值与 regime_of 刻意不同（后者是展示口径、
# 且被历史验证 by_regime 复用），此处的 20/25 是**交易分流**口径：
#   > 25 → TREND（趋势市，只跑趋势类看多信号）
#   < 20 → WEAK （无趋势，空仓观望，全部屏蔽）
#   20~25 → RANGE（震荡市，只跑反转类看多信号）
ENV_TREND_ADX = ADX_TREND
ENV_WEAK_ADX = ADX_RANGE
ENV_ZH = {
    "TREND": "趋势市",
    "RANGE": "震荡市",
    "WEAK": "无趋势·空仓",
    "UNKNOWN": "数据不足",
}
VERDICT_ZH = {
    "AVOID": "规避",
    "SIGNAL": "保留信号",
    "IGNORE": "忽略",
    "STANDBY": "空仓观望",
}
ENV_CATEGORY_ZH = {"trend": "趋势类", "reversal": "反转类"}

SIGNAL_META: dict[str, dict[str, str]] = {
    "vol_breakout": {"name": "量价突破", "category": "trend", "direction": "bullish"},
    "pv_resonance": {"name": "价量共振", "category": "trend", "direction": "bullish"},
    "pvt_trend": {"name": "价量趋势(PVT)", "category": "trend", "direction": "bullish"},
    "pvt_top_divergence": {"name": "PVT顶背离", "category": "trend", "direction": "bearish"},
    "pvt_bottom_divergence": {"name": "PVT底背离", "category": "trend", "direction": "bullish"},
    "vol_dry_reversal": {"name": "地量止跌", "category": "reversal", "direction": "bullish"},
    "vol_heavy_reversal": {"name": "天量滞涨", "category": "reversal", "direction": "bearish"},
    "pv_overheat": {"name": "价量过热", "category": "reversal", "direction": "bearish"},
    "pv_capitulation": {"name": "价量恐慌", "category": "reversal", "direction": "bullish"},
}

CATEGORY_ZH = {"trend": "趋势确认", "reversal": "反转捕捉"}
DIRECTION_ZH = {"bullish": "看多", "bearish": "看空"}
REGIME_ZH = {"trend": "强趋势", "range": "震荡", "neutral": "中性", "unknown": "数据不足"}


@dataclass
class SignalHit:
    """单条信号（某个交易日的某个信号触发）。"""

    key: str
    date: str
    bars_ago: int
    reason: str
    metrics: dict[str, float | None] = field(default_factory=dict)

    @property
    def name(self) -> str:
        return SIGNAL_META.get(self.key, {}).get("name", self.key)

    @property
    def category(self) -> str:
        return SIGNAL_META.get(self.key, {}).get("category", "")

    @property
    def direction(self) -> str:
        return SIGNAL_META.get(self.key, {}).get("direction", "")

    def to_dict(self) -> dict[str, Any]:
        return {
            "key": self.key,
            "name": self.name,
            "category": self.category,
            "category_zh": CATEGORY_ZH.get(self.category, self.category),
            "direction": self.direction,
            "direction_zh": DIRECTION_ZH.get(self.direction, self.direction),
            "date": self.date,
            "bars_ago": self.bars_ago,
            "reason": self.reason,
            "metrics": self.metrics,
        }


# ── 数组工具（全部返回与输入等长的数组，热身期为 nan）────────────────

def _roll_mean(x: np.ndarray, w: int) -> np.ndarray:
    out = np.full(x.shape, np.nan)
    if w <= 0 or x.size < w:
        return out
    cs = np.cumsum(np.insert(x, 0, 0.0))
    out[w - 1:] = (cs[w:] - cs[:-w]) / w
    return out


def _roll_max(x: np.ndarray, w: int) -> np.ndarray:
    out = np.full(x.shape, np.nan)
    if w <= 0 or x.size < w:
        return out
    win = np.lib.stride_tricks.sliding_window_view(x, w)
    out[w - 1:] = win.max(axis=1)
    return out


def _roll_min(x: np.ndarray, w: int) -> np.ndarray:
    out = np.full(x.shape, np.nan)
    if w <= 0 or x.size < w:
        return out
    win = np.lib.stride_tricks.sliding_window_view(x, w)
    out[w - 1:] = win.min(axis=1)
    return out


def _roll_std(x: np.ndarray, w: int) -> np.ndarray:
    m = _roll_mean(x, w)
    m2 = _roll_mean(x * x, w)
    with np.errstate(invalid="ignore"):
        var = np.maximum(m2 - m * m, 0.0)
    return np.sqrt(var)


def _roll_corr(x: np.ndarray, y: np.ndarray, w: int) -> np.ndarray:
    mx = _roll_mean(x, w)
    my = _roll_mean(y, w)
    mxy = _roll_mean(x * y, w)
    sx = _roll_std(x, w)
    sy = _roll_std(y, w)
    with np.errstate(invalid="ignore", divide="ignore"):
        cov = mxy - mx * my
        rho = cov / (sx * sy)
    return np.clip(rho, -0.999, 0.999)


def _shift(x: np.ndarray, k: int) -> np.ndarray:
    out = np.full(x.shape, np.nan)
    if 0 < k < x.size:
        out[k:] = x[: x.size - k]
    return out


def _wilder_rma(x: np.ndarray, period: int) -> np.ndarray:
    """Wilder 平滑（RMA）：指标标准算法，不能用简单均值近似。"""
    out = np.full(x.shape, np.nan)
    if x.size < period:
        return out
    s = float(np.mean(x[:period]))
    out[period - 1] = s
    for i in range(period, x.size):
        s = (s * (period - 1) + float(x[i])) / period
        out[i] = s
    return out


def _rolling_quantile(x: np.ndarray, w: int, q: float) -> np.ndarray:
    """滚动分位（窗口含当日）。用于马氏距离的极端阈值，避免逐 bar 循环。"""
    out = np.full(x.shape, np.nan)
    if w <= 1 or x.size < w:
        return out
    win = np.lib.stride_tricks.sliding_window_view(x, w)
    with warnings.catch_warnings():
        # 热身期的窗口可能全为 NaN（指标尚未成形），nanquantile 会告警但结果就是 NaN
        warnings.simplefilter("ignore", RuntimeWarning)
        out[w - 1:] = np.nanquantile(win, q, axis=1)
    return out


# ── 指标 ────────────────────────────────────────────────────────────

def adx_series(
    high: np.ndarray, low: np.ndarray, close: np.ndarray, period: int = ADX_PERIOD
) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """Wind ADX 的**逐日序列** ``(adx, +DI, -DI)``（热身期为 nan）。

    为什么要序列而不只取最新值：验证「ADX>25 用趋势策略、ADX<20 用反转策略」这条
    经验时，必须知道**信号当天**的环境，而不是今天的。
    """
    nan = np.full(high.shape, np.nan)
    n = high.size
    if n < period * 2 + 2:
        return nan, nan, nan
    pc = _shift(close, 1)
    ph = _shift(high, 1)
    pl = _shift(low, 1)
    tr = np.maximum.reduce([high - low, np.abs(high - pc), np.abs(low - pc)])
    up = high - ph
    dn = pl - low
    plus_dm = np.where((up > dn) & (up > 0), up, 0.0)
    minus_dm = np.where((dn > up) & (dn > 0), dn, 0.0)

    atr = _wilder_rma(tr[1:], period)
    pdm = _wilder_rma(plus_dm[1:], period)
    mdm = _wilder_rma(minus_dm[1:], period)
    with np.errstate(invalid="ignore", divide="ignore"):
        pdi = 100.0 * pdm / atr
        mdi = 100.0 * mdm / atr
        denom = pdi + mdi
        dx = 100.0 * np.abs(pdi - mdi) / np.where(denom == 0, np.nan, denom)
    adx_short = _wilder_rma(dx[period - 1:], period)

    adx = np.full(n, np.nan)
    pdi_full = np.full(n, np.nan)
    mdi_full = np.full(n, np.nan)
    # 索引对齐：`_wilder_rma(tr[1:], p)` 的第 k 项对应 bar k+1，故 DI 整体右移 1；
    # DX 又经过一次 period 平滑，故 ADX 再右移 period。
    adx[period:] = adx_short[: n - period]
    pdi_full[1:] = pdi[: n - 1]
    mdi_full[1:] = mdi[: n - 1]
    return adx, pdi_full, mdi_full


def adx_state(
    high: np.ndarray, low: np.ndarray, close: np.ndarray, period: int = ADX_PERIOD
) -> tuple[float | None, float | None, float | None]:
    """最新一根的 ``(adx, +DI, -DI)``；数据不足返回 None。"""
    adx, pdi, mdi = adx_series(high, low, close, period)
    if adx.size == 0:
        return None, None, None

    def _last(a: np.ndarray) -> float | None:
        if a.size == 0 or not np.isfinite(a[-1]):
            return None
        return round(float(a[-1]), 2)

    return _last(adx), _last(pdi), _last(mdi)


def regime_of(adx: float | None) -> str:
    """ADX → 环境标签（趋势/震荡/中性/数据不足）。**展示与历史验证口径**。"""
    if adx is None:
        return "unknown"
    if adx >= ADX_TREND:
        return "trend"
    if adx <= ADX_RANGE:
        return "range"
    return "neutral"


def env_of(adx: float | None) -> str:
    """ADX → **策略分流**环境（补丁二口径）：``TREND`` / ``RANGE`` / ``WEAK`` / ``UNKNOWN``。

    与 :func:`regime_of` 是两套口径，不可互相替代：

    ============  ========================  ==========================
    ADX           本函数（交易分流）          regime_of（展示 / 验证）
    ============  ========================  ==========================
    > 25          TREND 趋势市               trend 强趋势
    20 ~ 25       RANGE 震荡市               neutral 中性
    < 20          WEAK 无趋势 → 空仓         range 震荡（反转更可信）
    ============  ========================  ==========================

    差异的实质：``regime_of`` 把 ADX ≤ 20 判为「震荡」（适合反转策略），
    而补丁二把它判为「无趋势 → 空仓」。前者是描述，后者是**不下注**——两种语义都保留，
    但必须在页面上分开标注，不能混成一个标签。
    """
    if adx is None:
        return "UNKNOWN"
    if adx > ENV_TREND_ADX:
        return "TREND"
    if adx < ENV_WEAK_ADX:
        return "WEAK"
    return "RANGE"


def priority_of(key: str) -> int:
    """信号优先级（未登记的信号返回 0，即「不参与裁决」——新增信号必须补登记）。"""
    return int(SIGNAL_PRIORITY.get(key, 0))


def _arb_item(sig: dict[str, Any], why: str | None = None) -> dict[str, Any]:
    key = str(sig.get("key"))
    meta = SIGNAL_META.get(key, {})
    out = {
        "key": key,
        "name": meta.get("name", key),
        "category": meta.get("category", ""),
        "direction": meta.get("direction", ""),
        "direction_zh": DIRECTION_ZH.get(meta.get("direction", ""), ""),
        "bars_ago": sig.get("bars_ago"),
        "priority": priority_of(key),
    }
    if why:
        out["why"] = why
    return out


def arbitrate(
    signals: Sequence[dict[str, Any]], env: str, *, adx: float | None = None
) -> dict[str, Any]:
    """补丁一 + 补丁二合成流水线：**环境分流 → 优先级仲裁 → 唯一裁决**。

    输入 ``signals``：:func:`analyze` 产出的信号列表（已按 key 去重）。
    ``env``：:func:`env_of` 的结果。

    输出 ``verdict`` 四态：

    - ``AVOID``   —— 有风险类（优先级 ≥ 5）信号命中 → **从买入列表剔除**
    - ``SIGNAL``  —— 保留一条最高优先级信号（趋势类或反转类，取决于环境）
    - ``IGNORE``  —— 信号全被环境分流屏蔽（或本来就没有信号）→ 本轮不参与
    - ``STANDBY`` —— ADX < 20 无趋势 → 全部屏蔽，空仓观望

    ## 与用户伪代码的两处**刻意偏离**（重要，勿改回）

    1. **风险类（看空）信号不受环境过滤。** 伪代码里 ``run_strategy_by_regime`` 在趋势市
       只放行趋势类信号，会把「价量过热」这类**风险提示**一并过滤掉——那么伪代码自己给的
       例子（万科A：ADX=30.7 趋势市，靠「价量过热」判 AVOID）就**推不出来**。
       风险控制不属于「趋势/反转策略」之争，任何环境下都要生效；否则等于给风险信号开了
       免检通道（项目口径铁律：否决类判据不得被上层过滤绕过）。
    2. **ADX 缺失（UNKNOWN）不做分流，只做优先级仲裁**，并在 ``explain`` 中留痕。
       「没有数据」不能当成「通过」，也不能当成「拒绝」——这里选择不新增一道隐性门槛，
       但把 ``env_known=False`` 明确标出，避免被当成已验证结论。
    """
    env = str(env or "UNKNOWN").upper()
    if env not in ENV_ZH:
        env = "UNKNOWN"
    adx_v = None if adx is None else round(float(adx), 2)
    sigs = [s for s in signals if str(s.get("key")) in SIGNAL_META]
    kept: list[dict[str, Any]] = []
    dropped: list[dict[str, Any]] = []

    if env == "WEAK":
        for s in sigs:
            why = f"ADX < {ENV_WEAK_ADX:g} 无趋势环境 → 空仓观望，信号全部屏蔽"
            dropped.append(_arb_item(s, why))
        head = f"ADX {adx_v:g}" if adx_v is not None else "ADX"
        return {
            "env": env, "env_zh": ENV_ZH[env], "env_known": True, "adx": adx_v,
            "verdict": "STANDBY", "verdict_zh": VERDICT_ZH["STANDBY"],
            "final": None, "priority": None,
            "kept": [], "dropped": dropped,
            "explain": (
                f"{head} < {ENV_WEAK_ADX:g} → 无趋势（补丁二）→ 屏蔽全部 {len(dropped)} 条信号，"
                f"本轮空仓观望"
            ),
        }

    for s in sigs:
        key = str(s.get("key"))
        meta = SIGNAL_META[key]
        pr = priority_of(key)
        if pr >= PRIORITY_AVOID:
            kept.append(_arb_item(s))
            continue
        if env == "UNKNOWN":
            kept.append(_arb_item(s))
            continue
        want = "trend" if env == "TREND" else "reversal"
        if meta.get("category") == want:
            kept.append(_arb_item(s))
        else:
            dropped.append(_arb_item(
                s,
                f"{ENV_ZH[env]}只跑{ENV_CATEGORY_ZH[want]}信号，"
                f"本信号属{ENV_CATEGORY_ZH.get(meta.get('category'), meta.get('category'))} → 屏蔽",
            ))

    final: dict[str, Any] | None = None
    verdict = "IGNORE"
    if kept:
        # 并列优先级时取**最近触发**的一条（bars_ago 越小越新），再按 key 稳定排序
        final = sorted(kept, key=lambda x: (-int(x["priority"]), int(x.get("bars_ago") or 0), x["key"]))[0]
        verdict = "AVOID" if int(final["priority"]) >= PRIORITY_AVOID else "SIGNAL"

    if env == "UNKNOWN":
        head = "ADX 数据不足 → 不分流（仅按优先级仲裁，结论未经环境验证）"
    else:
        head = f"ADX {adx_v:g} → {ENV_ZH[env]}（补丁二分流）" if adx_v is not None else f"{ENV_ZH[env]}（补丁二分流）"
    if dropped and env != "UNKNOWN":
        head += f"，屏蔽 {len(dropped)} 条"
    if not sigs:
        explain = f"{head} → 窗口内无信号 → 忽略"
    elif verdict == "AVOID":
        explain = (
            f"{head} → 保留 {len(kept)} 条 → 优先级最高「{final['name']}」"
            f"（{final['priority']}，{final['direction_zh']}）→ 规避（从买入列表剔除）"
        )
    elif verdict == "SIGNAL":
        explain = (
            f"{head} → 保留 {len(kept)} 条 → 优先级最高「{final['name']}」"
            f"（{final['priority']}，{final['direction_zh']}）→ 保留该信号"
        )
    else:
        explain = f"{head} → 信号全部被屏蔽 → 忽略"
    return {
        "env": env, "env_zh": ENV_ZH[env], "env_known": env != "UNKNOWN", "adx": adx_v,
        "verdict": verdict, "verdict_zh": VERDICT_ZH[verdict],
        "final": final, "priority": (int(final["priority"]) if final else None),
        "kept": kept, "dropped": dropped,
        "explain": explain,
    }


VERDICT_SORT_ORDER = {"SIGNAL": 0, "AVOID": 1, "IGNORE": 2, "STANDBY": 3}


def pv_mahalanobis(
    close: np.ndarray, volume: np.ndarray, window: int = PV_WINDOW
) -> tuple[np.ndarray, np.ndarray, np.ndarray, np.ndarray]:
    """价量融合的**马氏距离**、**极角**与两个**标准化偏离分量**（逐日）。

    做法：对 ``log(价格)`` 与 ``log(成交量)`` 各自做滚动标准化得到 z 分数，
    用两者的滚动相关系数 ρ 构造相关矩阵，取马氏距离

        d = sqrt((zp² - 2ρ·zp·zv + zv²) / (1 - ρ²))

    极角 ``θ = atan2(zv, zp)``：45° = 价量齐升、-135° = 价量齐跌。
    距离衡量「偏离常态多远」，**方向由 (zp, zv) 的象限决定**——
    只看角度符号会把「缩量涨停」（zp>0, zv<0，实为惜售）误读成恐慌。

    **为何用 log 且用滚动统计**：价格与成交量水平非平稳，直接对原始值做距离会被
    长期趋势污染；取对数并只用近 ``window`` 日统计量，衡量的是「相对近期常态的偏离」。

    Returns: ``(d, theta, zp, zv)``
    """
    lp = np.log(np.maximum(close, 1e-9))
    lv = np.log(np.maximum(volume, 1.0))
    mzp = _roll_mean(lp, window)
    mzv = _roll_mean(lv, window)
    szp = _roll_std(lp, window)
    szv = _roll_std(lv, window)
    with np.errstate(invalid="ignore", divide="ignore"):
        zp = (lp - mzp) / szp
        zv = (lv - mzv) / szv
    rho = _roll_corr(lp, lv, window)
    with np.errstate(invalid="ignore", divide="ignore"):
        d = np.sqrt(np.maximum((zp * zp - 2 * rho * zp * zv + zv * zv) / (1 - rho * rho), 0.0))
    theta = np.arctan2(zv, zp)
    return d, theta, zp, zv


def _pct_rank(
    series: np.ndarray,
    idx: int,
    history: int = PV_HISTORY,
    exclude: int = PV_EXCLUDE,
) -> float | None:
    """``series[idx]`` 在**剔除最近 ``exclude`` 根**后的自身分位（0~1）。

    剔除最近部分是刻意的：分位基准若含当前这段行情，连续极端行情会把分位门槛
    抬到自己脚下，「极端」永远测不出来。
    """
    end = idx - exclude + 1
    start = max(0, end - history)
    if end <= start:
        return None
    win = series[start:end]
    win = win[np.isfinite(win)]
    if win.size < 30:
        return None
    cur = series[idx]
    if not np.isfinite(cur):
        return None
    return float((win <= cur).mean())


def _ref_window(series: np.ndarray, idx: int, history: int = PV_HISTORY,
                exclude: int = PV_EXCLUDE) -> np.ndarray:
    end = idx - exclude + 1
    start = max(0, end - history)
    win = series[start:end]
    return win[np.isfinite(win)]


# ── 各信号逐日条件 & 事件抽取 ───────────────────────────────────────

def _num(v: Any, digits: int = 2) -> float | None:
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    if math.isnan(f) or math.isinf(f):
        return None
    return round(f, digits)


def _sealed(
    o: np.ndarray, h: np.ndarray, lo: np.ndarray, c_: np.ndarray,
    idx: int, direction: str, limit_pct: float,
) -> tuple[str | None, str | None]:
    """第 ``idx`` 根出信号、第 ``idx+1`` 根能否成交（一字板封死 → 不能）。"""
    if idx + 1 >= c_.size:
        return None, None
    nxt = idx + 1
    prev_close = float(c_[idx])
    up = round(prev_close * (1 + limit_pct / 100.0) + 1e-9, 2)
    dn = round(prev_close * (1 - limit_pct / 100.0) + 1e-9, 2)
    if direction == "bullish" and o[nxt] >= up - 0.005 and lo[nxt] >= up - 0.005:
        return "limit_up_sealed", f"次日一字涨停（{up:.2f}），买入无法成交"
    if direction == "bearish" and o[nxt] <= dn + 0.005 and h[nxt] <= dn + 0.005:
        return "limit_down_sealed", f"次日一字跌停（{dn:.2f}），卖出无法成交"
    return None, None


def exec_block(
    candles: Sequence[Any], idx: int, *, direction: str, limit_pct: float = 10.0
) -> tuple[str | None, str | None]:
    """对外暴露的可成交性判定（回测/事件研究复用同一口径，勿各写一套）。

    返回 ``(code, 中文说明)``；可成交或尚无次日数据时返回 ``(None, None)``。
    """
    if idx + 1 >= len(candles):
        return None, None
    o = np.array([float(candles[idx].open), float(candles[idx + 1].open)])
    h = np.array([float(candles[idx].high), float(candles[idx + 1].high)])
    lo = np.array([float(candles[idx].low), float(candles[idx + 1].low)])
    c_ = np.array([float(candles[idx].close), float(candles[idx + 1].close)])
    return _sealed(o, h, lo, c_, 0, direction, limit_pct)


def analyze(
    candles: Sequence[Any],
    *,
    lookback_days: int = 10,
    limit_pct: float = 10.0,
    with_reason: bool = True,
    dedupe: bool = True,
) -> dict[str, Any]:
    """对单只标的算全部价量信号。

    ``candles``：按时间升序、含 ``open/high/low/close/volume/timestamp`` 的序列
    （与 ``app.core.candle.Candle`` 同构）。``limit_pct``：涨跌停幅度
    （主板 10 / 创业板科创板 20 / ST 5），用于 T+1 可成交性判定。
    ``with_reason=False``：跳过文案与指标字典的生成（事件研究/回测用，省时间）。
    ``dedupe=False``：保留同一信号的**每一次**触发（默认只留最近一次）。

    返回（``insufficient`` 为 True 时不判信号，只说明原因）::

        {"symbol": …, "as_of": …, "bars": …, "insufficient": bool, "reason": …,
         "close": …, "regime": {...}, "signals": [SignalHit.to_dict(), …],
         "latest_signals": [key, …], "newest_bars_ago": n, "categories": ["trend", …],
         "counts": {"trend": n, "reversal": n}, "tradable": {...}}
    """
    n = len(candles)
    base: dict[str, Any] = {
        "insufficient": False,
        "reason": "",
        "bars": n,
        "as_of": None,
        "close": None,
        "regime": {"state": "unknown", "adx": None, "plus_di": None, "minus_di": None},
        "signals": [],
        "latest_signals": [],
        "newest_bars_ago": None,
        "categories": [],
        "counts": {"trend": 0, "reversal": 0},
        "tradable": {"exec_hint": "", "pending_next_bar": True, "signal_date": None,
                     "blocked": None, "blocked_zh": None, "next_date": None,
                     "limit_pct": limit_pct},
        "metrics": {},
    }
    if n == 0:
        base.update(insufficient=True, reason="无 K 线数据")
        return base

    o = np.array([float(c.open) for c in candles], dtype=float)
    h = np.array([float(c.high) for c in candles], dtype=float)
    lo = np.array([float(c.low) for c in candles], dtype=float)
    c_ = np.array([float(c.close) for c in candles], dtype=float)
    v = np.array([float(c.volume) for c in candles], dtype=float)
    dates = [getattr(c, "timestamp", None) for c in candles]

    def _dstr(i: int) -> str:
        dt = dates[i]
        return dt.strftime("%Y-%m-%d") if hasattr(dt, "strftime") else str(dt)

    base["as_of"] = _dstr(n - 1)
    base["close"] = _num(c_[-1])
    if n < MIN_BARS:
        base.update(insufficient=True, reason=f"K 线不足 {MIN_BARS} 根（仅 {n} 根，指标未热身）")
        return base

    last = n - 1

    # ── 公共滚动量（一律 shift(1)：不含当日）─────────────────────────
    ma_v_short_prev = _shift(_roll_mean(v, BREAKOUT_VOL_MA), 1)
    ma_v_20_prev = _shift(_roll_mean(v, 20), 1)
    high_n_prev = _shift(_roll_max(h, BREAKOUT_N), 1)
    low_20_prev = _shift(_roll_min(lo, 20), 1)
    low_10_prev = _shift(_roll_min(lo, DRY_NO_NEW_LOW), 1)

    hits: list[SignalHit] = []

    def _emit(key: str, flag: np.ndarray, start: int, build) -> None:
        """把 ``flag`` 在 ``[start, last]`` 内为真的日子抽成事件（新→旧）。"""
        idxs = np.flatnonzero(flag[start: last + 1]) + start
        for i in idxs:
            if with_reason:
                reason, metrics = build(int(i))
            else:
                reason, metrics = "", {}
            hits.append(
                SignalHit(
                    key=key,
                    date=_dstr(int(i)),
                    bars_ago=int(last - i),
                    reason=reason,
                    metrics=metrics,
                )
            )

    window_start = max(0, last - lookback_days)
    # 事件抽取需要一点余量（信号可能落在窗口边界，指标仍要有值）
    scan_start = max(MIN_BARS - 1, last - lookback_days)

    # ── 趋势 ① 量价突破 ──────────────────────────────────────────
    with np.errstate(invalid="ignore"):
        bo = (c_ > high_n_prev) & (v > BREAKOUT_VOL_K * ma_v_short_prev)
    bo = bo & (ma_v_short_prev > 0)

    def _bo(i: int) -> tuple[str, dict[str, Any]]:
        ratio = v[i] / ma_v_short_prev[i] if ma_v_short_prev[i] else None
        brk = (c_[i] / high_n_prev[i] - 1) * 100 if high_n_prev[i] else None
        return (
            f"收盘 {c_[i]:.2f} 突破前 {BREAKOUT_N} 日最高 {high_n_prev[i]:.2f}"
            f"（+{brk:.2f}%），量 {ratio:.2f} 倍于前 {BREAKOUT_VOL_MA} 日均量",
            {"vol_ratio": _num(ratio), "break_pct": _num(brk)},
        )

    _emit("vol_breakout", bo, scan_start, _bo)

    # ── 趋势 ② 价量共振 ─────────────────────────────────────────
    ma50 = _roll_mean(c_, RESO_PRICE_MA)
    with np.errstate(invalid="ignore", divide="ignore"):
        price_power = ma50 / _shift(ma50, RESO_PRICE_LAG)
        vol_short = _roll_mean(v, RESO_VOL_SHORT)
        vol_long = _roll_mean(v, RESO_VOL_LONG)
        volume_power = vol_short / vol_long
        resonance = price_power * volume_power
    # 共振 = 价能、量能**同时**为正（都在上行），再要求乘积超阈值
    with np.errstate(invalid="ignore"):
        rs = (price_power > 1.0) & (volume_power > 1.0) & (resonance > RESO_PRODUCT_MIN)

    def _rs(i: int) -> tuple[str, dict[str, Any]]:
        return (
            f"价能 {price_power[i]:.4f}（MA{RESO_PRICE_MA} 三日斜率）"
            f"× 量能 {volume_power[i]:.2f}（MA{RESO_VOL_SHORT}/MA{RESO_VOL_LONG}）"
            f"= {resonance[i]:.4f} > {RESO_PRODUCT_MIN}",
            {
                "price_power": _num(price_power[i], 4),
                "volume_power": _num(volume_power[i], 3),
                "resonance": _num(resonance[i], 4),
            },
        )

    _emit("pv_resonance", rs, scan_start, _rs)

    # ── 趋势 ③ 价量趋势 PVT（斜率 + 背离）───────────────────────
    with np.errstate(invalid="ignore", divide="ignore"):
        pct = np.diff(c_, prepend=c_[0]) / np.where(c_ == 0, np.nan, c_)
        pvt = np.nancumsum(np.nan_to_num(pct * v, nan=0.0))
    # 归一化：用日均贡献的绝对值做尺度，使斜率跨票可比
    scale = _roll_mean(np.abs(np.nan_to_num(pct * v, nan=0.0)), 20)
    with np.errstate(invalid="ignore", divide="ignore"):
        pvt_slope = (pvt - _shift(pvt, 5)) / (scale * 5)
    with np.errstate(invalid="ignore"):
        pvt_up = (pvt_slope > 0.5) & (c_ > ma50)
        pvt_high = pvt >= _roll_max(pvt, PVT_WINDOW) * 0.999
        pvt_low = pvt <= _roll_min(pvt, PVT_WINDOW) * 1.001
        # 背离必须发生在**趋势尾部**：仅有「新高/新低」不够——横盘序列里
        # 「与前高持平」会被判成新高，配上斜率 -0.00 的噪声就会天天报背离。
        # 故额外要求价格相对 20 日前已明确上涨/下跌，且 PVT 斜率明确反向。
        price_high = (c_ >= _roll_max(c_, PVT_WINDOW) * 0.999) & (
            c_ > _shift(c_, 20) * (1 + PVT_DIV_TREND_PCT / 100.0)
        )
        price_low = (c_ <= _roll_min(c_, PVT_WINDOW) * 1.001) & (
            c_ < _shift(c_, 20) * (1 - PVT_DIV_TREND_PCT / 100.0)
        )
        top_div = price_high & (~pvt_high) & (pvt_slope < -PVT_DIV_SLOPE)
        bot_div = price_low & (~pvt_low) & (pvt_slope > PVT_DIV_SLOPE)

    def _pt(i: int) -> tuple[str, dict[str, Any]]:
        return (
            f"PVT 5 日归一化斜率 {pvt_slope[i]:.2f}（>0.5 且价在 MA{RESO_PRICE_MA} 上方）",
            {"pvt_slope": _num(pvt_slope[i], 3)},
        )

    def _td(i: int) -> tuple[str, dict[str, Any]]:
        return (
            f"价格创 {PVT_WINDOW} 日新高，但 PVT 未同步创新高且已转弱"
            f"（斜率 {pvt_slope[i]:.2f}）→ 资金未跟上，趋势衰竭预警",
            {"pvt_slope": _num(pvt_slope[i], 3)},
        )

    def _bd(i: int) -> tuple[str, dict[str, Any]]:
        return (
            f"价格创 {PVT_WINDOW} 日新低，但 PVT 未同步创新低且已转强"
            f"（斜率 {pvt_slope[i]:.2f}）→ 抛压衰竭",
            {"pvt_slope": _num(pvt_slope[i], 3)},
        )

    _emit("pvt_trend", pvt_up, scan_start, _pt)
    _emit("pvt_top_divergence", top_div, scan_start, _td)
    _emit("pvt_bottom_divergence", bot_div, scan_start, _bd)

    # ── 反转 ④ 极端成交量 ───────────────────────────────────────
    body = np.abs(c_ - o)
    upper = h - np.maximum(o, c_)
    rng = np.maximum(h - lo, 1e-9)
    with np.errstate(invalid="ignore"):
        # 「不再创新低」用 >= 而非 >：止跌首日的最低价常与前 10 日最低**相等**
        # （横盘地量），用严格大于会把这种最典型的形态全部排除。
        dry = (
            (v < DRY_VOL_RATIO * ma_v_20_prev)
            & (c_ > o)
            & (lo >= low_10_prev - 1e-9)
            & (c_ >= _shift(c_, 1))
        )
        # 天量滞涨：长上影 + 收阴即可（**不再要求**收盘低于前一日——放量冲高
        # 后收在上影下方但仍在昨收之上，同样是滞涨/派发，加上那条会漏掉它）。
        heavy = (
            (v > HEAVY_VOL_K * ma_v_short_prev)
            & (upper > HEAVY_SHADOW_BODY * body)
            & (upper > HEAVY_SHADOW_RANGE * rng)
            & (c_ < o)
        )
    dry = dry & (ma_v_20_prev > 0)
    heavy = heavy & (ma_v_short_prev > 0)

    def _dry(i: int) -> tuple[str, dict[str, Any]]:
        ratio = v[i] / ma_v_20_prev[i] if ma_v_20_prev[i] else None
        return (
            f"量能萎缩至 20 日均量的 {ratio:.2f} 倍（<{DRY_VOL_RATIO}），"
            f"且收阳、未创 {DRY_NO_NEW_LOW} 日新低 → 抛压衰竭",
            {"vol_ratio": _num(ratio), "close": _num(c_[i])},
        )

    def _heavy(i: int) -> tuple[str, dict[str, Any]]:
        ratio = v[i] / ma_v_short_prev[i] if ma_v_short_prev[i] else None
        sh = upper[i] / rng[i] * 100
        return (
            f"量能放大至 {BREAKOUT_VOL_MA} 日均量的 {ratio:.2f} 倍（>{HEAVY_VOL_K}），"
            f"上影占振幅 {sh:.0f}% 且收阴 → 高位滞涨/派发",
            {"vol_ratio": _num(ratio), "upper_shadow_pct": _num(sh, 1)},
        )

    _emit("vol_dry_reversal", dry, scan_start, _dry)
    _emit("vol_heavy_reversal", heavy, scan_start, _heavy)

    # ── 反转 ⑤ 价量融合极端（马氏距离 + 极角）──────────────────
    d, theta, zp, zv = pv_mahalanobis(c_, v)
    # 极端阈值取「剔除最近 PV_EXCLUDE 根」的参考分位。
    # ★ 窗口长度必须**按可用历史自适应**：硬要求满 PV_HISTORY(250) 根会让
    #   上市 1~2 年的次新股（本项目有 297 只处于 120~249 根）静默丢掉这个信号，
    #   而它们在反转类策略里恰恰是高发群体（次新股波动大）。
    usable = int(np.isfinite(d).sum())
    w_eff = int(min(PV_HISTORY, max(30, usable - PV_EXCLUDE)))
    hi_at = _shift(_rolling_quantile(d, w_eff, PV_HIGH_PCT), PV_EXCLUDE)
    with np.errstate(invalid="ignore"):
        extreme = np.isfinite(d) & np.isfinite(hi_at) & (d >= hi_at) & (d >= PV_MIN_DISTANCE)
        # 方向必须由**象限**决定，不能只看角度符号：缩量涨停（zp>0、zv<0）的极角
        # 为负却是惜售；放量下跌（zp<0、zv>0）也不是过热。
        overheat = extreme & (zp > 0) & (zv > 0)
        capitulation = extreme & (zp < 0) & (zv < 0)
    pv_flags = {"pv_overheat": overheat, "pv_capitulation": capitulation}

    def _pv(key: str):
        def build(i: int) -> tuple[str, dict[str, Any]]:
            hot = key == "pv_overheat"
            deg = math.degrees(float(theta[i])) if np.isfinite(theta[i]) else 0.0
            pct = _pct_rank(d, i)
            move = (
                "价量同步向上偏离（过热）→ 防反转下行"
                if hot
                else "价量同步向下偏离（恐慌）→ 关注反弹"
            )
            return (
                f"价量马氏距离 {d[i]:.2f} 处于自身历史极端（分位 {pct:.0%}，"
                f"参考基准 p{int(PV_HIGH_PCT * 100)}={hi_at[i]:.2f}），"
                f"价格偏离 {zp[i]:+.1f}σ / 量能偏离 {zv[i]:+.1f}σ（极角 {deg:.0f}°）→ {move}",
                {
                    "distance": _num(d[i], 3),
                    "percentile": _num((pct or 0.0) * 100, 1),
                    "theta_deg": _num(deg, 1),
                    "price_z": _num(zp[i], 2),
                    "volume_z": _num(zv[i], 2),
                    "ref_hi": _num(hi_at[i], 3),
                    "window": PV_WINDOW,
                },
            )

        return build

    _emit("pv_overheat", pv_flags["pv_overheat"], scan_start, _pv("pv_overheat"))
    _emit("pv_capitulation", pv_flags["pv_capitulation"], scan_start, _pv("pv_capitulation"))

    # ── 环境过滤（ADX）───────────────────────────────────────────
    adx, pdi, mdi = adx_state(h, lo, c_)
    state = regime_of(adx)
    base["regime"] = {
        "state": state,
        "state_zh": REGIME_ZH[state],
        "adx": adx,
        "plus_di": pdi,
        "minus_di": mdi,
    }

    # ── 整理：同一信号只保留**最近一次**，并附上窗口内命中次数 ──────
    # 信号（尤其「价量共振」「PVT 上行」）是**状态**而非一次性事件，逐日罗列会让
    # 榜单噪声爆炸（同一只票 10 天里出现 8 条同名信号）。这里按 key 去重取最近一次，
    # 把「窗口内出现过几次」作为 hits_in_window 保留，信息不丢、页面可读。
    hits = [x for x in hits if x.bars_ago <= lookback_days]
    by_key: dict[str, SignalHit] = {}
    occurrences: dict[str, int] = {}
    for x in hits:
        occurrences[x.key] = occurrences.get(x.key, 0) + 1
        cur = by_key.get(x.key)
        if cur is None or x.bars_ago < cur.bars_ago:
            by_key[x.key] = x
    if dedupe:
        sigs = sorted(by_key.values(), key=lambda x: (x.bars_ago, x.key))
    else:
        sigs = sorted(hits, key=lambda x: (x.bars_ago, x.key))

    cat_order = {"trend": 0, "reversal": 1}
    distinct = list(by_key.values())
    cats = sorted({x.category for x in distinct}, key=lambda k: cat_order.get(k, 9))
    newest_ago = min((x.bars_ago for x in distinct), default=None)
    latest = sorted(x.key for x in distinct if x.bars_ago == 0)
    sig_dicts = []
    for x in sigs:
        d0 = x.to_dict()
        d0["hits_in_window"] = occurrences.get(x.key, 1)
        sig_dicts.append(d0)

    base.update(
        signals=sig_dicts,
        latest_signals=latest,
        categories=cats,
        newest_bars_ago=newest_ago,
        counts={
            "trend": sum(1 for x in distinct if x.category == "trend"),
            "reversal": sum(1 for x in distinct if x.category == "reversal"),
        },
    )
    base["metrics"] = {
        "vol_ratio_vs_ma5": _num(v[last] / ma_v_short_prev[last]) if ma_v_short_prev[last] else None,
        "dist_to_20d_high_pct": _num((c_[last] / high_n_prev[last] - 1) * 100)
        if high_n_prev[last] else None,
        "pv_distance": _num(d[last], 3),
    }

    # ── T+1 可成交性 ────────────────────────────────────────────
    # 以**最近一条信号所在的那根**为基准判断次日能否成交（信号可能出在昨天，
    # 今天就是执行日；只看「当日信号」会漏掉这种情况）。
    hint = "t 日收盘出信号 → t+1 开盘执行（A 股 T+1，当日买入次日才能卖）"
    tradable: dict[str, Any] = {
        "exec_hint": hint,
        "pending_next_bar": True,
        "signal_date": None,
        "blocked": None,
        "blocked_zh": None,
        "next_date": None,
        "limit_pct": limit_pct,
    }
    if newest_ago is not None:
        sig_idx = last - newest_ago
        newest_keys = [x.key for x in sigs if x.bars_ago == newest_ago]
        tradable["signal_date"] = _dstr(sig_idx)
        bullish = any(SIGNAL_META[k]["direction"] == "bullish" for k in newest_keys)
        bearish = any(SIGNAL_META[k]["direction"] == "bearish" for k in newest_keys)
        direction = "bullish" if bullish else ("bearish" if bearish else "")
        # 混合方向（同时有看多与看空信号）不做封板判定，交由使用者看明细
        if bullish and bearish:
            direction = ""
        code, zh = _sealed(o, h, lo, c_, sig_idx, direction, limit_pct) if direction else (None, None)
        if sig_idx + 1 < n:
            tradable.update(
                pending_next_bar=False,
                blocked=code,
                blocked_zh=zh,
                next_date=_dstr(sig_idx + 1),
            )
    base["tradable"] = tradable
    return base


def signal_events(
    candles: Sequence[Any], *, lookback_days: int = 500, limit_pct: float = 10.0
) -> dict[str, list[int]]:
    """只取信号发生的 **bar 索引**（不生成文案）。

    事件研究与回测必须复用**同一套判据**，不能各写一份——两份实现迟早会漂移。
    """
    res = analyze(
        candles,
        lookback_days=lookback_days,
        limit_pct=limit_pct,
        with_reason=False,
        dedupe=False,
    )
    last = len(candles) - 1
    out: dict[str, list[int]] = {}
    for s in res["signals"]:
        out.setdefault(s["key"], []).append(last - int(s["bars_ago"]))
    for k in out:
        out[k].sort()
    return out


def signal_score(signals: list[dict[str, Any]]) -> float:
    """展示用强度分（0~100，**不参与任何打分**，仅用于排序）。

    越近的信号权重越高；趋势类与反转类分开看，不混成一个「好不好」的结论。
    """
    if not signals:
        return 0.0
    w = {0: 1.0, 1: 0.85, 2: 0.7, 3: 0.6, 4: 0.5}
    total = 0.0
    for s in signals:
        rec = w.get(int(s.get("bars_ago") or 0), 0.35)
        total += rec * (1.0 if s.get("category") == "trend" else 0.9)
    return round(min(100.0, total * 34.0), 1)
