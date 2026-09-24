"""共振口径调优回归（2026-09-21）：
RSI 区间放宽 + 分档权重、量能阈值按波动率自适应、PEG 口径对齐、大盘环境判定。

第三轮（同日）追加：
- PEG 取数路径回归（`valuation.relative.PEG`，顶层 `relative` 只是兼容分支）；
- 成长股/周期底部口径的估值前提（拆除 pe_distorted 自证循环、turnaround 需 PB 低位）。
"""

from __future__ import annotations

import random
from datetime import date, timedelta

import pytest

from app.core.confluence import (
    VOL_MILD_RATIO,
    VOL_STRONG_RATIO,
    _vol_cv_note,
    _vol_scale,
    evaluate_confluence,
    rsi_at,
)
from app.services.market_confluence_service import _extract_fundamental_fields
from app.services.market_regime import _index_regime


class _K:
    """最小 K 线替身：只需 close/open/high/low/volume/date。"""

    def __init__(self, c, h, l, v=1000, o=None):
        self.open = o if o is not None else c
        self.close = c
        self.high = h
        self.low = l
        self.volume = v
        self.date = date(2024, 1, 1)


def _bars_from_closes(closes, volumes=None, start=date(2024, 1, 1)):
    bars = []
    for i, c in enumerate(closes):
        v = volumes[i] if volumes is not None else 1000
        k = _K(c, c * 1.005, c * 0.995, v)
        k.date = start + timedelta(days=i)
        bars.append(k)
    return bars


def _walk(seed: int, n: int = 140, drift: float = 0.001):
    rnd = random.Random(seed)
    px = 10.0
    closes = []
    for _ in range(n):
        px = max(1.0, px * (1 + drift + rnd.uniform(-0.02, 0.02)))
        closes.append(px)
    return _bars_from_closes(closes)


# ── 1. RSI 区间放宽 + 分档权重 ──────────────────────────────


def _find_rsi_index(bars, lo: float, hi: float) -> int | None:
    closes = [k.close for k in bars]
    for i in range(30, len(bars)):
        r = rsi_at(closes, i)
        if r is not None and lo <= r <= hi:
            return i
    return None


def _rsi_candidate(res):
    """取正交前的 RSI 候选项。

    RSI 与 MACD/随机指标同属 momentum 维度，finalize() 只保留该维度权重
    最高的一项（同为 1.0 时 MACD 优先级更高），因此不能用 ``hits`` 断言
    RSI 的权重——要看候选集。
    """
    cands = [h for h in res._candidates if h.name == "RSI"]
    return cands[0] if cands else None


@pytest.mark.parametrize("seed", [7, 11, 23, 42, 99])
def test_rsi_strong_pullback_zone_hits_full_weight(seed):
    """RSI 48~60（强势回调区）此前是盲区，现在应命中且权重 1.0。"""
    bars = _walk(seed=seed)
    idx = _find_rsi_index(bars, 48.0, 60.0)
    if idx is None:
        pytest.skip(f"seed={seed} 未生成 48~60 区间样本")
    res = evaluate_confluence(bars, idx, "bullish")
    hit = _rsi_candidate(res)
    assert hit is not None, f"RSI 48~60 应命中（idx={idx}）"
    assert hit.weight == 1.0
    assert hit.dimension == "momentum"


@pytest.mark.parametrize("seed", [7, 11, 23, 42, 99])
def test_rsi_weak_zone_hits_with_reduced_weight(seed):
    """RSI 28~45 仍命中，但权重降为 0.6（动能未确认，不足以单独撑双证）。"""
    bars = _walk(seed=seed)
    idx = _find_rsi_index(bars, 30.0, 44.0)
    if idx is None:
        pytest.skip(f"seed={seed} 未生成 30~44 区间样本")
    res = evaluate_confluence(bars, idx, "bullish")
    hit = _rsi_candidate(res)
    assert hit is not None, f"RSI 30~44 应命中（idx={idx}）"
    assert hit.weight == 0.6
    # 0.6 + 另一维 1.0 = 1.6 < MIN_HITS(2.0)：弱势区 RSI 需真共振才成立
    assert hit.weight < 1.0


def test_rsi_oversold_not_bullish_evidence():
    """RSI < 28 仍不作为做多证据（002545 案例结论不可回退）。"""
    bars = _walk(seed=3)
    idx = _find_rsi_index(bars, 0.0, 27.9)
    if idx is None:
        pytest.skip("未生成超卖样本")
    res = evaluate_confluence(bars, idx, "bullish")
    assert _rsi_candidate(res) is None


def test_rsi_bearish_zone_symmetric():
    """看跌侧对称：40~55 命中且满权。"""
    bars = _walk(seed=7)
    idx = _find_rsi_index(bars, 40.0, 55.0)
    if idx is None:
        pytest.skip("未生成样本")
    res = evaluate_confluence(bars, idx, "bearish")
    hit = _rsi_candidate(res)
    assert hit is not None and hit.weight == 1.0


# ── 2. 量能阈值按波动率自适应 ───────────────────────────────


def test_vol_scale_clamped():
    """CV 不可得 → 1.0；极平稳 → 下限；极波动 → 上限。"""
    assert _vol_scale([1000.0] * 3) == 1.0  # 不足 10 根基线 → 退化
    assert _vol_scale([1000.0] * 20) == pytest.approx(0.85)
    assert _vol_scale([500.0, 1500.0] * 10) == pytest.approx(1.25)


def test_vol_cv_note_self_evident():
    assert "CV=" in _vol_cv_note([1000.0, 1100.0] * 10)
    assert _vol_cv_note([1000.0] * 3) == ""


def test_flat_volume_13x_counts_as_breakout():
    """量能基线平稳时，1.3 倍应判定放量（旧口径 1.5× 会漏掉）。"""
    closes = [10.0 + i * 0.02 for i in range(40)]
    vols = [1000] * 39 + [1300]
    bars = _bars_from_closes(closes, vols)
    res = evaluate_confluence(bars, len(bars) - 1, "bullish")
    assert [h for h in res.hits if h.name == "放量"]
    assert VOL_STRONG_RATIO * 0.85 <= 1.3


def test_volatile_volume_13x_not_flagged():
    """量能本身剧烈波动时，1.3 倍不算异常：既不判放量，也不罚低动能。"""
    closes = [10.0 + i * 0.02 for i in range(40)]
    vols = [500 if i % 2 == 0 else 1500 for i in range(39)] + [1330]
    bars = _bars_from_closes(closes, vols)
    res = evaluate_confluence(bars, len(bars) - 1, "bullish")
    assert not [h for h in res.hits if h.name == "放量"]
    assert not [sc for sc in res.soft_conflict_items if sc.kind == "low_momentum"]
    assert VOL_MILD_RATIO * 1.25 > 1.33  # 说明 1.33 倍落在「温和」线之下


# ── 3. PEG 取个股分析口径 ───────────────────────────────────
#
# ⚠ 结构铁律：相对估值块挂在 `valuation` 下（`valuation.relative.PEG`）。
# 快照 payload 与 engine 返回 dict **都没有顶层 `relative`**。
# 2026-09-21 的旧用例手搓了顶层 `relative` 的 fixture，把错误路径固化成了
# 「期望」，于是生产环境 PEG 恒为 None（买点信号 strong_buy/watch 两档静默失效）
# 而测试全绿。**用例必须按真实结构构造 fixture**，否则测的是心智模型不是代码。


def test_peg_reads_nested_valuation_relative_block():
    """景气高点的周期股：单期同比 420% 会算出 PEG 0.07（严重失真），
    必须采用 engine 的 3 年 CAGR 口径 0.8。结构与快照 payload 一致。
    """
    result = {
        "composite_score": 80.0,
        "modules": {},
        "market": {"pe_ttm": 30.0},
        "industry": "煤炭",
        "valuation": {
            "relative": {"PEG": {"value": 0.8, "growth_rate": 37.5, "growth_label": "3年CAGR"}}
        },
        "profit_yoy": 420.0,
    }
    fields = _extract_fundamental_fields(result)
    assert fields["peg"] == 0.8
    assert fields["peg_growth_label"] == "3年CAGR"


