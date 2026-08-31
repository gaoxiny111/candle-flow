"""Multi-track fundamental rules: growth / cyclical / value."""

from __future__ import annotations

import math
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from app.services.fundamental_screen import CheckItem, ScreenRow, ScreenThresholds

HARD_KEYS_GROWTH = frozenset({"roe_streak", "growth", "ocf"})
HARD_KEYS_CYCLICAL = frozenset({"roe_avg", "no_consec_loss", "ocf", "debt"})
HARD_KEYS_BY_TRACK: dict[str, frozenset[str]] = {
    "growth": HARD_KEYS_GROWTH,
    "cyclical": HARD_KEYS_CYCLICAL,
    "value": HARD_KEYS_CYCLICAL,
}

VALUE_INDUSTRY_KEYS: tuple[str, ...] = (
    "银行",
    "保险",
    "证券",
    "多元金融",
    "公用事业",
    "电力",
    "水务",
    "燃气",
    "热力",
    "高速公路",
    "港口",
    "机场",
    "铁路",
    "航运港口",
)
CYCLICAL_INDUSTRY_KEYS: tuple[str, ...] = (
    "煤炭",
    "钢铁",
    "有色",
    "化工",
    "石油",
    "石化",
    "采掘",
    "航运",
    "建材",
    "水泥",
    "稀土",
    "能源",
    "铜",
    "铝",
    "铅锌",
    "黄金",
    "白银",
    "工业金属",
    "化学原料",
)

TRACK_LABEL = {"growth": "成长轨", "cyclical": "周期轨", "value": "价值轨"}


def sector_classify(industry: str, *, name: str = "", symbol: str = "") -> str:
    """Classify stock into growth / cyclical / value by industry (name fallback)."""
    del symbol  # reserved for future symbol-based overrides
    blob = f"{industry or ''} {name or ''}"
    for k in VALUE_INDUSTRY_KEYS:
        if k in blob:
            return "value"
    for k in CYCLICAL_INDUSTRY_KEYS:
        if k in blob:
            return "cyclical"
    if any(x in blob for x in ("伊泰", "神华", "中煤", "陕煤", "焦煤")):
        return "cyclical"
    return "growth"


def hard_keys_for(track: str) -> frozenset[str]:
    return HARD_KEYS_BY_TRACK.get(track or "growth", HARD_KEYS_GROWTH)


def _mean(vals: list[float]) -> float | None:
    if not vals:
        return None
    return sum(vals) / len(vals)


def _cv(vals: list[float]) -> float | None:
    if len(vals) < 2:
        return None
    m = sum(vals) / len(vals)
    if abs(m) < 1e-9:
        return None
    var = sum((x - m) ** 2 for x in vals) / len(vals)
    return (var**0.5) / abs(m)


def _clamp01(x: float) -> float:
    return max(0.0, min(1.0, x))


def roe_series(
    hist_roe: dict[str, dict[str, float]], dates: list[str], sym: str, n: int
) -> list[float]:
    out: list[float] = []
    for d in dates[:n]:
        r = hist_roe.get(d, {}).get(sym)
        if r is not None:
            out.append(r)
    return out


def profit_series(
    hist_profit: dict[str, dict[str, float]], dates: list[str], sym: str, n: int
) -> list[float | None]:
    return [hist_profit.get(d, {}).get(sym) for d in dates[:n]]


def no_consec_loss(profits: list[float | None]) -> tuple[bool | None, int, str]:
    """True if no two consecutive years with net profit < 0 among known years."""
    loss_years = 0
    streak = 0
    max_streak = 0
    known = 0
    for p in profits:
        if p is None:
            streak = 0
            continue
        known += 1
        if p < 0:
            loss_years += 1
            streak += 1
            max_streak = max(max_streak, streak)
        else:
            streak = 0
    if known == 0:
        return None, 0, "近三年净利润数据不足"
    ok = max_streak < 2
    return ok, loss_years, f"亏损年数 {loss_years}/{known}；最长连亏 {max_streak} 年"


