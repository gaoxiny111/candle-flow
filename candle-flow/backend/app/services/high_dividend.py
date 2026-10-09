"""高股息股票筛选器（AkShare）。

口径（用户指定）：
  - 近 5 年窗口内连续现金分红 3～5 年（窗口内 streak ∈ [3, 5]）
  - 近 3 年平均股息率 ≥ 4%
  - 最近 1 年股息率 ≥ 3%
  - 股利支付率 30%～80%，且 ≤ 100%
  - PE(TTM) ≤ 15、PB ≤ 1.5（命中硬条件；不满足仍展示为未命中）
  - 近 3 年年报 ROE 均值 ≥ 8%，且最新年报 ROE ≥ 6%（加权净资产收益率优先）
  - 经营现金流净额 / 净利润 ≥ 0.8
  - 总市值 ≥ 200 亿

初始池默认 **A股大盘（市值≥200亿）**；可选中证红利 / 沪深市值前 N。
"""

from __future__ import annotations

import json
import logging
import threading
import time
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

logger = logging.getLogger(__name__)

# ── 筛选阈值 ──────────────────────────────────────────────────────
MIN_AVG_YIELD_3Y = 4.0
MIN_LAST_YIELD = 3.0
MIN_DIV_YEARS = 3
MAX_DIV_YEARS = 5
PAYOUT_MIN, PAYOUT_MAX = 30.0, 80.0
PAYOUT_HARD_MAX = 100.0
PE_MAX = 15.0
PB_MAX = 1.5
ROE_AVG_3Y_MIN = 8.0  # 近 3 年年报 ROE 均值
ROE_LATEST_MIN = 6.0  # 最新年报 ROE
OCF_NP_MIN = 0.8
MIN_MARKET_CAP = 200e8  # 200 亿
AVG_YIELD_YEARS = 3

CSI_DIVIDEND_INDEX = "000922"
CACHE_TTL_SEC = 6 * 3600
CACHE_VERSION = "v14"  # ROE：近3年均≥8% + 最新≥6%
UNIVERSES = ("large_cap", "csi_div", "all")
DEFAULT_UNIVERSE = "large_cap"
# 深扫并发（每票约 2 次 AkShare）
DEEP_WORKERS = 16
# 深扫每完成 N 只就写盘，避免整段失败后页面一直空
DEEP_CHECKPOINT_EVERY = 25
# 支付率过小视为失真（新浪「股息发放率」常给 0.0006 这类假值）
PAYOUT_NOISE_MAX = 1.0
# 中证红利 ~100 只 × 分红+财务两接口，同步扫会超过 Cloudflare 120s → 磁盘缓存 + 后台刷新
_DISK_CACHE_DIR = Path("data") / "cache" / "high_dividend"

_CACHE: dict[str, Any] = {"ts": 0.0, "key": None, "data": None}
_LOCK = threading.Lock()
_REFRESHING: set[str] = set()

# 兼容旧测试名
MIN_AVG_YIELD_5Y = MIN_AVG_YIELD_3Y
DIV_LOOKBACK_YEARS = AVG_YIELD_YEARS
PE_MIN, PB_MIN = 0.0, 0.0


def _to_float(v: Any) -> float | None:
    if v is None or v == "" or (isinstance(v, float) and np.isnan(v)):
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _normalize_code(raw: Any) -> str:
    s = str(raw or "").strip().lower()
    digits = "".join(ch for ch in s if ch.isdigit())
    return digits[-6:].zfill(6) if digits else ""


def _is_hs_a_code(code: str) -> bool:
    """沪深 A 股（含创业板/科创板）；排除北交所 4/8/92 开头。"""
    c = str(code or "").zfill(6)
    if c.startswith(("4", "8")):
        return False
    if c.startswith("92"):
        return False
    return c.startswith(("00", "30", "60", "68"))


def _spot_from_em() -> pd.DataFrame:
    import akshare as ak

    df = ak.stock_zh_a_spot_em()
    df = df.rename(
        columns={
            "代码": "code",
            "名称": "name",
            "最新价": "price",
            "市盈率-动态": "pe_ttm",
            "市净率": "pb",
            "总市值": "market_cap",
        }
    )
    return df


def _spot_from_sina_plus_factors() -> pd.DataFrame:
    """新浪价 + 因子库 PE/PB/市值。

    盘前/集合竞价阶段新浪 ``最新价`` 常为 0，改用 ``昨收`` 兜底，
    否则整池会被滤成空表，后续布尔索引还会丢掉 ``code`` 列触发 KeyError。
    """
    import akshare as ak

    spot = ak.stock_zh_a_spot()
    rename = {"代码": "code", "名称": "name", "最新价": "price", "昨收": "pre_close"}
    spot = spot.rename(columns={k: v for k, v in rename.items() if k in spot.columns})
    if "code" not in spot.columns:
        raise RuntimeError(f"新浪行情缺代码列: {list(spot.columns)}")
    spot["code"] = spot["code"].map(_normalize_code)
    price = pd.to_numeric(spot.get("price"), errors="coerce")
    if "pre_close" in spot.columns:
        pre = pd.to_numeric(spot["pre_close"], errors="coerce")
        price = price.where(price.fillna(0) > 0, pre)
    spot["price"] = price
    spot = spot[spot["code"].str.len() == 6][["code", "name", "price"]].copy()

    from sqlalchemy import text

    from app.database import SessionLocal

    sql = text(
        """
        SELECT substr(symbol, 1, 6) AS code,
               coalesce(pe_ttm, json_extract(payload, '$.market.pe_ttm')) AS pe_ttm,
               json_extract(payload, '$.market.pb') AS pb,
               json_extract(payload, '$.market.market_cap') AS market_cap
        FROM factor_snapshots
        WHERE composite_score IS NOT NULL
        """
    )
    db = SessionLocal()
    try:
        rows = [dict(r._mapping) for r in db.execute(sql)]
    finally:
        db.close()
    if not rows:
        spot["pe_ttm"] = np.nan
        spot["pb"] = np.nan
        spot["market_cap"] = np.nan
        return spot
    fac = pd.DataFrame(rows)
    fac["code"] = fac["code"].astype(str).str.zfill(6)
    return spot.merge(fac, on="code", how="left")


