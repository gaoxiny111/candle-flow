"""内在价值「先分类 → 再选模型 → 最后交叉验证」测试。

覆盖三层的边界：分类优先级与三态缺失、四个模型的公式与闸门、
交叉验证的 2 模型/单模型/0 模型与「不可信模型入池但整体取保守侧 min」，
以及引擎接入后 composite_valuation_score 的推导未被污染。
"""

from __future__ import annotations

import pandas as pd
import pytest

from app.analysis.dividend_profile import CN_10Y_BOND_YIELD_PCT
from app.analysis.engine import FundamentalEngine
from app.analysis.models import intrinsic as I

CYCLICALS = frozenset({"工业金属", "煤炭开采", "普钢"})
NO_CYCLICALS: frozenset[str] = frozenset()  # 显式「无周期行业」集合


def _row(**kw):
    base = {
        "industry": "半导体",
        "price": 10.0,
        "pe_ttm": 25.0,
        "pb": 2.0,
        "pb_percentile": 40.0,
        "dividend_yield": 1.0,
        "payout_ratio_pct": 30.0,
        "dividend_per_share": 0.5,
        "eps_ttm": 1.0,
        "eps_normalized": None,
        "bvps": 5.0,
        "roe_ttm": 12.0,
        "profit_yoy": 5.0,
        "growth_score": None,
    }
    base.update(kw)
    return base


# ── 1. 分类 ─────────────────────────────────────────────────────────────
def test_classify_cyclical_wins_over_dividend_and_growth():
    """周期优先：强周期行业 + |同比|>30%，即便同时是红利/成长资产也归周期。"""
    row = _row(industry="工业金属", profit_yoy=-42.0, dividend_yield=6.0, payout_ratio_pct=80.0)
    d = I.classify_intrinsic_style_detail(
        row, is_dividend_asset=True, is_growth_stock=True, cyclical_industries=CYCLICALS
    )
    assert d["style"] == I.STYLE_CYCLICAL
    assert d["matched"] == "cyclical_volatile"


def test_classify_cyclical_industry_always_beats_dividend():
    """**行业排他**（2026-09-29 定案）：强周期行业即便判为红利资产、盈利平稳，也走周期锚。

    线上实测形态（中远海控 601919：航运港口 + 净利同比 −23.48% + 引擎判红利）。
    修前把「|同比|>30%」当成周期**身份**的必要条件 → 这类票整批漏进股息锚；
    现已取消该闸：行业命中即强制周期，reason 里注明「周期锁生效：股息锚仅作对照」。
    """
    row = _row(industry="工业金属", profit_yoy=5.0, dividend_yield=5.0, payout_ratio_pct=70.0)
    d = I.classify_intrinsic_style_detail(
        row, is_dividend_asset=True, is_growth_stock=False, cyclical_industries=CYCLICALS
    )
    assert d["style"] == I.STYLE_CYCLICAL
    assert d["matched"] == "cyclical_industry"
    assert "周期锁" in d["reason"]
    assert "不影响周期归类" in d["reason"]


def test_classify_cyclical_industry_needs_neither_dividend_nor_volatility():
    """行业排他是**无条件**的：强周期 + 盈利平稳 + 非红利 → 仍归周期（不回落 value）。

    这是本轮与上一版「周期锁」的关键差别：上一版要求「判红利才抢归周期」，
    于是强周期里盈利平稳的**非红利**票会落到 value / growth；现在行业本身即判据。
    """
    row = _row(industry="工业金属", profit_yoy=5.0, dividend_yield=1.0, payout_ratio_pct=10.0)
    d = I.classify_intrinsic_style_detail(
        row, is_dividend_asset=False, is_growth_stock=False, cyclical_industries=CYCLICALS
    )
    assert d["style"] == I.STYLE_CYCLICAL
    assert d["matched"] == "cyclical_industry"
    assert "不影响周期归类" in d["reason"]


def test_classify_cyclical_lock_does_not_leak_outside_cyclical_industries():
    """非周期行业不受拦截影响，仍按红利处理（拦截只挂在周期行业分支内）。"""
    row = _row(industry="白酒Ⅱ", profit_yoy=3.0, dividend_yield=6.0, payout_ratio_pct=70.0)
    d = I.classify_intrinsic_style_detail(
        row, is_dividend_asset=True, is_growth_stock=False, cyclical_industries=CYCLICALS
    )
    assert d["style"] == I.STYLE_DIVIDEND
    assert d["matched"] == "dividend_authoritative"


