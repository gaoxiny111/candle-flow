"""Layer-1 strategic fundamental screen → 10–20 stock candidate pool."""

from __future__ import annotations

import json
import logging
import uuid
from dataclasses import dataclass, field
from datetime import date
from typing import Any, Optional

import pandas as pd
from sqlalchemy.orm import Session

from app.models.fundamental import FundamentalCandidate
from app.services.valuation import get_valuations
from app.utils.symbol import SymbolError, is_etf_symbol, is_index_symbol, normalize_symbol

logger = logging.getLogger(__name__)

# Theme → industry name keywords (所处行业 from Eastmoney yjbb)
THEME_KEYWORDS: dict[str, tuple[str, ...]] = {
    "新能源": ("光伏", "电池", "锂电", "储能", "风电", "新能源", "电力设备", "电网"),
    "AI": ("软件", "计算机", "半导体", "芯片", "电子", "通信", "互联网", "消费电子", "通信设备"),
    "高端制造": ("机械", "军工", "航空", "航天", "设备", "自动化", "机器人", "轨交"),
    "消费": ("白酒", "食品", "饮料", "零售", "家电", "纺织", "美容"),
    "医药": ("医药", "生物", "医疗", "中药", "器械"),
    "金融": ("银行", "保险", "证券", "多元金融"),
    "人形机器人": ("机器人", "自动化", "通用设备", "电机", "减速器"),
    "生物制造": ("生物", "医药", "化学制药", "医疗"),
    "低空装备": ("航空", "航天", "交运设备", "运输设备"),
    "自动驾驶": ("汽车", "汽车零部件", "汽车整车"),
    "卫星互联网": ("通信", "航天", "通信设备", "卫星"),
}

# 2026 政策白名单（赛迪未来产业 / 十五五相关）→ 政策端自动打勾
POLICY_THEME_IDS: frozenset[str] = frozenset(
    {
        "AI",
        "人形机器人",
        "生物制造",
        "低空装备",
        "自动驾驶",
        "卫星互联网",
        "新能源",  # 能源转型持续政策
        "高端制造",
    }
)

# 板块名匹配用关键词（供需/资金代理）
THEME_BOARD_KEYS: dict[str, tuple[str, ...]] = {
    "新能源": ("光伏", "电池", "储能", "风电", "电力", "电网"),
    "AI": ("半导体", "软件", "计算机", "通信", "电子", "消费电子"),
    "高端制造": ("机械", "军工", "航空", "航天", "自动化", "轨交", "机器人"),
    "消费": ("白酒", "食品", "零售", "家电", "纺织", "美容"),
    "医药": ("医药", "生物", "医疗", "中药", "器械"),
    "金融": ("银行", "保险", "证券"),
    "人形机器人": ("机器人", "自动化", "电机"),
    "生物制造": ("生物", "医药", "制药"),
    "低空装备": ("航空", "航天", "交运"),
    "自动驾驶": ("汽车", "零部件"),
    "卫星互联网": ("通信", "航天", "卫星"),
}

DEFAULT_THEMES = ("新能源", "AI", "高端制造")
AUTO_THEME_TOP_N = 3
MIN_THEME_SAMPLE = 6
PROFIT_REV_MIN = 10.0
RESONANCE_MIN = 3


@dataclass
class ThemeScorecard:
    theme: str
    profit_ok: bool = False
    supply_ok: bool = False
    policy_ok: bool = False
    capital_ok: bool = False
    resonance: int = 0
    selected: bool = False
    conclusion: str = ""
    score: float = 0.0
    sample: int = 0
    median_rev: float | None = None
    median_profit: float | None = None
    details: dict[str, str] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "theme": self.theme,
            "profit_ok": self.profit_ok,
            "supply_ok": self.supply_ok,
            "policy_ok": self.policy_ok,
            "capital_ok": self.capital_ok,
            "resonance": self.resonance,
            "selected": self.selected,
            "conclusion": self.conclusion,
            "score": round(self.score, 1),
            "sample": self.sample,
            "median_rev": None if self.median_rev is None else round(self.median_rev, 1),
            "median_profit": None if self.median_profit is None else round(self.median_profit, 1),
            "details": dict(self.details),
            # backward-compatible aliases for older UI
            "strong_share": 0.0,
        }


# Backward-compatible alias used by older tests / callers
ThemeProsperity = ThemeScorecard


@dataclass
class ScreenThresholds:
    roe_min: float = 15.0
    roe_years: int = 3
    growth_min: float = 15.0
    debt_max: float = 60.0
    pe_pct_max: float = 40.0
    pb_pct_max: float = 40.0
    peg_max: float = 1.5
    require_ocf_positive: bool = True
    pool_size: int = 20
    # cyclical / value track
    cyclical_roe_avg_min: float = 10.0
    cyclical_roe_years: int = 3
    dividend_bonus_min: float = 4.0


@dataclass
class CheckItem:
    key: str
    label: str
    ok: bool
    detail: str


@dataclass
class ScreenRow:
    symbol: str
    name: str
    industry: str
    themes: list[str]
    report_date: str
    roe: float | None = None
    roe_years_ok: int = 0
    revenue_yoy: float | None = None
    profit_yoy: float | None = None
    ocf_ps: float | None = None
    debt_ratio: float | None = None
    pe_ttm: float | None = None
    pb: float | None = None
    pe_percentile: float | None = None
    pb_percentile: float | None = None
    peg: float | None = None
    price: float | None = None
    change_pct: float | None = None
    checks: list[CheckItem] = field(default_factory=list)
    score: float = 0.0
    notes: str = ""
    track: str = "growth"  # growth | cyclical | value
    roe_avg: float | None = None
    no_consec_loss: bool | None = None
    loss_years: int = 0
    dividend_yield: float | None = None
    profit_cv: float | None = None
    profit_positive_years: int = 0
    revenue: float | None = None

    @property
    def passed_hard(self) -> bool:
        from app.services.fundamental_tracks import hard_keys_for

        keys = hard_keys_for(self.track)
        hard = {c.key for c in self.checks if c.key in keys and c.ok}
        return keys.issubset(hard)