def score_growth(row: ScreenRow, th: ScreenThresholds) -> float:
    s = 0.0
    if row.roe is not None:
        s += min(row.roe, 40) * 0.8
    if row.roe_years_ok >= th.roe_years:
        s += 15
    elif row.roe_years_ok == th.roe_years - 1:
        s += 8
    growth = max(row.revenue_yoy or -999, row.profit_yoy or -999)
    if growth > 0:
        s += min(growth, 50) * 0.5
    if row.ocf_ps is not None and row.ocf_ps > 0:
        s += 8
    if row.debt_ratio is not None and row.debt_ratio < th.debt_max:
        s += 6
    if row.pe_percentile is not None and row.pe_percentile < th.pe_pct_max:
        s += max(0, th.pe_pct_max - row.pe_percentile) * 0.35
    if row.pb_percentile is not None and row.pb_percentile < th.pb_pct_max:
        s += max(0, th.pb_pct_max - row.pb_percentile) * 0.25
    if row.peg is not None and 0 < row.peg <= th.peg_max:
        s += 10
    return round(s, 2)


def score_value_cyclical(row: ScreenRow, th: ScreenThresholds) -> float:
    """
    Weighted ~0–100 score for cyclical/value:
    0.20 ROE + 0.15 OCF + 0.15 dividend + 0.15 valuation
    + 0.10 debt + 0.10 stability + 0.10 continuity + 0.05 size
    """
    roe_ref = row.roe_avg if row.roe_avg is not None else row.roe
    roe_part = _clamp01((roe_ref or 0) / 20.0) * 100

    if row.ocf_ps is None:
        ocf_part = 40.0
    elif row.ocf_ps > 0:
        ocf_part = min(100.0, 60 + min(row.ocf_ps, 8) * 5)
    else:
        ocf_part = 0.0

    if row.dividend_yield is None:
        dy_part = 45.0
    else:
        dy_part = _clamp01(row.dividend_yield / 8.0) * 100

    pe_s = 100 - row.pe_percentile if row.pe_percentile is not None else 50
    pb_s = 100 - row.pb_percentile if row.pb_percentile is not None else 50
    val_part = (pe_s + pb_s) / 2

    if row.debt_ratio is None:
        debt_part = 50.0
    elif row.debt_ratio < th.debt_max:
        debt_part = 100 - max(0.0, row.debt_ratio) * 0.5
    else:
        debt_part = max(0.0, 40 - (row.debt_ratio - th.debt_max))

    if row.profit_cv is None:
        stab_part = 50.0
    else:
        stab_part = 100 / (1 + row.profit_cv)

    cont_part = _clamp01(row.profit_positive_years / 5.0) * 100

    if row.revenue is None:
        size_part = 50.0
    else:
        size_part = _clamp01((math.log10(max(row.revenue, 1)) - 7) / 5) * 100

    s = (
        0.20 * roe_part
        + 0.15 * ocf_part
        + 0.15 * dy_part
        + 0.15 * val_part
        + 0.10 * debt_part
        + 0.10 * stab_part
        + 0.10 * cont_part
        + 0.05 * size_part
    )
    if row.dividend_yield is not None and row.dividend_yield >= th.dividend_bonus_min:
        s = min(100.0, s + 5)
    return round(s, 2)


def score_row(row: ScreenRow, th: ScreenThresholds) -> float:
    if row.track in ("cyclical", "value"):
        return score_value_cyclical(row, th)
    return score_growth(row, th)


