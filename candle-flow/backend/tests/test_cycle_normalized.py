"""跨周期利润归一化 + 强周期估值换锚 + 存贷双高扣分的回归测试。

覆盖 2026-09-29 三改（SCORING_VERSION 2026.09.29.2）：
  ① cycle_normalized：单期顶点同比未获跨周期记录证实 → 成长性扣减
  ② engine.cycle_peak_trap_hit：低 PE + 利润处周期中高位 = 顶点假便宜
  ③ solvency：存贷双高从「周期股近乎免检」改为实质扣分

线上验证时抓到两处「单测全绿但生产静默失效」，本文件已补回归：
  · 存贷双高的 `monetary_funds` 键在 `build_financial_dataframe` 产出的
    balance_sheet 字典里**根本不存在**，而本文件的 `_bs()` fixture 自己造了
    这个键 → 生产上 cash 恒为 0、判据恒不触发，测试却通过。
  · 位置分母用算术平均时，窗口里一个结构断裂年就能把闸门打成「不判定」
    （盐湖股份 2019 年重整计提 -458.6 亿 → 七年均值 -3.9 亿 <0）→
    改用中性利润（中位数）并补盐湖真实序列回归。
"""

import pandas as pd
import pytest

from app.analysis.cycle_normalized import (
    annual_profit_series,
    check_cycle_peak_growth,
    cross_cycle_stats,
)
from app.analysis.engine import cycle_peak_trap_hit
from app.analysis.modules.growth import GrowthAnalyzer
from app.analysis.modules.solvency import SolvencyAnalyzer

YI = 1e8

# 天山铝业 002532 真实年报序列（2021–2025，归母净利）
TIANSHAN = [
    ["20211231", 38.33 * YI],
    ["20221231", 26.50 * YI],
    ["20231231", 22.05 * YI],
    ["20241231", 44.55 * YI],
    ["20251231", 48.18 * YI],
]


# ── ① annual_profit_series ───────────────────────────────────────────────

def test_annual_profit_series_only_keeps_annual_ascending():
    income_by = {
        "20230630": {"parent_netprofit": 5.0},   # 中报累计，必须剔除
        "20231231": {"parent_netprofit": 10.0},
        "20240331": {"parent_netprofit": 2.0},
        "20241231": {"parent_netprofit": 12.0},
        "20251231": {"revenue": 1.0},            # 缺归母净利 → 跳过
    }
    out = annual_profit_series(income_by)
    assert [d for d, _ in out] == ["20231231", "20241231"]
    assert [v for _, v in out] == [10.0, 12.0]


def test_annual_profit_series_empty_input():
    assert annual_profit_series(None) == []
    assert annual_profit_series({}) == []


# ── ② cross_cycle_stats ─────────────────────────────────────────────────

def test_cross_cycle_stats_needs_five_points():
    assert cross_cycle_stats(TIANSHAN[:4]) is None
    assert cross_cycle_stats([]) is None


def test_cross_cycle_stats_tianshan_values():
    st = cross_cycle_stats(TIANSHAN)
    assert st is not None
    # 2021→2025 四年复合：(48.18/38.33)^(1/4)-1 ≈ 5.87%
    assert st["span_years"] == 4
    assert st["cagr_pct"] == pytest.approx(5.87, abs=0.02)
    # 位置分母 = 中性利润（5 年中位 38.33 亿，非算术平均 35.92 亿）
    assert st["median"] == pytest.approx(38.33 * YI, rel=1e-6)
    assert st["neutral"] == pytest.approx(38.33 * YI, rel=1e-6)
    assert st["position_ratio"] == pytest.approx(48.18 / 38.33, abs=0.01)
    assert st["n"] == 5


# ── ②-b 中性利润对「结构断裂年」的稳健性（线上实测靶点：盐湖股份）────────

# 盐湖股份 000792 真实年报序列（2019–2025，归母净利）：
# 2019 年破产重整处置盐湖镁业/海纳化工资产包，计提约 417 亿减值，当年
# 归母净利 -458.6 亿（A 股当年「亏损王」）—— 真实披露值，不是数据异常。
YANHU = [
    ["20191231", -45859976778.09],
    ["20201231", 2039507350.78],
    ["20211231", 4478386639.86],
    ["20221231", 15568396475.75],
    ["20231231", 7913614646.24],
    ["20241231", 4663116528.38],
    ["20251231", 8475535262.69],
]


def test_cross_cycle_stats_median_robust_to_restructuring_year():
    """七年算术平均被 2019 重整巨亏打成负数，中位数仍可用 → 位置可判定。"""
    st = cross_cycle_stats(YANHU)
    assert st is not None
    assert st["avg"] < 0                      # 算术平均被单点摧毁
    assert st["median"] == pytest.approx(4663116528.38, rel=1e-6)
    assert st["neutral"] == pytest.approx(4663116528.38, rel=1e-6)
    assert st["position_ratio"] == pytest.approx(8475535262.69 / 4663116528.38, abs=0.01)
    # 基期为负 → 复合增速不可得（不得编造成 0%）
    assert st["cagr_pct"] is None


