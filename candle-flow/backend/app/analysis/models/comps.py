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
CAP_DIFF_RELAXED = 1.0  # 样本不足时放宽到 100%
DEFAULT_LIMIT = 8
BAND = 0.10  # 估值区间上下浮动 10%
MIN_PEER_SAMPLE = 5  # 少于该数则行业/可比估值显示 N/A


def _soft_industry_match(row_ind: str, target: str) -> bool:
    a, b = (row_ind or "").strip(), (target or "").strip()
    if not a or not b:
        return False
    if a == b:
        return True
    if len(b) >= 2 and (b in a or a in b):
        return True
    return False


def _pick(row: Any, *keys: str) -> float | None:
    for k in keys:
        v = _num(row.get(k)) if hasattr(row, "get") else None
        if v is not None:
            return float(v)
    return None


def _annual_report_date(report_date: str | None) -> str | None:
    """
    同业对比用年报口径。
    中报/季报不能映射到「当年 1231」——当年年报通常尚未披露，应回退到上一完整年报。
    例：20260630 → 20251231；20251231 → 20251231。
    """
    if not report_date:
        return None
    d = str(report_date).replace("-", "")[:8]
    if len(d) < 8:
        return str(report_date)
    if d.endswith("1231"):
        return d
    try:
        y = int(d[:4])
    except ValueError:
        return d
    return f"{y - 1}1231"


def _yjbb_annual_candidates(report_date: str | None) -> list[str]:
    """优先最近可用年报，再逐年回退（避免空表/未披露年报）。"""
    primary = _annual_report_date(report_date)
    if not primary or len(primary) < 4:
        return []
    try:
        y = int(primary[:4])
    except ValueError:
        return [primary]
    return [f"{yy}1231" for yy in range(y, y - 4, -1)]


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
    if not industry:
        return []
    df = None
    used_date = None
    for d in _yjbb_annual_candidates(report_date):
        cand = _fetch_yjbb(d)
        if cand is not None and not cand.empty and "所处行业" in cand.columns:
            df = cand
            used_date = d
            break
    if df is None or df.empty:
        return []

    exclude_key = exclude.upper()
    exact: list[dict[str, Any]] = []
    soft: list[dict[str, Any]] = []
    for _, row in df.iterrows():
        row_ind = str(row.get("所处行业") or "")
        if row_ind == industry:
            bucket = exact
        elif _soft_industry_match(row_ind, industry):
            bucket = soft
        else:
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
        bucket.append(
            {
                "symbol": sym,
                "name": name,
                "net_profit": net_profit,
                "net_assets": equity,
                "revenue": _pick(row, "营业总收入", "营业总收入-营业总收入"),
                "eps": _pick(row, "每股收益"),
                "roe": roe,
                "peer_report_date": used_date,
            }
        )
    # 精确同行优先；不足再并入近似行业
    if len(exact) >= MIN_PEER_SAMPLE:
        return exact
    seen = {p["symbol"] for p in exact}
    merged = list(exact)
    for p in soft:
        if p["symbol"] not in seen:
            merged.append(p)
            seen.add(p["symbol"])
    return merged

def _batch_quotes(symbols: list[str], db: Any = None) -> dict[str, dict[str, Any]]:
    if not symbols:
        return {}
    out: dict[str, dict[str, Any]] = {}
    chunk = 50
    for i in range(0, len(symbols), chunk):
        batch = symbols[i : i + chunk]
        try:
            for row in get_valuations(
                batch,
                db=None,
                include_history=False,
                max_symbols=max(len(batch), 1),
            ):
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
    cap_diff_max: float = CAP_DIFF_MAX,
) -> list[dict[str, Any]]:
    """
    步骤2：筛选可比公司。
    条件：同行业（可软匹配）、非 ST/亏损、市值偏离限制，按市值接近度取前 limit 家。
    """
    peers = _industry_peer_rows(industry, report_date, exclude=stock_code)
    if not peers:
        return []

    # 先按利润规模接近度缩小名单，再拉行情（避免对 60+ 同行全市场报价）
    quote_n = max(limit * 2, MIN_PEER_SAMPLE + 4)

    def _size_key(p: dict[str, Any]) -> float:
        np_ = p.get("net_profit")
        if np_ is None:
            return 1e18
        return -abs(float(np_))

    shortlist = sorted(peers, key=_size_key)[:quote_n]

    quotes = _batch_quotes([p["symbol"] for p in shortlist], db=db)

    def _rank(max_dist: float, pool: list[dict[str, Any]]) -> list[dict[str, Any]]:
        ranked: list[tuple[float, dict[str, Any]]] = []
        for peer in pool:
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
                if dist > max_dist:
                    continue
            elif target_market_cap and target_market_cap > 0:
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

    selected = _rank(cap_diff_max, shortlist)
    # 样本不足时放宽市值偏离，尽量凑够 MIN_PEER_SAMPLE
    if len(selected) < MIN_PEER_SAMPLE and cap_diff_max < CAP_DIFF_RELAXED:
        selected = _rank(CAP_DIFF_RELAXED, shortlist)
    # 龙头相对小同行市值差常 >100%，最后取消市值闸门（仍按接近度排序取 limit）
    if len(selected) < MIN_PEER_SAMPLE:
        selected = _rank(999.0, shortlist)
    return selected


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
            "insufficient_sample": True,
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
    peer_n = len(comparables)
    insufficient = peer_n < MIN_PEER_SAMPLE
    if insufficient:
        warning = f"N/A（样本不足）：可比公司仅 {peer_n} 家，需至少 {MIN_PEER_SAMPLE} 家"
        # 样本不足时不输出有误导性的均 PE/PB、信号与套算区间
        avg_pe = None
        avg_pb = None
        valuation_range = {}
        signal = None
    else:
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

    price = target.get("price") or market.get("price")

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
        "peer_count": peer_n,
        "insufficient_sample": insufficient,
    }
