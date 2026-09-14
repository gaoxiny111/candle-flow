"""可比公司相对估值：同行业筛选 → 平均 PE/PB → 套算目标价区间。"""

from __future__ import annotations

import logging
from typing import Any

import numpy as np
import pandas as pd

from app.core.bull_tactics import is_st_name
from app.services.fundamental_screen import _fetch_yjbb, _num, _to_symbol
from app.services.valuation import get_valuations
from app.utils.symbol import is_etf_symbol, is_index_symbol

logger = logging.getLogger(__name__)

PE_MAX = 300.0
PB_MAX = 20.0
CAP_DIFF_MAX = 0.5  # 市值偏离上限 50%
DEFAULT_LIMIT = 5
BAND = 0.10  # 估值区间上下浮动 10%


def _pick(row: Any, *keys: str) -> float | None:
    for k in keys:
        v = _num(row.get(k)) if hasattr(row, "get") else None
        if v is not None:
            return float(v)
    return None


def _annual_report_date(report_date: str | None) -> str | None:
    if not report_date:
        return None
    d = str(report_date)
    if len(d) >= 8 and not d.endswith("1231"):
        return f"{d[:4]}1231"
    return d


def _shares_from_market(market: dict[str, Any]) -> float | None:
    raw = market.get("total_shares")
    if raw is not None and float(raw) > 0:
        return float(raw)
    price = market.get("price")
    mcap = market.get("market_cap")
    if price and mcap and float(price) > 0 and float(mcap) > 0:
        shares = float(mcap) / float(price)
        if shares > 1e6:
            return shares
    return None


def target_fundamentals(
    symbol: str,
    *,
    market: dict[str, Any],
    meta: dict[str, Any],
    fin_df: pd.DataFrame | None = None,
) -> dict[str, Any] | None:
    """步骤1：目标企业核心数据（扣非优先不可得时用归母净利）。"""
    net_profit = None
    net_assets = None
    revenue = None
    eps = meta.get("eps")

    if fin_df is not None and not fin_df.empty:
        last = fin_df.iloc[-1]
        if pd.notna(last.get("net_profit")):
            net_profit = float(last["net_profit"])
        if pd.notna(last.get("equity")):
            net_assets = float(last["equity"])
        if pd.notna(last.get("revenue")):
            revenue = float(last["revenue"])
        if eps is None and pd.notna(last.get("eps")):
            eps = float(last["eps"])

    shares = _shares_from_market(market)
    if shares is None and net_profit is not None and eps is not None and abs(float(eps)) > 1e-9:
        shares = float(net_profit) / float(eps)

    if net_profit is None or shares is None or shares <= 0:
        return None

    return {
        "symbol": symbol,
        "name": meta.get("name") or market.get("name") or "",
        "industry": meta.get("industry") or "",
        "net_profit": float(net_profit),
        "net_assets": float(net_assets) if net_assets is not None else None,
        "revenue": float(revenue) if revenue is not None else None,
        "total_shares": float(shares),
        "eps": float(eps) if eps is not None else None,
        "market_cap": market.get("market_cap"),
        "price": market.get("price"),
    }


def _industry_peer_rows(
    industry: str,
    report_date: str | None,
    *,
    exclude: str,
) -> list[dict[str, Any]]:
    """同行业股票池（业绩快报），已剔除指数/ETF/ST/亏损。"""
    d = _annual_report_date(report_date)
    if not industry or not d:
        return []
    df = _fetch_yjbb(d)
    if df is None or df.empty or "所处行业" not in df.columns:
        return []

    exclude_key = exclude.upper()
    out: list[dict[str, Any]] = []
    for _, row in df.iterrows():
        if str(row.get("所处行业") or "") != industry:
            continue
        sym = _to_symbol(row.get("股票代码"))
        if not sym or sym.upper() == exclude_key:
            continue
        if is_index_symbol(sym) or is_etf_symbol(sym):
            continue
        name = str(row.get("股票简称") or "")
        if is_st_name(name) or "退" in name:
            continue
        net_profit = _pick(row, "净利润", "归母净利润", "净利润-净利润")
        if net_profit is None or net_profit <= 0:
            continue
        roe = _pick(row, "净资产收益率")
        equity = None
        if net_profit is not None and roe is not None and abs(roe) >= 0.01:
            equity = net_profit / (roe / 100.0)
        out.append(
            {
                "symbol": sym,
                "name": name,
                "net_profit": net_profit,
                "net_assets": equity,
                "revenue": _pick(row, "营业总收入", "营业总收入-营业总收入"),
                "eps": _pick(row, "每股收益"),
                "roe": roe,
            }
        )
    return out


def _batch_quotes(symbols: list[str], db: Any = None) -> dict[str, dict[str, Any]]:
    if not symbols:
        return {}
    out: dict[str, dict[str, Any]] = {}
    chunk = 50
    for i in range(0, len(symbols), chunk):
        batch = symbols[i : i + chunk]
        try:
            for row in get_valuations(batch, db=db):
                out[str(row.get("symbol") or "").upper()] = row
        except Exception as e:
            logger.warning("comps valuation quotes failed for %s: %s", batch[:3], e)
    return out


