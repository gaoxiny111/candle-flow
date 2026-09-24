"""本地单测：quality_value 判定逻辑（纯函数，不连库）。

覆盖四个刻意偏离点的靶点：
- 红利资产 PEG 缺失 → 不淘汰（贵州茅台型）
- 低毛利行业走行业分位（江西铜业型）
- 现金流用 5 年均值（江西铜业年报 OCF/NP = -0.97 但 5 年均值 0.89）
- 缺失值不等于坏值（不赋中性分也不误杀）
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.services.quality_value import (  # noqa: E402
    apply_trading_rules,
    compute_qv,
    evaluate_position,
    is_low_margin_industry,
    sort_qv,
)


def _row(sym, name, ind, **kw):
    base = {
        "symbol": sym, "name": name, "industry": ind,
        "composite_score": 70.0, "market_cap": 5e10, "price": 20.0,
        "pe_ttm": None, "pb": None, "pe_percentile": None, "pb_percentile": None,
        "dividend_yield": None, "roe_pct": None, "gross_margin_pct": None,
        "roic_pct": None, "debt_ratio_pct": None, "ocf_np_5y": None,
        "revenue_yoy": None, "profit_yoy_pct": None, "peg": None,
    }
    base.update(kw)
    return base


def test_low_margin_industry():
    assert is_low_margin_industry("工业金属")
    assert is_low_margin_industry("大宗供应链")
    assert not is_low_margin_industry("白酒Ⅱ")
    assert not is_low_margin_industry(None)


def test_dividend_asset_peg_missing_not_rejected():
    """茅台型：PEG 被口径置空 → 只记 missing，不因 PEG 淘汰。"""
    rows = [
        _row("600519.SH", "贵州茅台", "白酒Ⅱ", roe_pct=32.53, gross_margin_pct=91.34,
             roic_pct=42.75, debt_ratio_pct=16.0, ocf_np_5y=0.90,
             pe_ttm=19.25, pb=6.24, pe_percentile=1.8, pb_percentile=2.2,
             dividend_yield=4.15, peg=None),
    ] * 6  # 行业内样本 ≥5 才信分位
    passed, rejected = compute_qv(rows)
    assert len(passed) == len(rows), f"茅台型被误杀: {rejected[0]['qv_reasons'] if rejected else ''}"
    assert "PEG" in passed[0]["qv_missing"]


def test_low_margin_industry_uses_relative_pct():
    """江西铜业型：毛利率 4.4% 绝对不达标，但行业内分位高 → 通过。

    （ROIC 取 10 达标值：本用例只验毛利率行业分位通道，不验 ROIC 门槛——
      ROIC 门槛修复单边失效后 6.49 会被拦，属预期行为，另有专项用例。）
    """
    rows = [
        _row(f"60036{i}.SH", f"铜业{i}", "工业金属", roe_pct=8.96 + i, gross_margin_pct=4.4,
             roic_pct=10.0, debt_ratio_pct=55.0, ocf_np_5y=0.89,
             pe_ttm=13.48, pb=1.78, pe_percentile=39.2, pb_percentile=30.0)
        for i in range(0, 6)
    ]
    # 让第一只毛利率行业分位最高
    rows[0]["gross_margin_pct"] = 9.9
    passed, rejected = compute_qv(rows)
    assert len(passed) >= 1, "低毛利行业全部被拦（绝对阈值误用）"
    assert passed[0]["qv_components"]["low_margin_industry"] is True


def test_ocf_np_single_year_would_miss():
    """现金流闸门读的是 5 年均值字段：单年 -0.97 的票不应被 5 年均值 0.89 拦下。"""
    rows = [_row(f"00000{i}.SZ", f"票{i}", "工业金属", roe_pct=15.0, gross_margin_pct=20.0,
                 roic_pct=10.0, debt_ratio_pct=40.0, ocf_np_5y=0.89,
                 pe_ttm=13.0, pb=1.7, pe_percentile=30.0, pb_percentile=30.0)
            for i in range(6)]
    passed, _ = compute_qv(rows)
    assert len(passed) == 6


def test_missing_is_not_zero():
    """全字段缺失 → 不淘汰（缺失≠坏 value），但缺项要留痕。"""
    rows = [_row(f"30000{i}.SZ", f"缺{i}", "白酒Ⅱ") for i in range(6)]
    passed, rejected = compute_qv(rows)
    assert len(passed) == 6, "缺失被当成了不达标"
    assert {"ROE", "ROIC", "毛利率"} & set(passed[0]["qv_missing"])


def test_reject_reason_self_evidence():
    """真淘汰要给出可自证理由。"""
    rows = [_row(f"60000{i}.SH", f"差{i}", "白酒Ⅱ", roe_pct=3.0, gross_margin_pct=12.0,
                 roic_pct=2.0, debt_ratio_pct=85.0, ocf_np_5y=0.1,
                 pe_ttm=80.0, pb=9.0, pe_percentile=95.0, pb_percentile=95.0)
            for i in range(6)]
    passed, rejected = compute_qv(rows)
    assert not passed
    assert len(rejected) == 6
    assert all(r["qv_reasons"] for r in rejected)
    assert rejected[0]["qv_components"]["quality_pct"] < 50


def test_sort_orders_none_last():
    items = [
        {"symbol": "A", "qv_score": None, "qv_components": {}, "roe_pct": 1},
        {"symbol": "B", "qv_score": 80.0, "qv_components": {}, "roe_pct": 2},
        {"symbol": "C", "qv_score": 30.0, "qv_components": {}, "roe_pct": 3},
    ]
    out = sort_qv(items, "qv_score")
    assert [r["symbol"] for r in out] == ["B", "C", "A"]


def test_sort_pe_puts_loss_makers_last():
    """按 PE 升序时，亏损股（PE<0）是「无盈利」不是「最便宜」→ 必须排最后。

    实测靶点：000031 大悦城 PE=-3.23 会挤到 600841 动力新科 PE=2.49 之前。
    """
    items = [
        {"symbol": "LOSS", "pe_ttm": -3.23, "qv_components": {}},
        {"symbol": "CHEAP", "pe_ttm": 2.49, "qv_components": {}},
        {"symbol": "MID", "pe_ttm": 12.0, "qv_components": {}},
        {"symbol": "NONE", "pe_ttm": None, "qv_components": {}},
    ]
    out = sort_qv(items, "pe_ttm")
    assert [r["symbol"] for r in out] == ["CHEAP", "MID", "LOSS", "NONE"]


def test_sort_pb_zero_is_not_cheapest():
    items = [
        {"symbol": "ZERO", "pb": 0.0, "qv_components": {}},
        {"symbol": "LOW", "pb": 0.44, "qv_components": {}},
    ]
    out = sort_qv(items, "pb")
    assert [r["symbol"] for r in out] == ["LOW", "ZERO"]


def test_industry_median_hidden_when_sample_small():
    """样本 < 5 时行业中位数退化（=自己）→ 必须置 null，不能当行业水平用。

    实测靶点：江西铜业单独查行业时 PE 中位 13.48 == 自身 PE。
    """
    rows = [
        _row("600362.SH", "江西铜业", "工业金属", roe_pct=12.0, gross_margin_pct=4.4,
             roic_pct=9.0, debt_ratio_pct=55.0, ocf_np_5y=0.89,
             pe_ttm=13.48, pb=1.78, pe_percentile=39.2, pb_percentile=30.0),
        _row("000001.SZ", "平安银行", "银行Ⅱ", roe_pct=12.0, gross_margin_pct=30.0,
             roic_pct=9.0, debt_ratio_pct=50.0, ocf_np_5y=0.9,
             pe_ttm=5.0, pb=0.6, pe_percentile=20.0, pb_percentile=20.0),
    ]
    passed, _ = compute_qv(rows)
    cu = next(r for r in passed if r["symbol"] == "600362.SH")
    assert cu["qv_components"]["industry_sample"] == 1
    assert cu["qv_components"]["industry_median"]["pe"] is None, "样本=1 的中位数被当成了行业水平"


def test_negative_pe_goes_percentile_only():
    """亏损股 PE<0：绝对 PE 无意义 → 只走分位，分位缺失则放行。"""
    rows = [_row(f"60010{i}.SH", f"亏{i}", "工业金属", roe_pct=12.0, gross_margin_pct=25.0,
                 roic_pct=9.0, debt_ratio_pct=45.0, ocf_np_5y=0.9,
                 pe_ttm=-5.0, pb=0.44, pe_percentile=None, pb_percentile=None)
            for i in range(6)]
    passed, rejected = compute_qv(rows)
    assert len(passed) == 6, f"亏损股被 PE<0 误杀: {[r['qv_reasons'] for r in rejected]}"


# ══════════════════════════════════════════════════════════════════════════
# 交易规则（硬编码调仓）
# ══════════════════════════════════════════════════════════════════════════

def _cand(sym, name, qv, mcap_yi, price=20.0, comp=70.0):
    """调仓规则用的候选条目（qv_score / market_cap_yi / price 是判据三件套）。"""
    return {
        "symbol": sym, "name": name, "industry": "测试行业",
        "qv_score": qv, "composite_score": comp,
        "market_cap_yi": mcap_yi, "price": price,
    }


def test_buy_needs_both_score_and_market_cap():
    """调入必须两项同时满足：得分 > 80 **且** 市值 > 50 亿。"""
    r = evaluate_position(_cand("600519.SH", "茅台", 83.3, 15673.5))
    assert r["action"] == "buy", r

    # 得分够、市值不够 → 不买（小盘流动性风险）
    r = evaluate_position(_cand("000001.SZ", "小盘", 85.0, 30.0))
    assert r["action"] == "skip"
    assert any("市值" in x for x in r["reasons"])

    # 市值够、得分不够 → 不买
    r = evaluate_position(_cand("000002.SZ", "平庸", 79.9, 500.0))
    assert r["action"] == "skip"
    assert any("得分" in x for x in r["reasons"])


def test_buy_boundary_is_strict_greater_than():
    """边界：恰好 80 分 / 恰好 50 亿 **都不满足**（规则写的是「>」，不是「≥」）。"""
    assert evaluate_position(_cand("A", "x", 80.0, 100.0))["action"] == "skip"
    assert evaluate_position(_cand("B", "x", 80.1, 100.0))["action"] == "buy"
    assert evaluate_position(_cand("C", "x", 90.0, 50.0))["action"] == "skip"
    assert evaluate_position(_cand("D", "x", 90.0, 50.1))["action"] == "buy"


def test_sell_on_score_below_50():
    """调出（**仅持仓**）：持仓票得分 < 50 → sell。"""
    r = evaluate_position(_cand("600001.SH", "衰减", 49.9, 500.0), entry_price=20.0)
    assert r["action"] == "sell"
    assert any("得分" in x for x in r["reasons"])
    # 边界：恰好 50 不卖
    assert evaluate_position(
        _cand("600002.SH", "卡线", 50.0, 500.0), entry_price=20.0
    )["action"] == "hold"


def test_unheld_low_score_is_skip_never_sell():
    """★ 未持仓票跌破卖出线 → 只算「未达调入线」（skip），**不得**报 sell。

    用户口径：「选股结果不展示不合格股票」。旧实现把全市场低分票都判成 sell，
    空仓用户的「风控调出」条被 79 只不合格股票刷屏。
    """
    r = evaluate_position(_cand("600013.SH", "空仓低分", 30.0, 500.0))
    assert r["action"] == "skip", r
    assert "得分" in (r["reasons"] or [""])[0]

    # 批量层面：空仓时 sell 桶必须为空 —— 这是前端「风控调出」条的唯一数据源
    items = [
        _cand("600014.SH", "低分A", 10.0, 500.0),
        _cand("600015.SH", "低分B", 49.9, 500.0),
        _cand("600016.SH", "达标", 85.0, 500.0),
    ]
    res = apply_trading_rules(items)  # 不传 holdings = 空仓
    assert res["sell_count"] == 0, [x["symbol"] for x in res["sell"]]
    assert res["buy_count"] == 1 and res["skip_count"] == 2


def test_sell_on_month_drop():
    """调出：相对持仓基准价跌幅 > 20%。"""
    it = _cand("600003.SH", "暴跌", 70.0, 500.0, price=76.0)
    r = evaluate_position(it, entry_price=100.0)
    assert r["action"] == "sell", r
    assert r["month_drop_pct"] == -24.0

    # 跌 20% 整 → 不卖（规则是「> 20%」）
    r = evaluate_position(_cand("600004.SH", "卡线", 70.0, 500.0, price=80.0), entry_price=100.0)
    assert r["action"] != "sell"
    # 跌 20.1% → 卖
    r = evaluate_position(_cand("600005.SH", "过线", 70.0, 500.0, price=79.9), entry_price=100.0)
    assert r["action"] == "sell"


def test_sell_beats_buy_when_both_hit():
    """卖出优先级高于买入：阈值误配（buy < sell）时以风控为准，不得既买又卖。"""
    # qv=60：> 50(buy) 且 < 70(sell) → 同时命中两侧（持仓票）
    r = evaluate_position(
        _cand("600006.SH", "矛盾", 60.0, 500.0), buy_score=50.0, sell_score=70.0,
        entry_price=20.0,
    )
    assert r["action"] == "sell", "同一只票在两侧都命中时必须以卖出为准"


def test_missing_entry_price_never_triggers_stop_loss():
    """基准价缺失 → 不触发止损（缺失不是利空），但要留痕让用户看见规则未生效。"""
    it = _cand("600007.SH", "无基准", 70.0, 500.0, price=10.0)
    r = evaluate_position(it)  # 不传 entry_price
    assert r["action"] == "skip", r
    assert "entry_price" in r["missing"]


def test_missing_market_cap_blocks_buy():
    """市值缺失 = 流动性风险未知 → 不调入（不能默认安全）。"""
    it = _cand("600008.SH", "无市值", 90.0, None)
    r = evaluate_position(it)
    assert r["action"] == "skip"
    assert "market_cap_yi" in r["missing"]


def test_missing_qv_blocks_buy_but_not_sell():
    """qv_score 缺失 → 不买；同时也不因缺失触发卖出。"""
    it = _cand("600009.SH", "无分", None, 500.0)
    r = evaluate_position(it)
    assert r["action"] == "skip"
    assert "qv_score" in r["missing"]
    assert r["action"] != "sell"


def test_month_drop_takes_stricter_of_two_sources():
    """两条跌幅口径（持仓 vs 单月）取**更负者**，不是相加也不是取平均。"""
    it = _cand("600010.SH", "双跌", 70.0, 500.0, price=90.0)
    # 持仓跌幅 -10%，单月跌幅 -35% → 取 -35%
    r = evaluate_position(it, entry_price=100.0, month_drop_pct=-35.0)
    assert r["action"] == "sell"
    assert r["month_drop_pct"] == -35.0
    assert r["drop_source"] == "单月"

    # 反向：持仓 -30%，单月 -5% → 取 -30%
    it2 = _cand("600011.SH", "持仓跌", 70.0, 500.0, price=70.0)
    r2 = evaluate_position(it2, entry_price=100.0, month_drop_pct=-5.0)
    assert r2["month_drop_pct"] == -30.0
    assert r2["drop_source"] == "持仓"


def test_holdings_turn_skip_into_hold():
    """同一只票：空仓时是不达标 skip，持仓时是无信号 hold —— 规则不因持仓而改变判定。"""
    items = [_cand("600012.SH", "平庸", 65.0, 500.0)]
    empty = apply_trading_rules(items)
    assert empty["skip_count"] == 1 and empty["hold_count"] == 0

    held = apply_trading_rules(items, holdings={"600012.SH": 20.0})
    assert held["hold_count"] == 1 and held["skip_count"] == 0


def test_held_stock_that_still_qualifies_is_hold_not_buy():
    """已持仓且仍达调入线 → hold，**不得报 buy**（否则等于暗示加仓）。"""
    it = _cand("600519.SH", "茅台", 83.3, 15673.5, price=1253.8)
    assert evaluate_position(it)["action"] == "buy"          # 空仓 → 买
    r = evaluate_position(it, entry_price=1000.0)             # 持仓 → 持有
    assert r["action"] == "hold", f"持仓票被报成了 {r['action']}"
    assert any("已持仓" in x for x in r["reasons"])

    # 批量层面：持仓票不得出现在 buy 桶
    res = apply_trading_rules([it], holdings={"600519.SH": 1000.0})
    assert res["buy_count"] == 0 and res["hold_count"] == 1


def test_apply_rules_buckets_and_no_duplicates():
    """批量：每只票必须**恰好**落入一个桶，不得重复计数。"""
    items = [
        _cand("600519.SH", "茅台", 83.3, 15673.5),
        _cand("000001.SZ", "小盘", 85.0, 30.0),
        _cand("600001.SH", "衰减", 49.9, 500.0),
        _cand("600003.SH", "暴跌", 70.0, 500.0, price=76.0),
        _cand("600012.SH", "平庸", 65.0, 500.0),
    ]
    res = apply_trading_rules(items, holdings={"600003.SH": 100.0})
    total = res["buy_count"] + res["sell_count"] + res["hold_count"] + res["skip_count"]
    assert total == len(items), f"桶计数 {total} != 输入 {len(items)}"

    all_syms = [x["symbol"] for k in ("buy", "sell", "hold", "skip") for x in res[k]]
    assert len(all_syms) == len(set(all_syms)), f"同一只票进了多个桶: {all_syms}"
    assert res["buy_count"] == 1
    # 600001（衰减 49.9）**未持仓** → 只算 skip，不得进 sell（调出仅对持仓）
    assert {x["symbol"] for x in res["sell"]} == {"600003.SH"}
    assert res["skip_count"] == 3


def test_rules_do_not_recompute_or_create_new_score():
    """铁律 10 守卫：规则只读 qv_score，不得写出任何新总分字段。"""
    r = evaluate_position(_cand("600519.SH", "茅台", 83.3, 15673.5))
    assert "total_score" not in r and "score" not in r
    # composite_score 必须原样透传（回显用），不得被改写
    assert r["composite_score"] == 70.0


def test_sell_scan_covers_full_universe_not_just_top_n():
    """★ 止损不得被分页截断：排名第 501 名之后的持仓票也必须收到调出信号。

    背景：榜单端点的 MAX_TOP=500 会把候选截断。若调仓判定复用被截断的列表，
    一只排在 600 名、却已跌破卖出线的持仓票将**永远收不到调出信号**，
    规则静默失效且零报错。此用例守住「判定走全量、只有展示才截断」。
    """
    # 造 600 只票：前 599 只高分，最后一只（排名垫底）跌破卖出线
    items = [_cand(f"6005{i:02d}.SH", f"高分{i}", 90.0 - i * 0.01, 500.0) for i in range(599)]
    items.append(_cand("000999.SZ", "垫底破线", 30.0, 500.0))
    assert len(items) == 600

    res = apply_trading_rules(items, holdings={"000999.SZ": 20.0})
    # 判定层面必须覆盖全部 600 只，且垫底那只被判为 sell
    total = res["buy_count"] + res["sell_count"] + res["hold_count"] + res["skip_count"]
    assert total == 600, f"判定只覆盖了 {total}/600 只"
    assert "000999.SZ" in {x["symbol"] for x in res["sell"]}, (
        "排名 600 的破线持仓票没收到调出信号 —— 止损被截断了"
    )


def test_verdict_carries_full_item_fields_and_meets_buy():
    """判定结果继承完整候选字段 + meets_buy 标记（前端整表直渲染的契约）。"""
    it = _cand("600100.SH", "全字段", 85.0, 100.0)
    it["roe_pct"] = 15.0
    it["qv_components"] = {"quality_pct": 80.0}
    r = evaluate_position(it)
    assert r["action"] == "buy"
    assert r["roe_pct"] == 15.0, "原始字段必须保留（选股表直接渲染用）"
    assert r["qv_components"] == {"quality_pct": 80.0}
    assert r["meets_buy"] is True

    # 不达标 → meets_buy False
    assert evaluate_position(_cand("600101.SH", "不达标", 60.0, 100.0))["meets_buy"] is False

    # 持仓且仍达标 → hold 且 meets_buy True（选股表继续展示，徽标「持有」）
    r3 = evaluate_position(_cand("600102.SH", "持仓达标", 85.0, 100.0), entry_price=10.0)
    assert r3["action"] == "hold"
    assert r3["meets_buy"] is True

    # 持仓但已衰减（无止损）→ hold 且 meets_buy False（不进选股表）
    r4 = evaluate_position(_cand("600103.SH", "持仓衰减", 60.0, 100.0), entry_price=10.0)
    assert r4["action"] == "hold"
    assert r4["meets_buy"] is False

    # 止损触发（即使仍达标）→ sell 照样优先
    r5 = evaluate_position(_cand("600104.SH", "止损优先", 85.0, 100.0, price=7.0), entry_price=10.0)
    assert r5["action"] == "sell"


def test_single_side_gate_must_reject():
    """★ 单边门槛（只给绝对值、无分位兜底）失败时必须拒绝。

    背景（2026-09-24 生产实测）：_gate 旧实现把未指定的分位侧默认 True 再做
    `abs_ok or rel_ok`，导致 ROIC / 资产负债率 / OCF-NP(5y) / PEG / 市值
    五个门槛拒绝数恒 0 —— 负债率 91.65% 的常熟银行、75.35% 的三棵树都被放行。
    """
    # ROIC = 2.59（中原环保型）：其余全优，仅 ROIC 不达标 → 必须被拦
    rows = [_row(f"60030{i}.SH", f"低ROIC{i}", "环境治理", roe_pct=11.89,
                 gross_margin_pct=46.81, roic_pct=2.59, debt_ratio_pct=30.0,
                 ocf_np_5y=1.2, pe_ttm=8.0, pb=0.9,
                 pe_percentile=5.0, pb_percentile=5.0)
            for i in range(6)]
    passed, rejected = compute_qv(rows)
    assert not passed and len(rejected) == 6
    assert any(r.startswith("ROIC:") for r in rejected[0]["qv_reasons"]), rejected[0]["qv_reasons"]

    # 负债率 75.35（三棵树型）：无分位判据可用 → 绝对门槛失败必须拒绝
    rows = [_row(f"60040{i}.SH", f"高负债{i}", "装修建材", roe_pct=20.0,
                 gross_margin_pct=35.0, roic_pct=12.0, debt_ratio_pct=75.35,
                 ocf_np_5y=1.0, pe_ttm=10.0, pb=1.5,
                 pe_percentile=5.0, pb_percentile=5.0)
            for i in range(6)]
    passed, rejected = compute_qv(rows)
    assert not passed and len(rejected) == 6
    assert any(r.startswith("资产负债率:") for r in rejected[0]["qv_reasons"]), rejected[0]["qv_reasons"]


def test_growth_component_uses_profit_yoy():
    """★ 展示分成长维必须真吃 profit_yoy_pct，不得恒中性 50。

    背景：compute_qv 读 profit_yoy_pct，但视图读取层（_LIGHT_SQL/_from_payload）
    旧实现从未映射该字段 → growth_pct 恒 50，qv_score 被系统性压平。
    """
    rows = [_row(f"60050{i}.SH", f"成长{i}", "白酒Ⅱ", roe_pct=15.0,
                 profit_yoy_pct=25.0)
            for i in range(6)]
    passed, _ = compute_qv(rows)
    assert len(passed) == 6
    assert passed[0]["qv_components"]["growth_pct"] == 25.0, passed[0]["qv_components"]

    rows2 = [_row(f"60060{i}.SH", f"负增长{i}", "白酒Ⅱ", roe_pct=15.0,
                  profit_yoy_pct=-30.0) for i in range(6)]
    passed2, _ = compute_qv(rows2)
    assert passed2[0]["qv_components"]["growth_pct"] == 0.0  # 裁剪到 0~100


if __name__ == "__main__":
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    failed = 0
    for fn in fns:
        try:
            fn()
            print(f"PASS {fn.__name__}")
        except AssertionError as e:
            failed += 1
            print(f"FAIL {fn.__name__}: {e}")
        except Exception as e:  # noqa: BLE001
            failed += 1
            print(f"ERROR {fn.__name__}: {type(e).__name__}: {e}")
    print(f"\n{len(fns) - failed}/{len(fns)} passed")
    sys.exit(1 if failed else 0)