def _fetch_spot() -> pd.DataFrame:
    df: pd.DataFrame | None = None
    try:
        df = _spot_from_em()
        logger.info("高股息行情：东财 spot_em 成功，%s 行", len(df))
    except Exception as e:  # noqa: BLE001
        logger.warning("东财 stock_zh_a_spot_em 失败，回退新浪+因子库：%s", e)
        df = _spot_from_sina_plus_factors()
        logger.info("高股息行情：回退路径成功，%s 行", len(df))

    keep = ["code", "name", "price", "pe_ttm", "pb", "market_cap"]
    for c in keep:
        if c not in df.columns:
            df[c] = np.nan
    df = df[keep].copy()
    df["code"] = df["code"].map(_normalize_code)
    df = df[df["code"].str.len() == 6]
    df = df[df["code"].map(_is_hs_a_code)].copy()
    df = df[~df["name"].astype(str).str.contains("ST|退", regex=True, na=False)].copy()
    df = df[~df["name"].astype(str).str.contains(r"B$|Ｂ", regex=True, na=False)].copy()
    df = df[pd.to_numeric(df["price"], errors="coerce").fillna(0) > 0]
    for col in ("price", "pe_ttm", "pb", "market_cap"):
        df[col] = pd.to_numeric(df[col], errors="coerce")
    return df.drop_duplicates(subset=["code"]).reset_index(drop=True)


def _fetch_csi_dividend_codes() -> list[str]:
    import akshare as ak

    cons = ak.index_stock_cons_csindex(symbol=CSI_DIVIDEND_INDEX)
    col = "成分券代码" if "成分券代码" in cons.columns else cons.columns[4]
    return [str(c).zfill(6) for c in cons[col].tolist()]


def _annual_cash_dps(hist: pd.DataFrame) -> pd.Series:
    """按年度聚合现金分红（元/股）。新浪 ``派息`` = 每 10 股派息。

    优先只计「实施」记录，避免预案与实施重复；年份优先用除权除息日。
    """
    if hist is None or hist.empty:
        return pd.Series(dtype=float)

    df = hist.copy()
    if "进度" in df.columns:
        impl = df[df["进度"].astype(str).str.contains("实施", na=False)]
        if not impl.empty:
            df = impl

    if "每股分红" in df.columns:
        dps = pd.to_numeric(df["每股分红"], errors="coerce")
    elif "派息" in df.columns:
        dps = pd.to_numeric(df["派息"], errors="coerce") / 10.0
    else:
        return pd.Series(dtype=float)

    if "除权除息日" in df.columns:
        year = pd.to_datetime(df["除权除息日"], errors="coerce").dt.year
        if "公告日期" in df.columns:
            year = year.fillna(pd.to_datetime(df["公告日期"], errors="coerce").dt.year)
    elif "报告期" in df.columns:
        year = pd.to_datetime(df["报告期"], errors="coerce").dt.year
    elif "公告日期" in df.columns:
        year = pd.to_datetime(df["公告日期"], errors="coerce").dt.year
    else:
        return pd.Series(dtype=float)

    tmp = pd.DataFrame({"year": year, "dps": dps}).dropna()
    if tmp.empty:
        return pd.Series(dtype=float)
    tmp = tmp[tmp["dps"] > 0]
    if tmp.empty:
        return pd.Series(dtype=float)
    return tmp.groupby("year")["dps"].sum().sort_index()


def _consecutive_div_years(cash_div: pd.Series, *, window: int | None = None) -> int:
    """从最近一次现金分红年度起，向前连续有分红的年数。

    ``window`` 若给定（如 5），只在「最近分红年往前 window 年」内计 streak，
    这样长期分红股也会落在 3～5 的考察带，而不会因历史 10+ 年被误杀。
    """
    if cash_div is None or cash_div.empty:
        return 0
    years = sorted(int(y) for y in cash_div.index if pd.notna(y))
    if not years:
        return 0
    if window is not None and window > 0:
        latest = years[-1]
        years = [y for y in years if y >= latest - window + 1]
        if not years:
            return 0
    streak = 1
    for i in range(len(years) - 1, 0, -1):
        if years[i] - years[i - 1] == 1:
            streak += 1
        else:
            break
    return streak


def _annual_eps_series(fin: pd.DataFrame) -> pd.Series:
    """年报摊薄每股收益（元），按财年索引。"""
    if fin is None or fin.empty or "日期" not in fin.columns:
        return pd.Series(dtype=float)
    ann = fin[fin["日期"].astype(str).str.contains(r"-12-31", regex=True)].copy()
    if ann.empty:
        return pd.Series(dtype=float)
    eps_col = "摊薄每股收益(元)" if "摊薄每股收益(元)" in ann.columns else "加权每股收益(元)"
    if eps_col not in ann.columns:
        return pd.Series(dtype=float)
    years = pd.to_datetime(ann["日期"], errors="coerce").dt.year
    eps = pd.to_numeric(ann[eps_col], errors="coerce")
    tmp = pd.DataFrame({"year": years, "eps": eps}).dropna()
    if tmp.empty:
        return pd.Series(dtype=float)
    return tmp.groupby("year")["eps"].last().sort_index()


def _payout_ratio_3y(cash_div: pd.Series, eps_by_year: pd.Series) -> float | None:
    """近 3 年现金分红总额 ÷ 近 3 年归母净利（每股口径：ΣDPS / ΣEPS）。

    避免单期预案未披露 / 中期分红与全年利润错期导致支付率≈0。
    """
    if cash_div is None or cash_div.empty or eps_by_year is None or eps_by_year.empty:
        return None
    overlap = sorted(set(int(y) for y in cash_div.index) & set(int(y) for y in eps_by_year.index))
    if len(overlap) < 2:
        return None
    years = overlap[-AVG_YIELD_YEARS:]
    sum_dps = float(pd.to_numeric(cash_div.reindex(years), errors="coerce").fillna(0).sum())
    sum_eps = float(pd.to_numeric(eps_by_year.reindex(years), errors="coerce").fillna(0).sum())
    if sum_eps <= 0 or sum_dps <= 0:
        return None
    return sum_dps / sum_eps * 100.0


