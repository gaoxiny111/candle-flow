"""高股息股票筛选器（AkShare 量化框架）。

核心逻辑对齐用户给定脚本：
  1. 数据获取：``stock_zh_a_spot_em`` 行情 + ``stock_history_dividend_detail`` 分红
  2. 指标计算：近 5 年平均股息率、分红年份数
  3. 条件过滤：息≥4% ∧ 分红年≥3 ∧ PE∈(0,30) ∧ PB∈(0,5) ∧ 市值>50 亿

性能：默认先取中证红利（000922）成分股作初始池（用户建议的优化点）；
``universe=all`` 时可扫全 A（可用 ``limit`` 截断，避免超时）。
"""

from __future__ import annotations

import logging
import threading
import time
from typing import Any

import numpy as np
import pandas as pd

logger = logging.getLogger(__name__)

# ── 筛选阈值（与用户脚本一致）──────────────────────────────────────
MIN_AVG_YIELD_5Y = 4.0
MIN_DIV_YEARS = 3
PE_MIN, PE_MAX = 0.0, 30.0
PB_MIN, PB_MAX = 0.0, 5.0
MIN_MARKET_CAP = 50e8  # 50 亿
DIV_LOOKBACK_YEARS = 5

CSI_DIVIDEND_INDEX = "000922"  # 中证红利
CACHE_TTL_SEC = 6 * 3600

_CACHE: dict[str, Any] = {"ts": 0.0, "key": None, "data": None}
_LOCK = threading.Lock()


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


def _spot_from_em() -> pd.DataFrame:
    """用户脚本主路径：东财全 A 快照（含 PE/PB/总市值）。"""
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
    """东财不可用时的回退：新浪价 + 本地因子快照补 PE/PB/市值。"""
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
        # 无因子时无法做 PE/PB/市值过滤，置空让后续自然筛掉
        spot["pe_ttm"] = np.nan
        spot["pb"] = np.nan
        spot["market_cap"] = np.nan
        return spot
    fac = pd.DataFrame(rows)
    fac["code"] = fac["code"].astype(str).str.zfill(6)
    return spot.merge(fac, on="code", how="left")


def _fetch_spot() -> pd.DataFrame:
    """第一步：A 股实时行情（含 PE / PB / 总市值）。优先东财，失败回退新浪+因子库。"""
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
    # 过滤 ST、退市、B 股；排除停牌/无价
    df = df[~df["name"].astype(str).str.contains("ST|退", regex=True, na=False)].copy()
    df = df[~df["name"].astype(str).str.contains(r"B$|Ｂ", regex=True, na=False)].copy()
    df = df[pd.to_numeric(df["price"], errors="coerce").fillna(0) > 0]
    for col in ("price", "pe_ttm", "pb", "market_cap"):
        df[col] = pd.to_numeric(df[col], errors="coerce")
    return df.drop_duplicates(subset=["code"]).reset_index(drop=True)


def _fetch_csi_dividend_codes() -> list[str]:
    """中证红利成分股代码（生产默认初始池）。"""
    import akshare as ak

    cons = ak.index_stock_cons_csindex(symbol=CSI_DIVIDEND_INDEX)
    col = "成分券代码" if "成分券代码" in cons.columns else cons.columns[4]
    return [str(c).zfill(6) for c in cons[col].tolist()]


def _annual_cash_dps(hist: pd.DataFrame) -> pd.Series:
    """按年度聚合现金分红（元/股）。

    新浪口径 ``派息`` = 每 10 股派息（元）；兼容旧列名 ``每股分红`` / ``报告期``。
    """
    if hist is None or hist.empty:
        return pd.Series(dtype=float)

    df = hist.copy()
    if "每股分红" in df.columns:
        dps_col = "每股分红"
        dps = pd.to_numeric(df[dps_col], errors="coerce")
    elif "派息" in df.columns:
        # 每 10 股派息 → 每股
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
    # 只统计现金分红 > 0 的年度
    tmp = tmp[tmp["dps"] > 0]
    if tmp.empty:
        return pd.Series(dtype=float)
    return tmp.groupby("year")["dps"].sum().sort_index()


def _calc_div_metrics(code: str, price: float) -> dict[str, Any] | None:
    """第二步：单只分红指标（近 5 年平均股息率 + 分红年份数）。"""
    import akshare as ak

    try:
        hist = ak.stock_history_dividend_detail(symbol=code, indicator="分红")
    except Exception:  # noqa: BLE001 — 单只失败跳过
        return None
    cash_div = _annual_cash_dps(hist)
    if cash_div.empty:
        return None
    recent_years = sorted(cash_div.index)[-DIV_LOOKBACK_YEARS:]
    if len(recent_years) < MIN_DIV_YEARS:
        return None
    if price is None or price <= 0:
        return None
    avg_yield_5y = float(cash_div[recent_years].mean() / price * 100.0)
    return {
        "code": code,
        "avg_div_yield_5y": round(avg_yield_5y, 2),
        "consecutive_div_years": int(len(recent_years)),
        "last_year_div": round(float(cash_div.iloc[-1]), 4),
    }