def test_classify_cyclical_industry_covers_missing_yoy():
    """同比缺失 + 判红利 → 仍归周期（缺失不得当作「波动为 0」而漏进股息锚）。"""
    row = _row(industry="普钢", profit_yoy=None, dividend_yield=5.1, payout_ratio_pct=63.2)
    d = I.classify_intrinsic_style_detail(
        row, is_dividend_asset=True, is_growth_stock=False, cyclical_industries=CYCLICALS
    )
    assert d["style"] == I.STYLE_CYCLICAL
    assert d["matched"] == "cyclical_industry_yoy_missing"
    assert d["partial"] is True
    assert "缺失" in d["reason"]
    assert "周期锁" in d["reason"]


def test_classify_cyclical_yoy_missing_stays_cyclical_with_partial_flag():
    """三态：同比缺失 → **仍强制归周期**，但打 ``partial=True`` 留痕（周期位置未判定）。

    口径要点：缺失只影响「周期位置」，不影响「是不是周期股」——所以既不能
    按「波动为 0」免检放去股息锚，也不能因缺失就退回 value / growth。
    """
    row = _row(industry="普钢", profit_yoy=None)
    d = I.classify_intrinsic_style_detail(
        row, is_dividend_asset=False, is_growth_stock=False, cyclical_industries=CYCLICALS
    )
    assert d["style"] == I.STYLE_CYCLICAL
    assert d["matched"] == "cyclical_industry_yoy_missing"
    assert d["partial"] is True
    assert "缺失" in d["reason"]


def test_classify_authoritative_flags_take_precedence_over_thresholds():
    """引擎传入权威判定时优先采信（铁律 10：全系统一份红利/成长定义）。"""
    row = _row(industry="白酒Ⅱ", dividend_yield=0.2, payout_ratio_pct=10.0, profit_yoy=1.0)
    d = I.classify_intrinsic_style_detail(
        row, is_dividend_asset=True, is_growth_stock=None, cyclical_industries=NO_CYCLICALS
    )
    assert d["style"] == I.STYLE_DIVIDEND
    assert d["matched"] == "dividend_authoritative"


def test_classify_growth_blocked_by_low_pe_high_yield():
    """低估值+高股息不按成长定价（防止 ICBC 类票被成长分支吃掉）。

    注意分红率要落在 30% 以下，否则会先被「红利」分支接走（红利在成长之前）。
    """
    row = _row(industry="白酒Ⅱ", profit_yoy=40.0, growth_score=80.0,
               dividend_yield=4.5, pe_ttm=10.0, payout_ratio_pct=20.0)
    d = I.classify_intrinsic_style_detail(
        row, is_dividend_asset=None, is_growth_stock=None, cyclical_industries=NO_CYCLICALS
    )
    assert d["style"] == I.STYLE_VALUE
    assert d["matched"] == "growth_blocked_by_dividend"


def test_classify_thresholds_usable_standalone():
    """独立运行（不注入权威判定）时自带阈值仍可用。"""
    row = _row(industry="半导体", profit_yoy=45.0, growth_score=75.0)
    assert I.classify_intrinsic_style(
        row, cyclical_industries=NO_CYCLICALS
    ) == I.STYLE_GROWTH
    assert I.STYLE_PRECEDENCE == ("cyclical", "dividend", "growth", "value")


# ── 2. 四类模型 ─────────────────────────────────────────────────────────
def test_dividend_anchor_formula():
    """股息锚定：V = DPS / (国债 + 风险溢价)，零增长资本化。"""
    v, note = I.intrinsic_value_dividend(_row(dividend_per_share=2.0, dividend_yield=4.0))
    assert v == pytest.approx(2.0 / (CN_10Y_BOND_YIELD_PCT / 100.0 + 0.02), rel=1e-6)
    assert "红利锚定价值" in note

    assert I.intrinsic_value_dividend(_row(dividend_per_share=0.0))[0] is None
    assert I.intrinsic_value_dividend(_row(dividend_per_share=None))[0] is None


