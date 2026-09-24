"""右侧信号检测器（观察池择时层）。

设计口径（2026-09-23 用户第二版提案：**三步 N 字结构**）：

1. **观察池本身不设技术面门槛** —— 基本面合格的标的即使处于空头趋势/震荡
   也保留在池内（磨底阶段的技术面破位常是市场非理性，非基本面恶化）。
   本模块只做两件事：给池内标的标记「技术状态」，在右侧信号出现时点亮提示。

2. **趋势反转 = 三步 N 字结构**（取代旧的「EMA12 上穿 EMA50 + RSI≥50」）。

   旧判据的致命缺陷（用户实测指出，已证伪）：EMA 金叉只反映**均线的短期
   修复**，会把「超跌反弹」误判成「趋势反转」。实测焦作万方 000612：
   9-16 一根 +5.42% 大阳使 EMA12 上穿 EMA50，旧判据立即亮灯；但该股
   ①基本面有重组终止利空 ②主力资金 09-22/09-23 连续净流出
   ③破局后只有 1 日回踩就滞涨、无量起爆。技术面「一瞬间的金叉」不等于
   资金/消息/盘面的共振。

   N 字三步（缺一不可）：
     ① **破局**：一根放量**大阳**（涨幅 ≥ ``BREAKOUT_PCT_MIN``）**突破关键
        阻力位**（前 ``RESISTANCE_LB`` 日最高收盘 = 箱体上沿），且当日量
        ≥ ``BREAKOUT_VOL_MULT`` × 前 ``VOL_AVG_DAYS`` 日均量。
     ② **回踩确认**：破局后出现**一段回调**并已结束（出现回升日），回调
        最低收盘**不破大阳实体中点**（低点抬高），回调天数 ≤ ``PULLBACK_MAX_DAYS``。
     ③ **起爆**：回调低点之后**再次放量突破回调段高点**（涨幅 ≥ ``BOOM_MIN_CHG``、
        量比 ≥ ``BREAKOUT_VOL_MULT``），且距今 ≤ ``BOOM_FRESH_DAYS`` 日（新鲜度）。

3. **三条过滤器**（用户提案，实测调参后保留两条 + 一条降级）：
   - **滤B 3日原则**（保留，硬）：破局后须连续 ``STAND_DAYS`` 个交易日收盘
     不低于突破前一日收盘，否则判「假突破」。
   - **滤C 多指标共振**（保留，硬）：均线多头排列 MA5>MA10>MA20，且 MACD
     的 DIF > DEA 且 DIF 在零轴附近或上方（``dif > -0.2``）。
   - **滤A 回踩缩量**（**降级为展示**，2026-09-23 全库实测后决定）：
     用户原意是「回踩量萎缩至前期一半以下」。实测全库两版分母：
     ① 分母 = 破局日量：p50 = 0.95，仅 6 只 ≤0.5；
     ② 分母 = 破局前 20 日均量：p50 = **2.94**（回调期量能普遍是突破前
     均量的 3 倍），**0 只** ≤0.5。
     原因是回调段常常只有 1 日（177 只里 121 只），仍处天量余波，日线级别
     测不出「缩量」。若保留硬门槛，通过数 177 → 6，**判据整体失效**。
     → 改为展示字段 ``pullback_shrink``，不卡准入。用户口径由「必选」变
     「可选确认」，与提案③「基本面打底」原则一致。

4. **资金面（主力资金）降级为展示**（2026-09-23 用户决定）：
   用户要求「资金/消息/盘面共振」为真反转必要条件。资金面数据源已接入
   （``services/market_flow.py``，东财 datacenter 全市场 5199 只 3.5s）；
   但该源**只提供当日快照、无历史日期**，「持续净流入」需逐日采集累积。
   → 本期**只展示**「主力净流入/流出 N 万」，**不进必要条件**（不改铁律 13：
   资金面仅展示不进分）。实测 49 只技术面候选里 31 只是主力净流出（63%），
   展示即可让用户自行否决，无需硬门槛。

5. **筹码结构（低位单峰密集、高位套牢盘出清）不做**：数据源无（铁律 13）。

6. 所有输出带数值自证文案（指标名/展示值同口径，铁律 4）；数据不足显式
   返回「数据不足」，不静默伪装成「无信号」（铁律 13 精神）。

7. **量能维度整体降级为展示标签**（沿用 2026-09-23 决策）：线上合格池
   A/B 实测 — 单用 EMA 金叉得 36 只，加「量比≥2」只剩 6 只且 4 只是 C/D/E
   档（含全场最低 33.9 E），被砍掉的是焦作万方 82.2 / 中国人寿 81.3 等。
   量能是交易行为的**结果**而非资产质量的**因**，当准入闸门会系统性歧视
   低估值优质股，违背提案①「基本面打底」原则。
   → 但 N 字结构的①③两步**必须**有量能确认（这是「突破」的定义本身，
   无量的突破不是突破），故在这两步内保留量能判据，只是不额外加门槛。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Sequence

from app.core.timeframe import to_weekly

# ── 三步 N 字结构 ──
BREAKOUT_PCT_MIN = 5.0        # ① 破局大阳涨幅下限（%）
BREAKOUT_VOL_MULT = 1.5       # ① 破局量 ≥ 1.5 × 20日均量；③ 起爆同阈值
VOL_AVG_DAYS = 20
RESISTANCE_LB = 20            # ① 关键阻力位回溯窗（前 20 日最高收盘 = 箱体上沿）
NSHAPE_WINDOW = 30            # ① 破局日回溯窗（给「破局→回踩→起爆」留时间）
STAND_DAYS = 3                # 滤B：3 日原则
PULLBACK_MAX_DAYS = 15        # ② 回调段最长天数
BOOM_MIN_CHG = 3.0            # ③ 起爆中阳涨幅下限（%）
BOOM_FRESH_DAYS = 5           # ③ 起爆新鲜度窗口

# ── 量价配合（周线平台突破，独立第二信号）──
WEEKLY_PLATFORM_WEEKS = 10
WEEKLY_PLATFORM_MAX_AMP = 30.0
VOLUME_MULT = 2.0

# ── MACD（滤C 共振 + 辅助展示）──
MACD_FAST = 12
MACD_SLOW = 26
MACD_SIGNAL = 9
MACD_ZERO_TOL = -0.2          # 滤C：DIF 允许略低于零轴（「零轴附近」）

MIN_BARS = 80                 # 破局回溯窗 30 + 阻力位 20 + 缓冲


def ema_series(values: list[float], period: int) -> list[float | None]:
    """标准 EMA（前 period 个的 SMA 作种子）。与 confluence._ema_series 同式。"""
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


def rsi14_at(closes: list[float], index: int) -> float | None:
    """Wilder RSI(14)。与 confluence.rsi_at 同式（平滑版）。"""
    period = 14
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
        return 100.0 if avg_gain > 0 else 50.0
    rs = avg_gain / avg_loss
    return 100.0 - 100.0 / (1.0 + rs)


def macd_state(closes: list[float]) -> tuple[bool, bool, float | None, float | None]:
    """MACD 状态（DIF > DEA 读法）。

    返回 ``(bullish, fresh_cross, dif, dea)``：
    - ``bullish``：DIF > DEA —— **状态读法**，多头排列期间持续为 True；
    - ``fresh_cross``：当日新金叉（DIF 上穿 DEA）。

    为什么用状态读法而非「近 N 日新金叉」：长期多头排列的票（DIF 连续 8 日
    在 DEA 上方，如 002519 银河电子）会被动作读法判成「未金叉」而误杀。
    两种读法实测差异 20 vs 3（线上合格池），状态读法与 EMA 金叉方向一致。
    """
    n = len(closes)
    if n < MACD_SLOW + MACD_SIGNAL:
        return False, False, None, None
    ema_f = ema_series(closes, MACD_FAST)
    ema_s = ema_series(closes, MACD_SLOW)
    dif_series: list[float | None] = [
        (ema_f[i] - ema_s[i]) if (ema_f[i] is not None and ema_s[i] is not None) else None
        for i in range(n)
    ]
    valid = [v for v in dif_series if v is not None]
    if len(valid) < MACD_SIGNAL:
        return False, False, None, None
    dea_vals = ema_series(valid, MACD_SIGNAL)
    dea_tail: list[float | None] = [None] * (n - len(valid)) + list(dea_vals)
    dif_now, dea_now = dif_series[-1], dea_tail[-1]
    if dif_now is None or dea_now is None:
        return False, False, dif_now, dea_now
    bullish = dif_now > dea_now
    fresh = False
    dif_p, dea_p = dif_series[-2], dea_tail[-2]
    if dif_p is not None and dea_p is not None:
        fresh = dif_p <= dea_p and dif_now > dea_now
    return bullish, fresh, dif_now, dea_now


def _pct(a: float, b: float) -> float:
    return (a - b) / b * 100 if b else 0.0


def detect_nshape(closes: list[float], volumes: list[float]) -> dict[str, Any]:
    """三步 N 字结构判定。返回含 ``ok`` / ``stage`` / ``reason`` 与全部中间量。

    ``stage`` 记录卡在哪一步（自证文案用，铁律 4）：
    ``①破局`` / ``滤B-3日`` / ``②回踩`` / ``③起爆`` / ``滤C-共振`` / ``通过``
    """
    n = len(closes)
    d: dict[str, Any] = {"ok": False, "stage": "", "reason": ""}
    if n < MIN_BARS:
        d.update(stage="数据", reason=f"K线{n}根不足{MIN_BARS}")
        return d
    last = n - 1

    # ══ ① 破局：放量大阳 + 突破关键阻力位（前 20 日高点 = 箱体上沿）══
    b1 = None
    for i in range(last - STAND_DAYS, last - NSHAPE_WINDOW - 1, -1):
        if i <= RESISTANCE_LB:
            break
        if _pct(closes[i], closes[i - 1]) < BREAKOUT_PCT_MIN:
            continue
        if closes[i] <= max(closes[i - RESISTANCE_LB:i]):
            continue
        avg = sum(volumes[i - VOL_AVG_DAYS:i]) / VOL_AVG_DAYS
        if avg <= 0 or volumes[i] < avg * BREAKOUT_VOL_MULT:
            continue
        b1 = i
        break
    if b1 is None:
        d.update(
            stage="①破局",
            reason=(
                f"近{NSHAPE_WINDOW}日内无「涨≥{BREAKOUT_PCT_MIN:.0f}% + "
                f"量≥{BREAKOUT_VOL_MULT}倍20日均量 + 破前{RESISTANCE_LB}日高点」的破局大阳"
            ),
        )
        return d
    b_chg = _pct(closes[b1], closes[b1 - 1])
    b_avg = sum(volumes[b1 - VOL_AVG_DAYS:b1]) / VOL_AVG_DAYS
    mid = (closes[b1] + closes[b1 - 1]) / 2
    d.update(
        b1=b1, b1_gap=last - b1, b1_chg=b_chg,
        b1_vr=(volumes[b1] / b_avg) if b_avg else 0.0,
        b1_mid=mid, b1_res=max(closes[b1 - RESISTANCE_LB:b1]),
    )

    # ══ 滤B：3 日原则（突破须连续 3 日站稳，否则假突破）══
    if last < b1 + STAND_DAYS:
        d.update(stage="滤B-3日", reason=f"破局距今仅{last - b1}日，不满{STAND_DAYS}日原则")
        return d
    for k in range(1, STAND_DAYS + 1):
        if closes[b1 + k] < closes[b1 - 1]:
            d.update(
                stage="滤B-3日",
                reason=(
                    f"破局后第{k}日收盘{closes[b1 + k]:.2f}跌回突破前"
                    f"{closes[b1 - 1]:.2f}下方（假突破）"
                ),
            )
            return d

    # ══ ② 回踩：定位「已完成的回调段」（出现回升日即结束）══
    li = None
    for j in range(b1 + 2, last + 1):
        if closes[j] > closes[j - 1]:
            li = j - 1
            break
    if li is None:
        d.update(
            stage="②回踩",
            reason=f"破局后连续{last - b1}日无回升，回调未结束（仍在下行）",
        )
        return d
    pb_days = li - b1
    if pb_days > PULLBACK_MAX_DAYS:
        d.update(stage="②回踩", reason=f"回调段{pb_days}日超{PULLBACK_MAX_DAYS}日，形态已过期")
        return d
    if closes[li] < mid:
        d.update(
            stage="②回踩",
            reason=f"回调最低{closes[li]:.2f}跌破大阳实体中点{mid:.2f}（低点未抬高）",
        )
        return d
    pb_vol_max = max(volumes[b1 + 1:li + 1]) if li > b1 else 0.0
    d.update(
        b2_days=pb_days, b2_low=closes[li], b2_low_gap=last - li,
        b2_volmax=pb_vol_max,
        # 滤A 降级为展示：回踩段最大量 / 破局日量（口径见模块 docstring 第 3 条）
        pullback_shrink=(pb_vol_max / volumes[b1]) if volumes[b1] else None,
    )

    # ══ ③ 起爆：低点之后放量突破回调段高点 ══
    after = closes[li + 1:last + 1]
    if not after:
        d.update(stage="③起爆", reason="回调低点在最后一根（距今0日），尚未起爆")
        return d
    pb_high = max(closes[li:last + 1])
    bi = li + 1 + max(range(len(after)), key=lambda k: after[k])
    bchg = _pct(closes[bi], closes[bi - 1])
    bavg = sum(volumes[bi - VOL_AVG_DAYS:bi]) / VOL_AVG_DAYS
    bvr = (volumes[bi] / bavg) if bavg > 0 else 0.0
    d.update(b3=bi, b3_gap=last - bi, b3_chg=bchg, b3_vr=bvr, b3_high=pb_high)
    miss: list[str] = []
    if closes[bi] < pb_high:
        miss.append(f"未破回调段高点{pb_high:.2f}（现{closes[bi]:.2f}）")
    if bchg < BOOM_MIN_CHG:
        miss.append(f"起爆涨幅{bchg:.1f}%<{BOOM_MIN_CHG:.0f}%")
    if bvr < BREAKOUT_VOL_MULT:
        miss.append(f"起爆量比{bvr:.1f}<{BREAKOUT_VOL_MULT}")
    if miss:
        d.update(stage="③起爆", reason="；".join(miss))
        return d
    if last - bi > BOOM_FRESH_DAYS:
        d.update(stage="③起爆", reason=f"起爆距今{last - bi}日超{BOOM_FRESH_DAYS}日新鲜度窗口")
        return d

    # ══ 滤C：多指标共振（均线多头排列 + MACD 零轴附近/上方金叉）══
    ma = [sum(closes[last - p + 1:last + 1]) / p for p in (5, 10, 20)]
    ma_prev = [sum(closes[last - p:last]) / p for p in (5, 10, 20)]
    ma_bull = ma[0] > ma[1] > ma[2]
    ma_cross = ma_prev[0] <= ma_prev[1] and ma[0] > ma[1]
    mb, mf, dif, dea = macd_state(closes)
    macd_ok = bool(mb) and dif is not None and dif > MACD_ZERO_TOL
    d.update(
        ma5=ma[0], ma10=ma[1], ma20=ma[2], ma_bull=ma_bull, ma_cross=ma_cross,
        macd_bull=mb, macd_fresh=mf, dif=dif, dea=dea,
    )
    miss = []
    if not ma_bull:
        miss.append(f"均线非多头排列（MA5 {ma[0]:.2f}/MA10 {ma[1]:.2f}/MA20 {ma[2]:.2f}）")
    if not macd_ok:
        dif_txt = "不可算" if dif is None else f"{dif:.3f}"
        miss.append(f"MACD 未在零轴附近/上方金叉（DIF={dif_txt}）")
    if miss:
        d.update(stage="滤C-共振", reason="；".join(miss))
        return d

    d["ok"] = True
    d["stage"] = "通过"
    d["note"] = (
        f"破局{closes[b1]:.2f}(+{b_chg:.1f}%，量比{d['b1_vr']:.1f}，{d['b1_gap']}日前) "
        f"→回调{pb_days}日低{closes[li]:.2f}({d['b2_low_gap']}日前) "
        f"→起爆{closes[bi]:.2f}(+{bchg:.1f}%，量比{bvr:.1f}，{last - bi}日前)"
    )
    return d


@dataclass
class RightSideResult:
    tech_state: str = "数据不足"
    trend_reversal: bool = False
    volume_breakout: bool = False
    rsi14: float | None = None
    # N 字结构明细（自证 + 前端展示）
    nshape_stage: str = ""
    nshape_detail: str = ""
    breakout_gap_days: int | None = None
    pullback_days: int | None = None
    pullback_shrink: float | None = None
    boom_gap_days: int | None = None
    # 均线/MACD 共振辅助
    ma_bullish: bool = False
    macd_bullish: bool = False
    macd_fresh_cross: bool = False
    # 资金面（仅展示，不进必要条件，铁律 13）
    main_flow_net: float | None = None
    signals: list[str] = field(default_factory=list)
    detail: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {
            "tech_state": self.tech_state,
            "trend_reversal": self.trend_reversal,
            "volume_breakout": self.volume_breakout,
            "rsi14": self.rsi14,
            "nshape_stage": self.nshape_stage,
            "nshape_detail": self.nshape_detail,
            "breakout_gap_days": self.breakout_gap_days,
            "pullback_days": self.pullback_days,
            "pullback_shrink": self.pullback_shrink,
            "boom_gap_days": self.boom_gap_days,
            "ma_bullish": self.ma_bullish,
            "macd_bullish": self.macd_bullish,
            "macd_fresh_cross": self.macd_fresh_cross,
            "main_flow_net": self.main_flow_net,
            "signals": list(self.signals),
            "detail": self.detail,
        }


def detect_right_side(bars: Sequence[Any], main_flow: float | None = None) -> RightSideResult:
    """对一段升序日线检测右侧信号。

    ``bars``：升序 K 线（KlineData / Candle / 任何有 close/volume/date 的对象）。
    ``main_flow``：当日主力资金净流入（元），**仅展示**，不参与判定。
    数据不足时返回 ``tech_state="数据不足"`` 并在 detail 写明原因，不抛异常。
    """
    if len(bars) < MIN_BARS:
        return RightSideResult(
            main_flow_net=main_flow,
            detail=f"K线{len(bars)}根，不足{MIN_BARS}根，无法评估右侧信号",
        )

    closes = [float(b.close) for b in bars]
    volumes = [float(getattr(b, "volume", 0) or 0) for b in bars]
    last = len(bars) - 1
    close_now = closes[last]

    # ── 技术状态（短/中均线关系，与 N 字判据同源数据）──
    ema_fast = ema_series(closes, 12)
    ema_slow = ema_series(closes, 50)
    f_now, s_now = ema_fast[last], ema_slow[last]
    if f_now is None or s_now is None:
        return RightSideResult(
            main_flow_net=main_flow,
            detail=f"K线{len(bars)}根，EMA50 不可算",
        )
    if f_now > s_now and close_now > s_now:
        tech_state = "多头"
    elif f_now > s_now:
        tech_state = "回踩"
    else:
        tech_state = "空头"

    # ── 趋势反转 = 三步 N 字结构 ──
    ns = detect_nshape(closes, volumes)
    trend_reversal = bool(ns["ok"])
    rsi = rsi14_at(closes, last)

    # ── 量价配合：周线平台突破 + 当日量 ≥ 2 × 前20日均量（独立第二信号）──
    volume_breakout = False
    weekly_note = ""
    try:
        weeks = to_weekly(bars)
        if len(weeks) < WEEKLY_PLATFORM_WEEKS + 1:
            weekly_note = f"周线仅{len(weeks)}周不足{WEEKLY_PLATFORM_WEEKS + 1}周"
        else:
            platform = weeks[-(WEEKLY_PLATFORM_WEEKS + 1): -1]   # 不含当前未完成周
            p_high = max(float(w.close) for w in platform)
            p_low = min(float(w.close) for w in platform)
            amp = (p_high - p_low) / p_high * 100 if p_high > 0 else None
            broke = close_now > p_high
            platform_ok = amp is not None and amp <= WEEKLY_PLATFORM_MAX_AMP
            if broke and not platform_ok and amp is not None:
                weekly_note = (
                    f"突破但前{WEEKLY_PLATFORM_WEEKS}周振幅{amp:.0f}%"
                    f"超{WEEKLY_PLATFORM_MAX_AMP:.0f}%（趋势非平台）"
                )
            elif broke:
                avg_vol = (
                    sum(volumes[-(VOL_AVG_DAYS + 1): -1]) / VOL_AVG_DAYS
                    if len(volumes) >= VOL_AVG_DAYS + 1 else 0
                )
                if avg_vol > 0:
                    vol_ratio = volumes[last] / avg_vol
                    if vol_ratio >= VOLUME_MULT:
                        volume_breakout = True
                        weekly_note = (
                            f"收盘{close_now:.2f}突破{WEEKLY_PLATFORM_WEEKS}周平台高点{p_high:.2f}"
                            f"（振幅{amp:.0f}%），量比{vol_ratio:.1f}倍"
                        )
                    else:
                        weekly_note = f"已突破平台但量比{vol_ratio:.1f}不足{VOLUME_MULT}倍"
                else:
                    weekly_note = "突破平台但前20日均量为0，量能不可判"
            else:
                weekly_note = f"收盘{close_now:.2f}未突破{WEEKLY_PLATFORM_WEEKS}周平台高点{p_high:.2f}"
    except Exception:   # 周线合成失败不拖垮整个检测
        weekly_note = "周线合成失败"

    signals: list[str] = []
    if trend_reversal:
        signals.append("趋势反转")
    if volume_breakout:
        signals.append("量价突破")

    # ── 自证文案（铁律 4：带数值、口径同源）──
    if trend_reversal:
        n_txt = f"N字型态成立：{ns['note']}"
    else:
        n_txt = f"N字未成（卡在{ns['stage']}）：{ns['reason']}"
    rsi_txt = f"RSI{rsi:.0f}" if rsi is not None else "RSI不可算"
    macd_bull = bool(ns.get("macd_bull"))
    ma_bull = bool(ns.get("ma_bull"))
    macd_txt = "MACD多头" if macd_bull else "MACD空头"
    ma_txt = "均线多头" if ma_bull else "均线未多头"
    parts = [n_txt, f"{ma_txt}/{macd_txt}", rsi_txt, weekly_note or "周线不可判"]
    if not signals:
        parts.append("无右侧信号")
    detail = "；".join(parts)

    return RightSideResult(
        tech_state=tech_state,
        trend_reversal=trend_reversal,
        volume_breakout=volume_breakout,
        rsi14=rsi,
        nshape_stage=ns["stage"],
        nshape_detail=ns["reason"] or ns.get("note", ""),
        breakout_gap_days=ns.get("b1_gap"),
        pullback_days=ns.get("b2_days"),
        pullback_shrink=ns.get("pullback_shrink"),
        boom_gap_days=ns.get("b3_gap"),
        ma_bullish=ma_bull,
        macd_bullish=macd_bull,
        macd_fresh_cross=bool(ns.get("macd_fresh")),
        main_flow_net=main_flow,
        signals=signals,
        detail=detail,
    )
