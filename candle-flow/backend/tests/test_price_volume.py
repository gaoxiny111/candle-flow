"""价量策略引擎测试。

每条断言都对应一个**实际踩到过的坑**，不是为覆盖率凑数：
1. 均量基线含当日 → 放量倍数被系统性低估（伪代码常见写法）。
2. 「长上影」写成 ``(high-close) > 2*(close-open)`` → 任何阴线都命中。
3. 马氏距离「距离小 = 极端低位」→ 横盘天天报极端（距离是「离常态多远」，小 = 常态）。
4. PVT 背离不加趋势上下文 → 横盘里「与前高持平」被当成新高，天天报顶背离。
5. T+1 一字板 → 回测凭空赚到买不进的涨停。
"""

from datetime import datetime, timedelta

import numpy as np
import pytest

from app.core import price_volume as pv
from app.core.candle import Candle


def _series(closes, vols, *, start="2025-01-02"):
    d0 = datetime.strptime(start, "%Y-%m-%d")
    out = []
    for i, (c, v) in enumerate(zip(closes, vols)):
        prev = closes[i - 1] if i else c
        out.append(
            Candle(
                open=prev, high=max(prev, c) * 1.005, low=min(prev, c) * 0.995,
                close=c, volume=v, timestamp=d0 + timedelta(days=i),
            )
        )
    return out


def _flat(n=140, base=10.0, vol=10000):
    return [base + (i % 5) * 0.01 for i in range(n)], [vol] * n


def _keys(res):
    return {s["key"] for s in res["signals"]}


# ── 基础工具 ────────────────────────────────────────────────────────

def test_roll_mean_and_shift_semantics():
    x = np.array([1.0, 2.0, 3.0, 4.0, 5.0])
    m = pv._roll_mean(x, 3)
    assert np.isnan(m[0]) and np.isnan(m[1])
    assert m[2] == pytest.approx(2.0)      # (1+2+3)/3，含当日
    s = pv._shift(m, 1)
    assert np.isnan(s[2]) and s[3] == pytest.approx(2.0)  # 右移一位 = 不含当日


def test_adx_series_matches_latest_state():
    closes = [10.0 * (1.01 ** i) for i in range(200)]
    c = _series(closes, [10000 + i * 500 for i in range(200)])
    h = np.array([b.high for b in c], dtype=float)
    l = np.array([b.low for b in c], dtype=float)
    cl = np.array([b.close for b in c], dtype=float)
    adx_s, _p, _m = pv.adx_series(h, l, cl)
    adx, pdi, mdi = pv.adx_state(h, l, cl)
    assert adx == pytest.approx(round(float(adx_s[-1]), 2))
    assert adx > pv.ADX_TREND            # 单边上涨必是强趋势
    assert pv.regime_of(adx) == "trend"


def test_regime_labels():
    assert pv.regime_of(None) == "unknown"
    assert pv.regime_of(30.0) == "trend"
    assert pv.regime_of(10.0) == "range"
    assert pv.regime_of(22.0) == "neutral"


# ── 趋势类 ──────────────────────────────────────────────────────────

def test_volume_breakout_fires_and_volume_baseline_excludes_today():
    """放量突破命中；且倍数按**昨日为止**的均量算（含当日会明显偏低）。"""
    closes, vols = _flat(140, vol=10000)
    for _ in range(20):
        closes.append(closes[-1] * 1.004)
        vols.append(14000)
    closes.append(closes[-1] * 1.03)
    vols.append(60000)                      # 5 倍量
    res = pv.analyze(_series(closes, vols), lookback_days=3)
    hit = next(s for s in res["signals"] if s["key"] == "vol_breakout")
    ratio = hit["metrics"]["vol_ratio"]
    assert ratio == pytest.approx(60000 / 14000, rel=0.02)
    # 若把当日 60000 也算进 5 日均量（伪代码写法），倍数会掉到 ~2.4，差距显著
    assert ratio > 3.5


def test_breakout_requires_volume_expansion():
    """创 20 日新高但**没放量** → 不触发（量价配合是过滤器）。"""
    closes, vols = _flat(140, vol=10000)
    for _ in range(20):
        closes.append(closes[-1] * 1.004)
        vols.append(10000)
    closes.append(closes[-1] * 1.03)
    vols.append(10000)                      # 缩量新高
    res = pv.analyze(_series(closes, vols), lookback_days=3)
    assert "vol_breakout" not in _keys(res)