def test_cyclical_anchor_takes_weighted_cross():
    """双锚加权：0.6×PE + 0.4×PB，不再取 min。"""
    # PE=1×12=12，PB=5×1.5=7.5 → 加权 0.6*12+0.4*7.5=10.2
    row = _row(industry="工业金属", eps_normalized=1.0, bvps=5.0, roe_ttm=10.0,
               pb_percentile=30.0)
    a = I.cyclical_anchors(row)
    assert a["value_normalized_pe"] == pytest.approx(1.0 * I.CYCLICAL_NORMAL_PE)
    assert a["value_pb_roe"] == pytest.approx(5.0 * 1.5)
    assert a["anchor_used"] == "weighted"
    assert a["intrinsic_value_per_share"] == pytest.approx(
        0.6 * a["value_normalized_pe"] + 0.4 * a["value_pb_roe"]
    )
    assert a["discount"] == 1.0

    # 仅 PE 锚
    row2 = _row(industry="工业金属", eps_normalized=1.0, bvps=None, roe_ttm=None,
                pb_percentile=30.0)
    a2 = I.cyclical_anchors(row2)
    assert a2["anchor_used"] == "normalized_pe"
    assert a2["intrinsic_value_per_share"] == pytest.approx(I.CYCLICAL_NORMAL_PE)


def test_cyclical_pb_percentile_discount_applies_to_pb_only():
    """PB 分位极端折价只打在 PB 锚上，不再对 min(PE,PB) 叠 ×0.6。"""
    row = _row(
        industry="工业金属",
        eps_normalized=2.8,
        bvps=20.0,
        roe_ttm=12.0,
        pb_percentile=95.0,
        roe_trend=-1.0,
    )
    a = I.cyclical_anchors(row)
    pe = 2.8 * I.CYCLICAL_NORMAL_PE
    pb_raw = 20.0 * max(1.0, 0.12 * 15.0)
    assert a["value_normalized_pe"] == pytest.approx(pe)
    assert a["discount"] == I.CYCLICAL_DISCOUNT_EXTREME_ROE_DOWN
    assert a["value_pb_roe"] == pytest.approx(pb_raw * a["discount"])
    assert a["intrinsic_value_per_share"] == pytest.approx(
        0.6 * pe + 0.4 * a["value_pb_roe"]
    )

    row["pb_percentile"] = 75.0
    row["roe_trend"] = 0.0
    a2 = I.cyclical_anchors(row)
    assert a2["discount"] == I.CYCLICAL_DISCOUNT_HIGH


def test_cyclical_anchor_falls_back_to_median_eps_then_annualized_interim():
    row = _row(industry="工业金属", eps_normalized=None, eps_5y_median=0.6, eps_ttm=2.0,
               bvps=None, roe_ttm=None, pb_percentile=10.0)
    a = I.cyclical_anchors(row)
    assert a["eps_source"] == "近 5 年 EPS 中位数"
    assert a["intrinsic_value_per_share"] == pytest.approx(0.6 * I.CYCLICAL_NORMAL_PE)

    row["eps_5y_median"] = None
    row["eps_is_interim"] = True
    row["eps_report_date"] = "20260630"
    row["eps_ttm"] = 1.34
    a2 = I.cyclical_anchors(row)
    assert "年化" in a2["eps_source"]
    assert a2["eps_normalized"] == pytest.approx(2.68)
    # 禁止把 1.34 当全年
    assert a2["intrinsic_value_per_share"] == pytest.approx(2.68 * I.CYCLICAL_NORMAL_PE)


def test_cyclical_shenhua_style_not_eight_yuan():
    """神华案：H1 EPS 1.34 + PB99% 不得再算出 ~8 元。"""
    row = _row(
        industry="煤炭开采",
        eps_normalized=None,
        eps_5y_median=None,
        eps_ttm=1.34,
        eps_is_interim=True,
        eps_report_date="20260630",
        bvps=20.81,
        roe_ttm=12.75,
        pb_percentile=99.0,
        soft_cyclical=True,
        cyclical_integrated=True,
        roe_trend=0.5,
    )
    a = I.cyclical_anchors(row)
    assert a["eps_normalized"] == pytest.approx(2.68)
    assert a["soft_cyclical"] is True
    assert a["intrinsic_value_per_share"] is not None
    assert a["intrinsic_value_per_share"] > 25.0  # 远高于旧 bug 的 8 元


def test_cyclical_anchor_missing_returns_none_not_zero():
    v, note = I.intrinsic_value_cyclical(
        _row(industry="工业金属", eps_normalized=None, eps_5y_median=None, eps_ttm=None,
             bvps=None, roe_ttm=None)
    )
    assert v is None
    assert "数据不足" in note