def _soft_checks(row: ScreenRow, th: ScreenThresholds, *, include_debt: bool = True) -> list[CheckItem]:
    from app.services.fundamental_screen import CheckItem

    checks: list[CheckItem] = []
    if include_debt:
        debt_ok = row.debt_ratio is None or row.debt_ratio < th.debt_max
        checks.append(
            CheckItem(
                "debt",
                f"资产负债率＜{th.debt_max}%",
                debt_ok if row.debt_ratio is not None else False,
                f"{row.debt_ratio:.1f}%" if row.debt_ratio is not None else "未取到负债率（软条件）",
            )
        )
    pe_ok = row.pe_percentile is not None and row.pe_percentile < th.pe_pct_max
    checks.append(
        CheckItem(
            "pe_pct",
            f"PE历史分位＜{th.pe_pct_max}%",
            pe_ok,
            f"{row.pe_percentile:.1f}%" if row.pe_percentile is not None else "估值分位加载中/缺失",
        )
    )
    pb_ok = row.pb_percentile is not None and row.pb_percentile < th.pb_pct_max
    checks.append(
        CheckItem(
            "pb_pct",
            f"PB历史分位＜{th.pb_pct_max}%",
            pb_ok,
            f"{row.pb_percentile:.1f}%" if row.pb_percentile is not None else "估值分位加载中/缺失",
        )
    )
    peg_ok = row.peg is not None and 0 < row.peg <= th.peg_max
    checks.append(
        CheckItem(
            "peg",
            f"PEG＜{th.peg_max}",
            peg_ok,
            f"PEG {row.peg}" if row.peg is not None else "无法计算（需正增长与PE）",
        )
    )
    checks.append(
        CheckItem("integrity", "管理层诚信（人工）", True, "系统无法自动核验，入池后请人工复核")
    )
    return checks


def build_checks_growth(row: ScreenRow, th: ScreenThresholds) -> list[CheckItem]:
    from app.services.fundamental_screen import CheckItem

    checks: list[CheckItem] = [
        CheckItem(
            "roe_streak",
            f"ROE≥{th.roe_min}% 连续{th.roe_years}年",
            row.roe_years_ok >= th.roe_years,
            f"近{row.roe_years_ok}年达标" + (f"；最新 {row.roe:.1f}%" if row.roe is not None else ""),
        )
    ]
    growth_ok = (row.revenue_yoy is not None and row.revenue_yoy >= th.growth_min) or (
        row.profit_yoy is not None and row.profit_yoy >= th.growth_min
    )
    checks.append(
        CheckItem(
            "growth",
            f"营收或净利增速≥{th.growth_min}%",
            growth_ok,
            f"营收 {row.revenue_yoy}% / 净利 {row.profit_yoy}%"
            if row.revenue_yoy is not None or row.profit_yoy is not None
            else "无增速数据",
        )
    )
    ocf_ok = (not th.require_ocf_positive) or (row.ocf_ps is not None and row.ocf_ps > 0)
    checks.append(
        CheckItem(
            "ocf",
            "经营现金流（每股）为正",
            ocf_ok,
            f"每股经营现金流 {row.ocf_ps}" if row.ocf_ps is not None else "无数据",
        )
    )
    checks.extend(_soft_checks(row, th, include_debt=True))
    checks.append(
        CheckItem("dividend", "股息率＞行业均值（人工）", True, "暂无行业股息均值数据，请人工对比")
    )
    return checks


def build_checks_cyclical(row: ScreenRow, th: ScreenThresholds) -> list[CheckItem]:
    from app.services.fundamental_screen import CheckItem

    roe_ok = row.roe_avg is not None and row.roe_avg >= th.cyclical_roe_avg_min
    checks: list[CheckItem] = [
        CheckItem(
            "roe_avg",
            f"ROE近{th.cyclical_roe_years}年均值≥{th.cyclical_roe_avg_min}%",
            roe_ok,
            f"均值 {row.roe_avg:.1f}%" if row.roe_avg is not None else "ROE 序列不足",
        )
    ]
    loss_ok = bool(row.no_consec_loss)
    checks.append(
        CheckItem(
            "no_consec_loss",
            "近3年未连续亏损",
            loss_ok if row.no_consec_loss is not None else False,
            f"亏损年 {row.loss_years}" if row.no_consec_loss is not None else "净利润数据不足",
        )
    )
    ocf_ok = row.ocf_ps is not None and row.ocf_ps > 0
    checks.append(
        CheckItem(
            "ocf",
            "经营现金流（每股）为正",
            ocf_ok,
            f"每股经营现金流 {row.ocf_ps}" if row.ocf_ps is not None else "无数据",
        )
    )
    debt_ok = row.debt_ratio is not None and row.debt_ratio < th.debt_max
    checks.append(
        CheckItem(
            "debt",
            f"资产负债率＜{th.debt_max}%",
            debt_ok,
            f"{row.debt_ratio:.1f}%" if row.debt_ratio is not None else "未取到负债率",
        )
    )
    dy_ok = row.dividend_yield is not None and row.dividend_yield >= th.dividend_bonus_min
    checks.append(
        CheckItem(
            "dividend",
            f"股息率≥{th.dividend_bonus_min}%（加分）",
            dy_ok if row.dividend_yield is not None else True,
            f"{row.dividend_yield:.2f}%" if row.dividend_yield is not None else "暂无股息率数据",
        )
    )
    checks.extend(_soft_checks(row, th, include_debt=False))
    return checks