HARD_KEYS_GROWTH = frozenset({"roe_streak", "growth", "ocf"})
HARD_KEYS_CYCLICAL = frozenset({"roe_avg", "no_consec_loss", "ocf", "debt"})
HARD_KEYS_BY_TRACK: dict[str, frozenset[str]] = {
    "growth": HARD_KEYS_GROWTH,
    "cyclical": HARD_KEYS_CYCLICAL,
    "value": HARD_KEYS_CYCLICAL,
}
# backward-compatible alias
HARD_KEYS = HARD_KEYS_GROWTH

# re-export classify for callers / tests
from app.services.fundamental_tracks import TRACK_LABEL, sector_classify  # noqa: E402

# Performance report frames are large; cache in-process to speed watchlist analyze.
_YJBB_CACHE: dict[str, tuple[float, pd.DataFrame]] = {}
_YJBB_TTL = 6 * 3600
_DEBT_CACHE: dict[str, tuple[float, dict[str, float]]] = {}
_DEBT_TTL = 6 * 3600


def _num(v: Any) -> float | None:
    if v is None:
        return None
    try:
        x = float(v)
    except (TypeError, ValueError):
        return None
    if x != x:  # NaN
        return None
    return x


def _code6(raw: Any) -> str:
    s = str(raw or "").strip()
    if s.isdigit():
        return s.zfill(6)
    return s


def _to_symbol(code: str) -> str | None:
    c = _code6(code)
    if len(c) != 6 or not c.isdigit():
        return None
    try:
        sym = normalize_symbol(c)
    except SymbolError:
        return None
    if is_index_symbol(sym) or is_etf_symbol(sym):
        return None
    # Skip ST / BJ for this pool
    if c.startswith(("4", "8")):
        return None
    return sym


def match_themes(industry: str, themes: list[str]) -> list[str]:
    ind = industry or ""
    hit: list[str] = []
    for t in themes:
        keys = THEME_KEYWORDS.get(t, ())
        if any(k in ind for k in keys):
            hit.append(t)
    return hit


def _median(vals: list[float]) -> float | None:
    if not vals:
        return None
    xs = sorted(vals)
    mid = len(xs) // 2
    if len(xs) % 2:
        return xs[mid]
    return (xs[mid - 1] + xs[mid]) / 2


def _theme_series(df: pd.DataFrame, theme: str) -> tuple[list[float], list[float], list[float], int]:
    """Collect rev/profit/roe YoY lists for a theme from one yjbb frame."""
    keys = THEME_KEYWORDS.get(theme, ())
    revs: list[float] = []
    profits: list[float] = []
    roes: list[float] = []
    sample = 0
    if not keys or df is None or df.empty:
        return revs, profits, roes, sample
    for _, row in df.iterrows():
        industry = str(row.get("所处行业") or "")
        if not any(k in industry for k in keys):
            continue
        if _to_symbol(row.get("股票代码")) is None:
            continue
        rev = _num(row.get("营业总收入-同比增长"))
        profit = _num(row.get("净利润-同比增长"))
        roe = _num(row.get("净资产收益率"))
        if rev is None and profit is None and roe is None:
            continue
        sample += 1
        if rev is not None:
            revs.append(rev)
        if profit is not None:
            profits.append(profit)
        if roe is not None:
            roes.append(roe)
    return revs, profits, roes, sample


def score_theme_prosperity(df: pd.DataFrame, theme: str) -> ThemeScorecard | None:
    """Legacy single-snapshot growth score (kept for tests / fallback)."""
    revs, profits, _roes, sample = _theme_series(df, theme)
    if sample < MIN_THEME_SAMPLE:
        return None
    med_rev = _median(revs)
    med_profit = _median(profits)
    growth_score = 0.0
    n = 0
    if med_rev is not None:
        growth_score += med_rev
        n += 1
    if med_profit is not None:
        growth_score += med_profit
        n += 1
    if not n:
        return None
    growth_score /= n
    strong = sum(1 for x in (revs + profits) if x >= 15)
    strong_share = strong / max(len(revs) + len(profits), 1)
    score = min(max(growth_score, -50), 120) * 0.7 + strong_share * 100 * 0.3
    return ThemeScorecard(
        theme=theme,
        score=score,
        sample=sample,
        median_rev=med_rev,
        median_profit=med_profit,
        profit_ok=bool(med_rev is not None and med_rev > PROFIT_REV_MIN),
        conclusion="单期增速代理",
    )


def recent_quarter_ends(n: int = 4, today: date | None = None) -> list[str]:
    """Recent report period ends YYYYMMDD (quarterly), newest first."""
    today = today or date.today()
    out: list[str] = []
    for year in range(today.year, today.year - 4, -1):
        for month, day in ((12, 31), (9, 30), (6, 30), (3, 31)):
            d = date(year, month, day)
            if d <= today:
                out.append(f"{year}{month:02d}{day:02d}")
    return out[: n + 2]


def resolve_quarter_frames(need: int = 2, today: date | None = None) -> tuple[list[str], dict[str, pd.DataFrame]]:
    frames: dict[str, pd.DataFrame] = {}
    found: list[str] = []
    for d in recent_quarter_ends(need, today=today):
        df = _fetch_yjbb(d)
        if df is None or df.empty:
            continue
        frames[d] = df
        found.append(d)
        logger.info("quarter yjbb %s rows=%s", d, len(df))
        if len(found) >= need:
            break
    return found, frames


def _fetch_industry_board() -> pd.DataFrame:
    import time

    import akshare as ak

    last_err: Exception | None = None
    for attempt in range(1, 3):
        try:
            df = ak.stock_board_industry_name_em()
            if df is not None and not df.empty:
                return df
            last_err = ValueError("empty")
        except Exception as e:
            last_err = e
            logger.warning("industry board attempt %s failed: %s", attempt, e)
        time.sleep(0.8 * attempt)
    if last_err:
        logger.warning("industry board unavailable: %s", last_err)
    return pd.DataFrame()