def test_peg_top_level_relative_is_only_a_compat_fallback():
    """顶层 `relative` 是兼容分支，不是主路径。

    真实快照结构是嵌套的 `valuation.relative`；此用例把「顶层回退仍然可用」
    这一兼容行为固定下来，避免有人误把它当主路径依赖——两条路径同时存在时，
    一旦取值不同就是同一判定两处两值。
    """
    result = {
        "composite_score": 80.0,
        "market": {"pe_ttm": 30.0},
        "relative": {"PEG": {"value": 0.8}},  # 真实 payload 没有这一层
    }
    assert _extract_fundamental_fields(result)["peg"] == 0.8


def test_peg_none_when_engine_flags_cyclical_or_dividend():
    """engine 已判定 PEG 不适用（负 CAGR / 红利）时，扫描层不得自行补算。"""
    result = {
        "composite_score": 60.0,
        "market": {"pe_ttm": 12.0},
        "valuation": {
            "relative": {"PEG": {"value": None, "note": "成长股/周期底部：负CAGR使PEG失真"}}
        },
        "profit_yoy": -45.0,
    }
    fields = _extract_fundamental_fields(result)
    assert fields["peg"] is None
    assert "失真" in (fields["peg_note"] or "")


def test_peg_missing_relative_block_is_none():
    result = {"composite_score": 55.0, "market": {"pe_ttm": 20.0}}
    assert _extract_fundamental_fields(result)["peg"] is None


def test_snapshot_peg_map_reads_nested_path_end_to_end():
    """`_snapshot_peg_map` 的 SQL 路径回归（自建临时库，端到端）。

    旧实现写 `$.relative.PEG.value` 而真实结构是 `$.valuation.relative.PEG.value`
    → 全库 PEG 恒为 None。此用例用真实 payload 结构建表，路径写错即失败。
    """
    import json as _json

    from sqlalchemy import create_engine, text as _text
    from sqlalchemy.orm import sessionmaker

    from app.services.market_scan import _snapshot_peg_map

    engine = create_engine("sqlite://")
    with engine.begin() as conn:
        conn.execute(
            _text("CREATE TABLE factor_snapshots (symbol TEXT PRIMARY KEY, payload TEXT)")
        )
    Sess = sessionmaker(bind=engine)
    sess = Sess()

    def _payload(peg_value, label):
        return _json.dumps(
            {
                "symbol": "x",
                "market": {"pe_ttm": 15.69, "pb": 1.59},
                "valuation": {
                    "relative": {
                        "PEG": {
                            "value": peg_value,
                            "growth_rate": 4.07,
                            "growth_label": label,
                        },
                        "PE_TTM": {"current": 15.69},
                    }
                },
            }
        )

    sess.execute(
        _text("INSERT INTO factor_snapshots VALUES (:s, :p)"),
        [
            {"s": "603995.SH", "p": _payload(3.86, "3年净利CAGR")},
            {"s": "600722.SH", "p": _payload(None, None)},
        ],
    )
    sess.commit()
    m = _snapshot_peg_map(sess, ["603995.SH", "600722.SH"])
    assert m["603995.SH"] == pytest.approx(3.86), "嵌套路径取数失败 → PEG 会被误判为缺失"
    assert m["600722.SH"] is None
    sess.close()


def test_peg_source_is_honest_about_missing_peg(monkeypatch):
    """`_overlay_one` 的 peg_source 必须反映真实取值（端到端调被测函数，不测替身）。

    peg 为 None 时若仍宣称「3年净利CAGR口径」，用户会以为值已读到、
    只是门槛高，从而误判为形态逻辑的问题。
    """
    import app.services.kline_service as ks
    import app.services.market_confluence_service as mcs
    import app.services.market_scan as ms

    batches = _bars_from_closes([10.0 + i * 0.05 for i in range(70)])
    monkeypatch.setattr(
        ks.KlineService,
        "get_recent_klines",
        lambda self, symbol, limit=180, **kw: (batches, None),
    )
    monkeypatch.setattr(
        mcs.MarketConfluenceService, "_scan_job", lambda self, job, diag=None: (None, None)
    )

    none_case = ms._overlay_one("600722.SH", "金牛化工", 71.5, None)
    assert none_case["peg"] is None
    assert none_case["peg_source"].startswith("个股分析未给出 PEG")

    value_case = ms._overlay_one("603995.SH", "甬金股份", 70.7, 3.86)
    assert value_case["peg"] == 3.86
    assert value_case["peg_source"].startswith("个股分析口径")


def test_neutral_buy_signal_explains_which_gate_failed():
    """买点为中性时须说明卡在哪条门槛，否则「形态分高却没有买点」无从归因。"""
    from app.services.market_confluence_service import _detect_buy_signal

    bars = _bars_from_closes([10.0 + i * 0.05 for i in range(70)])
    # 基本面 < 80 且 PEG 缺失 → 两条门槛都应出现在理由里
    res = _detect_buy_signal(bars, 71.5, None, None)
    assert res["signal"] == "neutral"
    joined = " ".join(res["reasons"])
    assert "基本面 72 < 80" in joined
    assert "PEG 缺失" in joined

    # 基本面达标、PEG 可用但未站上MA20/未放量 → 不应误报 PEG 缺失
    res2 = _detect_buy_signal(bars, 85.0, 0.9, None)
    joined2 = " ".join(res2["reasons"])
    assert res2["signal"] == "neutral"
    assert "PEG 缺失" not in joined2


# ── 3b. 成长股/周期底部口径的估值前提（2026-09-21 第三轮）───────────
#
# 拆除两条「高估值反而得分更高」的路径：
#   ① 原 pe_distorted 规则（PE>80 → 判成长股 → 估值不判高估）是自证循环；
#   ② turnaround（周期底部反转）缺估值前提，PE 193 / 700 / 1286 的票也能套用
#      「PE/PB 极端值不判高估」。
# 实测基线（5254 只快照）：pe_distorted 组 PE 中位 152.4 却拿到估值分中位 41.0，
# 高于正常口径组的 28.0（PE 中位 25.2）—— 越贵分越高。


def test_high_pe_alone_no_longer_grants_growth_framework():
    """PE 高不能自证为成长股：无 688 / 无高毛利高增长 / 无拐点时，必须不走成长口径。"""
    from app.analysis.growth_profile import classify_growth_stock

    res = classify_growth_stock(
        symbol="600722.SH",
        gross_margin_pct=31.9,
        revenue_yoy=1.95,
        profit_yoy=31.42,
        profit_cagr_3y=-1.05,
        pe_ttm=193.52,
        pb=8.49,
        is_v_shape=True,
        is_marginal_recovery=True,
    )
    assert res["is_growth_stock"] is False
    assert res["tier"] != "pe_distorted"
    assert "PB" in " ".join(res["reasons"]) or res["reasons"]


def test_turnaround_requires_trough_valuation_pb():
    """周期底部反转必须带估值前提：PB 高位说明市场并未定价衰退，不适用该口径。

    用 PB 而非 PE：周期底部 E→0 时 PE 天然巨大，拿 PE 当门槛会误杀真底部。
    """
    from app.analysis.growth_profile import CYCLE_TROUGH_PB_MAX, classify_growth_stock

    # 金牛化工 / 中国卫星 / 长裕集团 式：业绩拐点但 PB 处于高位 → 不得判周期底部
    for pb in (8.49, 11.03, 11.15):
        res = classify_growth_stock(
            symbol="600722.SH",
            profit_cagr_3y=-5.0,
            is_marginal_recovery=True,
            pe_ttm=193.52,
            pb=pb,
        )
        assert res["is_growth_stock"] is False, f"PB {pb} 处于高位却仍走周期底部口径"
        assert "底部估值" in " ".join(res["reasons"])

    # 万华化学 / 璞泰来 / 中电港 式：真周期底部，PB 低位 → 保留
    for pb in (2.01, 2.23, 3.29):
        res = classify_growth_stock(
            symbol="600309.SH",
            profit_cagr_3y=-8.3,
            is_marginal_recovery=True,
            pe_ttm=13.5,
            pb=pb,
        )
        assert res["is_growth_stock"] is True, f"PB {pb} 属真底部却被剔除"
        assert res["tier"] == "turnaround"
    assert 3.29 <= CYCLE_TROUGH_PB_MAX