def test_gate_triggers_on_yanhu_despite_restructuring_year():
    """盐湖 2026H1 +137.9% 未被跨周期证实 → 必须触发（改前因均值<0 静默不判定）。"""
    out = check_cycle_peak_growth(profit_yoy=137.88, series=YANHU)
    assert out["triggered"] is True
    assert out["deduction"] == pytest.approx(24.8, abs=0.1)
    assert out["cagr_available"] is False
    assert "中性利润" in out["reason"]
    assert "复合增速因基期非正不可得" in out["reason"]


# ── ③ check_cycle_peak_growth ───────────────────────────────────────────

def test_gate_skips_when_window_insufficient():
    out = check_cycle_peak_growth(profit_yoy=120.0, series=TIANSHAN[:3])
    assert out["triggered"] is False
    assert out["deduction"] == 0.0
    assert "不足" in out["reason"]


def test_gate_triggers_on_peak_yoy():
    """天山铝业：单期 +100.44% 未被跨周期 5.87% 证实 → 扣减。"""
    out = check_cycle_peak_growth(profit_yoy=100.44, series=TIANSHAN)
    assert out["triggered"] is True
    assert 10.0 < out["deduction"] <= 25.0
    assert "跨周期" in out["reason"]
    assert out["sustained_yoy"] == pytest.approx(5.87, abs=0.02)


def test_gate_skips_when_profit_below_neutral_profit():
    """利润仍在中性利润之下 = 底部修复，不扣减（保住既有复苏叙事）。"""
    trough = [
        ["20211231", 100.0 * YI],
        ["20221231", 90.0 * YI],
        ["20231231", 40.0 * YI],
        ["20241231", 20.0 * YI],
        ["20251231", 25.0 * YI],
    ]
    out = check_cycle_peak_growth(profit_yoy=150.0, series=trough)
    assert out["triggered"] is False
    assert "中低位" in out["reason"]


def test_gate_skips_when_yoy_corroborated_by_cycle():
    """跨周期复合 30% 的成长股，单期 45% 与之相称 → 不扣减。"""
    grower = [
        ["20211231", 10.0 * YI],
        ["20221231", 13.0 * YI],
        ["20231231", 17.0 * YI],
        ["20241231", 22.0 * YI],
        ["20251231", 28.0 * YI],
    ]
    out = check_cycle_peak_growth(profit_yoy=45.0, series=grower)
    assert out["triggered"] is False
    assert out["deduction"] == 0.0


def test_gate_missing_yoy_not_judged():
    out = check_cycle_peak_growth(profit_yoy=None, series=TIANSHAN)
    assert out["triggered"] is False
    assert out["deduction"] == 0.0


# ── ④ growth 模块集成：闸门真的进分且归因正确 ────────────────────────────

def _growth_frame() -> pd.DataFrame:
    rows = [
        {"revenue": 330e8, "net_profit": 38.33 * YI},
        {"revenue": 310e8, "net_profit": 26.50 * YI},
        {"revenue": 290e8, "net_profit": 22.05 * YI},
        {"revenue": 281e8, "net_profit": 44.55 * YI},
        {"revenue": 295e8, "net_profit": 48.18 * YI},
    ]
    return pd.DataFrame(
        rows,
        index=["20211231", "20221231", "20231231", "20241231", "20251231"],
    )


def test_growth_module_applies_cycle_peak_gate():
    fd = _growth_frame()
    base = GrowthAnalyzer().analyze(
        fd,
        industry="工业金属",
        revenue_yoy=14.2,
        profit_yoy=100.44,
        annual_dates=list(fd.index),
        latest_report="20260630",
    )
    gated = GrowthAnalyzer().analyze(
        fd,
        industry="工业金属",
        revenue_yoy=14.2,
        profit_yoy=100.44,
        annual_dates=list(fd.index),
        latest_report="20260630",
        cycle_annual_profit=TIANSHAN,
    )
    assert gated.score < base.score
    assert gated.metadata["cycle_peak_penalty"] > 0
    assert gated.metadata["cycle_growth_check"]["triggered"] is True
    assert any("跨周期" in w for w in gated.warnings)


def test_growth_metadata_exposes_cagr3y():
    """engine 读 metadata['profit_cagr_3y'] 做 classify_growth_stock，必须存在。"""
    fd = _growth_frame()
    res = GrowthAnalyzer().analyze(
        fd, industry="工业金属", revenue_yoy=14.2, profit_yoy=100.44
    )
    assert "profit_cagr_3y" in res.metadata
    assert res.metadata["profit_cagr_3y"] is not None


# ── ⑤ cycle_peak_trap_hit ───────────────────────────────────────────────