def build_checks(row: ScreenRow, th: ScreenThresholds) -> list[CheckItem]:
    if row.track in ("cyclical", "value"):
        return build_checks_cyclical(row, th)
    return build_checks_growth(row, th)


def verdict(row: ScreenRow) -> tuple[str, str]:
    if row.roe is None and row.roe_avg is None and row.revenue_yoy is None and row.profit_yoy is None:
        return "无财报数据", "na"
    hard_ok = row.passed_hard
    strong_cut = 55 if row.track in ("cyclical", "value") else 40
    mid_cut = 35 if row.track in ("cyclical", "value") else 25
    tag = TRACK_LABEL.get(row.track, "")
    suffix = f"（{tag}）" if tag else ""
    if hard_ok and row.score >= strong_cut:
        return f"基本面偏强{suffix}", "strong"
    if hard_ok or row.score >= mid_cut:
        return f"基本面一般{suffix}", "mid"
    return f"基本面偏弱{suffix}", "weak"


def enrich_track_metrics(
    row: ScreenRow,
    *,
    th: ScreenThresholds,
    hist_roe: dict[str, dict[str, float]],
    hist_profit: dict[str, dict[str, float]],
    dates: list[str],
) -> None:
    """Fill track + history metrics, then checks and score."""
    row.track = sector_classify(row.industry, name=row.name, symbol=row.symbol)
    n = th.cyclical_roe_years
    roes = roe_series(hist_roe, dates, row.symbol, n)
    m = _mean(roes)
    row.roe_avg = round(m, 2) if m is not None else None
    profits = profit_series(hist_profit, dates, row.symbol, max(n, 5))
    ok, loss_n, _ = no_consec_loss(profits[:n])
    row.no_consec_loss = ok
    row.loss_years = loss_n
    known = [p for p in profits if p is not None]
    row.profit_cv = _cv(known) if len(known) >= 2 else None
    row.profit_positive_years = sum(1 for p in known if p > 0)
    row.checks = build_checks(row, th)
    row.score = score_row(row, th)


def metrics_line(row: ScreenRow) -> str:
    parts: list[str] = []
    tag = TRACK_LABEL.get(row.track)
    if tag:
        parts.append(tag)
    if row.track in ("cyclical", "value") and row.roe_avg is not None:
        parts.append(f"ROE均 {row.roe_avg:.1f}%")
    elif row.roe is not None:
        parts.append(f"ROE {row.roe:.1f}%")
        if row.roe_years_ok:
            parts.append(f"连续{row.roe_years_ok}年")
    if row.revenue_yoy is not None:
        sign = "+" if row.revenue_yoy > 0 else ""
        parts.append(f"营收{sign}{row.revenue_yoy:.1f}%")
    if row.profit_yoy is not None:
        sign = "+" if row.profit_yoy > 0 else ""
        parts.append(f"净利{sign}{row.profit_yoy:.1f}%")
    if row.debt_ratio is not None:
        parts.append(f"负债{row.debt_ratio:.0f}%")
    if row.dividend_yield is not None:
        parts.append(f"股息{row.dividend_yield:.1f}%")
    if row.peg is not None and row.track == "growth":
        parts.append(f"PEG {row.peg:.2f}")
    return " · ".join(parts) if parts else "—"