def test_pv_resonance_needs_both_sides():
    """价量共振要求价能与量能**同时**为正：只有价涨、量不活跃时不出信号。"""
    closes, vols = _flat(140, vol=10000)
    for _ in range(25):
        closes.append(closes[-1] * 1.004)
        vols.append(9000)                   # 量能持续低于长期均量
    res = pv.analyze(_series(closes, vols), lookback_days=2)
    assert "pv_resonance" not in _keys(res)


def test_pvt_trend_fires_on_rising_pvt():
    closes, vols = _flat(140, vol=10000)
    for _ in range(20):
        closes.append(closes[-1] * 1.004)
        vols.append(14000)
    closes.append(closes[-1] * 1.03)
    vols.append(60000)
    res = pv.analyze(_series(closes, vols), lookback_days=3)
    assert "pvt_trend" in _keys(res)


# ── 反转类 ──────────────────────────────────────────────────────────

def test_dry_volume_reversal_fires():
    closes, vols = _flat(140, vol=20000)
    for _ in range(15):
        closes.append(closes[-1] * 0.995)
        vols.append(20000)
    closes.append(closes[-1] * 1.01)
    vols.append(4000)                       # 地量 + 收阳
    res = pv.analyze(_series(closes, vols), lookback_days=3)
    assert "vol_dry_reversal" in _keys(res)


def test_dry_volume_requires_no_new_low():
    """持续创新低的地量（下跌中继）不算止跌。"""
    closes, vols = _flat(140, vol=20000)
    for _ in range(15):
        closes.append(closes[-1] * 0.99)    # 每天都是新低
        vols.append(4000)
    res = pv.analyze(_series(closes, vols), lookback_days=3)
    assert "vol_dry_reversal" not in _keys(res)


def test_heavy_volume_reversal_needs_long_upper_shadow():
    """天量 + 收阴但**没有长上影** → 不触发（放量下跌不等于派发）。"""
    closes, vols = _flat(140, vol=10000)
    for _ in range(10):
        closes.append(closes[-1] * 1.006)
        vols.append(12000)
    c = _series(closes, vols)
    base = closes[-1]
    # 光头阴线：开盘=最高，无上影
    c.append(Candle(open=base * 1.02, high=base * 1.02, low=base * 0.98,
                    close=base * 0.99, volume=50000,
                    timestamp=c[-1].timestamp + timedelta(days=1)))
    res = pv.analyze(c, lookback_days=3)
    assert "vol_heavy_reversal" not in _keys(res)


def test_heavy_volume_reversal_fires_with_shadow_even_if_close_above_prev():
    """★ 回归：放量冲高后收阴但**收盘仍在昨收之上**，同样是滞涨/派发。

    伪代码要求「收阴」是靠 ``(high-close) > 2*(close-open)`` 表达的，那个写法在
    阴线时右式为负、恒真；反过来把它改成「收盘低于前一日」又会漏掉这种形态。
    """
    closes, vols = _flat(140, vol=10000)
    for _ in range(10):
        closes.append(closes[-1] * 1.006)
        vols.append(12000)
    c = _series(closes, vols)
    base = closes[-1]
    c.append(Candle(open=base * 1.02, high=base * 1.08, low=base * 1.005,
                    close=base * 1.002, volume=50000,
                    timestamp=c[-1].timestamp + timedelta(days=1)))
    res = pv.analyze(c, lookback_days=3)
    hit = next(s for s in res["signals"] if s["key"] == "vol_heavy_reversal")
    assert hit["direction"] == "bearish"
    assert hit["metrics"]["upper_shadow_pct"] > 50


def test_mahalanobis_extreme_uses_quadrant_not_angle_sign():
    """★ 回归：缩量涨停（价偏离正、量偏离负）不得被判成「价量恐慌」。"""
    closes, vols = _flat(150, vol=10000)
    for _ in range(8):
        closes.append(closes[-1] * 1.02)
        vols.append(30000)
    res = pv.analyze(_series(closes, vols), lookback_days=2)
    keys = _keys(res)
    assert "pv_overheat" in keys           # 价量齐升 → 过热
    assert "pv_capitulation" not in keys   # 同向上行不可能同时是恐慌