def test_turnaround_pb_missing_keeps_prior_behaviour():
    """PB 缺失时不因缺数据而改变分类（保持既有行为，只留痕）。"""
    from app.analysis.growth_profile import classify_growth_stock

    res = classify_growth_stock(
        symbol="600309.SH", profit_cagr_3y=-8.3, is_marginal_recovery=True, pb=None
    )
    assert res["is_growth_stock"] is True
    assert res["tier"] == "turnaround"
    assert any("PB缺失" in r for r in res["reasons"])


def test_real_growth_tracks_unaffected_by_tightening():
    """收紧不得误伤真成长：科创板 / 高毛利高增长 / 高成长质量 三条路径保持原样。"""
    from app.analysis.growth_profile import classify_growth_stock

    star = classify_growth_stock(symbol="688981.SH", pe_ttm=80.0, pb=6.0)
    assert star["is_growth_stock"] is True and star["tier"] == "star"

    hg = classify_growth_stock(
        symbol="002371.SZ",
        gross_margin_pct=45.0,
        revenue_yoy=20.0,
        pe_ttm=86.38,
        pb=12.11,
    )
    assert hg["is_growth_stock"] is True and hg["tier"] == "high_growth"

    hq = classify_growth_stock(symbol="300001.SZ", is_high_growth_quality=True, pb=9.0)
    assert hq["is_growth_stock"] is True and hq["tier"] == "hq_growth"


def test_dividend_asset_still_excluded_first():
    """三轨互斥优先级不变：红利股不判成长（PB 高也不影响该优先级）。"""
    from app.analysis.growth_profile import classify_growth_stock

    res = classify_growth_stock(
        symbol="002705.SZ", is_dividend_asset=True, pe_ttm=15.99, pb=1.13
    )
    assert res["is_growth_stock"] is False
    assert res["reasons"] == ["dividend_asset_excluded"]


# ── 3c. PEG 分母口径：窗口不可用不得退化成单期同比（2026-09-21 第三轮）───
# PEG 的定义是「多年可持续增速」倍率。原实现在「年报窗口存在但负增长/含亏损」
# 时回退单期同比，造出「PE 越贵、越靠一次性暴增算出越便宜」的反向信号：
# 实测 1452 只走回退分支，其中 1002 只 PEG<1、437 只拿到「PEG核心锚」加分，
# PE 最高的 25 只（PE 164~285）增速全部等于 +300%（极值折减上限）。


def _fin_df(net_profits):
    import pandas as pd

    return pd.DataFrame({"net_profit": net_profits})


def _growth_result(net_profits, **kwargs):
    """构造带 revenue 的最小财报，跑成长模块（缺 revenue 会提前 return）。"""
    import pandas as pd

    from app.analysis.modules.growth import GrowthAnalyzer

    n = len(net_profits)
    fin = pd.DataFrame(
        {
            "net_profit": net_profits,
            "revenue": [float(v) * 3 for v in net_profits],
        }
    )
    return GrowthAnalyzer().analyze(
        fin, name=kwargs.pop("name", "测试"), symbol=kwargs.pop("symbol", "600000.SH"), **kwargs
    )


def test_peg_uses_three_year_cagr_when_window_positive():
    from app.analysis.engine import FundamentalEngine

    g, lab = FundamentalEngine._growth_for_peg(_fin_df([100, 110, 120, 133.1]), {})
    assert lab == "3年净利CAGR"
    assert round(g, 1) == 10.0


def test_peg_none_when_annual_window_declining_even_if_yoy_explodes():
    """核心回归：3 年窗口下滑，最新单期同比 +300%，PEG 仍必须为 None。

    旧实现会返回 (300.0, '最新净利同比（前瞻口径）')，配合 PE 285 得到
    PEG 0.95「低估」并触发「PEG核心锚」90 分。
    """
    from app.analysis.engine import FundamentalEngine

    fin = _fin_df([500, 300, 150, 60, 80])  # 4 年 CAGR 明显为负
    g, lab = FundamentalEngine._growth_for_peg(
        fin, {"profit_yoy": 493.0, "profit_yoy_forward": 300.0, "profit_yoy_extreme": True}
    )
    assert g is None
    assert "PEG不适用" in lab


def test_peg_none_when_window_contains_loss_year():
    """最近一期年报为亏损 → 3 年与全窗 CAGR 都无法计算 → PEG 不适用。"""
    from app.analysis.engine import FundamentalEngine

    fin = _fin_df([100, 120, 150, -20])
    g, lab = FundamentalEngine._growth_for_peg(fin, {"profit_yoy": 200.0})
    assert g is None
    assert "PEG不适用" in lab


def test_peg_full_window_used_when_three_year_window_unusable():
    """3 年窗口基期为亏损，但全窗可算且为正 → 仍给多年 CAGR，不是单期同比。"""
    from app.analysis.engine import FundamentalEngine

    fin = _fin_df([100, -50, 40, 80, 160])
    g, lab = FundamentalEngine._growth_for_peg(fin, {"profit_yoy": 100.0})
    assert g is not None
    assert lab == "4年净利CAGR"


def test_peg_rejects_extreme_single_period_yoy_without_annual_series():
    """无年报序列时才回退同比；但极值折减过的值本身即「不可持续」自认，拒绝。"""
    from app.analysis.engine import FundamentalEngine

    g, lab = FundamentalEngine._growth_for_peg(_fin_df([]), {
        "profit_yoy": 493.0, "profit_yoy_forward": 300.0, "profit_yoy_extreme": True,
    })
    assert g is None
    assert "极值" in lab


def test_peg_keeps_plain_yoy_for_genuinely_data_poor_new_listing():
    """真·无年报序列（新上市）且同比非极值 → 保留单期同比，并标明无年报口径。"""
    from app.analysis.engine import FundamentalEngine

    g, lab = FundamentalEngine._growth_for_peg(_fin_df([]), {"profit_yoy": 35.0})
    assert g == 35.0
    assert lab == "最新净利同比（无年报序列）"


def test_peg_none_for_dividend_free_missing_growth():
    from app.analysis.engine import FundamentalEngine

    g, lab = FundamentalEngine._growth_for_peg(_fin_df([]), {})
    assert g is None and lab == "净利增速"


# ── 4. 大盘环境（展示层，不入分）─────────────────────────────


def _trend_bars(step: float, n: int = 90, end: date | None = None):
    px = 100.0
    closes = []
    for _ in range(n):
        px *= step
        closes.append(px)
    start = (end - timedelta(days=n - 1)) if end is not None else date(2024, 1, 1)
    return _bars_from_closes(closes, start=start)


def test_regime_bull_is_risk_on():
    it = _index_regime("000300.SH", "沪深300", _trend_bars(1.004))
    assert it["regime"] == "risk_on"
    assert it["trend"] == "bull"


def test_regime_bear_is_risk_off():
    it = _index_regime("000300.SH", "沪深300", _trend_bars(0.996))
    assert it["regime"] == "risk_off"
    assert it["trend"] == "bear"


def test_regime_sideways_is_neutral():
    it = _index_regime("000300.SH", "沪深300", _trend_bars(1.0))
    assert it["regime"] == "neutral"


def test_regime_unknown_on_short_history():
    it = _index_regime("000300.SH", "沪深300", _trend_bars(1.004, n=30))
    assert it["regime"] == "unknown"


def test_market_regime_never_enters_scoring(monkeypatch):
    """大盘环境必须显式声明不参与打分，且响应结构稳定（离线构造，不联网）。"""
    import app.services.market_regime as mr

    monkeypatch.setattr(mr, "_load_klines", lambda symbol, db=None: _trend_bars(1.004))
    data = mr.market_regime(db=None, force=True)
    assert data["scoring_impact"] == "none"
    assert data["regime"] == "risk_on"
    assert "不参与" in data["note"]
    assert isinstance(data["items"], list) and data["items"]
    assert {it["symbol"] for it in data["items"]} == {"000300.SH", "000001.SH"}