def test_dcf_three_stage_positive_and_gated_on_eps():
    v, note = I.intrinsic_value_dcf(_row(eps_ttm=1.0, profit_yoy=20.0))
    assert v is not None and v > 1.0
    assert "DCF 三阶段" in note
    assert I.intrinsic_value_dcf(_row(eps_ttm=-1.0))[0] is None
    assert I.intrinsic_value_dcf(_row(eps_ttm=1.0, profit_yoy=20.0, wacc=0.02,
                                      terminal_growth=0.02))[0] is None


def test_ddm_payout_gate_three_states():
    """DDM 分红率闸门：<20% 或 >90% 不适用；缺失（None）同样不判。"""
    r = _row(dividend_per_share=1.0)
    assert I.ddm_payout_allows(None) is False
    assert I.ddm_payout_allows(15.0) is False
    assert I.ddm_payout_allows(95.0) is False
    assert I.ddm_payout_allows(50.0) is True

    assert I.intrinsic_value_ddm({**r, "payout_ratio_pct": None})[0] is None
    assert I.intrinsic_value_ddm({**r, "payout_ratio_pct": 10.0})[0] is None
    v, note = I.intrinsic_value_ddm({**r, "payout_ratio_pct": 50.0})
    assert v is not None and "DDM" in note


# ── 3. 交叉验证 ─────────────────────────────────────────────────────────
def test_cross_validation_two_models_median():
    o = I.compute_intrinsic_value(
        _row(industry="半导体", eps_ttm=1.2, profit_yoy=35.0, dividend_per_share=0.3,
             payout_ratio_pct=35.0),
        dcf={"value": 52.0, "note": "DCF", "reliable": True},
        ddm={"value": 46.0, "note": "DDM", "reliable": True},
        is_dividend_asset=False,
        is_growth_stock=True,
        cyclical_industries=CYCLICALS,
    )
    assert o["cross_model"] is True
    assert o["model_count"] == 2
    assert o["cross_basis"] == "median"
    assert o["conservative"] is False
    assert o["unreliable_models"] == []
    assert o["intrinsic_value_per_share"] == pytest.approx(49.0)
    assert o["margin_of_safety_pct"] == pytest.approx((49.0 / 10.0 - 1) * 100, abs=0.05)
    assert "交叉验证" in o["note"]
    assert o["display_only"] is True


def test_single_model_is_labeled_not_crossed():
    o = I.compute_intrinsic_value(
        _row(industry="半导体", eps_ttm=1.2, profit_yoy=35.0, dividend_per_share=0.3,
             payout_ratio_pct=None),
        dcf={"value": 52.0, "note": "DCF", "reliable": True},
        ddm=None,
        is_dividend_asset=False,
        is_growth_stock=True,
        cyclical_industries=CYCLICALS,
    )
    assert o["cross_model"] is False
    assert o["model_count"] == 1
    assert "单模型" in o["note"] and "未交叉" in o["note"]


def test_unreliable_model_enters_pool_and_takes_conservative_min():
    """不可信模型仍入池（保住交叉），但整体改取保守侧 min。"""
    o = I.compute_intrinsic_value(
        _row(industry="半导体", eps_ttm=1.2, profit_yoy=35.0, dividend_per_share=0.3,
             payout_ratio_pct=35.0),
        dcf={"value": 12.0, "note": "DCF 不可信", "reliable": False},
        ddm={"value": 46.0, "note": "DDM", "reliable": True},
        is_dividend_asset=False,
        is_growth_stock=True,
        cyclical_industries=CYCLICALS,
    )
    assert o["cross_model"] is True
    assert o["model_count"] == 2
    assert o["cross_basis"] == "conservative"
    assert o["conservative"] is True
    assert o["intrinsic_value_per_share"] == pytest.approx(12.0)  # min(12, 46)
    assert o["unreliable_models"] == ["dcf"]
    assert "保守" in o["note"] and "dcf" in o["note"]
    assert o["models"]["ddm"]["value"] == pytest.approx(46.0)