def test_mahalanobis_low_distance_is_normal_not_extreme():
    """★ 回归：横盘（距离≈0 常态）不得报极端信号。"""
    res = pv.analyze(_series(*_flat(200)), lookback_days=10)
    assert "pv_overheat" not in _keys(res)
    assert "pv_capitulation" not in _keys(res)


def test_flat_market_emits_no_signals():
    """★ 回归：完美横盘序列曾天天报「PVT 顶背离」（平价即新高 + 斜率 -0.00 噪声）。"""
    res = pv.analyze(_series(*_flat(200)), lookback_days=10)
    assert res["signals"] == []
    assert res["regime"]["state"] == "range"


# ── 边界与 T+1 ──────────────────────────────────────────────────────

def test_insufficient_bars_returns_reason():
    res = pv.analyze(_series(*_flat(60)))
    assert res["insufficient"] is True
    assert "不足" in res["reason"]
    assert res["signals"] == []


def test_empty_input():
    res = pv.analyze([])
    assert res["insufficient"] is True
    assert res["reason"] == "无 K 线数据"


def test_exec_block_limit_up_sealed():
    """信号次日的**一字涨停**：买入无法成交。"""
    closes, vols = _flat(140)
    for _ in range(20):
        closes.append(closes[-1] * 1.004)
        vols.append(14000)
    closes.append(closes[-1] * 1.03)
    vols.append(60000)
    c = _series(closes, vols)
    prev = closes[-1]
    lim = round(prev * 1.1, 2)
    c.append(Candle(open=lim, high=lim, low=lim, close=lim, volume=8000,
                    timestamp=c[-1].timestamp + timedelta(days=1)))
    code, zh = pv.exec_block(c, len(c) - 2, direction="bullish")
    assert code == "limit_up_sealed"
    assert "一字涨停" in (zh or "")
    # 看空方向不受涨停影响
    assert pv.exec_block(c, len(c) - 2, direction="bearish") == (None, None)


def test_exec_block_limit_down_sealed():
    closes = [10.0] * 140
    vols = [10000] * 140
    c = _series(closes, vols)
    prev = closes[-1]
    lim = round(prev * 0.9, 2)
    c.append(Candle(open=lim, high=lim, low=lim, close=lim, volume=8000,
                    timestamp=c[-1].timestamp + timedelta(days=1)))
    code, _ = pv.exec_block(c, len(c) - 2, direction="bearish")
    assert code == "limit_down_sealed"


def test_exec_block_pending_when_no_next_bar():
    c = _series(*_flat(140))
    assert pv.exec_block(c, len(c) - 1, direction="bullish") == (None, None)


def test_tradable_pending_on_last_bar():
    closes, vols = _flat(140, vol=10000)
    for _ in range(20):
        closes.append(closes[-1] * 1.004)
        vols.append(14000)
    closes.append(closes[-1] * 1.03)
    vols.append(60000)
    res = pv.analyze(_series(closes, vols), lookback_days=3)
    assert res["tradable"]["pending_next_bar"] is True
    assert "T+1" in res["tradable"]["exec_hint"]


def test_dedupe_keeps_latest_with_hit_count():
    """同一信号连续触发时只留最近一次，但要保留窗口内命中次数（信息不丢、页面不炸）。"""
    closes, vols = _flat(140, vol=10000)
    for _ in range(20):
        closes.append(closes[-1] * 1.004)
        vols.append(14000)
    closes.append(closes[-1] * 1.03)
    vols.append(60000)
    res = pv.analyze(_series(closes, vols), lookback_days=5)
    keys = [s["key"] for s in res["signals"]]
    assert len(keys) == len(set(keys)), "同一 key 不得重复出现"
    reso = next(s for s in res["signals"] if s["key"] == "pv_resonance")
    assert reso["hits_in_window"] >= 2


def test_signal_events_matches_analyze():
    """事件接口与明细接口必须同源（两份实现迟早漂移）。"""
    closes, vols = _flat(140, vol=10000)
    for _ in range(20):
        closes.append(closes[-1] * 1.004)
        vols.append(14000)
    closes.append(closes[-1] * 1.03)
    vols.append(60000)
    c = _series(closes, vols)
    ev = pv.signal_events(c)
    detail = pv.analyze(c, lookback_days=30)
    last = len(c) - 1
    for s in detail["signals"]:
        assert last - s["bars_ago"] in ev[s["key"]]