def _pick_roe_weighted(row: Any) -> float | None:
    """优先加权 ROE，摊薄列仅兜底。"""
    get = row.get if hasattr(row, "get") else lambda k, d=None: row[k] if k in row.index else d
    for col in ("加权净资产收益率(%)", "净资产收益率(%)"):
        v = get(col)
        if v is None or (isinstance(v, float) and np.isnan(v)):
            continue
        s = str(v).strip()
        if s in ("", "--", "nan", "None"):
            continue
        try:
            return float(v)
        except (TypeError, ValueError):
            continue
    return None


def _annual_roe_series(fin: pd.DataFrame) -> pd.Series:
    """年报加权 ROE（%），按财年索引（每年取该年最晚一期）。"""
    if fin is None or fin.empty or "日期" not in fin.columns:
        return pd.Series(dtype=float)
    df = fin.copy()
    df["日期"] = pd.to_datetime(df["日期"], errors="coerce")
    df = df.dropna(subset=["日期"])
    if df.empty:
        return pd.Series(dtype=float)
    ann = df[df["日期"].dt.month == 12]
    if ann.empty:
        return pd.Series(dtype=float)
    ann = ann.assign(_year=ann["日期"].dt.year)
    # 同年多行时取日期最新
    ann = ann.sort_values("日期").groupby("_year", as_index=False).tail(1)
    roes: dict[int, float] = {}
    for _, row in ann.iterrows():
        v = _pick_roe_weighted(row)
        if v is not None:
            roes[int(row["_year"])] = v
    if not roes:
        return pd.Series(dtype=float)
    return pd.Series(roes).sort_index()


def _latest_annual_fundamentals(code: str) -> dict[str, Any]:
    """年报基本面：最新 ROE、近 3 年 ROE 均值，以及 EPS / 经营现金流比率。"""
    import akshare as ak

    empty: dict[str, Any] = {
        "roe": None,
        "roe_avg_3y": None,
        "report_date": None,
        "eps": None,
        "ocf_np": None,
        "eps_by_year": pd.Series(dtype=float),
    }
    try:
        df = ak.stock_financial_analysis_indicator(symbol=code)
    except Exception:  # noqa: BLE001
        return empty
    if df is None or df.empty or "日期" not in df.columns:
        return empty

    df = df.copy()
    df["日期"] = pd.to_datetime(df["日期"], errors="coerce")
    df = df.dropna(subset=["日期"])
    if df.empty:
        return empty

    eps_by_year = _annual_eps_series(df)
    roe_by_year = _annual_roe_series(df)

    roe = None
    roe_avg_3y = None
    report_date = None
    if not roe_by_year.empty:
        recent = roe_by_year.tail(AVG_YIELD_YEARS)
        roe = float(recent.iloc[-1])
        if len(recent) >= AVG_YIELD_YEARS:
            roe_avg_3y = float(recent.mean())
        # 报告期：该最新财年对应的年报日期
        y = int(recent.index[-1])
        ann = df[(df["日期"].dt.month == 12) & (df["日期"].dt.year == y)]
        if not ann.empty:
            report_date = ann["日期"].max()

    # EPS / OCF：取最近年报行（按日期）
    annual = df[df["日期"].dt.month == 12]
    src = annual if not annual.empty else df
    row = src.loc[src["日期"].idxmax()]

    eps = _to_float(row.get("摊薄每股收益(元)"))
    if eps is None:
        eps = _to_float(row.get("加权每股收益(元)"))

    ocf_np = _to_float(row.get("经营现金净流量与净利润的比率(%)"))
    if ocf_np is not None and ocf_np > 10:
        ocf_np = ocf_np / 100.0

    return {
        "roe": roe,
        "roe_avg_3y": None if roe_avg_3y is None else round(roe_avg_3y, 2),
        "report_date": report_date.strftime("%Y-%m-%d") if report_date is not None and pd.notna(report_date) else None,
        "eps": eps,
        "ocf_np": ocf_np,
        "eps_by_year": eps_by_year,
    }


def _empty_metrics(code: str, reason: str = "分红数据不足") -> dict[str, Any]:
    return {
        "code": code,
        "avg_div_yield_3y": None,
        "avg_div_yield_5y": None,
        "last_year_yield": None,
        "consecutive_div_years": None,
        "last_year_div": None,
        "payout_ratio": None,
        "payout_soft": False,
        "roe": None,
        "roe_avg_3y": None,
        "roe_report_date": None,
        "ocf_to_np": None,
        "data_ok": False,
        "data_note": reason,
    }


def _calc_div_metrics(code: str, price: float) -> dict[str, Any]:
    """单票指标（软计算：不因未达标而丢弃，便于前端列出未命中）。"""
    import akshare as ak

    try:
        hist = ak.stock_history_dividend_detail(symbol=code, indicator="分红")
    except Exception:  # noqa: BLE001
        return _empty_metrics(code, "分红接口失败")
    cash_div = _annual_cash_dps(hist)
    if cash_div.empty:
        return _empty_metrics(code, "无现金分红记录")

    consec = _consecutive_div_years(cash_div, window=MAX_DIV_YEARS)
    years = sorted(int(y) for y in cash_div.index)
    recent = years[-AVG_YIELD_YEARS:] if len(years) >= AVG_YIELD_YEARS else years

    avg_yield_3y = None
    last_yield = None
    last_dps = None
    if price and price > 0 and recent:
        # 近3年有分红的年份均值；若不足3年仍算可得年份
        avg_yield_3y = float(cash_div[recent].mean() / price * 100.0)
        last_year = years[-1]
        last_yield = float(cash_div.loc[last_year] / price * 100.0)
        last_dps = float(cash_div.loc[last_year])

    fund = _latest_annual_fundamentals(code)
    eps_by_year = fund.get("eps_by_year")
    if not isinstance(eps_by_year, pd.Series):
        eps_by_year = pd.Series(dtype=float)
    payout = _payout_ratio_3y(cash_div, eps_by_year)
    # 单年兜底：仅当 3 年口径不可用时用最近一年 DPS/EPS
    if payout is None:
        eps = fund.get("eps")
        if eps is not None and eps > 0 and last_dps is not None:
            payout = last_dps / float(eps) * 100.0
    # 过滤新浪「股息发放率」类噪声（常为 0.0006）；本函数已不再读该字段
    if payout is not None and payout < PAYOUT_NOISE_MAX:
        payout = None

    payout_soft = False
    if payout is None and consec >= MIN_DIV_YEARS and avg_yield_3y is not None and avg_yield_3y > 0:
        # 缺失/失真时不直接判负：连续分红 + 有现金分红则支付率条件软通过
        payout_soft = True

    return {
        "code": code,
        "avg_div_yield_3y": None if avg_yield_3y is None else round(avg_yield_3y, 2),
        "avg_div_yield_5y": None if avg_yield_3y is None else round(avg_yield_3y, 2),
        "last_year_yield": None if last_yield is None else round(last_yield, 2),
        "consecutive_div_years": int(consec),
        "last_year_div": None if last_dps is None else round(last_dps, 4),
        "payout_ratio": None if payout is None else round(float(payout), 2),
        "payout_soft": payout_soft,
        "roe": None if fund.get("roe") is None else round(float(fund["roe"]), 2),
        "roe_avg_3y": (
            None if fund.get("roe_avg_3y") is None else round(float(fund["roe_avg_3y"]), 2)
        ),
        "roe_report_date": fund.get("report_date"),
        "ocf_to_np": None if fund.get("ocf_np") is None else round(float(fund["ocf_np"]), 3),
        "data_ok": True,
        "data_note": "",
    }