def select_comparables(
    stock_code: str,
    *,
    industry: str,
    report_date: str | None,
    target_market_cap: float | None,
    db: Any = None,
    limit: int = DEFAULT_LIMIT,
) -> list[dict[str, Any]]:
    """
    步骤2：筛选可比公司。
    条件：同行业、非 ST/亏损、市值偏离 ≤50%，按市值接近度取前 limit 家。
    """
    peers = _industry_peer_rows(industry, report_date, exclude=stock_code)
    if not peers:
        return []

    quotes = _batch_quotes([p["symbol"] for p in peers], db=db)
    ranked: list[tuple[float, dict[str, Any]]] = []
    for peer in peers:
        q = quotes.get(peer["symbol"].upper()) or {}
        pe = q.get("pe_ttm")
        pb = q.get("pb")
        mcap = q.get("market_cap")
        price = q.get("price")
        if pe is None and pb is None:
            continue
        dist = 0.0
        if target_market_cap and target_market_cap > 0 and mcap is not None and float(mcap) > 0:
            dist = abs(float(mcap) - float(target_market_cap)) / float(target_market_cap)
            if dist > CAP_DIFF_MAX:
                continue
        elif target_market_cap and target_market_cap > 0:
            # 无线上市值时仍保留，但排到后面
            dist = 1.0
        ranked.append(
            (
                dist,
                {
                    **peer,
                    "pe": float(pe) if pe is not None else None,
                    "pb": float(pb) if pb is not None else None,
                    "market_cap": float(mcap) if mcap is not None else None,
                    "price": float(price) if price is not None else None,
                    "name": q.get("name") or peer.get("name") or "",
                },
            )
        )

    ranked.sort(key=lambda x: x[0])
    return [item for _, item in ranked[: max(1, limit)]]


def calculate_comparable_valuation(
    stock_code: str,
    *,
    market: dict[str, Any],
    meta: dict[str, Any],
    fin_df: pd.DataFrame | None = None,
    db: Any = None,
    limit: int = DEFAULT_LIMIT,
) -> dict[str, Any]:
    """
    步骤3–4：计算可比公司平均 PE/PB，并套算目标每股估值区间（±10%）。
    """
    target = target_fundamentals(stock_code, market=market, meta=meta, fin_df=fin_df)
    if not target:
        return {
            "stock_code": stock_code,
            "comparables": [],
            "avg_pe": None,
            "avg_pb": None,
            "valuation_range": {},
            "warning": "目标公司基本面数据不足，无法套算可比估值",
            "peer_count": 0,
        }

    industry = target.get("industry") or meta.get("industry") or ""
    report_date = meta.get("latest_report") or (
        str(fin_df.index[-1]) if fin_df is not None and not fin_df.empty else None
    )
    comparables = select_comparables(
        stock_code,
        industry=industry,
        report_date=report_date,
        target_market_cap=target.get("market_cap") or market.get("market_cap"),
        db=db,
        limit=limit,
    )

    valid_pe = [m["pe"] for m in comparables if m.get("pe") is not None and 0 < float(m["pe"]) < PE_MAX]
    valid_pb = [m["pb"] for m in comparables if m.get("pb") is not None and 0 < float(m["pb"]) < PB_MAX]
    avg_pe = float(np.mean(valid_pe)) if valid_pe else None
    avg_pb = float(np.mean(valid_pb)) if valid_pb else None

    valuation_range: dict[str, Any] = {}
    shares = target["total_shares"]
    if avg_pe is not None and target["net_profit"] > 0 and shares > 0:
        base = target["net_profit"] * avg_pe / shares
        valuation_range["pe_based"] = {
            "mid": round(base, 2),
            "low": round(base * (1 - BAND), 2),
            "high": round(base * (1 + BAND), 2),
        }
    if avg_pb is not None and target.get("net_assets") and target["net_assets"] > 0 and shares > 0:
        base = target["net_assets"] * avg_pb / shares
        valuation_range["pb_based"] = {
            "mid": round(base, 2),
            "low": round(base * (1 - BAND), 2),
            "high": round(base * (1 + BAND), 2),
        }

    warning = None
    if len(comparables) < 3:
        warning = "可比公司不足 3 家，估值结果可能不准确"

    price = target.get("price") or market.get("price")
    signal = None
    mid_candidates = [
        valuation_range[k]["mid"]
        for k in ("pe_based", "pb_based")
        if k in valuation_range and valuation_range[k].get("mid") is not None
    ]
    if price and mid_candidates:
        mid = float(np.mean(mid_candidates))
        if float(price) < mid * 0.9:
            signal = "低估"
        elif float(price) > mid * 1.1:
            signal = "高估"
        else:
            signal = "合理"

    return {
        "stock_code": stock_code,
        "industry": industry,
        "comparables": [
            {
                "symbol": c["symbol"],
                "name": c.get("name") or "",
                "pe": round(c["pe"], 2) if c.get("pe") is not None else None,
                "pb": round(c["pb"], 2) if c.get("pb") is not None else None,
                "market_cap": c.get("market_cap"),
                "price": c.get("price"),
            }
            for c in comparables
        ],
        "avg_pe": round(avg_pe, 2) if avg_pe is not None else None,
        "avg_pb": round(avg_pb, 2) if avg_pb is not None else None,
        "valuation_range": valuation_range,
        "target": {
            "net_profit": target["net_profit"],
            "net_assets": target.get("net_assets"),
            "total_shares": target["total_shares"],
            "price": price,
        },
        "signal": signal,
        "warning": warning,
        "peer_count": len(comparables),
    }