def _collect_dividend_records(df: pd.DataFrame) -> list[dict[str, Any]]:
    """并行拉取分红（保持与用户脚本相同的指标口径）。"""
    from concurrent.futures import ThreadPoolExecutor, as_completed

    records: list[dict[str, Any]] = []
    pairs = list(zip(df["code"].tolist(), df["price"].tolist()))
    workers = 8
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
                m = None
            if m:
                records.append(m)
            if done % 20 == 0:
                logger.info(
                    "高股息筛选进度 %s/%s，已取到分红 %s 只",
                    done,
                    len(pairs),
                    len(records),
                )
    return records


def _apply_filters(df: pd.DataFrame) -> pd.DataFrame:
    """第三步：多条件筛选（与用户脚本一致）。"""
    return df[
        (df["avg_div_yield_5y"] >= MIN_AVG_YIELD_5Y)
        & (df["consecutive_div_years"] >= MIN_DIV_YEARS)
        & (df["pe_ttm"] > PE_MIN)
        & (df["pe_ttm"] < PE_MAX)
        & (df["pb"] > PB_MIN)
        & (df["pb"] < PB_MAX)
        & (df["market_cap"] > MIN_MARKET_CAP)
    ].copy()


def screen_high_dividend_stocks(
    *,
    universe: str = "csi_div",
    limit: int | None = None,
) -> dict[str, Any]:
    """
    高股息股票筛选器。

    universe:
      - ``csi_div``：中证红利成分股（默认，推荐）
      - ``all``：全 A（可用 limit 截断，演示/调试）
    """
    logger.info("高股息筛选：获取 A 股实时行情…")
    df_spot = _fetch_spot()

    if universe == "csi_div":
        codes = set(_fetch_csi_dividend_codes())
        df = df_spot[df_spot["code"].isin(codes)].copy()
        pool_note = f"中证红利({CSI_DIVIDEND_INDEX}) 成分股"
    else:
        df = df_spot.copy()
        pool_note = "全 A（已剔 ST/退/B）"
        if limit is not None and limit > 0:
            df = df.head(int(limit)).copy()
            pool_note += f"，limit={int(limit)}"

    logger.info("高股息筛选：计算股息率与分红连续性，候选 %s 只…", len(df))
    records = _collect_dividend_records(df)
    scanned = len(df)

    df_div = pd.DataFrame(records)
    if df_div.empty:
        return {
            "count": 0,
            "scanned": scanned,
            "universe": universe,
            "pool_note": pool_note,
            "items": [],
            "notes": [
                "近5年平均股息率≥4%、连续分红年数≥3、PE∈(0,30)、PB∈(0,5)、市值>50亿",
                pool_note,
                "本次无有效分红记录可合并",
            ],
            "thresholds": {
                "avg_div_yield_5y_min": MIN_AVG_YIELD_5Y,
                "consecutive_div_years_min": MIN_DIV_YEARS,
                "pe_max": PE_MAX,
                "pb_max": PB_MAX,
                "market_cap_min_yi": MIN_MARKET_CAP / 1e8,
            },
        }

    merged = df.merge(df_div, on="code", how="inner")
    filtered = _apply_filters(merged)
    result = filtered.sort_values("avg_div_yield_5y", ascending=False).reset_index(drop=True)

    items: list[dict[str, Any]] = []
    for _, row in result.iterrows():
        cap = _to_float(row.get("market_cap"))
        code = str(row["code"])
        items.append(
            {
                "code": code,
                "symbol": _to_symbol(code),
                "name": str(row.get("name") or ""),
                "price": _to_float(row.get("price")),
                "avg_div_yield_5y": _to_float(row.get("avg_div_yield_5y")),
                "consecutive_div_years": int(row["consecutive_div_years"]),
                "last_year_div": _to_float(row.get("last_year_div")),
                "pe_ttm": _to_float(row.get("pe_ttm")),
                "pb": _to_float(row.get("pb")),
                "market_cap": cap,
                "market_cap_yi": None if cap is None else round(cap / 1e8, 2),
            }
        )

    return {
        "count": len(items),
        "scanned": scanned,
        "universe": universe,
        "pool_note": pool_note,
        "items": items,
        "notes": [
            "口径：近5年平均股息率≥4% ∧ 分红年份≥3 ∧ PE∈(0,30) ∧ PB∈(0,5) ∧ 市值>50亿",
            "股息率 = 年度每股现金分红均值 / 当前股价；派息按每10股÷10 折每股",
            pool_note,
            "全量遍历较慢；生产默认中证红利成分股，可用 universe=all&limit=N 扩展",
        ],
        "thresholds": {
            "avg_div_yield_5y_min": MIN_AVG_YIELD_5Y,
            "consecutive_div_years_min": MIN_DIV_YEARS,
            "pe_max": PE_MAX,
            "pb_max": PB_MAX,
            "market_cap_min_yi": MIN_MARKET_CAP / 1e8,
        },
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
    """带缓存的筛选入口（供 API 调用）。"""
    uni = universe if universe in ("csi_div", "all") else "csi_div"
    lim = None if uni == "csi_div" else (limit if limit and limit > 0 else 200)
    cache_key = f"{uni}:{lim}"

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
    top_n = max(1, min(int(top), 500))
    out["items"] = items[:top_n]
    out["count"] = len(out["items"])
    out["total_matched"] = len(items)
    out["cached"] = cached
    return out


def invalidate_cache() -> None:
    with _LOCK:
        _CACHE.update(ts=0.0, key=None, data=None)