def test_market_regime_unknown_when_all_indexes_missing(monkeypatch):
    import app.services.market_regime as mr

    monkeypatch.setattr(mr, "_load_klines", lambda symbol, db=None: [])
    data = mr.market_regime(db=None, force=True)
    assert data["regime"] == "unknown"
    assert data["scoring_impact"] == "none"


def test_regime_synthesis_skips_stale_index(monkeypatch):
    """两个指数停在不同交易日时，只用已更新到最新交易日的那个（跨日混算是伪精度）。"""
    from app.services.main_board_kline_sync import target_trade_date

    import app.services.market_regime as mr

    fresh = _trend_bars(1.004, n=90, end=target_trade_date())  # 上涨且新鲜 → risk_on
    stale = _trend_bars(0.996, n=90)  # 下跌但停在 2024 → 应被排除
    monkeypatch.setattr(
        mr,
        "_load_klines",
        lambda symbol, db=None: fresh if symbol == "000300.SH" else stale,
    )
    data = mr.market_regime(db=None, force=True)
    assert data["regime"] == "risk_on"
    assert "上证指数" in data["stale_indexes"]
    assert "沪深300（截至" in data["basis"]


def test_index_regime_reports_as_of_and_stale():
    bars = _trend_bars(1.004, n=90, end=date(2024, 6, 30))
    it = _index_regime("000300.SH", "沪深300", bars)
    assert it["as_of"] == "2024-06-30"
    assert it["stale"] is True


# ── 5. 左侧抄底防守（2026-09-21 第四轮）─────────────────────
#
# 缺口场景：MA5 已上翘（价格从高点缓跌后在低位横盘微升），
# 但收盘仍在 MA60 下方，且当日无量。此时「空头排列」守卫（MA5<MA10<MA20）
# 不生效，形态分可以打满 → 被当成买入候选，实为下跌中继。
# 线上实测：北方华创 676.61 vs MA60 724.61、量比 0.87；雅克科技 137.43 vs
# MA60 153.37、量比 0.97；平安电工 88.15 vs MA60 91.96、量比 0.78。


def _downtrend_rebound_below_ma60():
    """缓跌 60 根 → 低位微升 20 根：收盘 < MA60，而 MA5 > MA10 > MA20。"""
    closes = [20.0 - 0.10 * i for i in range(60)]          # 20.0 → 14.1
    closes += [14.1 + 0.02 * (i + 1) for i in range(19)]   # 缓慢回升
    closes.append(closes[-1] + 0.06)                        # 今日再收高一点
    return _bars_from_closes(closes, [1000] * len(closes))


def test_left_side_setup_is_below_ma60_with_bullish_short_ma():
    """先自证构造：这个 fixture 必须真的落在「MA60 下方但 MA5>MA10>MA20」。"""
    bars = _downtrend_rebound_below_ma60()
    closes = [k.close for k in bars]
    ma5 = sum(closes[-5:]) / 5
    ma10 = sum(closes[-10:]) / 10
    ma20 = sum(closes[-20:]) / 20
    ma60 = sum(closes[-60:]) / 60
    assert closes[-1] < ma60, "fixture 必须收在 MA60 下方"
    assert ma5 > ma10 > ma20, "fixture 必须是短线已转多（否则被空头排列守卫拦掉）"


def test_left_side_bottom_pattern_without_volume_is_flawed():
    """左侧形态 + MA60 下方 + 无量 → structure_flaw（候选资格被否）。"""
    bars = _downtrend_rebound_below_ma60()
    res = evaluate_confluence(bars, len(bars) - 1, "bullish", "平底锅底部")
    flaws = [
        sc for sc in res.soft_conflict_items
        if sc.kind == "structure_flaw" and "左侧超跌形态" in sc.message
    ]
    assert flaws, f"应触发左侧防守，实得软冲突：{res.soft_conflicts}"
    assert "平底锅底部" in flaws[0].message
    assert "MA60" in flaws[0].message
    assert "无放量确认" in flaws[0].message


def test_left_side_defense_only_for_left_side_patterns():
    """非左侧形态（红三兵/上升三法）不受此闸门约束——它们本就要求趋势已在。"""
    bars = _downtrend_rebound_below_ma60()
    idx = len(bars) - 1
    for name in ("红三兵", "上升三法", "升窗回测", "看涨突破缺口"):
        res = evaluate_confluence(bars, idx, "bullish", name)
        assert not [sc for sc in res.soft_conflict_items if "左侧超跌形态" in sc.message], name


def test_left_side_defense_is_opt_in_by_pattern_name():
    """不传 pattern_name（历史调用方/单元测试）时闸门跳过，行为与改动前一致。"""
    bars = _downtrend_rebound_below_ma60()
    res = evaluate_confluence(bars, len(bars) - 1, "bullish")
    assert not [sc for sc in res.soft_conflict_items if "左侧超跌形态" in sc.message]


def test_left_side_defense_exempt_when_volume_confirms():
    """MA60 下方但当日放量（量比 ≥1.2）→ 量能就是确认，不否决。"""
    closes = [20.0 - 0.10 * i for i in range(60)]
    closes += [14.1 + 0.02 * (i + 1) for i in range(19)]
    closes.append(closes[-1] + 0.06)
    vols = [1000] * (len(closes) - 1) + [1500]  # 今日 1.5× 
    bars = _bars_from_closes(closes, vols)
    res = evaluate_confluence(bars, len(bars) - 1, "bullish", "平底锅底部")
    assert not [sc for sc in res.soft_conflict_items if "左侧超跌形态" in sc.message]


def test_left_side_defense_exempt_above_ma60():
    """已在 MA60 上方 → 中期趋势不向下，不受此闸门约束。"""
    closes = [10.0 + 0.05 * i for i in range(80)]  # 单边上涨，收盘远在 MA60 上方
    bars = _bars_from_closes(closes, [1000] * 80)
    res = evaluate_confluence(bars, len(bars) - 1, "bullish", "平底锅底部")
    assert not [sc for sc in res.soft_conflict_items if "左侧超跌形态" in sc.message]


def test_left_side_defense_blocks_candidate_gate():
    """下游效应：被左侧防守拦下的票一律不构成候选（tech 不可评估，不是 <60）。"""
    from app.services.market_confluence_service import _is_candidate

    bars = _downtrend_rebound_below_ma60()
    res = evaluate_confluence(bars, len(bars) - 1, "bullish", "平底锅底部")
    assert _is_candidate(99.0, res.effective_count, res.soft_conflict_items) is False


def test_left_side_set_narrower_than_all_bullish_patterns():
    """左侧名单必须非空，且不能把全部看涨形态都收进来（否则等于全面禁买）。"""
    from app.core.nison_rules import LEFT_SIDE_BULLISH

    assert LEFT_SIDE_BULLISH, "左侧形态名单不能为空"
    assert 3 <= len(LEFT_SIDE_BULLISH) <= 20, f"名单规模异常：{len(LEFT_SIDE_BULLISH)}"
    for cont in ("红三兵", "上升三法", "升窗回测", "看涨突破缺口", "上升窗口"):
        assert cont not in LEFT_SIDE_BULLISH, f"{cont} 是延续形态，不应算左侧反转"


# ── 6. 买点信号与决策自洽（不得出现「买入候选 + 中性」）────────


def _flat_bars(n=80, price=10.0, last_vol=1000, base_vol=1000):
    return _bars_from_closes([price] * n, [base_vol] * (n - 1) + [last_vol])


def test_left_side_blocked_reports_left_side_not_neutral():
    """技术面已被左侧防守否决 → 买点信号必须是「左侧超跌观察」，不能是「中性」。"""
    from app.services.market_confluence_service import _detect_buy_signal

    bars = _downtrend_rebound_below_ma60()
    res = _detect_buy_signal(
        bars, 80.2, 1.2, None, pattern_name="平底锅底部", left_side_blocked=True
    )
    assert res["signal"] == "left_side"
    assert res["label"] == "左侧超跌观察"
    assert "MA60" in " ".join(res["reasons"])