def test_signal_score_bounds():
    assert pv.signal_score([]) == 0.0
    many = [{"key": "vol_breakout", "category": "trend", "bars_ago": 0}] * 5
    assert 0 < pv.signal_score(many) <= 100.0


# ── 服务层纯逻辑（成本口径 / 涨跌停规则）────────────────────────────

def test_cost_rate_matches_manual_2026_fees():
    """一买一卖合计：佣金万2.5×2 + 过户费0.001%×2 + 滑点0.1%×2 + 印花税0.05%。"""
    from app.services import price_volume_service as svc

    expected = 2 * (0.00025 + 0.00001 + 0.001) + 0.0005
    assert svc.cost_rate() == pytest.approx(expected, rel=1e-6)
    assert svc.cost_rate() == pytest.approx(0.00302, rel=1e-3)


def test_min_commission_matters_for_small_position(monkeypatch):
    """★ 单笔最低 5 元佣金在小资金下会显著抬高成本率，必须体现在口径里。"""
    from app.services import price_volume_service as svc

    big = svc.cost_rate()
    monkeypatch.setattr(svc, "POSITION_CNY", 1000.0)
    small = svc.cost_rate()
    assert small > big * 3


def test_limit_pct_by_board_and_st():
    from app.services import price_volume_service as svc

    assert svc.limit_pct("600519.SH") == 10.0
    assert svc.limit_pct("000001.SZ") == 10.0
    assert svc.limit_pct("300750.SZ") == 20.0
    assert svc.limit_pct("688981.SH") == 20.0
    assert svc.limit_pct("600519.SH", "ST 摘帽") == 5.0


# ── 信号处理流水线：ADX 环境分流（补丁二）+ 优先级仲裁（补丁一）──────

def _sig(key, bars_ago=0, date="2026-09-24"):
    m = pv.SIGNAL_META[key]
    return {"key": key, "name": m["name"], "category": m["category"],
            "direction": m["direction"], "bars_ago": bars_ago, "date": date}


def test_env_of_thresholds_differ_from_regime_of():
    """补丁二的 20/25 阈值：>25 趋势 / 20~25 震荡 / <20 无趋势（空仓）。"""
    assert pv.env_of(40.0) == "TREND"
    assert pv.env_of(30.7) == "TREND"      # 用户例子里的万科A
    assert pv.env_of(25.01) == "TREND"
    assert pv.env_of(25.0) == "RANGE"
    assert pv.env_of(20.0) == "RANGE"
    assert pv.env_of(19.9) == "WEAK"
    assert pv.env_of(None) == "UNKNOWN"
    # ★ 与展示口径 regime_of 的差异必须真实存在（不能悄悄合并成一套）
    assert pv.regime_of(18.0) == "range" and pv.env_of(18.0) == "WEAK"


def test_priority_covers_every_signal():
    """★ 防漏配：SIGNAL_META 里每个信号都必须有优先级，否则仲裁时静默 fallback 到 0。"""
    missing = set(pv.SIGNAL_META) - set(pv.SIGNAL_PRIORITY)
    assert not missing, f"未登记优先级的信号：{missing}"
    assert pv.priority_of("pv_overheat") == pv.PRIORITY_AVOID
    # 看空信号一律为风险类（≥ AVOID 线）
    for k, v in pv.SIGNAL_META.items():
        if v["direction"] == "bearish":
            assert pv.priority_of(k) >= pv.PRIORITY_AVOID


def test_arbitrator_user_example_vanke_returns_avoid():
    """用户例子：万科A 同时命中 价量共振/PVT/地量止跌/价量过热，ADX=30.7 → AVOID。"""
    sigs = [_sig("pv_resonance"), _sig("pvt_trend"), _sig("vol_dry_reversal"), _sig("pv_overheat")]
    res = pv.arbitrate(sigs, pv.env_of(30.7), adx=30.7)
    assert res["env"] == "TREND"
    assert res["verdict"] == "AVOID"
    assert res["final"]["key"] == "pv_overheat"
    # 趋势市屏蔽反转类看多信号（地量止跌），但**不屏蔽**风险类
    dropped = {d["key"] for d in res["dropped"]}
    assert dropped == {"vol_dry_reversal"}
    assert "pv_overheat" in {k["key"] for k in res["kept"]}