def test_unreliable_high_growth_dcf_does_not_inflate_result():
    """关键回归：不可信的高成长 DCF 远高于 DDM 时取 min，而不是被中位数拉高。"""
    o = I.compute_intrinsic_value(
        _row(industry="半导体", eps_ttm=1.2, profit_yoy=80.0, dividend_per_share=0.3,
             payout_ratio_pct=40.0),
        dcf={"value": 99.0, "note": "峰值 DCF", "reliable": False},
        ddm={"value": 46.0, "note": "DDM", "reliable": True},
        is_dividend_asset=False,
        is_growth_stock=True,
        cyclical_industries=CYCLICALS,
    )
    assert o["cross_model"] is True
    assert o["cross_basis"] == "conservative"
    assert o["intrinsic_value_per_share"] == pytest.approx(46.0)  # 中位数会是 72.5


def test_high_growth_reference_dcf_stays_out_of_the_pool():
    """营收高增压力测试 DCF 只作对照，不能把可信 DDM 拖成 min。"""
    o = I.compute_intrinsic_value(
        _row(industry="消费电子", eps_ttm=1.2, profit_yoy=96.0, dividend_per_share=0.5,
             payout_ratio_pct=35.0, price=20.0),
        dcf={"value": 4.55, "note": "OCF×0.5 压力测试", "reliable": False,
             "role": "high_growth_reference"},
        ddm={"value": 18.0, "note": "DDM", "reliable": True},
        is_dividend_asset=False,
        is_growth_stock=False,
        cyclical_industries=CYCLICALS,
    )
    assert o["style"] == I.STYLE_VALUE
    assert "dcf" not in o["models"]
    assert set(o["models"]) == {"ddm"}
    assert o["cross_model"] is False
    assert o["intrinsic_value_per_share"] == pytest.approx(18.0)
    assert o["unreliable_models"] == []
    aux = o["auxiliary"]["dcf"]
    assert aux["value"] == pytest.approx(4.55)
    assert aux["reliable"] is False
    assert "不参与交叉" in aux["reason"]
    assert "仅作对照" in o["note"]
    assert "未入池" in o["note"]


def test_high_growth_reference_dcf_alone_does_not_become_the_value():
    """DDM 被分红率闸掉时，压力测试 DCF 也不能兜底成内在价值。"""
    o = I.compute_intrinsic_value(
        _row(industry="消费电子", eps_ttm=1.2, profit_yoy=96.0, dividend_per_share=0.5,
             payout_ratio_pct=10.0, price=20.0),
        dcf={"value": 4.55, "note": "OCF×0.5 压力测试", "reliable": False,
             "role": "high_growth_reference"},
        ddm={"value": 18.0, "note": "DDM", "reliable": True},
        is_dividend_asset=False,
        is_growth_stock=False,
        cyclical_industries=CYCLICALS,
    )
    assert o["intrinsic_value_per_share"] is None
    assert o["model_count"] == 0
    assert o["models"] == {}
    assert o["auxiliary"]["dcf"]["value"] == pytest.approx(4.55)
    assert "4.55" in o["note"]
    assert "无法给出内在价值" in o["note"]


def test_single_unreliable_model_is_flagged_not_hidden():
    """只有一个模型且被标不可信 → 仍给值，但 note 明确提醒。"""
    o = I.compute_intrinsic_value(
        _row(industry="半导体", eps_ttm=1.2, profit_yoy=35.0, payout_ratio_pct=None),
        dcf={"value": 12.0, "note": "DCF 不可信", "reliable": False},
        ddm=None,
        is_dividend_asset=False,
        is_growth_stock=True,
        cyclical_industries=CYCLICALS,
    )
    assert o["cross_model"] is False
    assert o["cross_basis"] is None
    assert o["conservative"] is False
    assert o["intrinsic_value_per_share"] == pytest.approx(12.0)
    assert o["unreliable_models"] == ["dcf"]
    assert "不可信" in o["note"]


