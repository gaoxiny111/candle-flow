"""跨周期利润归一化 —— 抑制「单期同比站在周期/爆款顶点」被当成可持续成长。

背景（2026-09-29 用户口径）：
成长模块的「前瞻成长性」以最新报告期归母同比为 60% 权重的代理增速。周期股在
景气顶点时单期同比动辄 +100%~+180%（天山铝业 +100.4%、盐湖股份 +137.9%、
巨人网络 +176.0%），这与「5~7 年跨周期复合增速」往往相差一个数量级。直接拿
单期同比打分，等于奖励「恰好站在周期/爆款顶点」的公司。

本模块只做一件可复算的事：用**年度归母净利序列**（东财利润表 1231 口径，
单一来源）算出跨周期复合增速与当前所处位置，判定单期高增是否被跨周期记录
证实；未被证实、且利润已处周期中高位（不在底部）时，按未获证实的超额增速
折算成长性扣减。

为什么「位置」必须进判据：
  周期底部复苏（利润仍在跨周期中性利润之下）时的单期高增是合理的边际改善，
  不能与顶点高增同等对待。故 position_ratio < 1.0 一律不扣减，把「超卖/底部
  修复」的既有逻辑完整保留（避免把已固化的修复叙事打回）。位置分母用
  **中性利润（窗口中位数）** 而非算术平均，理由见 ``cross_cycle_stats``。

口径与数据边界：
  · 序列取 `financials.fetch_income_by_period` 的 `parent_netprofit`（归母口径），
    与 yjbb 快照的 `net_profit` 同源同口径（同一 1231 报告期），不做跨源拼接。
  · 年度点数 < CYCLE_MIN_ANNUAL_POINTS 时返回 None —— 缺失一律留痕不判定，
    绝不拿不完整窗口拼出貌似完整的复合增速。
"""

from __future__ import annotations

from typing import Any, Iterable, Sequence

# 跨周期窗口
CYCLE_MIN_ANNUAL_POINTS = 5   # 少于 5 个完整年度不做跨周期判定
CYCLE_WINDOW_MAX = 7          # 最多回看 7 个年度（对应 5~7 年跨周期）

# 单期高增「未获跨周期证实」的判据
CYCLE_PEAK_MIN_YOY = 40.0     # 触发线：单期归母同比 ≥ 40%
CYCLE_PEAK_MULT = 2.0         # 且 单期同比 > 跨周期增速 × 2
CYCLE_PEAK_FLOOR = 20.0       # 且 绝对超出 ≥ 20pct（双条件防小基数误触发）
CYCLE_PEAK_POSITION_MIN = 1.0 # 且 利润处跨周期中性利润及以上（不在底部）

# 扣减：未获证实的超额增速按比例折算，设硬上限（防击穿）
CYCLE_PEAK_DEDUCT_RATE = 0.18
CYCLE_PEAK_DEDUCT_MAX = 25.0


def annual_profit_series(
    income_by: dict[str, dict[str, Any]] | None,
    *,
    max_years: int = CYCLE_WINDOW_MAX,
) -> list[tuple[str, float]]:
    """东财利润表「报告期 → 指标」字典 → 年度归母净利序列（仅 1231，升序）。

    只认 1231（年报累计口径）：0331/0630/0930 是当年累计值，混入会把
    「半年利润」当成年度利润。缺 parent_netprofit 的报告期直接跳过。
    """
    if not income_by:
        return []
    out: list[tuple[str, float]] = []
    for ymd in sorted(str(k) for k in income_by):
        if len(ymd) != 8 or not ymd.endswith("1231"):
            continue
        row = income_by.get(ymd) or {}
        v = row.get("parent_netprofit")
        if v is None:
            continue
        try:
            out.append((ymd, float(v)))
        except (TypeError, ValueError):
            continue
    return out[-max_years:] if max_years > 0 else out


def _median(vals: list[float]) -> float:
    """中位数（偶数个取中间两值均值）。"""
    s = sorted(vals)
    n = len(s)
    mid = n // 2
    return s[mid] if n % 2 else (s[mid - 1] + s[mid]) / 2.0


def cross_cycle_stats(series: Sequence[Iterable] | None) -> dict[str, Any] | None:
    """跨周期统计：复合增速 / 中性利润 / 当前所处位置。

    返回 None 表示窗口不足（< CYCLE_MIN_ANNUAL_POINTS 个年度），调用方按
    「不判定」处理，不得赋中性值蒙混。

    **中性利润取中位数而非算术平均**：窗口里出现一次结构断裂年（破产重整的
    一次性巨额计提、资产处置）时，算术平均会被单点摧毁。实测盐湖股份
    2019 年归母净利 -458.6 亿（重整处置盐湖镁业/海纳化工资产包，计提约
    417 亿减值，当年 A 股「亏损王」），使 2019–2025 七年平均变成 **-3.9 亿**
    （<0）→ 位置无法判定 → 整条顶点闸门静默失效，恰好漏掉用户点名的
    「盐湖 2026H1 净利同比 +138%」这一例。中位数对单点异常稳健，语义即
    「典型年份的利润水平」，比算术平均更贴近「中性利润」。两个值都回传，
    展示与判据同口径；`neutral` 是判据实际使用的那个。
    """
    pts: list[tuple[str, float]] = []
    for item in series or []:
        try:
            d, v = item  # type: ignore[misc]
            pts.append((str(d), float(v)))
        except (TypeError, ValueError):
            continue
    pts.sort(key=lambda x: x[0])
    if len(pts) < CYCLE_MIN_ANNUAL_POINTS:
        return None

    vals = [v for _, v in pts]
    base, latest = pts[0][1], pts[-1][1]
    span = len(pts) - 1
    avg = sum(vals) / len(vals)
    median = _median(vals)

    cagr: float | None = None
    if span > 0 and base > 0 and latest > 0:
        cagr = round(((latest / base) ** (1.0 / span) - 1.0) * 100.0, 2)

    # 中性利润：中位优先（稳健），中位不可用时退算术平均；都非正 → 不判定。
    neutral = median if median > 0 else (avg if avg > 0 else None)

    position: float | None = None
    if neutral is not None and neutral > 0:
        position = round(latest / neutral, 3)

    return {
        "n": len(pts),
        "base_period": pts[0][0],
        "latest_period": pts[-1][0],
        "span_years": span,
        "base": round(base, 2),
        "latest": round(latest, 2),
        "avg": round(avg, 2),
        "median": round(median, 2),
        "neutral": round(neutral, 2) if neutral is not None else None,
        "cagr_pct": cagr,
        "position_ratio": position,
        "series": [[d, round(v, 2)] for d, v in pts],
    }