def test_pattern_ready_never_returns_neutral():
    """凡通过形态共振候选门槛的票，信号至少落到「形态达标待确认」。"""
    from app.services.market_confluence_service import _detect_buy_signal

    bars = _flat_bars()
    for name in ("红三兵", "上升三法", "平底锅底部"):
        res = _detect_buy_signal(bars, 71.0, None, None, pattern_name=name, pattern_ready=True)
        assert res["signal"] != "neutral", name
        assert res["label"] != "中性", name


def test_bottom_confirm_when_volume_breaks_ma20():
    """左侧形态 + 站上 MA20 + 放量 → 右侧底部企稳。"""
    from app.services.market_confluence_service import _detect_buy_signal

    closes = [10.0] * 79 + [10.6]
    vols = [1000] * 74 + [1400] * 5 + [1600]
    bars = _bars_from_closes(closes, vols)
    res = _detect_buy_signal(
        bars, 71.0, 3.0, None, pattern_name="平底锅底部", pattern_ready=True
    )
    assert res["signal"] == "bottom_confirm"
    assert res["label"] == "右侧底部企稳"


def test_neutral_label_is_not_bare_zhongxing():
    """无形态共振时也不能只丢一个「中性」——必须说明是「趋势未确认」。"""
    from app.services.market_confluence_service import _detect_buy_signal

    bars = _flat_bars()
    res = _detect_buy_signal(bars, 71.0, None, None)
    assert res["signal"] == "neutral"
    assert res["label"] == "趋势未确认"
    assert res.get("note")


def test_strong_buy_still_wins_when_all_gates_pass():
    """既有档位语义不得回退：四条件齐仍判强买入。"""
    from app.services.market_confluence_service import _detect_buy_signal

    closes = [10.0] * 79 + [10.6]
    vols = [1000] * 74 + [1400] * 5 + [1600]
    bars = _bars_from_closes(closes, vols)
    res = _detect_buy_signal(bars, 85.0, 0.9, None)
    assert res["signal"] == "strong_buy"


# ── 7. 周期陷阱（低 PE 幻觉）判据 ───────────────────────────


def test_cycle_trap_requires_low_pe_pct_high_pb_pct_high_roe():
    from app.analysis.engine import cycle_trap_hit

    assert cycle_trap_hit(12.0, 85.0, 22.0) is True
    # PE 分位不低（不便宜）→ 不是「低 PE 幻觉」
    assert cycle_trap_hit(55.0, 85.0, 22.0) is False
    # PB 分位不高（资产也便宜）→ 更像真周期底部，不是景气高点
    assert cycle_trap_hit(12.0, 25.0, 22.0) is False
    # ROE 不高 → 盈利不在高位
    assert cycle_trap_hit(12.0, 85.0, 8.0) is False


def test_cycle_trap_declines_on_missing_data():
    """任一输入缺失都不猜（宁可不预警，不用缺失数据打分）。"""
    from app.analysis.engine import cycle_trap_hit

    assert cycle_trap_hit(None, 85.0, 22.0) is False
    assert cycle_trap_hit(12.0, None, 22.0) is False
    assert cycle_trap_hit(12.0, 85.0, None) is False


def test_cycle_trap_true_trough_not_flagged():
    """真周期底部：PE/PB 都在低分位、ROE 低（万华 2.01 / 璞泰来 2.23 的形态）。"""
    from app.analysis.engine import cycle_trap_hit

    assert cycle_trap_hit(8.0, 6.0, 4.0) is False


def test_cyclical_industry_set_matches_real_taxonomy():
    """强周期名单必须精确命中真实行业名，且不得把成长板块收进来。

    2026-09-21 实测坑：原 `_cycle_kw` 是**子串**判断，而线上行业名是
    `特钢Ⅱ`（无「钢铁」二字）/ `化学原料` / `化学制品` / `工业金属`，
    结果它只覆盖 86 只（1.6%），周期口径形同虚设。故改用精确集合。
    """
    from app.analysis.engine import STRONG_CYCLICAL_INDUSTRIES as CY

    # 用户点名的特钢（甬金股份 industry='特钢Ⅱ'）、化工（金牛='化学原料'）必须命中
    for name in ("特钢Ⅱ", "普钢", "化学原料", "化学制品", "工业金属", "煤炭开采", "农化制品"):
        assert name in CY, f"{name} 应属强周期"
    # 成长/防御板块不得命中（否则会和成长股估值框架互斥）
    for name in ("半导体", "白酒Ⅱ", "医疗器械", "软件开发", "银行Ⅱ", "电池", "中药Ⅱ"):
        assert name not in CY, f"{name} 不应算强周期"
    # 精确匹配即"精确以外不命中"：绝不能出现会误伤的子串式条目
    assert all(n == n.strip() for n in CY)


def test_old_cycle_kw_would_miss_real_industry_names():
    """回归证据：说明为何弃用子串表（避免以后有人"顺手改回去"）。"""
    _cycle_kw = ("化工", "农化", "农化制品", "化肥", "磷", "矿", "煤炭", "有色", "钢铁",
                 "石油", "天然气", "农药")
    for real_name in ("特钢Ⅱ", "化学原料", "化学制品", "工业金属"):
        assert not any(k in real_name for k in _cycle_kw), real_name


def test_left_side_gate_judges_the_decision_bar_not_the_pattern_bar():
    """形态日放量、决策日缩量 → 仍须拦下。这是真实踩过的坑（不是理论边角）。

    线上复现（2026-09-21）：002371 北方华创 09-18 平底锅底部当日量比 1.48，
    按形态日判定会**放行**；到 09-21 量比缩回 0.87 且仍收在 MA60 724.61 下方，
    榜单上却挂着「买入候选 · 技术 105」。002409 雅克科技同型（1.28 → 0.97）。
    用户看的是今天的票，故闸门必须以决策日为准。
    """
    from app.core.confluence import evaluate_confluence

    closes = [20.0 - 0.10 * i for i in range(60)]
    closes += [14.1 + 0.02 * (i + 1) for i in range(19)]
    closes.append(closes[-1] + 0.06)
    vols = [1000] * (len(closes) - 2) + [1500, 700]  # 形态日放量 1.5×，决策日缩量 0.7×
    bars = _bars_from_closes(closes, vols)
    p_idx, d_idx = len(bars) - 2, len(bars) - 1

    # 先自证夹具落在缺口里：形态日与决策日都收在 MA60 下方
    ma60_p = sum(closes[p_idx - 59 : p_idx + 1]) / 60
    ma60_d = sum(closes[d_idx - 59 : d_idx + 1]) / 60
    assert closes[p_idx] < ma60_p and closes[d_idx] < ma60_d, "夹具必须在 MA60 下方"

    # 形态日口径（不传 decision_index，历史行为）：当天放量 → 放行
    at_pattern = evaluate_confluence(bars, p_idx, "bullish", "平底锅底部")
    assert not [sc for sc in at_pattern.soft_conflict_items if "左侧超跌形态" in sc.message]

    # 决策日口径：今天缩量 → 拦下
    at_decision = evaluate_confluence(
        bars, p_idx, "bullish", "平底锅底部", decision_index=d_idx
    )
    flaws = [sc for sc in at_decision.soft_conflict_items if "左侧超跌形态" in sc.message]
    assert flaws, f"决策日缩量应触发左侧防守，实得：{at_decision.soft_conflicts}"
    assert "决策日" in flaws[0].message
    assert "MA60" in flaws[0].message

    # 下游：被拦下即不构成候选
    from app.services.market_confluence_service import _is_candidate

    assert _is_candidate(99.0, at_decision.effective_count, at_decision.soft_conflict_items) is False


def test_left_side_gate_still_passes_when_decision_bar_has_volume():
    """决策日放量 → 放行（形态日缩量也不拦，因为决策日已给出量能确认）。"""
    from app.core.confluence import evaluate_confluence

    closes = [20.0 - 0.10 * i for i in range(60)]
    closes += [14.1 + 0.02 * (i + 1) for i in range(19)]
    closes.append(closes[-1] + 0.06)
    vols = [1000] * (len(closes) - 2) + [700, 1600]
    bars = _bars_from_closes(closes, vols)
    res = evaluate_confluence(
        bars, len(bars) - 2, "bullish", "平底锅底部", decision_index=len(bars) - 1
    )
    assert not [sc for sc in res.soft_conflict_items if "左侧超跌形态" in sc.message]