def test_risk_signal_not_filtered_by_regime():
    """★ 核心偏离：趋势市里只命中「价量过热」也必须判 AVOID（伪代码会把它过滤掉）。"""
    res = pv.arbitrate([_sig("pv_overheat")], "TREND", adx=31.0)
    assert res["verdict"] == "AVOID"
    assert res["dropped"] == []


def test_range_env_blocks_trend_signals():
    res = pv.arbitrate([_sig("vol_breakout")], "RANGE", adx=22.0)
    assert res["verdict"] == "IGNORE"
    assert {d["key"] for d in res["dropped"]} == {"vol_breakout"}
    # 震荡市放行反转类（含抄底）
    res2 = pv.arbitrate([_sig("vol_breakout"), _sig("vol_dry_reversal")], "RANGE", adx=22.0)
    assert res2["verdict"] == "SIGNAL"
    assert res2["final"]["key"] == "vol_dry_reversal"


def test_trend_env_keeps_trend_signals_and_picks_highest_priority():
    sigs = [_sig("vol_breakout", bars_ago=2), _sig("pv_resonance", bars_ago=0)]
    res = pv.arbitrate(sigs, "TREND", adx=30.0)
    assert res["verdict"] == "SIGNAL"
    assert res["final"]["key"] == "pv_resonance"   # 同优先级取更近的一条
    assert res["priority"] == pv.PRIORITY_TREND


def test_weak_env_blocks_everything():
    res = pv.arbitrate([_sig("pv_overheat"), _sig("pv_resonance")], "WEAK", adx=18.0)
    assert res["verdict"] == "STANDBY"
    assert res["kept"] == []
    assert len(res["dropped"]) == 2


def test_unknown_env_does_not_add_hidden_threshold():
    """ADX 缺失 → 只仲裁不分流，且必须留痕 env_known=False（不得当成已验证）。"""
    res = pv.arbitrate([_sig("vol_dry_reversal")], "UNKNOWN")
    assert res["verdict"] == "SIGNAL"
    assert res["env_known"] is False
    assert "数据不足" in res["explain"]


def test_arbitrate_handles_empty_and_unknown_keys():
    res = pv.arbitrate([], "TREND", adx=30.0)
    assert res["verdict"] == "IGNORE" and res["final"] is None
    # 未登记的信号不参与裁决，也不会让流程崩溃
    res2 = pv.arbitrate([{"key": "not_a_signal", "bars_ago": 0}], "TREND", adx=30.0)
    assert res2["verdict"] == "IGNORE"


# ── 读层筛选：裁决 / 环境 ───────────────────────────────────────────

def _fake_cache(monkeypatch):
    from app.services import price_volume_service as svc

    rows = [
        {"symbol": "000002.SZ", "name": "万科A", "arb_verdict": "AVOID", "arb_env": "TREND",
         "arb": {"priority": 5}, "signals": [], "newest_bars_ago": 0, "signal_score": 90.0,
         "categories": ["trend"]},
        {"symbol": "600519.SH", "name": "贵州茅台", "arb_verdict": "SIGNAL", "arb_env": "TREND",
         "arb": {"priority": 3}, "signals": [], "newest_bars_ago": 1, "signal_score": 40.0,
         "categories": ["trend"]},
        {"symbol": "000001.SZ", "name": "平安银行", "arb_verdict": "STANDBY", "arb_env": "WEAK",
         "arb": {"priority": None}, "signals": [], "newest_bars_ago": 3, "signal_score": 10.0,
         "categories": ["reversal"]},
    ]
    monkeypatch.setitem(svc._scan_cache, "items", rows)
    monkeypatch.setitem(svc._scan_cache, "stats", {"universe": 3})
    return svc


def test_view_filters_and_sorts_by_verdict(monkeypatch):
    svc = _fake_cache(monkeypatch)
    assert svc.view(None, verdict="AVOID")["total"] == 1
    assert svc.view(None, verdict="BUY")["total"] == 1        # BUY 别名 → SIGNAL
    assert svc.view(None, exclude_avoid=True)["total"] == 2
    assert svc.view(None, env="TREND")["total"] == 2
    ordered = [r["symbol"] for r in svc.view(None, sort_by="verdict")["items"]]
    assert ordered == ["600519.SH", "000002.SZ", "000001.SZ"]  # 候选 → 规避 → 空仓
    assert svc.limit_pct("830799.BJ") == 30.0