_SCAN_PROGRESS: dict[str, Any] = {
    "running": False,
    "phase": "",
    "planned": 0,
    "done": 0,
    "deep": 0,
    "light": 0,
}


def _set_scan_progress(**kwargs: Any) -> None:
    with _LOCK:
        _SCAN_PROGRESS.update(kwargs)


def _collect_dividend_records(df: pd.DataFrame) -> list[dict[str, Any]]:
    from concurrent.futures import ThreadPoolExecutor, as_completed

    records: list[dict[str, Any]] = []
    if df is None or df.empty or "code" not in df.columns:
        return records
    pairs = list(zip(df["code"].tolist(), df["price"].tolist()))
    workers = min(DEEP_WORKERS, max(4, len(pairs)))
    done = 0
    _set_scan_progress(running=True, phase="deep", planned=len(pairs), done=0, deep=len(pairs))
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futs = {
            pool.submit(
                _calc_div_metrics,
                str(code),
                float(price) if price is not None else 0.0,
            ): code
            for code, price in pairs
        }
        for fut in as_completed(futs):
            done += 1
            try:
                m = fut.result()
            except Exception:  # noqa: BLE001
                m = _empty_metrics(str(futs[fut]), "计算异常")
            if m:
                records.append(m)
            if done % 10 == 0 or done == len(pairs):
                _set_scan_progress(done=done)
                logger.info(
                    "高股息深扫进度 %s/%s，已回填 %s 只",
                    done,
                    len(pairs),
                    len(records),
                )
    return records


def _item_spot_reject(row: pd.Series) -> dict[str, Any]:
    """估值未过门：不打分红/财报接口，仅用行情标未命中（大幅加速大盘池）。"""
    cap = _to_float(row.get("market_cap"))
    code = str(row.get("code") or "")
    fails: list[str] = []
    pe = _to_float(row.get("pe_ttm"))
    pb = _to_float(row.get("pb"))
    if pe is None or pe <= 0 or pe > PE_MAX:
        fails.append(f"PE≤{PE_MAX:g}")
    if pb is None or pb <= 0 or pb > PB_MAX:
        fails.append(f"PB≤{PB_MAX:g}")
    if cap is None or cap < MIN_MARKET_CAP:
        fails.append(f"市值≥{MIN_MARKET_CAP / 1e8:.0f}亿")
    if not fails:
        fails.append("估值未过门")
    return {
        "code": code,
        "symbol": _to_symbol(code),
        "name": str(row.get("name") or ""),
        "price": _to_float(row.get("price")),
        "avg_div_yield_3y": None,
        "avg_div_yield_5y": None,
        "last_year_yield": None,
        "consecutive_div_years": None,
        "last_year_div": None,
        "payout_ratio": None,
        "payout_soft": False,
        "roe": None,
        "roe_avg_3y": None,
        "roe_report_date": None,
        "ocf_to_np": None,
        "pe_ttm": pe,
        "pb": pb,
        "market_cap": cap,
        "market_cap_yi": None if cap is None else round(cap / 1e8, 2),
        "passed": False,
        "fail_reasons": fails,
    }


def _item_deep_pending(row: pd.Series) -> dict[str, Any]:
    """估值过门、分红尚未算完：先上榜，标「分红计算中」。"""
    cap = _to_float(row.get("market_cap"))
    code = str(row.get("code") or "")
    return {
        "code": code,
        "symbol": _to_symbol(code),
        "name": str(row.get("name") or ""),
        "price": _to_float(row.get("price")),
        "avg_div_yield_3y": None,
        "avg_div_yield_5y": None,
        "last_year_yield": None,
        "consecutive_div_years": None,
        "last_year_div": None,
        "payout_ratio": None,
        "payout_soft": False,
        "roe": None,
        "roe_avg_3y": None,
        "roe_report_date": None,
        "ocf_to_np": None,
        "pe_ttm": _to_float(row.get("pe_ttm")),
        "pb": _to_float(row.get("pb")),
        "market_cap": cap,
        "market_cap_yi": None if cap is None else round(cap / 1e8, 2),
        "passed": False,
        "fail_reasons": ["分红计算中"],
    }