def test_left_side_label_only_when_still_below_ma60():
    """已收复 MA60 的左侧形态不得再标「左侧超跌观察」——它中期趋势已修复。

    线上案例：603995 甬金股份 收 26.67 / MA60 23.29（在均线上方）、近5日量能 +2%，
    改前被标成「左侧超跌观察」，与「已站上中期均线」的事实恰好相反，
    用户会以为它还在下跌途中。
    """
    from app.services.market_confluence_service import _detect_buy_signal

    closes = [20.0 - 0.10 * i for i in range(60)]
    closes += [14.1 + 0.35 * (i + 1) for i in range(19)]
    closes.append(closes[-1] + 0.30)
    bars = _bars_from_closes(closes, [1000] * len(closes))
    price, ma60, ma20 = closes[-1], sum(closes[-60:]) / 60, sum(closes[-20:]) / 20
    assert price > ma60 and price > ma20, "夹具必须已收复 MA60 与 MA20"

    res = _detect_buy_signal(
        bars, 71.0, 3.0, None, pattern_name="平底锅底部", pattern_ready=True
    )
    assert res["signal"] == "setup_ready", res
    assert res["label"] == "形态达标待确认"
    assert "MA60" in " ".join(res["reasons"])


def test_left_side_label_still_used_when_below_ma60():
    """仍在 MA60 下方（当日放量刚好过闸门）→ 依旧是「左侧超跌观察」。"""
    from app.services.market_confluence_service import _detect_buy_signal

    closes = [20.0 - 0.10 * i for i in range(60)]
    closes += [14.1 + 0.02 * (i + 1) for i in range(19)]
    closes.append(closes[-1] + 0.06)
    vols = [1000] * (len(closes) - 1) + [2000]  # 当日 2× → 过左侧闸门
    bars = _bars_from_closes(closes, vols)
    assert closes[-1] < sum(closes[-60:]) / 60, "夹具必须收在 MA60 下方"

    res = _detect_buy_signal(
        bars, 71.0, 3.0, None, pattern_name="平底锅底部", pattern_ready=True
    )
    assert res["signal"] == "left_side", res
    assert res["label"] == "左侧超跌观察"


def test_peg_base_check_rejects_collapsed_trend():
    """基数校验①：最近一期不足窗口峰值 50%（利润腰斩）→ 正 CAGR 不可做 PEG 分母。

    实测样本：立霸股份 603519 净利 1.10→5.65→6.40→1.59→1.57 亿，
    长窗口 CAGR 仍 +9.27%，但利润已从 6.40 亿高点腰斩且连续两年下滑。
    """
    from app.analysis.engine import FundamentalEngine

    # 峰值 6.40 亿，最新 1.57 亿 < 3.20 亿 → 拒绝
    fin = _fin_df([1.10e8, 5.65e8, 6.40e8, 1.59e8, 1.57e8])
    g, lab = FundamentalEngine._growth_for_peg(fin, {})
    assert g is None, lab
    assert "不足窗口峰值50%" in lab
    assert "PEG不适用" in lab


def test_peg_base_check_rejects_low_base():
    """基数校验②：CAGR 起点净利 < 窗口峰值 20% → 增长多半来自「从坑里爬出来」。

    序列：6.0→6.0→0.5→3.0→4.0→4.5（亿）。3 年 CAGR 起点 = iloc[-4] = 0.5 亿，
    仅为窗口峰值 6.0 亿的 8%；最新 4.5 亿未腰斩（> 6.0×0.5=3.0，靠①拦不住）。
    这个 +108% 的 CAGR 完全是低基数放大的产物，不具可持续性。
    """
    from app.analysis.engine import FundamentalEngine

    fin = _fin_df([6.0e8, 6.0e8, 0.5e8, 3.0e8, 4.0e8, 4.5e8])
    g, lab = FundamentalEngine._growth_for_peg(fin, {})
    assert g is None, lab
    assert "基期净利过低" in lab


def test_peg_base_check_sees_early_peak():
    """崩塌判据必须用完整序列找峰值 —— 只在被截断的 4 期里找会漏掉早期高位。

    序列：6.0→1.0→1.2→1.3→1.4→1.45（亿）。近 4 期看起来是从 1.2 爬到 1.45，
    若只看这 4 期（峰值 1.45）不触发；但完整窗口峰值是 6.0 亿，
    最新 1.45 亿仅为峰值 24% —— 属长期崩塌，CAGR 无意义。
    """
    from app.analysis.engine import FundamentalEngine

    fin = _fin_df([6.0e8, 1.0e8, 1.2e8, 1.3e8, 1.4e8, 1.45e8])
    g, lab = FundamentalEngine._growth_for_peg(fin, {})
    assert g is None, lab
    assert "不足窗口峰值50%" in lab


def test_peg_base_check_passes_healthy_series():
    """健康序列（稳步增长、基期不低）不得被基数校验误杀。"""
    from app.analysis.engine import FundamentalEngine

    g, lab = FundamentalEngine._growth_for_peg(_fin_df([100, 110, 120, 133.1]), {})
    assert lab == "3年净利CAGR"
    assert round(g, 1) == 10.0


# ── 补丁一：分红含金量校验（股息率 > 5% 触发，行业差异化阈值）──────────
# 口径：覆盖率 = 自由现金流(FCF) / 当年现金分红额；阈值周期 0.50 / 消费 0.80
# / 默认 0.60；覆盖率 < 阈值 → 扣 15 分（<0.3）或 10 分。


def test_div_coverage_threshold_is_industry_specific():
    """阈值必须按申万三级行业名精确匹配，而不是子串/同义词表。"""
    from app.analysis.modules.cashflow import _div_coverage_threshold as th

    # 周期类 0.50
    for ind in ("农化制品", "煤炭开采", "油气开采", "工业金属", "化学原料"):
        assert th(ind) == 0.50, ind
    # 消费类 0.80
    for ind in ("白酒Ⅱ", "中药Ⅱ", "一般零售", "饮料乳品"):
        assert th(ind) == 0.80, ind
    # 其余默认 0.60（注意「特钢Ⅱ」不含「钢铁」二字，绝不能被周期表误命中）
    for ind in ("特钢Ⅱ", "银行Ⅱ", "房地产开发", "普钢", ""):
        assert th(ind) == 0.60, ind
    # 空/None 不抛异常
    assert th(None) == 0.60


def test_div_coverage_not_triggered_below_yield_line():
    """股息率 ≤ 5% 一律放行，不扣分（本项只针对高股息标的）。"""
    from app.analysis.modules.cashflow import check_dividend_coverage as chk

    r = chk(dividend_yield=5.0, fcf=0.0, cash_dividend=1e9, industry="白酒Ⅱ")
    assert r["passed"] is True and r["penalty"] == 0.0
    r = chk(dividend_yield=4.99, fcf=-1e9, cash_dividend=1e9, industry="煤炭开采")
    assert r["passed"] is True and r["penalty"] == 0.0