def _fetch_sector_fund_flow() -> pd.DataFrame:
    import time

    import akshare as ak

    last_err: Exception | None = None
    for indicator in ("今日", "5日", "10日"):
        for attempt in range(1, 3):
            try:
                df = ak.stock_sector_fund_flow_rank(indicator=indicator)
                if df is not None and not df.empty:
                    return df
                last_err = ValueError("empty")
            except Exception as e:
                last_err = e
                logger.warning("fund flow %s attempt %s failed: %s", indicator, attempt, e)
            time.sleep(0.6 * attempt)
    if last_err:
        logger.warning("fund flow unavailable: %s", last_err)
    return pd.DataFrame()


def _board_change_col(df: pd.DataFrame) -> str | None:
    for c in df.columns:
        s = str(c)
        if "涨跌幅" in s:
            return c
    return None


def _board_name_col(df: pd.DataFrame) -> str | None:
    for c in df.columns:
        s = str(c)
        if s in ("板块名称", "名称") or ("板块" in s and "名称" in s) or s == "行业":
            return c
    # eastmoney often uses 板块名称
    for c in df.columns:
        if "名称" in str(c):
            return c
    return None


def _score_supply_proxy(theme: str, board_df: pd.DataFrame) -> tuple[bool, str]:
    """供需端代理：匹配行业板块涨跌幅处全市场前 30%。"""
    if board_df is None or board_df.empty:
        return False, "板块行情不可用（代理）"
    name_col = _board_name_col(board_df)
    chg_col = _board_change_col(board_df)
    if not name_col or not chg_col:
        return False, "板块字段缺失（代理）"
    keys = THEME_BOARD_KEYS.get(theme, THEME_KEYWORDS.get(theme, ()))
    changes: list[float] = []
    for _, row in board_df.iterrows():
        name = str(row.get(name_col) or "")
        chg = _num(row.get(chg_col))
        if chg is None:
            continue
        changes.append(chg)
    if not changes:
        return False, "无板块涨跌数据（代理）"
    ranked = sorted(changes, reverse=True)
    cut = ranked[max(0, int(len(ranked) * 0.3) - 1)] if ranked else 0.0
    matched: list[tuple[str, float]] = []
    for _, row in board_df.iterrows():
        name = str(row.get(name_col) or "")
        if not any(k in name for k in keys):
            continue
        chg = _num(row.get(chg_col))
        if chg is None:
            continue
        matched.append((name, chg))
    if not matched:
        return False, "未匹配到行业板块（代理）"
    best_name, best_chg = max(matched, key=lambda x: x[1])
    ok = best_chg >= cut
    return ok, f"{best_name} 涨跌 {best_chg:.1f}%（前30%阈值 {cut:.1f}%）"


def _flow_net_col(df: pd.DataFrame) -> str | None:
    for c in df.columns:
        s = str(c)
        if "流入" in s or "净额" in s:
            return c
    return None


def _score_capital_proxy(theme: str, flow_df: pd.DataFrame) -> tuple[bool, str]:
    """资金端：板块资金流净流入为正或排名前 30%。"""
    if flow_df is None or flow_df.empty:
        return False, "资金流数据不可用"
    name_col = _board_name_col(flow_df)
    net_col = _flow_net_col(flow_df)
    if not name_col:
        return False, "资金流缺少板块名"
    keys = THEME_BOARD_KEYS.get(theme, THEME_KEYWORDS.get(theme, ()))
    rows: list[tuple[str, float | None, int]] = []
    for i, row in flow_df.iterrows():
        name = str(row.get(name_col) or "")
        net = _num(row.get(net_col)) if net_col else None
        rows.append((name, net, int(i) if isinstance(i, int) else 0))
    n = len(rows)
    if not n:
        return False, "资金流为空"
    top_n = max(1, int(n * 0.3))
    matched: list[tuple[str, float | None, int]] = []
    for idx, (name, net, _) in enumerate(rows):
        if any(k in name for k in keys):
            matched.append((name, net, idx))
    if not matched:
        return False, "未匹配到资金流板块"
    # Prefer best net inflow among matches; also check rank
    best = max(matched, key=lambda x: (x[1] is not None, x[1] or -1e18))
    name, net, rank = best
    in_top = rank < top_n
    positive = net is not None and net > 0
    ok = positive or in_top
    detail = f"{name}"
    if net is not None:
        detail += f" 净流入 {net:.0f}"
    detail += f"（排名 {rank + 1}/{n}）"
    return ok, detail