def test_cycle_peak_trap_low_pe_at_cycle_high():
    assert cycle_peak_trap_hit(12.0, 9.5, 1.34) is True


def test_cycle_peak_trap_not_at_cycle_high():
    assert cycle_peak_trap_hit(12.0, 9.5, 0.70) is False


def test_cycle_peak_trap_needs_position():
    assert cycle_peak_trap_hit(12.0, 9.5, None) is False


def test_cycle_peak_trap_expensive_pe_not_cheap():
    # 既非低分位、绝对 PE 也不便宜 → 不属「低 PE 幻觉」
    assert cycle_peak_trap_hit(85.0, 42.0, 1.5) is False


# ── ⑥ solvency 存贷双高 ─────────────────────────────────────────────────

def _bs(cash: float, ibd: float, *, explicit: bool = True, short: float = 0.0) -> dict:
    return {
        "report_date": "20260630",
        "total_assets": 500e8,
        "total_liabilities": 190e8,
        "monetary_funds": cash,
        "short_term_borrowings": short,
        "interest_bearing_debt": ibd,
        "interest_bearing_explicit": explicit,
        "operating_liabilities": 60e8,
        "interest_bearing_ratio": 19.37,
    }


def test_solvency_dual_high_penalises():
    clean = SolvencyAnalyzer().analyze(
        pd.DataFrame(), debt_ratio=38.0, balance_sheet=_bs(20e8, 2e8),
        short_term_borrowings=2e8, industry="工业金属",
    )
    dual = SolvencyAnalyzer().analyze(
        pd.DataFrame(), debt_ratio=38.0, balance_sheet=_bs(120e8, 90e8),
        short_term_borrowings=90e8, industry="工业金属",
    )
    assert dual.score < clean.score
    names = [i.name for i in dual.indicators]
    assert "存贷双高" in names
    assert dual.metadata["deposit_loan_dual_high"] is True
    assert dual.metadata["deposit_loan_dual_ratio"] == pytest.approx(0.75, abs=0.01)
    assert dual.metadata["restricted_cash_observable"] is False
    assert any("存贷双高" in w for w in dual.warnings)


def test_solvency_dual_high_uses_interest_bearing_debt_not_short_debt():
    """天山铝业形态：有息负债 100.3 亿、短债仅 23.3 亿、现金 120 亿。

    只拿短债当分子 → 23.3/120 = 0.19 < 0.40 不触发（这正是改前线上的失效路径）；
    拿有息负债 → 0.84 ≥ 0.40 触发。用户点名的「天山铝业存贷双高」由此才成立。
    """
    res = SolvencyAnalyzer().analyze(
        pd.DataFrame(), debt_ratio=38.0,
        balance_sheet=_bs(120e8, 100.27e8, short=23.32e8),
        short_term_borrowings=23.32e8, industry="工业金属",
    )
    assert res.metadata["deposit_loan_dual_high"] is True
    assert res.metadata["deposit_loan_dual_ratio"] == pytest.approx(0.84, abs=0.01)
    assert "存贷双高" in [i.name for i in res.indicators]


def test_solvency_dual_high_not_triggered_below_ratio():
    res = SolvencyAnalyzer().analyze(
        pd.DataFrame(), debt_ratio=38.0, balance_sheet=_bs(120e8, 30e8),
        short_term_borrowings=30e8, industry="工业金属",
    )
    assert "存贷双高" not in [i.name for i in res.indicators]
    assert res.metadata["deposit_loan_dual_high"] is False


def test_solvency_dual_high_unjudged_when_cash_missing():
    """balance_sheet 缺 monetary_funds（改前生产链路的真实状态）→ 三态 None + 留痕。

    这是改前那处静默失效的回归守卫：缺键时绝不能返回 False 被读成「无问题」。
    """
    bs = _bs(120e8, 100e8)
    bs.pop("monetary_funds")
    res = SolvencyAnalyzer().analyze(
        pd.DataFrame(), debt_ratio=38.0, balance_sheet=bs,
        short_term_borrowings=23e8, industry="工业金属",
    )
    assert res.metadata["deposit_loan_dual_high"] is None
    assert "不可得" in (res.metadata["deposit_loan_dual_note"] or "")
    assert "存贷双高" not in [i.name for i in res.indicators]


def test_solvency_dual_high_unjudged_when_ibd_estimated():
    """有息负债为残差估计值（interest_bearing_explicit=False）→ 不判定 + 留痕。"""
    res = SolvencyAnalyzer().analyze(
        pd.DataFrame(), debt_ratio=38.0,
        balance_sheet=_bs(120e8, 90e8, explicit=False),
        short_term_borrowings=23e8, industry="工业金属",
    )
    assert res.metadata["deposit_loan_dual_high"] is None
    assert "估计值" in (res.metadata["deposit_loan_dual_note"] or "")
    assert "存贷双高" not in [i.name for i in res.indicators]