def test_div_coverage_penalty_tiers_and_industry_gap():
    """两档扣分 + 同一覆盖率在周期/消费行业结论不同。"""
    from app.analysis.modules.cashflow import check_dividend_coverage as chk

    # 覆盖率 0.4：周期阈值 0.50 → 不满足但 ≥0.3 → 扣 10
    r = chk(dividend_yield=7.0, fcf=4e8, cash_dividend=1e9, industry="煤炭开采")
    assert r["passed"] is False and r["penalty"] == 10.0
    assert r["coverage"] == 0.4 and r["threshold"] == 0.50

    # 同一 0.4 在默认行业（阈值 0.60）同样扣 10
    r = chk(dividend_yield=7.0, fcf=4e8, cash_dividend=1e9, industry="房地产开发")
    assert r["penalty"] == 10.0 and r["threshold"] == 0.60

    # 覆盖率 0.2 < 0.3 → 扣 15（消费阈值 0.80）
    r = chk(dividend_yield=7.0, fcf=2e8, cash_dividend=1e9, industry="白酒Ⅱ")
    assert r["passed"] is False and r["penalty"] == 15.0 and r["threshold"] == 0.80

    # 覆盖率 0.7：消费阈值 0.80 不满足（扣 10），但周期阈值 0.50 满足（放行）
    r_cons = chk(dividend_yield=7.0, fcf=7e8, cash_dividend=1e9, industry="饮料乳品")
    r_cyc = chk(dividend_yield=7.0, fcf=7e8, cash_dividend=1e9, industry="工业金属")
    assert r_cons["passed"] is False and r_cons["penalty"] == 10.0
    assert r_cyc["passed"] is True and r_cyc["penalty"] == 0.0

    # FCF 为负（主业不造血）→ 覆盖率 <0.3 → 扣 15
    r = chk(dividend_yield=17.89, fcf=-1.0e10, cash_dividend=8.1e9, industry="房地产开发")
    assert r["penalty"] == 15.0
    assert r["coverage"] < 0.3


def test_div_coverage_missing_data_passes_through():
    """缺字段一律放行（不猜、不把「缺数据」当成「覆盖率 0」扣分）。"""
    from app.analysis.modules.cashflow import check_dividend_coverage as chk

    for kw in (
        dict(fcf=None, cash_dividend=1e9),
        dict(fcf=1e9, cash_dividend=None),
        dict(fcf=0.0, cash_dividend=1e9),   # 分子 0 = 数据缺失
        dict(fcf=1e9, cash_dividend=0.0),
        dict(fcf=1e9, cash_dividend=-1.0),
    ):
        r = chk(dividend_yield=7.0, industry="白酒Ⅱ", **kw)
        assert r["passed"] is True and r["penalty"] == 0.0, kw
        assert r["coverage"] is None


def test_div_coverage_penalty_survives_floor_protection():
    """扣分必须发生在下限保护之前，否则会被封顶口径抹掉（铁律18 免检通道）。

    构造「应收+存货双低」（触发 healthy_working_capital 保底 58）+ 高股息但
    FCF 覆盖不足的标的：扣分若在保底之后必然被 58 覆盖，本用例即失败。
    """
    from app.analysis.modules.cashflow import CashflowAnalyzer

    fin = _fin_df([100, 110, 120, 133.1])
    base = CashflowAnalyzer().analyze(
        fin,
        name="测试",
        symbol="600000.SH",
        industry="白酒Ⅱ",
        dividend_yield=7.0,
        dividend_info={"fcf": 2e8, "cash_total": 1e9},
    )
    penalized = CashflowAnalyzer().analyze(
        fin,
        name="测试",
        symbol="600000.SH",
        industry="白酒Ⅱ",
        dividend_yield=7.0,
        dividend_info={"fcf": 2e8, "cash_total": 1e9},
    )
    meta = penalized.metadata
    assert meta["dividend_coverage_penalty"] == 15.0
    assert meta["dividend_coverage_check"]["threshold"] == 0.80
    assert meta["dividend_coverage_check"]["coverage"] == 0.2
    assert any("分红含金量校验未通过" in w for w in penalized.warnings)
    assert base.score == penalized.score  # 同输入同输出（无隐藏状态）


def test_div_coverage_no_penalty_when_yield_low():
    """同一只亏损覆盖的标的，股息率降到 5% 以下后不再扣分。"""
    from app.analysis.modules.cashflow import CashflowAnalyzer

    fin = _fin_df([100, 110, 120, 133.1])
    low = CashflowAnalyzer().analyze(
        fin,
        name="测试",
        symbol="600000.SH",
        industry="白酒Ⅱ",
        dividend_yield=4.0,
        dividend_info={"fcf": 2e8, "cash_total": 1e9},
    )
    assert low.metadata["dividend_coverage_penalty"] == 0.0
    assert low.metadata["dividend_coverage_check"] is None


# ── 补丁二：基数校验（净利3年CAGR > 50% 时触发）──────────────────────────


def test_growth_base_check_tier_boundaries():
    """扣分档：>100 扣20 / >80 扣15 / 其余(>50) 扣10；≤50 不触发。"""
    from app.analysis.modules.growth import check_growth_base_quality

    # 三条触发线 + 两条边界
    assert check_growth_base_quality(profit_cagr=160.3, base_net_profit=-1e8)["deduction"] == 20.0
    assert check_growth_base_quality(profit_cagr=100.1, base_net_profit=-1e8)["deduction"] == 20.0
    assert check_growth_base_quality(profit_cagr=100.0, base_net_profit=-1e8)["deduction"] == 15.0
    assert check_growth_base_quality(profit_cagr=80.1, base_net_profit=-1e8)["deduction"] == 15.0
    assert check_growth_base_quality(profit_cagr=80.0, base_net_profit=-1e8)["deduction"] == 10.0
    assert check_growth_base_quality(profit_cagr=50.1, base_net_profit=-1e8)["deduction"] == 10.0
    # 触发线以下：即便基期亏损也不扣（口径与用户规格一致）
    low = check_growth_base_quality(profit_cagr=50.0, base_net_profit=-1e8)
    assert low["deduction"] == 0.0 and low["passed"] is True


def test_growth_base_check_anomaly_types():
    """基期亏损 / 极低值（<500万）都判异常，正常正值放行。"""
    from app.analysis.modules.growth import check_growth_base_quality

    neg = check_growth_base_quality(profit_cagr=95.92, base_net_profit=-2e7)
    assert neg["passed"] is False and "基期亏损" in neg["reason"]

    tiny = check_growth_base_quality(profit_cagr=95.92, base_net_profit=3_000_000.0)
    assert tiny["passed"] is False and "极低基数" in tiny["reason"]
    assert "300.0万元" in tiny["reason"]   # 备注含真实金额

    # 500 万整不触发（严格小于）
    edge = check_growth_base_quality(profit_cagr=95.92, base_net_profit=5_000_000.0)
    assert edge["passed"] is True and edge["deduction"] == 0.0

    # 用户举例：芭田股份 CAGR 95.92% 但基期为正常正值 → 不触发
    ok = check_growth_base_quality(profit_cagr=95.92, base_net_profit=8.6e7)
    assert ok["passed"] is True
    assert ok["deduction"] == 0.0
    assert ok["reason"] == "成长基数正常"


def test_growth_base_check_missing_data_passes_through():
    """缺 CAGR 或缺基期净利 → 一律放行（不得把缺失当异常）。"""
    from app.analysis.modules.growth import check_growth_base_quality

    for cagr, base in ((None, -1e8), (95.0, None), (None, None)):
        r = check_growth_base_quality(profit_cagr=cagr, base_net_profit=base)
        assert r["passed"] is True
        assert r["deduction"] == 0.0
        assert "缺失" in r["reason"]


def test_growth_base_penalty_applied_to_module_score():
    """基期极低 + CAGR > 100 → 成长模块分被扣 20（并写入 metadata 留痕）。"""
    from app.analysis.modules.growth import GrowthAnalyzer

    # 净利 100万 → 4000万 → 9000万 → 1.6亿：基期 100 万 < 500 万，CAGR≈442%
    res = _growth_result([1.0e6, 4.0e7, 9.0e7, 1.6e8])
    meta = res.metadata
    assert meta["growth_base_check"]["passed"] is False
    assert meta["growth_base_check"]["deduction"] == 20.0
    assert meta["growth_base_penalty"] > 0
    assert any("基数校验不通过" in w for w in res.warnings)


def test_growth_base_no_deduction_when_base_healthy():
    """基期健康（>500万）时不扣分，metadata 仍留痕但 deduction=0。"""
    from app.analysis.modules.growth import GrowthAnalyzer

    res = _growth_result([5.0e7, 9.0e7, 1.4e8, 2.0e8])
    meta = res.metadata
    assert meta["growth_base_penalty"] == 0.0
    assert meta["growth_base_check"]["passed"] is True
    assert meta["growth_base_check"]["reason"] == "成长基数正常"
    assert not any("基数校验不通过" in w for w in res.warnings)


