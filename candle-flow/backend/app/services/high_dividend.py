"""高股息股票筛选器（AkShare）。

口径（用户指定）：
  - 近 5 年窗口内连续现金分红 3～5 年（窗口内 streak ∈ [3, 5]）
  - 近 3 年平均股息率 ≥ 4%
  - 最近 1 年股息率 ≥ 3%
  - 股利支付率 30%～80%，且 ≤ 100%
  - PE(TTM) ≤ 15、PB ≤ 1.5（命中硬条件；不满足仍展示为未命中）
  - ROE（最近年报）≥ 10%
  - 经营现金流净额 / 净利润 ≥ 0.8
  - 总市值 ≥ 200 亿

初始池默认中证红利（000922）；``universe=all`` 可扫沪深市值前 N。
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
ROE_MIN = 10.0
OCF_NP_MIN = 0.8
MIN_MARKET_CAP = 200e8  # 200 亿
AVG_YIELD_YEARS = 3

CSI_DIVIDEND_INDEX = "000922"
CACHE_TTL_SEC = 6 * 3600
CACHE_VERSION = "v9"
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


def _latest_annual_fundamentals(code: str) -> dict[str, Any]:
    """最近年报：ROE、经营现金流/净利润、年报 EPS 序列。"""
    import akshare as ak

    empty: dict[str, Any] = {
        "roe": None,
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

    eps_by_year = _annual_eps_series(df)
    ann = df[df["日期"].astype(str).str.contains(r"-12-31", regex=True)].copy()
    if ann.empty:
        ann = df.copy()
    row = ann.iloc[-1]

    roe = _to_float(row.get("净资产收益率(%)"))
    if roe is None:
        roe = _to_float(row.get("加权净资产收益率(%)"))

    eps = _to_float(row.get("摊薄每股收益(元)"))
    if eps is None:
        eps = _to_float(row.get("加权每股收益(元)"))

    ocf_np = _to_float(row.get("经营现金净流量与净利润的比率(%)"))
    # 接口列名带 (%)，实际多为比率（约 0.5~2）；若像百分数则折算
    if ocf_np is not None and ocf_np > 10:
        ocf_np = ocf_np / 100.0

    return {
        "roe": roe,
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
        "ocf_to_np": None if fund.get("ocf_np") is None else round(float(fund["ocf_np"]), 3),
        "data_ok": True,
        "data_note": "",
    }


def _collect_dividend_records(df: pd.DataFrame) -> list[dict[str, Any]]:
    from concurrent.futures import ThreadPoolExecutor, as_completed

    records: list[dict[str, Any]] = []
    if df is None or df.empty or "code" not in df.columns:
        return records
    pairs = list(zip(df["code"].tolist(), df["price"].tolist()))
    workers = min(12, max(4, len(pairs)))
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
            if done % 10 == 0:
                logger.info(
                    "高股息筛选进度 %s/%s，已回填 %s 只",
                    done,
                    len(pairs),
                    len(records),
                )
    return records


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
    if roe is None or roe < ROE_MIN:
        reasons.append(f"ROE≥{ROE_MIN:g}%")
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
        "股利支付率30%～80%（近3年Σ派息/ΣEPS；失真缺失时软通过）· ROE≥10%",
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
        "roe_min": ROE_MIN,
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
        "ocf_to_np": _to_float(row.get("ocf_to_np")),
        "pe_ttm": _to_float(row.get("pe_ttm")),
        "pb": _to_float(row.get("pb")),
        "market_cap": cap,
        "market_cap_yi": None if cap is None else round(cap / 1e8, 2),
        "passed": len(fails) == 0,
        "fail_reasons": fails,
    }


def screen_high_dividend_stocks(
    *,
    universe: str = "csi_div",
    limit: int | None = None,
) -> dict[str, Any]:
    logger.info("高股息筛选：获取 A 股实时行情…")
    df_spot = _fetch_spot()

    if universe == "csi_div":
        codes = set(_fetch_csi_dividend_codes())
        df = df_spot[df_spot["code"].isin(codes)].copy()
        pool_note = f"中证红利({CSI_DIVIDEND_INDEX}) 成分股（含未命中，含 PE/PB 未过）"
    else:
        df = df_spot.sort_values("market_cap", ascending=False).copy()
        pool_note = "沪深 A 股（已剔北交所/ST，含未命中）"
        if limit is not None and limit > 0:
            df = df.head(int(limit)).copy()
            pool_note += f"，前 {int(limit)} 只"

    if df.empty:
        # 空表布尔索引在部分 pandas 版本会丢掉列名 → 后续 KeyError('code')
        logger.warning("高股息筛选：初始池为空（行情源可能无可用价）")
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
        }

    logger.info("高股息筛选：计算股息/支付率/ROE/OCF，候选 %s 只…", len(df))
    records = _collect_dividend_records(df)
    scanned = len(df)

    empty = {
        "count": 0,
        "matched": 0,
        "rejected": 0,
        "scanned": scanned,
        "universe": universe,
        "pool_note": pool_note,
        "items": [],
        "notes": _notes() + [pool_note, "本次无有效记录可合并"],
        "thresholds": _thresholds(),
    }
    df_div = pd.DataFrame(records)
    if df_div.empty:
        # 仍列出池内标的，全部记为数据不足
        items = []
        for _, row in df.iterrows():
            stub = row.copy()
            for k, v in _empty_metrics(str(row["code"])).items():
                stub[k] = v
            items.append(_row_to_item(stub))
        empty["items"] = items
        empty["count"] = len(items)
        empty["rejected"] = len(items)
        return empty

    merged = df.merge(df_div, on="code", how="left")
    # merge 后分红字段可能为 NaN → 补空指标
    for col, default in (
        ("data_ok", False),
        ("data_note", "分红数据不足"),
        ("avg_div_yield_3y", None),
        ("last_year_yield", None),
        ("consecutive_div_years", None),
        ("payout_ratio", None),
        ("payout_soft", False),
        ("roe", None),
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

    items = [_row_to_item(row) for _, row in merged.iterrows()]
    # 命中在前，再按近3年均息降序；未命中按均息降序
    def _sort_key(it: dict[str, Any]) -> tuple:
        y = it.get("avg_div_yield_3y")
        yv = float(y) if y is not None else -1.0
        return (0 if it.get("passed") else 1, -yv)

    items.sort(key=_sort_key)
    matched = sum(1 for it in items if it.get("passed"))
    rejected = len(items) - matched

    return {
        "count": len(items),
        "matched": matched,
        "rejected": rejected,
        "scanned": scanned,
        "universe": universe,
        "pool_note": pool_note,
        "items": items,
        "notes": _notes() + [pool_note, "列表含未命中标的，并标注未通过条件"],
        "thresholds": _thresholds(),
    }


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


def _computing_stub(universe: str, pool_note: str = "") -> dict[str, Any]:
    note = pool_note or (
        f"中证红利({CSI_DIVIDEND_INDEX}) 成分股"
        if universe == "csi_div"
        else "沪深 A 股"
    )
    return {
        "count": 0,
        "matched": 0,
        "rejected": 0,
        "scanned": 0,
        "universe": universe,
        "pool_note": note,
        "items": [],
        "notes": _notes() + [note, "后台筛选中，请稍候自动刷新"],
        "thresholds": _thresholds(),
    }


def _store_result(cache_key: str, data: dict[str, Any]) -> None:
    now = time.time()
    with _LOCK:
        _CACHE.update(ts=now, key=cache_key, data=data)
        _REFRESHING.discard(cache_key)
    _save_disk(cache_key, data, ts=now)


def _schedule_refresh(cache_key: str, universe: str, limit: int | None) -> bool:
    """单飞后台刷新；已在跑则返回 False。"""
    with _LOCK:
        if cache_key in _REFRESHING:
            return False
        _REFRESHING.add(cache_key)

    def _job() -> None:
        try:
            logger.info("高股息后台筛选开始 key=%s", cache_key)
            data = screen_high_dividend_stocks(universe=universe, limit=limit)
            _store_result(cache_key, data)
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
    universe: str = "csi_div",
    limit: int | None = None,
    refresh: bool = False,
    top: int = 50,
) -> dict[str, Any]:
    """HTTP 路径不阻塞全量 AkShare 扫描（避免 Cloudflare 524）。

    - 内存/磁盘有结果：立刻返回；过期或 refresh 时后台单飞重算
    - 冷启动无缓存：立刻返回 status=computing，后台计算，前端轮询
    """
    uni = universe if universe in ("csi_div", "all") else "csi_div"
    lim = None if uni == "csi_div" else (limit if limit and limit > 0 else 200)
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

    if mem_data is not None:
        if need_refresh and not refreshing:
            _schedule_refresh(cache_key, uni, lim)
            refreshing = True
        out = _slice_report(mem_data, top)
        out["cached"] = fresh and not refresh
        out["stale"] = not fresh
        out["status"] = "refreshing" if (need_refresh or refreshing) else "ready"
        return out

    # 冷启动：不阻塞，后台算
    started = _schedule_refresh(cache_key, uni, lim)
    out = _slice_report(_computing_stub(uni), top)
    out["cached"] = False
    out["stale"] = False
    out["status"] = "computing"
    out["refresh_started"] = started
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