def test_cyclical_keeps_precedence_and_records_dividend_anchor_as_auxiliary():
    """周期 > 红利：高股息强周期票走周期锚，股息锚只作辅助参考（不入交叉）。

    这正是「景气高点高股息会被骗出虚高内在价值」的场景：股息锚会给出远高于
    周期锚的值，但只有周期锚参与定价。
    """
    o = I.compute_intrinsic_value(
        _row(industry="煤炭开采", profit_yoy=-45.0, dividend_yield=9.0,
             payout_ratio_pct=80.0, dividend_per_share=4.0,
             eps_normalized=1.0, bvps=10.0, roe_ttm=10.0, pb_percentile=30.0),
        is_dividend_asset=True,
        is_growth_stock=False,
        cyclical_industries=CYCLICALS,
    )
    assert o["style"] == I.STYLE_CYCLICAL
    assert set(o["models"]) == {"cyclical_anchor"}
    # PE=1×12=12，PB=10×1.5=15 → 加权 0.6*12+0.4*15=13.2
    assert o["intrinsic_value_per_share"] == pytest.approx(13.2)
    assert "dividend_anchor" not in o["models"]
    aux = o["auxiliary"]["dividend_anchor"]
    assert aux["value"] == pytest.approx(
        4.0 / (CN_10Y_BOND_YIELD_PCT / 100.0 + 0.02), rel=1e-4
    )
    assert aux["value"] > o["intrinsic_value_per_share"] * 5  # 股息锚会虚高一大截
    assert aux["reliable"] is False
    assert "峰值利润" in aux["reason"]


def test_cosco_real_shape_is_locked_to_cyclical_anchor():
    """线上实测形态回归：中远海控 601919（2026-09-29 真实行情快照）。

    修前：行业「航运港口」命中了周期名单，但净利同比 −23.48% 过不了 30% 波动闸
    → 被标「红利资产（引擎 classify_dividend_asset 判定）」、内在价值取股息锚。
    修后：周期锁生效，改取正常化利润 + PB-ROE 锚，股息锚降为对照。
    """
    o = I.compute_intrinsic_value(
        _row(
            industry="航运港口", price=16.33, pe_ttm=9.32, pb=1.07,
            pb_percentile=42.1, dividend_yield=6.12, payout_ratio_pct=49.9,
            dividend_per_share=1.0, eps_ttm=0.88,
            eps_normalized=2.0217418578063246, bvps=15.261682242990652,
            roe_ttm=13.17, profit_yoy=-23.48,
        ),
        is_dividend_asset=True,
        is_growth_stock=False,
        cyclical_industries=frozenset({"航运港口"}),
    )
    assert o["style"] == I.STYLE_CYCLICAL
    # 2026-09-29：取消 30% 波动闸后，行业命中即归周期，matched 统一为 cyclical_industry
    assert o["style_matched"] == "cyclical_industry"
    assert set(o["models"]) == {"cyclical_anchor"}
    # PE=2.0217×12≈24.26；PB=15.2617×1.9755≈30.15 → 加权 ≈26.62
    pe_a = 2.0217418578063246 * I.CYCLICAL_NORMAL_PE
    pb_a = 15.261682242990652 * max(1.0, 0.1317 * 15.0)
    expect = 0.6 * pe_a + 0.4 * pb_a
    assert o["intrinsic_value_per_share"] == pytest.approx(expect, abs=0.05)
    assert o["models"]["cyclical_anchor"]["anchors"]["anchor_used"] == "weighted"
    assert o["models"]["cyclical_anchor"]["anchors"]["discount"] == 1.0
    aux = o["auxiliary"]["dividend_anchor"]
    assert aux["value"] == pytest.approx(27.03, abs=0.01)  # 1.00 / (1.7%+2%)
    # 股息锚仍作对照；加权周期锚抬高后可能接近或略低于股息锚，只要求周期锚可信算出
    assert aux["reliable"] is False
    assert o["intrinsic_value_per_share"] > 20.0


def test_no_model_returns_none_with_reason():
    o = I.compute_intrinsic_value(
        {"industry": "半导体", "price": 10.0, "eps_ttm": None, "dividend_per_share": None},
        is_dividend_asset=False,
        is_growth_stock=False,
        cyclical_industries=CYCLICALS,
    )
    assert o["intrinsic_value_per_share"] is None
    assert o["model_count"] == 0
    assert o["note"] == "数据不足，无法计算内在价值"
    assert o["cross_model"] is False


def test_cyclical_style_ignores_injected_dcf():
    """周期股禁用峰值 DCF：即使引擎注入了可信 DCF，周期锚也不采用它。"""
    o = I.compute_intrinsic_value(
        _row(industry="工业金属", profit_yoy=58.0, eps_normalized=1.0, bvps=10.0,
             roe_ttm=10.0, pb_percentile=20.0),
        dcf={"value": 99.0, "note": "峰值 DCF", "reliable": True},
        is_dividend_asset=False,
        is_growth_stock=False,
        cyclical_industries=CYCLICALS,
    )
    assert o["style"] == I.STYLE_CYCLICAL
    assert "dcf" not in o["models"]
    # 加权周期锚，绝不用注入的峰值 DCF 99
    assert o["intrinsic_value_per_share"] == pytest.approx(13.2)
    assert o["intrinsic_value_per_share"] < 99.0