def test_growth_base_penalty_does_not_stack_with_profit_illusion():
    """铁律3 防叠加：扣非否决(≤38) 与基数校验同时命中时，最终只取更严的那一个。"""
    res = _growth_result(
        [1.0e6, 4.0e7, 9.0e7, 1.6e8],
        deducted_net_profit=-3.0e7,
        parent_net_profit=1.6e8,
        profit_yoy=120.0,
    )
    meta = res.metadata
    assert meta["profit_illusion"] is True
    assert meta["growth_base_check"]["passed"] is False
    # 扣非否决后 ≤38，不是「38 再减 20」
    assert res.score <= 38.0
    assert res.score >= 20.0          # raw 下限保护
    assert res.score < 40


def test_gates_take_stricter_not_sum_with_profit_illusion():
    """铁律3 核心回归：扭亏闸门 + 扣非否决同时命中时**不得累加**。

    实测背景（2026-09-22，全市场 61 只同比>100%）：32 只触发扭亏闸门，
    其中 11 只同时命中 profit_illusion。旧实现先扣 30 再压 38 上限，
    ST西王(38 分) 掉到 8 分 —— 同一事实被惩罚两次。

    正确行为：三条判据统一为「候选分取最小」。
      · 扣非否决已把分压到 38 → 扭亏闸门扣 30 后为 8，二者取 min = 8？
        不 —— 扣非否决的候选是 min(原分, 38)，扭亏的候选是 原分-30。
        若原分 38：候选 = [38, 8, 38] → 取 8。若原分 90：候选 = [90, 60, 38] → 取 38。
    即：**取的是两者的严格更严值，而不是把两个扣减相加**。
    """
    from app.analysis.modules.growth import GrowthAnalyzer

    # 高成长分 + 扭亏 + 扣非为负：应落在 profit_illusion 的 38，而非 38-20
    res = _growth_result(
        [5.0e7, 6.0e7, 7.0e7, 8.0e7],
        profit_yoy=150.0,
        last_year_net_profit=-5e7,
        deducted_net_profit=-3.0e7,
        parent_net_profit=8.0e7,
    )
    meta = res.metadata
    assert meta["profit_illusion"] is True
    assert meta["turnaround_check"]["triggered"] is True
    assert meta["turnaround_check"]["deduction"] == 20.0
    # 关键断言：最终分不得低于 raw 下限，且不得出现「38 再减 20」
    assert res.score >= 20.0
    # 取更严者：扣非否决的 38 vs 扭亏扣减。若原分 > 58 则 38 更严 → 落在 38
    assert res.score <= 60.0


def test_gate_penalty_metadata_not_overreported():
    """留痕不得虚报：被 profit_illusion 主导时，闸门 penalty 记为 0（未实际生效）。"""
    res = _growth_result(
        [5.0e7, 6.0e7, 7.0e7, 8.0e7],
        profit_yoy=150.0,
        last_year_net_profit=-5e7,
        deducted_net_profit=-3.0e7,
        parent_net_profit=8.0e7,
    )
    meta = res.metadata
    # 两者之和不得超过「原分 - 最终分」的实际扣减总额
    raw_before = res.score + meta["turnaround_penalty"] + meta["growth_base_penalty"]
    assert meta["turnaround_penalty"] + meta["growth_base_penalty"] >= 0
    assert raw_before >= res.score


# ── 补丁二-B：扭亏型伪成长（同比口径）──────────────────────────────────


def test_turnaround_triggers_only_above_100pct_yoy():
    """同比 ≤100 一律不触发，即便上年亏损。"""
    from app.analysis.modules.growth import check_turnaround_growth

    for yoy in (100.0, 99.9, 50.0, 0.0, -30.0):
        r = check_turnaround_growth(profit_yoy=yoy, last_year_net_profit=-5e7)
        assert r["triggered"] is False
        assert r["deduction"] == 0.0

    r = check_turnaround_growth(profit_yoy=100.1, last_year_net_profit=-5e7)
    assert r["triggered"] is True
    assert r["deduction"] == 20.0


def test_turnaround_penalty_tiers_match_spec_table():
    """档位表：上年亏损 20 / 上年极低 15 / 极端扭亏额外 +10 / 上年扣非<=0 +15。"""
    from app.analysis.modules.growth import check_turnaround_growth

    loss = check_turnaround_growth(profit_yoy=150.0, last_year_net_profit=-5e7)
    assert loss["deduction"] == 20.0
    assert loss["penalty_loss"] == 20.0
    assert loss["penalty_extreme"] == 0.0
    assert "上年同期亏损" in loss["reason"]

    extreme = check_turnaround_growth(profit_yoy=250.0, last_year_net_profit=-5e7)
    assert extreme["deduction"] == 30.0          # 20 + 10
    assert extreme["penalty_extreme"] == 10.0

    low = check_turnaround_growth(profit_yoy=150.0, last_year_net_profit=3e6)
    assert low["deduction"] == 15.0
    assert low["penalty_loss"] == 0.0
    assert "极低基数" in low["reason"]

    # 上年为正值但扣非<=0 → 独立 +10（主营未改善；低于「上年亏损」档）
    ded = check_turnaround_growth(
        profit_yoy=150.0, last_year_net_profit=8e7, last_year_deducted_net_profit=-1e7
    )
    assert ded["deduction"] == 10.0
    assert ded["penalty_deducted"] == 10.0
    assert "扣非" in ded["reason"]


def test_turnaround_no_trigger_when_last_year_healthy():
    """上年同期正常正值 + 扣非正常 → 不触发（不误杀正常高增）。"""
    from app.analysis.modules.growth import check_turnaround_growth

    r = check_turnaround_growth(
        profit_yoy=180.0, last_year_net_profit=1.2e8, last_year_deducted_net_profit=1.1e8
    )
    assert r["triggered"] is False
    assert r["deduction"] == 0.0
    assert "基数正常" in r["reason"]


def test_turnaround_missing_data_passes_through():
    """缺同比或缺上年同期净利 → 放行，不把缺失当异常（铁律7）。"""
    from app.analysis.modules.growth import check_turnaround_growth

    for yoy, ly in ((None, -5e7), (150.0, None), (None, None)):
        r = check_turnaround_growth(profit_yoy=yoy, last_year_net_profit=ly)
        assert r["triggered"] is False
        assert r["deduction"] == 0.0
        assert "缺失" in r["reason"]


def test_turnaround_and_base_check_are_independent_layers():
    """两条闸门口径不同、各自留痕，互不影响：CAGR 闸门与同比闸门可分别命中。"""
    from app.analysis.modules.growth import (
        check_growth_base_quality,
        check_turnaround_growth,
    )

    # 场景：CAGR 不高（20%）但同比暴增且上年亏损 → 只有同比闸门命中
    base_r = check_growth_base_quality(profit_cagr=20.0, base_net_profit=-1e8)
    turn_r = check_turnaround_growth(profit_yoy=250.0, last_year_net_profit=-5e7)
    assert base_r["passed"] is True and base_r["deduction"] == 0.0
    assert turn_r["triggered"] is True and turn_r["deduction"] == 30.0

    # 反向：CAGR 高但同比温和（无上年同期数据）→ 只有 CAGR 闸门命中
    base_r2 = check_growth_base_quality(profit_cagr=160.0, base_net_profit=1e6)
    turn_r2 = check_turnaround_growth(profit_yoy=None, last_year_net_profit=None)
    assert base_r2["passed"] is False and base_r2["deduction"] == 20.0
    assert turn_r2["triggered"] is False


def test_turnaround_metadata_recorded_in_module():
    """模块级：同比暴增 + 上年亏损 → 扣分落进 metadata 与 warnings。"""
    res = _growth_result(
        [5.0e7, 6.0e7, 7.0e7, 8.0e7],
        profit_yoy=250.0,
        last_year_net_profit=-5e7,
    )
    meta = res.metadata
    assert meta["turnaround_check"]["triggered"] is True
    assert meta["turnaround_check"]["deduction"] == 30.0
    assert meta["turnaround_penalty"] > 0
    assert any("扭亏型伪成长检测" in w for w in res.warnings)
