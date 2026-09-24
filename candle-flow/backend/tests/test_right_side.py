"""右侧信号检测器（core/right_side.py）单测。

覆盖：三步 N 字结构（破局/回踩/起爆）、三条过滤器（3日原则、共振；缩量降级
为展示）、周线平台突破+2倍量、数据不足、量纲/零量边界。

合成 K 线构造，不依赖数据库与网络。

判据演进（2026-09-23）：旧判据「EMA12 上穿 EMA50 + RSI≥50」实测会把超跌
反弹误判成趋势反转（焦作万方 000612：9-16 一根 +5.42% 大阳触发金叉即亮灯，
但破局后 1 日即滞涨、无量起爆，且主力资金连续净流出）→ 重做为 N 字三步。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta

from app.core.right_side import detect_nshape, detect_right_side


@dataclass
class Bar:
    date: date
    open: float
    high: float
    low: float
    close: float
    volume: float


BASE = date(2026, 1, 5)  # 周一
BASE_VOL = 1_000_000.0


def make_bars(
    closes: list[float],
    volumes: list[float] | None = None,
    start: date = BASE,
) -> list[Bar]:
    vols = volumes or [BASE_VOL] * len(closes)
    return [
        Bar(date=start + timedelta(days=i), open=c, high=c * 1.01, low=c * 0.99, close=c, volume=v)
        for i, (c, v) in enumerate(zip(closes, vols))
    ]


# ══ N 字结构合成器 ══════════════════════════════════════════════════════════


def nshape_series(
    *,
    breakout_pct: float = 6.0,
    breakout_vol_mult: float = 2.0,
    pullback_days: int = 3,
    pullback_drop_pct: float = 0.8,
    pullback_vol_mult: float = 1.8,
    boom_pct: float = 4.0,
    boom_vol_mult: float = 2.0,
    tail_days: int = 0,
    base_len: int = 90,
) -> tuple[list[float], list[float]]:
    """合成一段「横盘 → 破局大阳 → 缩量回踩 → 放量起爆」的收盘/成交量。

    ``tail_days``：起爆后追加的横盘天数（用于测新鲜度/3日原则）。
    返回 ``(closes, volumes)``，长度 = base_len + 1 + pullback_days + 1 + tail_days。
    ``base_len`` 默认 90，保证总长 > ``MIN_BARS``(80)，且 EMA50 可算。
    """
    closes = [50.0 + (i % 5) * 0.2 for i in range(base_len)]      # 50.0~50.8 箱体
    vols = [BASE_VOL] * base_len

    pre = closes[-1]
    # ① 破局大阳：突破前 20 日高点
    closes.append(pre * (1 + breakout_pct / 100))
    vols.append(BASE_VOL * breakout_vol_mult)
    # ② 回踩：连续小阴，不破大阳实体中点
    for k in range(pullback_days):
        closes.append(closes[-1] * (1 - pullback_drop_pct / 100))
        vols.append(BASE_VOL * pullback_vol_mult)
    # ③ 起爆：放量中阳，破回调段高点
    closes.append(closes[-1] * (1 + boom_pct / 100))
    vols.append(BASE_VOL * boom_vol_mult)
    # 尾声横盘
    for _ in range(tail_days):
        closes.append(closes[-1])
        vols.append(BASE_VOL)
    return closes, vols


def test_nshape_full_pattern_passes():
    """完整 N 字：破局 → 回踩 → 起爆，且均线多头 + MACD 共振 → 趋势反转成立。"""
    closes, vols = nshape_series()
    res = detect_nshape(closes, vols)
    assert res["ok"] is True, f"[{res['stage']}] {res['reason']}"
    assert res["stage"] == "通过"
    assert res["b1_gap"] >= 3
    assert res["b2_days"] >= 1
    assert res["b3_gap"] <= 5
    assert res["ma_bull"] is True
    assert res["macd_bull"] is True

    rs = detect_right_side(make_bars(closes, vols))
    assert rs.trend_reversal is True, rs.detail
    assert "趋势反转" in rs.signals
    assert rs.nshape_stage == "通过"
    assert rs.breakout_gap_days is not None
    assert rs.pullback_days is not None
    # 自证文案必须带数值（铁律 4）
    assert "破局" in rs.detail and "起爆" in rs.detail


def test_nshape_requires_breakout_big_bull():
    """① 破局必须是大阳：涨幅不足 5% 不算破局。"""
    closes, vols = nshape_series(breakout_pct=3.0)
    res = detect_nshape(closes, vols)
    assert res["ok"] is False
    assert res["stage"] == "①破局", res["reason"]


def test_nshape_requires_breakout_volume():
    """① 破局必须放量：量比不足 1.5 不算破局。"""
    closes, vols = nshape_series(breakout_vol_mult=1.2)
    res = detect_nshape(closes, vols)
    assert res["ok"] is False
    assert res["stage"] == "①破局", res["reason"]


def test_nshape_requires_break_resistance():
    """① 破局必须突破关键阻力位（前 20 日高点 = 箱体上沿）。"""
    # 前 20 日有一根远超后续箱体的高点，破局日无法越过
    closes = [50.0 + (i % 5) * 0.2 for i in range(80)]
    closes[65] = 60.0                     # 箱体上沿 = 60
    vols = [BASE_VOL] * len(closes)
    closes.append(closes[-1] * 1.06)      # +6%，但 50.8*1.06=53.8 < 60
    vols.append(BASE_VOL * 2.0)
    for _ in range(5):
        closes.append(closes[-1] * 0.995)
        vols.append(BASE_VOL)
    res = detect_nshape(closes, vols)
    assert res["ok"] is False
    assert res["stage"] == "①破局", res["reason"]


def test_nshape_three_day_rule_rejects_fake_breakout():
    """滤B 3 日原则：破局后第 3 日跌回突破前收盘下方 → 假突破。"""
    closes, vols = nshape_series()
    # 破局日下标 = base_len-1；把破局后第 2 日（3 日原则窗口内）打回破局前收盘
    b1 = 89
    closes[b1 + 2] = closes[b1 - 1] * 0.99
    res = detect_nshape(closes, vols)
    assert res["ok"] is False
    assert res["stage"] == "滤B-3日", res["reason"]
    assert "假突破" in res["reason"]


def test_nshape_pullback_must_hold_midpoint():
    """② 回踩不破大阳实体中点（低点抬高），破了就否。

    注意：回踩过深时可能先被「滤B 3日原则」拦下（跌破突破前收盘比跌破实体
    中点更早发生）。两种拦截都证明「该回调不健康」，故断言接受二者之一，
    但**必须**是「未通过」且带明确数值原因。
    """
    closes, vols = nshape_series(pullback_drop_pct=5.0, pullback_days=4)
    res = detect_nshape(closes, vols)
    assert res["ok"] is False
    assert res["stage"] in ("②回踩", "滤B-3日"), res["reason"]
    assert ("实体中点" in res["reason"]) or ("假突破" in res["reason"]), res["reason"]


def test_nshape_pullback_not_finished_rejected():
    """② 破局后一路下跌无回升 → 回调未结束，不算回踩成功。"""
    closes = [50.0 + (i % 5) * 0.2 for i in range(80)]
    vols = [BASE_VOL] * 80
    closes.append(closes[-1] * 1.06)
    vols.append(BASE_VOL * 2.0)
    for _ in range(6):                      # 连续阴跌，无一日回升
        closes.append(closes[-1] * 0.995)
        vols.append(BASE_VOL)
    res = detect_nshape(closes, vols)
    assert res["ok"] is False
    # 跌幅轻微时先撞「回调未结束」；跌幅大时先撞 3 日原则。二者都算正确拦截。
    assert res["stage"] in ("②回踩", "滤B-3日"), res["reason"]
    assert ("回调未结束" in res["reason"]) or ("假突破" in res["reason"]), res["reason"]


def test_pullback_shrink_is_display_only_not_gate():
    """滤A「回踩缩量」降级为展示字段：回踩量比 >1（未缩量）**不再**否决。

    实测依据：分母取破局日量时 p50=0.95（仅 6/177 ≤0.5），取破局前 20 日均量
    时 p50=2.94（0/177 ≤0.5）—— 回调段常只有 1 日，仍处天量余波，日线测不出
    「缩量」。保留硬门槛会让判据整体失效（177 → 6）。故只展示不卡准入。
    """
    # 回踩幅度浅（不破中点）+ 回踩量放大（远未缩量）→ 仍应通过
    closes, vols = nshape_series(
        pullback_drop_pct=0.3, pullback_vol_mult=3.0, boom_vol_mult=3.0
    )
    res = detect_nshape(closes, vols)
    assert res["ok"] is True, f"[{res['stage']}] {res['reason']}"
    assert res["pullback_shrink"] is not None
    assert res["pullback_shrink"] > 0.5, "本用例刻意构造未缩量的回踩"


def test_nshape_boom_must_break_pullback_high():
    """③ 起爆必须突破回调段高点。"""
    closes, vols = nshape_series(boom_pct=0.5)
    res = detect_nshape(closes, vols)
    assert res["ok"] is False
    assert res["stage"] == "③起爆", res["reason"]


def test_nshape_boom_requires_volume():
    """③ 起爆必须放量（量比 ≥1.5）。"""
    closes, vols = nshape_series(boom_vol_mult=0.8)
    res = detect_nshape(closes, vols)
    assert res["ok"] is False
    assert res["stage"] == "③起爆", res["reason"]
    assert "量比" in res["reason"]


def test_nshape_stale_boom_rejected():
    """③ 起爆新鲜度：起爆距今天数超过窗口不认。"""
    closes, vols = nshape_series(tail_days=8)
    res = detect_nshape(closes, vols)
    assert res["ok"] is False
    assert res["stage"] == "③起爆", res["reason"]


def test_nshape_resonance_filter_rejects_ma_not_bullish():
    """滤C 多指标共振：均线非多头排列 → 否。

    构造：破局/回踩/起爆都成立，但整体仍是长期下跌趋势（MA5<MA10<MA20）。
    """
    closes = [80.0 - i * 0.5 for i in range(80)]     # 单边下跌，MA 空头排列
    vols = [BASE_VOL] * 80
    pre = closes[-1]
    closes.append(pre * 1.06)                        # 破局大阳
    vols.append(BASE_VOL * 2.0)
    for _ in range(2):
        closes.append(closes[-1] * 0.985)
        vols.append(BASE_VOL * 1.8)
    closes.append(closes[-1] * 1.04)                 # 起爆
    vols.append(BASE_VOL * 2.0)
    res = detect_nshape(closes, vols)
    assert res["ok"] is False
    # 单边下跌中「破局」可能先撞阻力位（前 20 日高点远在上方），
    # 只要正确拦截即可；若通过前两步则必须是滤C 拦下。
    assert res["stage"] in ("①破局", "滤C-共振"), res["reason"]
    if res["stage"] == "滤C-共振":
        assert "均线非多头" in res["reason"]


def test_nshape_insufficient_bars():
    res = detect_nshape([50.0] * 40, [BASE_VOL] * 40)
    assert res["ok"] is False
    assert res["stage"] == "数据"
    assert "不足" in res["reason"]


def test_nshape_zero_volume_does_not_crash():
    closes, _ = nshape_series()
    res = detect_nshape(closes, [0.0] * len(closes))
    assert res["ok"] is False
    assert res["stage"] == "①破局"


# ══ detect_right_side 集成 ═══════════════════════════════════════════════


def test_oversold_bounce_is_not_trend_reversal():
    """旧判据的核心缺陷回归测试：MA 金叉的短期修复 ≠ 趋势反转。

    构造：长期阴跌 → 一根大阳把 EMA12 推上 EMA50（旧判据会亮灯），
    但该大阳**未突破箱体上沿**（无破局）、且之后一路阴跌（无回踩/起爆）。
    新判据必须判 False。
    """
    closes = [100.0 - i * 0.3 for i in range(90)]
    last = closes[-1]
    for k in range(1, 5):                    # 连续 4 日反弹造出 EMA 金叉
        closes.append(last * (1.02 ** k))
    for k in range(1, 6):                    # 随后再度回落
        closes.append(closes[-1] * 0.985)
    rs = detect_right_side(make_bars(closes))
    # 允许 tech_state 因 EMA 关系落在「回踩」，但绝不报趋势反转
    assert rs.trend_reversal is False, rs.detail
    assert "趋势反转" not in rs.signals, rs.detail
    assert rs.nshape_stage != "通过"


def test_no_signal_on_pure_downtrend():
    closes = [120.0 - i * 0.15 for i in range(120)]
    rs = detect_right_side(make_bars(closes))
    assert rs.tech_state == "空头"
    assert rs.trend_reversal is False
    assert rs.signals == []
    assert "无右侧信号" in rs.detail


def test_volume_breakout_on_platform():
    """独立第二信号：周线平台突破 + 当日量 ≥2 × 前20日均量。"""
    closes = [50.0 + (i % 5) * 0.4 for i in range(85)]      # 50.0~51.6 窄幅平台
    closes.append(55.0)
    vols = [BASE_VOL] * 85 + [BASE_VOL * 2.5]
    rs = detect_right_side(make_bars(closes, vols))
    assert rs.volume_breakout is True, rs.detail
    assert "量价突破" in rs.signals
    assert "量比" in rs.detail and "突破" in rs.detail


def test_breakout_without_volume_fails():
    closes = [50.0 + (i % 5) * 0.4 for i in range(85)] + [55.0]
    vols = [BASE_VOL] * 85 + [BASE_VOL * 1.2]
    rs = detect_right_side(make_bars(closes, vols))
    assert rs.volume_breakout is False
    assert "量比" in rs.detail


def test_trending_market_is_not_platform():
    closes = [40.0 + i * 0.35 for i in range(85)] + [72.0]
    vols = [BASE_VOL] * 85 + [BASE_VOL * 3.0]
    rs = detect_right_side(make_bars(closes, vols))
    assert rs.volume_breakout is False, rs.detail
    assert "趋势非平台" in rs.detail or "平台" in rs.detail


def test_insufficient_bars():
    rs = detect_right_side(make_bars([100.0] * 40))
    assert rs.tech_state == "数据不足"
    assert rs.signals == []
    assert "不足" in rs.detail


def test_zero_volumes_do_not_crash():
    closes = [100.0 - i * 0.05 for i in range(80)]
    rs = detect_right_side(make_bars(closes, [0.0] * 80))
    assert rs.tech_state in ("空头", "回踩", "多头")
    assert rs.volume_breakout is False


def test_main_flow_is_display_only():
    """资金面仅展示、不进必要条件（铁律 13：资金面仅展示不进分）。"""
    closes, vols = nshape_series()
    rs_ok = detect_right_side(make_bars(closes, vols), main_flow=5e7)
    rs_out = detect_right_side(make_bars(closes, vols), main_flow=-5e7)
    assert rs_ok.trend_reversal == rs_out.trend_reversal is True
    assert rs_ok.main_flow_net == 5e7
    assert rs_out.main_flow_net == -5e7


def test_as_dict_contains_new_fields():
    closes, vols = nshape_series()
    d = detect_right_side(make_bars(closes, vols)).as_dict()
    for key in (
        "nshape_stage", "nshape_detail", "breakout_gap_days", "pullback_days",
        "pullback_shrink", "boom_gap_days", "ma_bullish", "macd_bullish",
        "macd_fresh_cross", "main_flow_net",
    ):
        assert key in d, f"as_dict 缺少字段 {key}"


# ══ MACD ════════════════════════════════════════════════════════════════


def test_macd_state_uses_status_reading():
    """MACD 用「DIF > DEA」状态读法，不用「近 N 日新金叉」动作读法。

    实测来源：002519 银河电子 DIF 连续 8 日在 DEA 上方（长期多头排列、
    期间无新金叉），动作读法会误判成「MACD未金叉」而误杀。线上合格池两种
    读法差异 20 vs 3。
    """
    from app.core.right_side import macd_state

    closes = [100.0 - i * 0.1 for i in range(80)]
    last = closes[-1]
    for k in range(1, 7):
        closes.append(last * (1.015 ** k))
    bullish, fresh, dif, dea = macd_state(closes)
    assert dif is not None and dea is not None
    assert bullish is (dif > dea)
    if not fresh:
        assert bullish is True, "长期多头排列不应被动作读法判成未金叉"


def test_macd_state_short_series_returns_none():
    from app.core.right_side import macd_state

    bullish, fresh, dif, dea = macd_state([100.0] * 10)
    assert bullish is False and fresh is False
    assert dif is None and dea is None


# ══ 旧 API 兼容：ema/rsi helper 仍可用 ═══════════════════════════════════


def test_ema_and_rsi_helpers():
    from app.core.right_side import ema_series, rsi14_at

    vals = [float(i) for i in range(1, 61)]
    ema = ema_series(vals, 12)
    assert ema[10] is None and ema[11] is not None
    rsi = rsi14_at(vals, 40)
    assert rsi == 100.0, "单调上涨的 RSI 应为 100"
    assert rsi14_at(vals, 5) is None