# ── 4. 引擎接入 ─────────────────────────────────────────────────────────
def _fake_cyclical_build(symbol: str, years: int = 5):
    rows = [
        {"revenue": r, "net_profit": n, "equity": 50e8, "operating_cashflow": n * 1.2,
         "capital_expenditure": 1e8, "operating_profit": n * 1.1, "cogs": r * 0.6,
         "total_assets": 200e8, "current_liabilities": 60e8, "accounts_receivable": 8e8,
         "goodwill": 2e8, "monetary_funds": 20e8, "short_term_borrowings": 5e8,
         "roe": 12.0}
        for r, n in ((80e8, 4e8), (90e8, 6e8), (100e8, 12e8))
    ]
    fd = pd.DataFrame(rows, index=["20211231", "20221231", "20231231"])
    return fd, {
        "name": "测试铝业",
        "industry": "工业金属",
        "symbol": "000807.SZ",
        "report_dates": list(fd.index),
        "annual_dates": list(fd.index),
        "revenue_yoy": 12.0,
        "profit_yoy": 58.0,
        "debt_ratio": 40.0,
        "latest_report": "20231231",
        "latest_roe": 12.0,
        "eps": 0.8,
        "payout_ratio_pct": 30.0,
        "cycle_annual_profit": [
            ["20171231", 2e8], ["20181231", 3e8], ["20191231", 1e8],
            ["20201231", 4e8], ["20211231", 4e8], ["20221231", 6e8],
            ["20231231", 12e8],
        ],
    }


def _patch_engine(monkeypatch):
    monkeypatch.setattr("app.analysis.engine.build_financial_dataframe", _fake_cyclical_build)
    monkeypatch.setattr("app.analysis.engine.industry_averages", lambda *a, **k: {})
    monkeypatch.setattr(
        "app.analysis.engine.get_valuations",
        lambda *a, **k: [
            {
                "symbol": "000807.SZ",
                "name": "测试铝业",
                "price": 12.0,
                "pe_ttm": 9.0,
                "pb": 1.1,
                "pe_percentile": 22.0,
                "pb_percentile": 30.0,
                "market_cap": 12.0 * 20e8,
                "dividend_yield": 2.0,
                "total_shares": 20e8,
            }
        ],
    )
    monkeypatch.setattr(
        "app.analysis.engine.calculate_comparable_valuation",
        lambda *a, **k: {
            "stock_code": "000807.SZ",
            "comparables": [],
            "avg_pe": None,
            "avg_pb": None,
            "valuation_range": {},
            "peer_count": 0,
            "insufficient_sample": True,
        },
    )


def test_engine_emits_cyclical_intrinsic_and_suppresses_peak_dcf(monkeypatch):
    """周期票：内在价值走正常化锚；DCF 打「禁用峰值 DCF」展示标记但不改分。"""
    _patch_engine(monkeypatch)
    report = FundamentalEngine().run_full_analysis("000807.SZ", db=None)
    val = report["valuation"]

    iv = val["intrinsic_value"]
    assert iv["style"] == "cyclical"
    assert iv["display_only"] is True
    assert iv["intrinsic_value_per_share"] is not None
    assert set(iv["models"]) == {"cyclical_anchor"}
    assert iv["models"]["cyclical_anchor"]["anchors"]["normal_pe"] == I.CYCLICAL_NORMAL_PE
    assert iv["eps_normalized_source"]

    # 周期分支的股息锚对照（辅助参考，不入 models / 不参与交叉）
    aux = iv["auxiliary"]["dividend_anchor"]
    assert aux["reliable"] is False
    assert aux["value"] == pytest.approx(
        0.24 / (CN_10Y_BOND_YIELD_PCT / 100.0 + 0.02), rel=1e-3
    )
    assert "dividend_anchor" not in iv["models"]
    assert val["combined_intrinsic_value"]["auxiliary"]["dividend_anchor"]

    # DCF 仍在（兜底评分依赖它），但被标记为不适用于定价
    assert val["dcf"]["role"] == "not_applicable_cyclical"
    assert val["dcf"]["suppressed"] is True
    assert "峰值" in val["dcf"]["note"]
    assert val["dcf"]["intrinsic_value_per_share"] is not None

    # 估值分推导未被本块污染：仍是 base − haircut（下限 20）
    assert val["intrinsic_value"]["display_only"] is True
    expected = max(20.0, val["valuation_score_base"] - val["valuation_score_haircut"])
    assert val["composite_valuation_score"] == pytest.approx(expected, abs=0.11)

    # 旧键位仍镜像新结果
    assert val["combined_intrinsic_value"]["style"] == "cyclical"
    assert val["combined_intrinsic_value"]["intrinsic_value_per_share"] == pytest.approx(
        iv["intrinsic_value_per_share"]
    )