def _eval_profit_dim(
    theme: str,
    q0: pd.DataFrame,
    q1: pd.DataFrame | None,
) -> tuple[bool, float, int, float | None, float | None, str]:
    """盈利端硬门槛：两季营收中位>10%；净利不减速；ROE 改善或由负转正。"""
    revs0, profits0, roes0, sample0 = _theme_series(q0, theme)
    if sample0 < MIN_THEME_SAMPLE:
        return False, 0.0, sample0, None, None, f"样本不足（{sample0}）"
    med_rev0 = _median(revs0)
    med_profit0 = _median(profits0)
    med_roe0 = _median(roes0)
    parts: list[str] = []
    rev_ok = med_rev0 is not None and med_rev0 > PROFIT_REV_MIN
    parts.append(f"本期营收中位 {med_rev0:.1f}%" if med_rev0 is not None else "本期营收缺失")

    med_rev1 = med_profit1 = med_roe1 = None
    if q1 is not None and not q1.empty:
        revs1, profits1, roes1, sample1 = _theme_series(q1, theme)
        med_rev1 = _median(revs1)
        med_profit1 = _median(profits1)
        med_roe1 = _median(roes1)
        if sample1 >= max(3, MIN_THEME_SAMPLE // 2):
            rev_ok = rev_ok and (med_rev1 is not None and med_rev1 > PROFIT_REV_MIN)
            parts.append(f"上期营收中位 {med_rev1:.1f}%" if med_rev1 is not None else "上期营收缺失")
        else:
            parts.append("上期样本不足，仅用本期营收")

    profit_accel = True
    if med_profit0 is not None and med_profit1 is not None:
        profit_accel = med_profit0 >= med_profit1
        parts.append(f"净利中位 {med_profit0:.1f}%→较上期{'改善/持平' if profit_accel else '走弱'}")
    elif med_profit0 is not None:
        profit_accel = med_profit0 > 0
        parts.append(f"净利中位 {med_profit0:.1f}%（无上期对比）")
    else:
        profit_accel = False
        parts.append("净利缺失")

    roe_ok = True
    if med_roe0 is not None and med_roe1 is not None:
        roe_ok = med_roe0 >= med_roe1 or (med_roe1 < 0 <= med_roe0)
        parts.append(f"ROE中位 {med_roe0:.1f}%→{med_roe1:.1f}%")
    elif med_roe0 is not None:
        parts.append(f"ROE中位 {med_roe0:.1f}%")

    ok = bool(rev_ok and profit_accel and roe_ok)
    score = 0.0
    n = 0
    if med_rev0 is not None:
        score += med_rev0
        n += 1
    if med_profit0 is not None:
        score += med_profit0
        n += 1
    if n:
        score /= n
    return ok, score, sample0, med_rev0, med_profit0, "；".join(parts)


def score_themes_four_dim(
    *,
    quarter_frames: dict[str, pd.DataFrame],
    quarter_dates: list[str],
    board_df: pd.DataFrame | None = None,
    flow_df: pd.DataFrame | None = None,
    catalog: list[str] | None = None,
    top_n: int = AUTO_THEME_TOP_N,
    min_resonance: int = RESONANCE_MIN,
) -> list[ThemeScorecard]:
    """四维验证：盈利否决 + 供需/政策/资金，共振≥3 入选。"""
    names = list(catalog or THEME_KEYWORDS.keys())
    q_dates = [d for d in quarter_dates if d in quarter_frames and not quarter_frames[d].empty]
    q0 = quarter_frames[q_dates[0]] if q_dates else pd.DataFrame()
    q1 = quarter_frames[q_dates[1]] if len(q_dates) > 1 else None

    if board_df is None:
        board_df = _fetch_industry_board()
    if flow_df is None:
        flow_df = _fetch_sector_fund_flow()

    cards: list[ThemeScorecard] = []
    for theme in names:
        profit_ok, pscore, sample, med_rev, med_profit, profit_detail = _eval_profit_dim(theme, q0, q1)
        supply_ok, supply_detail = _score_supply_proxy(theme, board_df)
        policy_ok = theme in POLICY_THEME_IDS
        capital_ok, capital_detail = _score_capital_proxy(theme, flow_df)

        dims = [profit_ok, supply_ok, policy_ok, capital_ok]
        resonance = sum(1 for x in dims if x)
        # 盈利一票否决：未过则不可入选
        selected = bool(profit_ok and resonance >= min_resonance)
        if not profit_ok:
            conclusion = "假景气/淘汰（盈利端未过）"
        elif selected:
            conclusion = "强景气" if resonance == 4 else "观察入选"
        else:
            conclusion = f"共振不足（{resonance}/{min_resonance}）"

        cards.append(
            ThemeScorecard(
                theme=theme,
                profit_ok=profit_ok,
                supply_ok=supply_ok,
                policy_ok=policy_ok,
                capital_ok=capital_ok,
                resonance=resonance,
                selected=selected,
                conclusion=conclusion,
                score=pscore,
                sample=sample,
                median_rev=med_rev,
                median_profit=med_profit,
                details={
                    "profit": profit_detail,
                    "supply": supply_detail,
                    "policy": "2026政策白名单" if policy_ok else "非政策清单赛道",
                    "capital": capital_detail,
                },
            )
        )

    cards.sort(key=lambda c: (c.selected, c.resonance, c.score), reverse=True)
    picked = [c for c in cards if c.selected][: max(1, top_n)]
    if picked:
        # Keep full scorecards but mark only top_n selected for screening
        picked_names = {c.theme for c in picked}
        for c in cards:
            if c.theme not in picked_names:
                if c.selected:
                    c.selected = False
                    c.conclusion = "入选名额已满"
        return cards

    # 无人达共振门槛：不降级硬塞非盈利赛道；若有盈利过关则降级取 TopN 并标注
    profit_pass = [c for c in cards if c.profit_ok]
    if profit_pass:
        for i, c in enumerate(profit_pass[: max(1, top_n)]):
            c.selected = True
            c.conclusion = f"降级观察（共振{c.resonance}<{min_resonance}）"
        return cards

    # 最后兜底：默认赛道仅供展示，不假装过关
    for c in cards:
        if c.theme in DEFAULT_THEMES:
            c.selected = True
            c.conclusion = "数据不足兜底"
            break
    if not any(c.selected for c in cards) and cards:
        cards[0].selected = True
        cards[0].conclusion = "数据不足兜底"
    return cards


def detect_hot_themes(
    df: pd.DataFrame,
    *,
    top_n: int = AUTO_THEME_TOP_N,
    catalog: list[str] | None = None,
) -> list[ThemeScorecard]:
    """Pick top-N themes from a single earnings snapshot (legacy / tests)."""
    names = list(catalog or THEME_KEYWORDS.keys())
    scored: list[ThemeScorecard] = []
    for name in names:
        item = score_theme_prosperity(df, name)
        if item:
            item.selected = True
            scored.append(item)
    scored.sort(key=lambda x: x.score, reverse=True)
    if scored:
        return scored[: max(1, top_n)]
    return [
        ThemeScorecard(theme=t, score=0.0, sample=0, selected=True, conclusion="fallback")
        for t in (DEFAULT_THEMES if catalog is None else names[:top_n])
    ]


def recent_year_ends(n: int = 3, today: date | None = None) -> list[str]:
    """Candidate annual report dates YYYY1231 (newest first), with extra fallback years."""
    today = today or date.today()
    year = today.year
    # Annual filings for year Y are due by ~Apr 30 of Y+1; keep older backups for API gaps.
    start = year - 1 if today.month >= 5 else year - 2
    return [f"{y}1231" for y in range(start, start - (n + 2), -1)]


def _fetch_yjbb(report_date: str, retries: int = 2) -> pd.DataFrame:
    import time

    import akshare as ak

    now = time.time()
    cached = _YJBB_CACHE.get(report_date)
    if cached is not None:
        ts, cached_df = cached
        if now - ts < _YJBB_TTL and cached_df is not None and not cached_df.empty:
            return cached_df

    last_err: Exception | None = None
    for attempt in range(1, retries + 1):
        try:
            df = ak.stock_yjbb_em(date=report_date)
            if df is not None and not df.empty:
                _YJBB_CACHE[report_date] = (now, df)
                return df
            last_err = ValueError("empty")
        except Exception as e:
            last_err = e
            logger.warning("yjbb %s attempt %s failed: %s", report_date, attempt, e)
        if attempt < retries:
            time.sleep(1.2 * attempt)
    if last_err:
        logger.warning("yjbb %s unavailable: %s", report_date, last_err)
    empty = pd.DataFrame()
    _YJBB_CACHE[report_date] = (now, empty)
    return empty


def resolve_report_frames(need: int = 3, today: date | None = None) -> tuple[list[str], dict[str, pd.DataFrame]]:
    """Probe Eastmoney until we have `need` usable annual snapshots."""
    frames: dict[str, pd.DataFrame] = {}
    found: list[str] = []
    for d in recent_year_ends(need, today=today):
        df = _fetch_yjbb(d)
        if df is None or df.empty:
            continue
        frames[d] = df
        found.append(d)
        logger.info("yjbb %s rows=%s", d, len(df))
        if len(found) >= need:
            break
    return found, frames


def resolve_report_dates(need: int = 3, today: date | None = None) -> list[str]:
    dates, _ = resolve_report_frames(need, today=today)
    return dates


def _fetch_debt_map(report_date: str) -> dict[str, float]:
    """资产负债率 from balance-sheet snapshot if available."""
    import time

    import akshare as ak

    now = time.time()
    cached = _DEBT_CACHE.get(report_date)
    if cached and now - cached[0] < _DEBT_TTL:
        return cached[1]

    out: dict[str, float] = {}
    try:
        df = ak.stock_zcfz_em(date=report_date)
    except Exception as e:
        logger.warning("zcfz fetch failed %s: %s", report_date, e)
        _DEBT_CACHE[report_date] = (now, out)
        return out
    if df is None or df.empty:
        _DEBT_CACHE[report_date] = (now, out)
        return out
    code_col = next((c for c in df.columns if "代码" in str(c)), None)
    debt_col = next((c for c in df.columns if "资产负债率" in str(c)), None)
    if not code_col or not debt_col:
        _DEBT_CACHE[report_date] = (now, out)
        return out
    for _, row in df.iterrows():
        sym = _to_symbol(row[code_col])
        d = _num(row[debt_col])
        if sym and d is not None:
            out[sym] = d
    _DEBT_CACHE[report_date] = (now, out)
    return out


def _peg(pe: float | None, profit_yoy: float | None) -> float | None:
    """PEG = PE / 净利同比增速(百分数，如 20 表示 20%)。"""
    if pe is None or pe <= 0 or profit_yoy is None or profit_yoy <= 0:
        return None
    return round(pe / profit_yoy, 2)


def _score(row: ScreenRow, th: ScreenThresholds) -> float:
    from app.services.fundamental_tracks import score_row

    return score_row(row, th)


def _build_checks(row: ScreenRow, th: ScreenThresholds) -> list[CheckItem]:
    from app.services.fundamental_tracks import build_checks

    return build_checks(row, th)


def screen_fundamentals(
    db: Session,
    *,
    themes: list[str] | None = None,
    auto_themes: bool = True,
    top_themes: int = AUTO_THEME_TOP_N,
    thresholds: ScreenThresholds | None = None,
    report_dates: list[str] | None = None,
    enrich_valuation: bool = True,
    enrich_debt: bool = True,
) -> tuple[str, list[ScreenRow], list[str], list[dict[str, Any]]]:
    """
    Run layer-1 screen.
    Hard gates: ROE streak, growth, OCF+. Soft: debt, PE/PB pct, PEG.
    Rank by score and keep pool_size.
    Returns (pool_run_id, rows, report_dates_used, theme_prosperity).
    """
    th = thresholds or ScreenThresholds()
    if report_dates:
        dates = list(report_dates)
        frames = {d: _fetch_yjbb(d) for d in dates}
        for d in dates:
            logger.info("yjbb %s rows=%s", d, len(frames[d]))
    else:
        dates, frames = resolve_report_frames(max(th.roe_years, 5))

    usable = [d for d in dates if frames.get(d) is not None and not frames[d].empty]
    if not usable:
        raise RuntimeError("无法获取业绩报表（年报接口暂时不可用，请稍后重试）")
    if len(usable) < min(2, th.roe_years):
        raise RuntimeError(
            f"可用年报不足（仅 {', '.join(usable)}），无法做连续 ROE 筛选，请稍后重试"
        )
    dates = usable
    latest = dates[0]
    base = frames[latest]

    theme_meta: list[ThemeScorecard] = []
    if auto_themes or not themes:
        q_dates, q_frames = resolve_quarter_frames(2)
        # Prefer dedicated quarter snapshots; fall back to annual frames already loaded
        if len(q_dates) < 1:
            q_dates, q_frames = dates[:2], {d: frames[d] for d in dates[:2]}
        theme_meta = score_themes_four_dim(
            quarter_frames=q_frames,
            quarter_dates=q_dates,
            top_n=top_themes,
        )
        themes = [t.theme for t in theme_meta if t.selected] or [t.theme for t in theme_meta[:top_themes]]
        logger.info("auto themes (four-dim): %s", themes)
    else:
        themes = list(themes)
        theme_meta = [
            ThemeScorecard(theme=t, selected=True, profit_ok=True, resonance=1, conclusion="手动指定")
            for t in themes
        ]

    debt_map = _fetch_debt_map(latest) if enrich_debt else {}

    # Index older years by symbol → ROE / net profit
    hist_roe: dict[str, dict[str, float]] = {d: {} for d in dates}
    hist_profit: dict[str, dict[str, float]] = {d: {} for d in dates}
    for d, df in frames.items():
        if df is None or df.empty:
            continue
        for _, row in df.iterrows():
            sym = _to_symbol(row.get("股票代码"))
            if not sym:
                continue
            roe = _num(row.get("净资产收益率"))
            if roe is not None:
                hist_roe[d][sym] = roe
            np_ = _num(row.get("净利润"))
            if np_ is None:
                np_ = _num(row.get("归母净利润"))
            if np_ is not None:
                hist_profit[d][sym] = np_

    candidates: list[ScreenRow] = []
    for _, row in base.iterrows():
        sym = _to_symbol(row.get("股票代码"))
        if not sym:
            continue
        industry = str(row.get("所处行业") or "")
        theme_hits = match_themes(industry, themes)
        if themes and not theme_hits:
            continue

        roe = _num(row.get("净资产收益率"))
        rev = _num(row.get("营业总收入-同比增长"))
        profit = _num(row.get("净利润-同比增长"))
        ocf = _num(row.get("每股经营现金流量"))
        years_ok = 0
        for d in dates:
            r = hist_roe.get(d, {}).get(sym)
            if r is not None and r >= th.roe_min:
                years_ok += 1
            else:
                break  # require consecutive from latest

        item = ScreenRow(
            symbol=sym,
            name=str(row.get("股票简称") or ""),
            industry=industry,
            themes=theme_hits,
            report_date=latest,
            roe=roe,
            roe_years_ok=years_ok,
            revenue_yoy=rev,
            profit_yoy=profit,
            ocf_ps=ocf,
            debt_ratio=debt_map.get(sym),
        )
        # Hard prefilter before valuation (keep list manageable)
        growth_ok = (rev is not None and rev >= th.growth_min) or (
            profit is not None and profit >= th.growth_min
        )
        ocf_ok = ocf is not None and ocf > 0
        if years_ok < max(1, th.roe_years - 1):
            continue
        if not growth_ok or not ocf_ok:
            continue
        if item.debt_ratio is not None and item.debt_ratio >= th.debt_max:
            continue
        candidates.append(item)

    # Prefer stronger ROE streak first, then rough score without valuation
    candidates.sort(
        key=lambda x: (
            x.roe_years_ok,
            x.roe or 0,
            max(x.revenue_yoy or 0, x.profit_yoy or 0),
        ),
        reverse=True,
    )
    shortlist = candidates[: max(th.pool_size * 4, 40)]

    if enrich_valuation and shortlist:
        vals = {
            v["symbol"]: v
            for v in get_valuations([r.symbol for r in shortlist], db=db)
        }
        for r in shortlist:
            v = vals.get(r.symbol) or {}
            r.pe_ttm = _num(v.get("pe_ttm"))
            r.pb = _num(v.get("pb"))
            r.pe_percentile = _num(v.get("pe_percentile"))
            r.pb_percentile = _num(v.get("pb_percentile"))
            r.price = _num(v.get("price"))
            r.change_pct = _num(v.get("change_pct"))
            r.peg = _peg(r.pe_ttm, r.profit_yoy)

    # Soft valuation filter: keep if PE/PB pct unknown OR under threshold
    from app.services.fundamental_tracks import enrich_track_metrics

    filtered: list[ScreenRow] = []
    for r in shortlist:
        pe_block = r.pe_percentile is not None and r.pe_percentile >= th.pe_pct_max
        pb_block = r.pb_percentile is not None and r.pb_percentile >= th.pb_pct_max
        peg_block = r.peg is not None and r.peg > th.peg_max
        soft_fails = sum([pe_block, pb_block, peg_block])
        enrich_track_metrics(
            r, th=th, hist_roe=hist_roe, hist_profit=hist_profit, dates=dates
        )
        notes = []
        if r.track == "growth" and r.roe_years_ok < th.roe_years:
            notes.append(f"ROE 连续年数不足（{r.roe_years_ok}/{th.roe_years}）")
        if soft_fails:
            notes.append(f"估值软条件未全过（{soft_fails}项）")
        r.notes = "；".join(notes)
        if r.roe_years_ok >= th.roe_years and soft_fails <= 1:
            filtered.append(r)
        elif r.roe_years_ok >= th.roe_years and soft_fails > 1:
            if r.score >= 45:
                filtered.append(r)

    filtered.sort(key=lambda x: x.score, reverse=True)
    pool = filtered[: th.pool_size]
    run_id = uuid.uuid4().hex[:12]
    _persist_pool(db, run_id, pool)
    return run_id, pool, dates, [t.to_dict() for t in theme_meta]


def _persist_pool(db: Session, run_id: str, rows: list[ScreenRow]) -> None:
    db.query(FundamentalCandidate).delete()
    for r in rows:
        db.add(
            FundamentalCandidate(
                symbol=r.symbol,
                name=r.name,
                industry=r.industry,
                themes=",".join(r.themes),
                report_date=r.report_date,
                score=r.score,
                roe=r.roe,
                roe_years_ok=r.roe_years_ok,
                revenue_yoy=r.revenue_yoy,
                profit_yoy=r.profit_yoy,
                ocf_ps=r.ocf_ps,
                debt_ratio=r.debt_ratio,
                pe_ttm=r.pe_ttm,
                pb=r.pb,
                pe_percentile=r.pe_percentile,
                pb_percentile=r.pb_percentile,
                peg=r.peg,
                checks_json=json.dumps(
                    [{"key": c.key, "label": c.label, "ok": c.ok, "detail": c.detail} for c in r.checks],
                    ensure_ascii=False,
                ),
                notes=r.notes,
                pool_run_id=run_id,
            )
        )
    db.commit()


def list_pool(db: Session) -> list[FundamentalCandidate]:
    return (
        db.query(FundamentalCandidate)
        .order_by(FundamentalCandidate.score.desc(), FundamentalCandidate.id.asc())
        .all()
    )


def clear_pool(db: Session) -> int:
    n = db.query(FundamentalCandidate).delete()
    db.commit()
    return int(n or 0)


def candidate_out(row: FundamentalCandidate, quote: dict[str, Any] | None = None) -> dict[str, Any]:
    try:
        checks = json.loads(row.checks_json or "[]")
    except json.JSONDecodeError:
        checks = []
    q = quote or {}
    return {
        "id": row.id,
        "symbol": row.symbol,
        "name": row.name or q.get("name") or "",
        "industry": row.industry,
        "themes": [t for t in (row.themes or "").split(",") if t],
        "report_date": row.report_date,
        "score": row.score,
        "roe": row.roe,
        "roe_years_ok": row.roe_years_ok,
        "revenue_yoy": row.revenue_yoy,
        "profit_yoy": row.profit_yoy,
        "ocf_ps": row.ocf_ps,
        "debt_ratio": row.debt_ratio,
        "pe_ttm": row.pe_ttm,
        "pb": row.pb,
        "pe_percentile": row.pe_percentile,
        "pb_percentile": row.pb_percentile,
        "peg": row.peg,
        "price": _num(q.get("price")),
        "change_pct": _num(q.get("change_pct")),
        "checks": checks,
        "notes": row.notes,
        "pool_run_id": row.pool_run_id,
        "created_at": row.created_at.isoformat() if row.created_at else None,
    }


def pool_items_out(rows: list[FundamentalCandidate], db: Session | None = None) -> list[dict[str, Any]]:
    quotes: dict[str, dict[str, Any]] = {}
    if rows and db is not None:
        try:
            quotes = {
                v["symbol"]: v
                for v in get_valuations([r.symbol for r in rows], db=db)
            }
        except Exception as e:
            logger.warning("pool quote enrich failed: %s", e)
    return [candidate_out(r, quotes.get(r.symbol)) for r in rows]


def screen_rows_out(rows: list[ScreenRow]) -> list[dict[str, Any]]:
    """Serialize in-memory screen rows (includes live price from valuation pass)."""
    out: list[dict[str, Any]] = []
    for i, r in enumerate(rows):
        out.append(
            {
                "id": i + 1,
                "symbol": r.symbol,
                "name": r.name,
                "industry": r.industry,
                "themes": list(r.themes),
                "report_date": r.report_date,
                "score": r.score,
                "roe": r.roe,
                "roe_years_ok": r.roe_years_ok,
                "revenue_yoy": r.revenue_yoy,
                "profit_yoy": r.profit_yoy,
                "ocf_ps": r.ocf_ps,
                "debt_ratio": r.debt_ratio,
                "pe_ttm": r.pe_ttm,
                "pb": r.pb,
                "pe_percentile": r.pe_percentile,
                "pb_percentile": r.pb_percentile,
                "peg": r.peg,
                "price": r.price,
                "change_pct": r.change_pct,
                "checks": [
                    {"key": c.key, "label": c.label, "ok": c.ok, "detail": c.detail} for c in r.checks
                ],
                "notes": r.notes,
                "pool_run_id": "",
                "created_at": None,
            }
        )
    return out


def _verdict(row: ScreenRow) -> tuple[str, str]:
    from app.services.fundamental_tracks import verdict

    return verdict(row)


def _metrics_line(row: ScreenRow) -> str:
    from app.services.fundamental_tracks import metrics_line

    return metrics_line(row)


def _index_yjbb(df: pd.DataFrame) -> dict[str, Any]:
    out: dict[str, Any] = {}
    if df is None or df.empty:
        return out
    for _, row in df.iterrows():
        sym = _to_symbol(row.get("股票代码"))
        if sym:
            out[sym] = row
    return out


def analyze_symbols(
    db: Session,
    symbols: list[str],
    *,
    thresholds: ScreenThresholds | None = None,
    enrich_valuation: bool = True,
    enrich_debt: bool = True,
) -> dict[str, Any]:
    """
    Fundamental snapshot for an arbitrary watchlist (no theme gate).
    Reuses annual performance reports + valuation percentiles.
    """
    th = thresholds or ScreenThresholds()
    wanted: list[str] = []
    seen: set[str] = set()
    for raw in symbols:
        try:
            sym = normalize_symbol(str(raw).strip())
        except SymbolError:
            continue
        if is_index_symbol(sym) or is_etf_symbol(sym):
            continue
        key = sym.upper()
        if key in seen:
            continue
        seen.add(key)
        wanted.append(sym)
        if len(wanted) >= 50:
            break

    if not wanted:
        return {"report_dates": [], "items": []}

    dates, frames = resolve_report_frames(max(th.roe_years, 5))
    if not dates:
        return {
            "report_dates": [],
            "items": [
                {
                    "symbol": s,
                    "name": "",
                    "industry": "",
                    "report_date": "",
                    "score": 0,
                    "roe": None,
                    "roe_years_ok": 0,
                    "revenue_yoy": None,
                    "profit_yoy": None,
                    "ocf_ps": None,
                    "debt_ratio": None,
                    "pe_ttm": None,
                    "pb": None,
                    "pe_percentile": None,
                    "pb_percentile": None,
                    "peg": None,
                    "track": "growth",
                    "checks": [],
                    "notes": "暂无业绩报表数据",
                    "verdict": "无财报数据",
                    "verdict_tone": "na",
                    "metrics": "—",
                }
                for s in wanted
            ],
        }

    latest = dates[0]
    base_df = frames.get(latest)
    if base_df is None:
        base_df = pd.DataFrame()
    base_index = _index_yjbb(base_df)
    hist_roe: dict[str, dict[str, float]] = {d: {} for d in dates}
    hist_profit: dict[str, dict[str, float]] = {d: {} for d in dates}
    for d, df in frames.items():
        if df is None or df.empty:
            continue
        for _, row in df.iterrows():
            sym = _to_symbol(row.get("股票代码"))
            if not sym:
                continue
            roe = _num(row.get("净资产收益率"))
            if roe is not None:
                hist_roe[d][sym] = roe
            # absolute net profit (万元/元，口径随源；仅用于盈亏符号与波动)
            np_ = _num(row.get("净利润"))
            if np_ is None:
                np_ = _num(row.get("归母净利润"))
            if np_ is not None:
                hist_profit[d][sym] = np_

    debt_map = _fetch_debt_map(latest) if enrich_debt else {}
    vals: dict[str, dict[str, Any]] = {}
    if enrich_valuation:
        try:
            vals = {v["symbol"]: v for v in get_valuations(wanted, db=db)}
        except Exception as e:
            logger.warning("watchlist valuation enrich failed: %s", e)

    from app.services.fundamental_tracks import enrich_track_metrics

    theme_ids = list(THEME_KEYWORDS.keys())
    items: list[dict[str, Any]] = []
    for sym in wanted:
        raw = base_index.get(sym)
        v = vals.get(sym) or {}
        if raw is None:
            items.append(
                {
                    "symbol": sym,
                    "name": str(v.get("name") or ""),
                    "industry": "",
                    "report_date": latest,
                    "score": 0,
                    "roe": None,
                    "roe_years_ok": 0,
                    "revenue_yoy": None,
                    "profit_yoy": None,
                    "ocf_ps": None,
                    "debt_ratio": debt_map.get(sym),
                    "pe_ttm": _num(v.get("pe_ttm")),
                    "pb": _num(v.get("pb")),
                    "pe_percentile": _num(v.get("pe_percentile")),
                    "pb_percentile": _num(v.get("pb_percentile")),
                    "peg": None,
                    "track": sector_classify("", name=str(v.get("name") or ""), symbol=sym),
                    "checks": [],
                    "notes": "业绩快报未覆盖该标的（可能为 B 股/新股）",
                    "verdict": "无财报数据",
                    "verdict_tone": "na",
                    "metrics": "—",
                }
            )
            continue

        industry = str(raw.get("所处行业") or "")
        roe = _num(raw.get("净资产收益率"))
        rev = _num(raw.get("营业总收入-同比增长"))
        profit = _num(raw.get("净利润-同比增长"))
        ocf = _num(raw.get("每股经营现金流量"))
        revenue = _num(raw.get("营业总收入"))
        years_ok = 0
        for d in dates:
            r = hist_roe.get(d, {}).get(sym)
            if r is not None and r >= th.roe_min:
                years_ok += 1
            else:
                break

        row = ScreenRow(
            symbol=sym,
            name=str(raw.get("股票简称") or v.get("name") or ""),
            industry=industry,
            themes=match_themes(industry, theme_ids),
            report_date=latest,
            roe=roe,
            roe_years_ok=years_ok,
            revenue_yoy=rev,
            profit_yoy=profit,
            ocf_ps=ocf,
            debt_ratio=debt_map.get(sym),
            pe_ttm=_num(v.get("pe_ttm")),
            pb=_num(v.get("pb")),
            pe_percentile=_num(v.get("pe_percentile")),
            pb_percentile=_num(v.get("pb_percentile")),
            price=_num(v.get("price")),
            change_pct=_num(v.get("change_pct")),
            dividend_yield=_num(v.get("dividend_yield")),
            revenue=revenue,
        )
        row.peg = _peg(row.pe_ttm, row.profit_yoy)
        enrich_track_metrics(
            row, th=th, hist_roe=hist_roe, hist_profit=hist_profit, dates=dates
        )
        label, tone = _verdict(row)
        notes = []
        if not row.passed_hard:
            notes.append(f"{TRACK_LABEL.get(row.track, row.track)}硬条件未全过")
        if row.pe_percentile is not None and row.pe_percentile >= th.pe_pct_max:
            notes.append("PE 分位偏高")
        if row.pb_percentile is not None and row.pb_percentile >= th.pb_pct_max:
            notes.append("PB 分位偏高")
        items.append(
            {
                "symbol": row.symbol,
                "name": row.name,
                "industry": row.industry,
                "themes": list(row.themes),
                "report_date": row.report_date,
                "score": row.score,
                "roe": row.roe,
                "roe_avg": row.roe_avg,
                "roe_years_ok": row.roe_years_ok,
                "revenue_yoy": row.revenue_yoy,
                "profit_yoy": row.profit_yoy,
                "ocf_ps": row.ocf_ps,
                "debt_ratio": row.debt_ratio,
                "pe_ttm": row.pe_ttm,
                "pb": row.pb,
                "pe_percentile": row.pe_percentile,
                "pb_percentile": row.pb_percentile,
                "peg": row.peg,
                "track": row.track,
                "track_label": TRACK_LABEL.get(row.track, row.track),
                "dividend_yield": row.dividend_yield,
                "checks": [
                    {"key": c.key, "label": c.label, "ok": c.ok, "detail": c.detail} for c in row.checks
                ],
                "notes": "；".join(notes),
                "verdict": label,
                "verdict_tone": tone,
                "metrics": _metrics_line(row),
            }
        )

    return {"report_dates": dates, "items": items}


def themes_catalog() -> list[dict[str, Any]]:
    return [
        {
            "id": k,
            "keywords": list(v),
            "policy": k in POLICY_THEME_IDS,
        }
        for k, v in THEME_KEYWORDS.items()
    ]