def _sort_items(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    def _sort_key(it: dict[str, Any]) -> tuple:
        y = it.get("avg_div_yield_3y")
        yv = float(y) if y is not None else -1.0
        return (0 if it.get("passed") else 1, -yv)

    items.sort(key=_sort_key)
    return items


def _report_from_items(
    items: list[dict[str, Any]],
    *,
    universe: str,
    pool_note: str,
    scanned: int,
    deep_n: int,
    light_n: int,
    partial: bool = False,
) -> dict[str, Any]:
    items = _sort_items(list(items))
    matched = sum(1 for it in items if it.get("passed"))
    rejected = len(items) - matched
    notes = _notes() + [
        pool_note,
        f"估值过门深扫 {deep_n} 只；行情未过门快标 {light_n} 只",
    ]
    if partial:
        notes.append("分红深扫进行中，已缓存行情快照；本页会自动刷新补全")
    else:
        notes.append("列表含未命中标的，并标注未通过条件")
    return {
        "count": len(items),
        "matched": matched,
        "rejected": rejected,
        "scanned": scanned,
        "deep_scanned": deep_n,
        "spot_rejected": light_n,
        "universe": universe,
        "pool_note": pool_note,
        "items": items,
        "notes": notes,
        "thresholds": _thresholds(),
        "partial": partial,
    }


def _resolve_universe_df(
    universe: str,
    limit: int | None,
) -> tuple[pd.DataFrame, str]:
    df_spot = _fetch_spot()
    if universe == "csi_div":
        codes = set(_fetch_csi_dividend_codes())
        df = df_spot[df_spot["code"].isin(codes)].copy()
        pool_note = f"中证红利({CSI_DIVIDEND_INDEX}) 成分股（含未命中，含 PE/PB 未过）"
    elif universe == "large_cap":
        cap = pd.to_numeric(df_spot["market_cap"], errors="coerce")
        df = df_spot[cap >= MIN_MARKET_CAP].copy()
        df = df.sort_values("market_cap", ascending=False)
        pool_note = "A股大盘（市值≥200亿，含未命中）"
    else:
        df = df_spot.sort_values("market_cap", ascending=False).copy()
        pool_note = "沪深 A 股（已剔北交所/ST，含未命中）"
        if limit is not None and limit > 0:
            df = df.head(int(limit)).copy()
            pool_note += f"，前 {int(limit)} 只"
    return df, pool_note


def _split_deep_light(df: pd.DataFrame) -> tuple[pd.DataFrame, pd.DataFrame]:
    deep_mask = df.apply(
        lambda r: _passes_hard_pe_pb(r.get("pe_ttm"), r.get("pb")),
        axis=1,
    )
    return df.loc[deep_mask].copy(), df.loc[~deep_mask].copy()


def _merge_deep_records(df_deep: pd.DataFrame, records: list[dict[str, Any]]) -> list[dict[str, Any]]:
    if df_deep.empty:
        return []
    if not records:
        out: list[dict[str, Any]] = []
        for _, row in df_deep.iterrows():
            stub = row.copy()
            for k, v in _empty_metrics(str(row["code"])).items():
                stub[k] = v
            out.append(_row_to_item(stub))
        return out
    df_div = pd.DataFrame(records)
    merged = df_deep.merge(df_div, on="code", how="left")
    for col, default in (
        ("data_ok", False),
        ("data_note", "分红数据不足"),
        ("avg_div_yield_3y", None),
        ("last_year_yield", None),
        ("consecutive_div_years", None),
        ("payout_ratio", None),
        ("payout_soft", False),
        ("roe", None),
        ("roe_avg_3y", None),
        ("roe_report_date", None),
        ("ocf_to_np", None),
    ):
        if col not in merged.columns:
            merged[col] = default
        else:
            if col == "data_ok":
                merged[col] = merged[col].fillna(False)
            elif col == "payout_soft":
                merged[col] = merged[col].fillna(False)
            elif col == "data_note":
                merged[col] = merged[col].fillna("分红数据不足")
    return [_row_to_item(row) for _, row in merged.iterrows()]


def _fail_reasons(row: dict[str, Any] | pd.Series) -> list[str]:
    """返回未通过条件的中文标签；空列表表示全部通过。"""
    reasons: list[str] = []
    get = row.get if hasattr(row, "get") else lambda k, d=None: row[k] if k in row.index else d

    if not get("data_ok", True):
        note = get("data_note") or "数据不足"
        return [str(note)]

    avg3 = _to_float(get("avg_div_yield_3y"))
    if avg3 is None:
        avg3 = _to_float(get("avg_div_yield_5y"))
    last_y = _to_float(get("last_year_yield"))
    consec = get("consecutive_div_years")
    payout = _to_float(get("payout_ratio"))
    pe = _to_float(get("pe_ttm"))
    pb = _to_float(get("pb"))
    roe = _to_float(get("roe"))
    roe_avg = _to_float(get("roe_avg_3y"))
    ocf = _to_float(get("ocf_to_np"))
    cap = _to_float(get("market_cap"))

    if consec is None or int(consec) < MIN_DIV_YEARS or int(consec) > MAX_DIV_YEARS:
        reasons.append(f"连续分红{MIN_DIV_YEARS}～{MAX_DIV_YEARS}年")
    if avg3 is None or avg3 < MIN_AVG_YIELD_3Y:
        reasons.append(f"近3年均息≥{MIN_AVG_YIELD_3Y:g}%")
    if last_y is None or last_y < MIN_LAST_YIELD:
        reasons.append(f"近1年息≥{MIN_LAST_YIELD:g}%")
    payout_soft = bool(get("payout_soft", False))
    if payout is None or (payout is not None and payout < PAYOUT_NOISE_MAX):
        if not payout_soft:
            reasons.append("支付率缺失")
        # 软通过：连续分红+有现金分红，不因支付率失真判负
    else:
        if payout < PAYOUT_MIN or payout > PAYOUT_MAX:
            reasons.append(f"支付率{PAYOUT_MIN:g}%～{PAYOUT_MAX:g}%")
        if payout > PAYOUT_HARD_MAX:
            reasons.append("支付率≤100%")
    # PE/PB：命中硬条件（不满足标未命中，但仍在列表展示）
    if pe is None or pe <= 0 or pe > PE_MAX:
        reasons.append(f"PE≤{PE_MAX:g}")
    if pb is None or pb <= 0 or pb > PB_MAX:
        reasons.append(f"PB≤{PB_MAX:g}")
    if roe_avg is None or roe_avg < ROE_AVG_3Y_MIN:
        reasons.append(f"近3年ROE均≥{ROE_AVG_3Y_MIN:g}%")
    if roe is None or roe < ROE_LATEST_MIN:
        reasons.append(f"最新ROE≥{ROE_LATEST_MIN:g}%")
    if ocf is None or ocf < OCF_NP_MIN:
        reasons.append(f"现金流/净利≥{OCF_NP_MIN:g}")
    if cap is None or cap < MIN_MARKET_CAP:
        reasons.append(f"市值≥{MIN_MARKET_CAP / 1e8:.0f}亿")
    return reasons


def _apply_filters(df: pd.DataFrame) -> pd.DataFrame:
    """保留全部通过条件的行（用于单测 / 兼容）。"""
    if df.empty:
        return df.copy()
    mask = df.apply(lambda r: len(_fail_reasons(r)) == 0, axis=1)
    return df[mask].copy()


def _passes_hard_pe_pb(pe: Any, pb: Any) -> bool:
    """PE/PB 命中硬门槛（须有效正值且 PE≤15、PB≤1.5）；不满足仍进列表。"""
    pe_v = _to_float(pe)
    pb_v = _to_float(pb)
    if pe_v is None or pe_v <= 0 or pe_v > PE_MAX:
        return False
    if pb_v is None or pb_v <= 0 or pb_v > PB_MAX:
        return False
    return True


def _notes() -> list[str]:
    return [
        "命中硬条件：PE≤15 且 PB≤1.5（不满足仍展示，标为未命中）",
        "近5年窗口内连续现金分红3～5年 · 近3年均息≥4% · 最近1年息≥3%",
        "股利支付率30%～80%（近3年Σ派息/ΣEPS；失真缺失时软通过）",
        "近3年年报加权ROE均值≥8% · 最新年报加权ROE≥6%",
        "经营现金流/净利润≥0.8 · 市值≥200亿；股息率=年度每股现金分红/现价",
    ]


def _thresholds() -> dict[str, float]:
    return {
        "avg_div_yield_3y_min": MIN_AVG_YIELD_3Y,
        "last_year_yield_min": MIN_LAST_YIELD,
        "consecutive_div_years_min": MIN_DIV_YEARS,
        "consecutive_div_years_max": MAX_DIV_YEARS,
        "payout_min": PAYOUT_MIN,
        "payout_max": PAYOUT_MAX,
        "pe_max": PE_MAX,
        "pb_max": PB_MAX,
        "roe_avg_3y_min": ROE_AVG_3Y_MIN,
        "roe_latest_min": ROE_LATEST_MIN,
        "ocf_to_np_min": OCF_NP_MIN,
        "market_cap_min_yi": MIN_MARKET_CAP / 1e8,
    }


def _row_to_item(row: pd.Series) -> dict[str, Any]:
    cap = _to_float(row.get("market_cap"))
    code = row.get("code")
    if code is None or (isinstance(code, float) and np.isnan(code)):
        code = ""
    code = str(code)
    avg3 = _to_float(row.get("avg_div_yield_3y"))
    if avg3 is None:
        avg3 = _to_float(row.get("avg_div_yield_5y"))
    consec = row.get("consecutive_div_years")
    fails = _fail_reasons(row)
    return {
        "code": code,
        "symbol": _to_symbol(code),
        "name": str(row.get("name") or ""),
        "price": _to_float(row.get("price")),
        "avg_div_yield_3y": avg3,
        "avg_div_yield_5y": avg3,
        "last_year_yield": _to_float(row.get("last_year_yield")),
        "consecutive_div_years": None if consec is None or (isinstance(consec, float) and np.isnan(consec)) else int(consec),
        "last_year_div": _to_float(row.get("last_year_div")),
        "payout_ratio": _to_float(row.get("payout_ratio")),
        "payout_soft": bool(row.get("payout_soft", False)),
        "roe": _to_float(row.get("roe")),
        "roe_avg_3y": _to_float(row.get("roe_avg_3y")),
        "roe_report_date": (
            None
            if row.get("roe_report_date") is None
            or (isinstance(row.get("roe_report_date"), float) and np.isnan(row.get("roe_report_date")))
            else str(row.get("roe_report_date"))
        ),
        "ocf_to_np": _to_float(row.get("ocf_to_np")),
        "pe_ttm": _to_float(row.get("pe_ttm")),
        "pb": _to_float(row.get("pb")),
        "market_cap": cap,
        "market_cap_yi": None if cap is None else round(cap / 1e8, 2),
        "passed": len(fails) == 0,
        "fail_reasons": fails,
    }


def build_quick_spot_board(
    *,
    universe: str = DEFAULT_UNIVERSE,
    limit: int | None = None,
) -> dict[str, Any]:
    """仅行情：秒级可缓存上榜（估值未过门快标 + 过门标「分红计算中」）。"""
    logger.info("高股息快照：拉取行情 universe=%s", universe)
    df, pool_note = _resolve_universe_df(universe, limit)
    if df.empty:
        _set_scan_progress(running=False, phase="", planned=0, done=0, deep=0, light=0)
        return {
            "count": 0,
            "matched": 0,
            "rejected": 0,
            "scanned": 0,
            "universe": universe,
            "pool_note": pool_note + "（初始池为空）",
            "items": [],
            "notes": _notes() + [pool_note, "初始池为空"],
            "thresholds": _thresholds(),
            "partial": True,
        }
    df_deep, df_light = _split_deep_light(df)
    _set_scan_progress(
        running=True,
        phase="quick",
        planned=len(df_deep),
        done=0,
        deep=len(df_deep),
        light=len(df_light),
    )
    items: list[dict[str, Any]] = []
    if not df_light.empty:
        items.extend(_item_spot_reject(row) for _, row in df_light.iterrows())
    if not df_deep.empty:
        items.extend(_item_deep_pending(row) for _, row in df_deep.iterrows())
    return _report_from_items(
        items,
        universe=universe,
        pool_note=pool_note,
        scanned=len(df),
        deep_n=len(df_deep),
        light_n=len(df_light),
        partial=True,
    )


def screen_high_dividend_stocks(
    *,
    universe: str = DEFAULT_UNIVERSE,
    limit: int | None = None,
    on_checkpoint: Any | None = None,
) -> dict[str, Any]:
    logger.info("高股息筛选：获取 A 股实时行情…")
    df, pool_note = _resolve_universe_df(universe, limit)

    if df.empty:
        logger.warning("高股息筛选：初始池为空（行情源可能无可用价）")
        _set_scan_progress(running=False, phase="", planned=0, done=0, deep=0, light=0)
        return {
            "count": 0,
            "matched": 0,
            "rejected": 0,
            "scanned": 0,
            "universe": universe,
            "pool_note": pool_note + "（初始池为空）",
            "items": [],
            "notes": _notes() + [pool_note, "初始池为空：东财不可用且新浪现价为0时请稍后重试"],
            "thresholds": _thresholds(),
            "partial": False,
        }

    scanned = len(df)
    df_deep, df_light = _split_deep_light(df)
    _set_scan_progress(
        running=True,
        phase="split",
        planned=len(df_deep),
        done=0,
        deep=len(df_deep),
        light=len(df_light),
    )
    logger.info(
        "高股息筛选：池 %s，估值过门深扫 %s，行情未过门快标 %s",
        scanned,
        len(df_deep),
        len(df_light),
    )

    light_items = (
        [_item_spot_reject(row) for _, row in df_light.iterrows()] if not df_light.empty else []
    )

    def _checkpoint(records: list[dict[str, Any]], done: int, total: int) -> None:
        if on_checkpoint is None:
            return
        # 已完成的深扫票 + 未完成的 pending + 行情未过门
        done_codes = {str(r.get("code")) for r in records}
        pending_rows = df_deep[~df_deep["code"].astype(str).isin(done_codes)]
        deep_done = _merge_deep_records(df_deep[df_deep["code"].astype(str).isin(done_codes)], records)
        deep_pend = [_item_deep_pending(row) for _, row in pending_rows.iterrows()]
        board = _report_from_items(
            light_items + deep_done + deep_pend,
            universe=universe,
            pool_note=pool_note,
            scanned=scanned,
            deep_n=len(df_deep),
            light_n=len(df_light),
            partial=done < total,
        )
        try:
            on_checkpoint(board)
        except Exception:  # noqa: BLE001
            logger.debug("高股息 checkpoint 回调失败", exc_info=True)

    records: list[dict[str, Any]] = []
    if not df_deep.empty:
        # 带进度的深扫
        from concurrent.futures import ThreadPoolExecutor, as_completed

        pairs = list(zip(df_deep["code"].tolist(), df_deep["price"].tolist()))
        workers = min(DEEP_WORKERS, max(4, len(pairs)))
        _set_scan_progress(running=True, phase="deep", planned=len(pairs), done=0, deep=len(pairs))
        done = 0
        with ThreadPoolExecutor(max_workers=workers) as pool:
            futs = {
                pool.submit(
                    _calc_div_metrics,
                    str(code),
                    float(price) if price is not None else 0.0,
                ): code
                for code, price in pairs
            }
            for fut in as_completed(futs):
                done += 1
                try:
                    m = fut.result()
                except Exception:  # noqa: BLE001
                    m = _empty_metrics(str(futs[fut]), "计算异常")
                if m:
                    records.append(m)
                _set_scan_progress(done=done)
                if done % DEEP_CHECKPOINT_EVERY == 0 or done == len(pairs):
                    logger.info("高股息深扫进度 %s/%s", done, len(pairs))
                    _checkpoint(records, done, len(pairs))

    deep_items = _merge_deep_records(df_deep, records)
    _set_scan_progress(running=False, phase="done", done=len(df_deep))
    return _report_from_items(
        light_items + deep_items,
        universe=universe,
        pool_note=pool_note,
        scanned=scanned,
        deep_n=len(df_deep),
        light_n=len(df_light),
        partial=False,
    )


def _to_symbol(code: str) -> str:
    c = str(code).zfill(6)
    if c.startswith(("5", "6", "9")):
        return f"{c}.SH"
    return f"{c}.SZ"


def _disk_path(cache_key: str) -> Path:
    safe = "".join(ch if ch.isalnum() or ch in "-_" else "_" for ch in cache_key)
    return _DISK_CACHE_DIR / f"{safe}.json"


def _load_disk(cache_key: str) -> tuple[dict[str, Any] | None, float]:
    path = _disk_path(cache_key)
    if not path.is_file():
        return None, 0.0
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
        ts = float(payload.get("ts") or 0.0)
        data = payload.get("data")
        if not isinstance(data, dict):
            return None, 0.0
        return data, ts
    except Exception:  # noqa: BLE001
        logger.warning("高股息磁盘缓存读取失败：%s", path, exc_info=True)
        return None, 0.0


def _save_disk(cache_key: str, data: dict[str, Any], ts: float | None = None) -> None:
    path = _disk_path(cache_key)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        payload = {"ts": float(ts if ts is not None else time.time()), "key": cache_key, "data": data}
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
        tmp.replace(path)
    except Exception:  # noqa: BLE001
        logger.warning("高股息磁盘缓存写入失败：%s", path, exc_info=True)


def _slice_report(data: dict[str, Any], top: int) -> dict[str, Any]:
    out = dict(data)
    items = list(out.get("items") or [])
    matched_all = sum(1 for it in items if it.get("passed"))
    rejected_all = len(items) - matched_all
    top_n = max(1, min(int(top), 500))
    passed = [it for it in items if it.get("passed")]
    failed = [it for it in items if not it.get("passed")]
    if len(passed) >= top_n:
        shown = passed[:top_n]
    else:
        shown = passed + failed[: max(0, top_n - len(passed))]
    out["items"] = shown
    out["count"] = len(shown)
    out["matched"] = matched_all
    out["rejected"] = rejected_all
    out["total_matched"] = matched_all
    return out


def _pool_wait_hint(universe: str) -> str:
    if universe == "large_cap":
        return "A股大盘约 600～700 只：先按 PE/PB 快筛，仅估值过门者拉分红（通常 1～3 分钟）"
    if universe == "csi_div":
        return f"中证红利({CSI_DIVIDEND_INDEX})约 100 只，约 1～2 分钟"
    return "沪深演示池，约 1～2 分钟"


def _computing_stub(universe: str, pool_note: str = "") -> dict[str, Any]:
    if pool_note:
        note = pool_note
    elif universe == "csi_div":
        note = f"中证红利({CSI_DIVIDEND_INDEX}) 成分股"
    elif universe == "large_cap":
        note = "A股大盘（市值≥200亿）"
    else:
        note = "沪深 A 股"
    with _LOCK:
        prog = dict(_SCAN_PROGRESS)
    return {
        "count": 0,
        "matched": 0,
        "rejected": 0,
        "scanned": 0,
        "universe": universe,
        "pool_note": note,
        "items": [],
        "notes": _notes() + [note, _pool_wait_hint(universe), "后台筛选中，请稍候自动刷新"],
        "thresholds": _thresholds(),
        "progress": prog,
        "wait_hint": _pool_wait_hint(universe),
    }


def _store_result(
    cache_key: str,
    data: dict[str, Any],
    *,
    keep_refreshing: bool = False,
) -> None:
    now = time.time()
    with _LOCK:
        _CACHE.update(ts=now, key=cache_key, data=data)
        if keep_refreshing or data.get("partial"):
            _REFRESHING.add(cache_key)
        else:
            _REFRESHING.discard(cache_key)
    _save_disk(cache_key, data, ts=now)


def _schedule_refresh(cache_key: str, universe: str, limit: int | None) -> bool:
    """单飞后台刷新；已在跑则返回 False。

    先写行情快照（立刻有列表可看），再深扫分红并阶段性落盘。
    """
    with _LOCK:
        if cache_key in _REFRESHING:
            return False
        _REFRESHING.add(cache_key)

    def _job() -> None:
        try:
            logger.info("高股息后台筛选开始 key=%s", cache_key)
            # ① 行情快照：数秒内可缓存，避免页面一直空
            try:
                quick = build_quick_spot_board(universe=universe, limit=limit)
                if quick.get("items"):
                    _store_result(cache_key, quick, keep_refreshing=True)
                    logger.info(
                        "高股息快照已缓存 key=%s scanned=%s deep=%s",
                        cache_key,
                        quick.get("scanned"),
                        quick.get("deep_scanned"),
                    )
            except Exception:  # noqa: BLE001
                logger.exception("高股息快照失败 key=%s，继续尝试全量", cache_key)

            def _ckpt(board: dict[str, Any]) -> None:
                _store_result(cache_key, board, keep_refreshing=True)

            data = screen_high_dividend_stocks(
                universe=universe,
                limit=limit,
                on_checkpoint=_ckpt,
            )
            _store_result(cache_key, data, keep_refreshing=False)
            logger.info(
                "高股息后台筛选完成 key=%s matched=%s scanned=%s",
                cache_key,
                data.get("matched"),
                data.get("scanned"),
            )
        except Exception:  # noqa: BLE001
            logger.exception("高股息后台筛选失败 key=%s", cache_key)
            with _LOCK:
                _REFRESHING.discard(cache_key)

    threading.Thread(target=_job, name=f"hd-refresh-{cache_key}", daemon=True).start()
    return True


def scan_high_dividend(
    *,
    universe: str = DEFAULT_UNIVERSE,
    limit: int | None = None,
    refresh: bool = False,
    top: int = 50,
) -> dict[str, Any]:
    """HTTP 路径不阻塞全量 AkShare 扫描（避免 Cloudflare 524）。

    - 内存/磁盘有结果：立刻返回；过期或 refresh 时后台单飞重算
    - 冷启动无缓存：立刻返回 status=computing，后台计算，前端轮询
    """
    uni = universe if universe in UNIVERSES else DEFAULT_UNIVERSE
    # large_cap / csi_div：池内全量扫描；all：可截前 N
    if uni in ("large_cap", "csi_div"):
        lim = None
    else:
        lim = limit if limit and limit > 0 else 200
    cache_key = f"{CACHE_VERSION}:{uni}:{lim}"
    now = time.time()

    mem_data: dict[str, Any] | None = None
    mem_ts = 0.0
    refreshing = False
    with _LOCK:
        if _CACHE["data"] is not None and _CACHE["key"] == cache_key:
            mem_data = dict(_CACHE["data"])
            mem_ts = float(_CACHE["ts"] or 0.0)
        refreshing = cache_key in _REFRESHING

    disk_data, disk_ts = (None, 0.0)
    if mem_data is None:
        disk_data, disk_ts = _load_disk(cache_key)
        if disk_data is not None:
            with _LOCK:
                # 重启后灌回内存，后续请求免读盘
                if _CACHE["data"] is None or _CACHE["key"] != cache_key:
                    _CACHE.update(ts=disk_ts, key=cache_key, data=disk_data)
            mem_data, mem_ts = disk_data, disk_ts

    fresh = mem_data is not None and (now - mem_ts) < CACHE_TTL_SEC
    need_refresh = refresh or not fresh

    with _LOCK:
        prog = dict(_SCAN_PROGRESS)

    if mem_data is not None:
        partial = bool(mem_data.get("partial"))
        if (need_refresh or partial) and not refreshing:
            _schedule_refresh(cache_key, uni, lim)
            refreshing = True
        out = _slice_report(mem_data, top)
        out["cached"] = fresh and not refresh and not partial
        out["stale"] = not fresh
        out["partial"] = partial
        out["status"] = (
            "refreshing"
            if (need_refresh or refreshing or partial)
            else "ready"
        )
        out["progress"] = prog
        out["wait_hint"] = _pool_wait_hint(uni)
        return out

    # 冷启动：不阻塞，后台算
    started = _schedule_refresh(cache_key, uni, lim)
    out = _slice_report(_computing_stub(uni), top)
    out["cached"] = False
    out["stale"] = False
    out["status"] = "computing"
    out["refresh_started"] = started
    out["progress"] = prog
    out["wait_hint"] = _pool_wait_hint(uni)
    return out


def invalidate_cache() -> None:
    with _LOCK:
        _CACHE.update(ts=0.0, key=None, data=None)
        _REFRESHING.clear()
    try:
        if _DISK_CACHE_DIR.is_dir():
            for p in _DISK_CACHE_DIR.glob("*.json"):
                p.unlink(missing_ok=True)
    except Exception:  # noqa: BLE001
        logger.warning("清理高股息磁盘缓存失败", exc_info=True)