def check_cycle_peak_growth(
    *,
    profit_yoy: float | None,
    series: Sequence[Iterable] | None,
) -> dict[str, Any]:
    """单期高增是否被跨周期记录证实；未证实的超额增速 → 成长性扣减。

    参数
    ----
    profit_yoy : 最新报告期归母净利同比（百分数，如 100.44）
    series : 年度归母净利序列 ``[(YYYYMMDD, 元), ...]``

    返回
    ----
    ``{"triggered", "deduction", "reason", "stats", "sustained_yoy",
       "excess_yoy", "position_ratio"}``

    缺数据一律放行（``triggered=False, deduction=0``）；只有序列 ≥5 个年度、
    同比 ≥ CYCLE_PEAK_MIN_YOY、利润不在底部且单期同比远超跨周期增速时才扣减。
    """
    out: dict[str, Any] = {
        "triggered": False,
        "deduction": 0.0,
        "reason": "",
        "stats": None,
        "sustained_yoy": None,
        "excess_yoy": None,
        "position_ratio": None,
        "cagr_available": False,
    }
    stats = cross_cycle_stats(series)
    if stats is None:
        out["reason"] = "跨周期年度利润不足（<5期），不判定"
        return out
    out["stats"] = stats

    if profit_yoy is None:
        out["reason"] = "缺少最新报告期同比，不判定"
        return out
    try:
        yoy = float(profit_yoy)
    except (TypeError, ValueError):
        out["reason"] = "最新报告期同比非数值，不判定"
        return out

    cagr = stats.get("cagr_pct")
    position = stats.get("position_ratio")
    out["position_ratio"] = position
    # 基期非正（如重整巨亏年）时复合增速不可得：阈值运算保守按 0% 计，但必须
    # 把「不可得」如实回传，避免把 0% 当成真实观测到的零增长（展示=判据同口径）。
    out["cagr_available"] = cagr is not None
    sustained = max(float(cagr), 0.0) if cagr is not None else 0.0
    out["sustained_yoy"] = sustained

    period = f"{str(stats['base_period'])[:4]}–{str(stats['latest_period'])[:4]}"
    neutral_yi = float(stats["neutral"]) / 1e8
    latest_yi = stats["latest"] / 1e8
    cagr_note = (
        ""
        if cagr is not None
        else "（跨周期复合增速因基期非正不可得，阈值运算保守按 0% 计）"
    )

    if yoy < CYCLE_PEAK_MIN_YOY:
        out["reason"] = ""
        return out

    # 位置闸门：利润仍在跨周期中性利润之下 → 属底部/中低位修复，不扣减。
    if position is None:
        out["reason"] = "跨周期中性利润不可得（中位与均值均非正），不判定"
        return out
    if position < CYCLE_PEAK_POSITION_MIN:
        out["reason"] = (
            f"跨周期({period})位置 {position:.2f}× 处中低位，"
            f"单期同比 {yoy:.1f}% 视为底部修复，不扣减"
        )
        return out

    # 证实性闸门：单期同比须显著超出「跨周期增速可支撑的水平」才算未被证实。
    if yoy <= sustained * CYCLE_PEAK_MULT + CYCLE_PEAK_FLOOR:
        out["reason"] = (
            f"单期同比 {yoy:.1f}% 与跨周期({period})增速 {sustained:.1f}% 相称，不扣减"
        )
        return out

    excess = round(yoy - sustained, 2)
    deduction = min(CYCLE_PEAK_DEDUCT_MAX, max(0.0, excess * CYCLE_PEAK_DEDUCT_RATE))
    if deduction <= 0:
        out["reason"] = ""
        return out

    out["triggered"] = True
    out["excess_yoy"] = excess
    out["deduction"] = round(deduction, 1)
    series_txt = " / ".join(f"{float(v) / 1e8:.1f}" for _, v in stats["series"])
    out["reason"] = (
        f"周期/爆款顶点增速未获跨周期证实：跨周期({period})归母净利 {series_txt} 亿，"
        f"复合增速 {sustained:.1f}%{cagr_note}；单期同比 {yoy:.1f}% 超出跨周期可支撑水平 "
        f"{excess:.1f}pct，且利润处周期中高位"
        f"（{latest_yi:.1f}亿 vs 中性利润 {neutral_yi:.1f}亿，{position:.2f}×），"
        f"成长性扣减 {deduction:.0f} 分"
    )
    return out
