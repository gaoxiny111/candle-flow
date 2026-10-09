"""高股息股票筛选器（AkShare）。

口径（用户指定）：
  - 近 5 年窗口内连续现金分红 3～5 年（窗口内 streak ∈ [3, 5]）
  - 近 3 年平均股息率 ≥ 4%
  - 最近 1 年股息率 ≥ 3%
  - 股利支付率 30%～80%，且 ≤ 100%
  - PE(TTM) ≤ 15、PB ≤ 1.5
  - ROE（最近年报）≥ 10%
  - 经营现金流净额 / 净利润 ≥ 0.8
  - 总市值 ≥ 200 亿

初始池默认中证红利（000922）；``universe=all`` 可扫沪深市值前 N。
"""

from __future__ import annotations

import logging
import threading
import time
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
CACHE_VERSION = "v4"

_CACHE: dict[str, Any] = {"ts": 0.0, "key": None, "data": None}
_LOCK = threading.Lock()

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
    import akshare as ak

    spot = ak.stock_zh_a_spot()
    spot = spot.rename(columns={"代码": "code", "名称": "name", "最新价": "price"})
    spot["code"] = spot["code"].map(_normalize_code)
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
    """按年度聚合现金分红（元/股）。新浪 ``派息`` = 每 10 股派息。"""
    if hist is None or hist.empty:
        return pd.Series(dtype=float)

    df = hist.copy()
    if "每股分红" in df.columns:
        dps = pd.to_numeric(df["每股分红"], errors="coerce")
    elif "派息" in df.columns:
        dps = pd.to_numeric(df["派息"], errors="coerce") / 10.0
    else:
        return pd.Series(dtype=float)

    if "报告期" in df.columns:
        year = pd.to_datetime(df["报告期"], errors="coerce").dt.year
    elif "公告日期" in df.columns:
        year = pd.to_datetime(df["公告日期"], errors="coerce").dt.year
    elif "除权除息日" in df.columns:
        year = pd.to_datetime(df["除权除息日"], errors="coerce").dt.year
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


def _latest_annual_fundamentals(code: str) -> dict[str, float | None]:
    """最近年报：ROE、EPS、经营现金流/净利润、股息发放率（若有）。"""
    import akshare as ak

    empty = {"roe": None, "eps": None, "ocf_np": None, "payout_api": None}
    try:
        df = ak.stock_financial_analysis_indicator(symbol=code)
    except Exception:  # noqa: BLE001
        return empty
    if df is None or df.empty or "日期" not in df.columns:
        return empty

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

    payout_api = _to_float(row.get("股息发放率(%)"))

    return {"roe": roe, "eps": eps, "ocf_np": ocf_np, "payout_api": payout_api}


def _empty_metrics(code: str, reason: str = "分红数据不足") -> dict[str, Any]:
    return {
        "code": code,
        "avg_div_yield_3y": None,
        "avg_div_yield_5y": None,
        "last_year_yield": None,
        "consecutive_div_years": None,
        "last_year_div": None,
        "payout_ratio": None,
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
    payout = fund.get("payout_api")
    eps = fund.get("eps")
    if payout is None and eps is not None and eps > 0 and last_dps is not None:
        payout = last_dps / eps * 100.0

    return {
        "code": code,
        "avg_div_yield_3y": None if avg_yield_3y is None else round(avg_yield_3y, 2),
        "avg_div_yield_5y": None if avg_yield_3y is None else round(avg_yield_3y, 2),
        "last_year_yield": None if last_yield is None else round(last_yield, 2),
        "consecutive_div_years": int(consec),
        "last_year_div": None if last_dps is None else round(last_dps, 4),
        "payout_ratio": None if payout is None else round(float(payout), 2),
        "roe": None if fund.get("roe") is None else round(float(fund["roe"]), 2),
        "ocf_to_np": None if fund.get("ocf_np") is None else round(float(fund["ocf_np"]), 3),
        "data_ok": True,
        "data_note": "",
    }


def _collect_dividend_records(df: pd.DataFrame) -> list[dict[str, Any]]:
    from concurrent.futures import ThreadPoolExecutor, as_completed

    records: list[dict[str, Any]] = []
    pairs = list(zip(df["code"].tolist(), df["price"].tolist()))
    workers = 6
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
    if payout is None:
        reasons.append("支付率缺失")
    else:
        if payout < PAYOUT_MIN or payout > PAYOUT_MAX:
            reasons.append(f"支付率{PAYOUT_MIN:g}%～{PAYOUT_MAX:g}%")
        if payout > PAYOUT_HARD_MAX:
            reasons.append("支付率≤100%")
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


def _notes() -> list[str]:
    return [
        "近5年窗口内连续现金分红3～5年 · 近3年均息≥4% · 最近1年息≥3%",
        "股利支付率30%～80%（且≤100%）· PE≤15 · PB≤1.5 · ROE≥10%",
        "经营现金流/净利润≥0.8 · 总市值≥200亿",
        "股息率=年度每股现金分红/现价；支付率优先股息发放率，缺失则用派息/EPS",
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
    code = str(row["code"])
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
        pool_note = f"中证红利({CSI_DIVIDEND_INDEX}) 成分股（含未命中）"
    else:
        df = df_spot.sort_values("market_cap", ascending=False).copy()
        pool_note = "沪深 A 股（已剔北交所/ST，含未命中）"
        if limit is not None and limit > 0:
            df = df.head(int(limit)).copy()
            pool_note += f"，前 {int(limit)} 只"

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
        ("roe", None),
        ("ocf_to_np", None),
    ):
        if col not in merged.columns:
            merged[col] = default
        else:
            if col == "data_ok":
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


def scan_high_dividend(
    *,
    universe: str = "csi_div",
    limit: int | None = None,
    refresh: bool = False,
    top: int = 50,
) -> dict[str, Any]:
    uni = universe if universe in ("csi_div", "all") else "csi_div"
    lim = None if uni == "csi_div" else (limit if limit and limit > 0 else 200)
    cache_key = f"{CACHE_VERSION}:{uni}:{lim}"

    with _LOCK:
        cached = (
            not refresh
            and _CACHE["data"] is not None
            and _CACHE["key"] == cache_key
            and time.time() - float(_CACHE["ts"]) < CACHE_TTL_SEC
        )
        data = dict(_CACHE["data"]) if cached else None

    if data is None:
        data = screen_high_dividend_stocks(universe=uni, limit=lim)
        with _LOCK:
            _CACHE.update(ts=time.time(), key=cache_key, data=data)
        cached = False

    out = dict(data)
    items = list(out.get("items") or [])
    matched_all = sum(1 for it in items if it.get("passed"))
    rejected_all = len(items) - matched_all
    top_n = max(1, min(int(top), 500))
    # 条数上限：优先保留全部命中，再补未命中至 top
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
    out["cached"] = cached
    return out


def invalidate_cache() -> None:
    with _LOCK:
        _CACHE.update(ts=0.0, key=None, data=None)