def _fake_dividend_build(symbol: str, years: int = 5):
    fd = _fake_cyclical_build(symbol, years)[0]
    return fd, {
        "name": "测试红利",
        "industry": "白酒Ⅱ",
        "symbol": "600519.SH",
        "report_dates": list(fd.index),
        "annual_dates": list(fd.index),
        "revenue_yoy": 6.0,
        "profit_yoy": 8.0,
        "debt_ratio": 20.0,
        "latest_report": "20231231",
        "latest_roe": 24.0,
        "eps": 6.0,
        "payout_ratio_pct": 75.0,
        "dividend": {"d0": 4.5, "fy": "2023"},
    }


def test_engine_emits_dividend_intrinsic_with_ddm_cross(monkeypatch):
    """红利票：股息锚 + DDM 两模型交叉（红利是最大票池，必须走通）。"""
    monkeypatch.setattr("app.analysis.engine.build_financial_dataframe", _fake_dividend_build)
    monkeypatch.setattr("app.analysis.engine.industry_averages", lambda *a, **k: {})
    monkeypatch.setattr(
        "app.analysis.engine.get_valuations",
        lambda *a, **k: [
            {
                "symbol": "600519.SH",
                "name": "测试红利",
                "price": 60.0,
                "pe_ttm": 10.0,
                "pb": 3.0,
                "pe_percentile": 40.0,
                "pb_percentile": 50.0,
                "market_cap": 60.0 * 20e8,
                "dividend_yield": 7.5,
                "total_shares": 20e8,
            }
        ],
    )
    monkeypatch.setattr(
        "app.analysis.engine.calculate_comparable_valuation",
        lambda *a, **k: {
            "stock_code": "600519.SH",
            "comparables": [],
            "avg_pe": None,
            "avg_pb": None,
            "valuation_range": {},
            "peer_count": 0,
            "insufficient_sample": True,
        },
    )

    report = FundamentalEngine().run_full_analysis("600519.SH", db=None)
    val = report["valuation"]
    iv = val["intrinsic_value"]

    assert val["is_dividend_asset"] is True
    assert iv["style"] == "dividend"
    assert set(iv["models"]) == {"dividend_anchor", "ddm"}
    assert iv["cross_model"] is True
    assert iv["intrinsic_value_per_share"] is not None
    # DCF 对红利资产仍是抑制态（原有行为不变）
    assert val["dcf"]["role"] == "not_applicable_dividend"


def test_build_ddm_has_no_hardcoded_forward_note():
    """回归：`_build_ddm` 不得再输出硬编码的「前瞻股息率」文案。

    此前 `forward_note` 写死「2026 中期已派 0.98 元…前瞻股息率约 5.5%-6%」，
    对**任何**走 DDM 的红利股都吐同一串数字（全仓零消费方，仅展示但会误导）。
    前瞻口径需机构 EPS 预测与中期分红进度，本仓都无数据源 → 宁可少一句，不臆造。
    """
    out = FundamentalEngine()._build_ddm(
        {"price": 47.0, "dividend_yield": 2.4},
        {"symbol": "000651.SZ", "dividend": {"d0": 2.26, "fy": "2023"}},
    )
    assert "forward_note" not in out
    assert "forward_dividend_yield_pct" not in out
    blob = str(out)
    for _ghost in ("0.98", "2.86", "5.5%"):
        assert _ghost not in blob
    assert len(out["scenarios"]) == 3
    assert out["d0"] == pytest.approx(2.26)


def test_build_ddm_still_skips_when_d0_unavailable():
    """D0 不可得时仍走「跳过」分支（原有行为不变）。"""
    out = FundamentalEngine()._build_ddm({"price": None, "dividend_yield": None}, {})
    assert out["d0"] is None
    assert out["scenarios"] == []
    assert "跳过" in out["note"]
