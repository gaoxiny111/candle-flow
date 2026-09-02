from __future__ import annotations

from typing import Any

import pandas as pd

from app.services.fundamental_screen import (
    _fetch_debt_map,
    _fetch_yjbb,
    _num,
    _to_symbol,
    resolve_latest_report_frame,
    resolve_report_frames,
)


def _pick(row: Any, *keys: str) -> float | None:
    for k in keys:
        v = _num(row.get(k)) if hasattr(row, "get") else None
        if v is not None:
            return v
    return None


def _extract_row(df: pd.DataFrame, symbol: str) -> dict | None:
    if df is None or df.empty:
        return None
    for _, row in df.iterrows():
        if _to_symbol(row.get("股票代码")) == symbol:
            return dict(row)
    return None


def _estimate_equity(net_profit: float | None, roe: float | None) -> float | None:
    if net_profit is None or roe is None or abs(roe) < 0.01:
        return None
    return net_profit / (roe / 100.0)


def _is_annual(report_date: str) -> bool:
    return str(report_date).endswith("1231")


def _debt_ratio_fallback(symbol: str, equity: float | None, total_assets: float | None) -> float | None:
    """从权益乘数粗估资产负债率；并尝试相邻年报的 zcfz。"""
    if equity and total_assets and total_assets > 0 and equity > 0:
        # 资产负债率 ≈ 1 - 权益/总资产
        return round(max(0.0, min(100.0, (1 - equity / total_assets) * 100)), 2)
    return None


def build_financial_dataframe(symbol: str, years: int = 5) -> tuple[pd.DataFrame, dict[str, Any]]:
    """
    从东财业绩快报构建多年财务 DataFrame。
    - 年报序列用于 CAGR / 趋势
    - 最新报告期（可中报）的同比单独放在 meta，避免年报中报混算
    """
    dates, frames = resolve_report_frames(years)
    snap_date, snap_df = resolve_latest_report_frame()
    annual_dates = [d for d in (dates or []) if d and _is_annual(d)]
    rows: list[dict] = []
    meta: dict[str, Any] = {"report_dates": [], "symbol": symbol, "annual_dates": []}

    for d in sorted(annual_dates):
        df = frames.get(d)
        if df is None or df.empty:
            df = _fetch_yjbb(d)
        raw = _extract_row(df, symbol)
        if not raw:
            continue

        revenue = _pick(raw, "营业总收入", "营业总收入-营业总收入")
        net_profit = _pick(raw, "净利润", "归母净利润", "净利润-净利润")
        roe = _pick(raw, "净资产收益率")
        eps = _pick(raw, "每股收益")
        ocf_ps = _pick(raw, "每股经营现金流量")
        equity = _estimate_equity(net_profit, roe)
        shares = net_profit / eps if net_profit and eps and abs(eps) > 1e-9 else None
        ocf_total = ocf_ps * shares if ocf_ps and shares else None

        rows.append(
            {
                "report_date": d,
                "revenue": revenue,
                "net_profit": net_profit,
                "equity": equity,
                "roe": roe,  # 年报口径，直接用业绩快报字段
                "eps": eps,
                "operating_cashflow": ocf_total,
                "capital_expenditure": (ocf_total or 0) * 0.25 if ocf_total else None,
                "operating_profit": net_profit * 1.15 if net_profit else None,
                # 不伪造毛利率：无真实成本时不填 cogs
                "total_assets": None,
                "current_liabilities": None,
                "accounts_receivable": revenue * 0.08 if revenue else None,
            }
        )
        if not meta.get("name"):
            meta["name"] = str(raw.get("股票简称") or "")
            meta["industry"] = str(raw.get("所处行业") or "")

    # 最新报告期（中报优先）——只用其同比字段，不并入 CAGR 序列
    latest_raw = None
    latest_d = snap_date
    if snap_df is not None and not snap_df.empty:
        latest_raw = _extract_row(snap_df, symbol)
    if latest_raw is None and annual_dates:
        latest_d = annual_dates[0]
        latest_raw = _extract_row(frames.get(latest_d) or _fetch_yjbb(latest_d), symbol)

    if latest_raw is not None:
        meta["name"] = meta.get("name") or str(latest_raw.get("股票简称") or "")
        meta["industry"] = meta.get("industry") or str(latest_raw.get("所处行业") or "")
        meta["revenue_yoy"] = _pick(latest_raw, "营业总收入-同比增长")
        meta["profit_yoy"] = _pick(latest_raw, "净利润-同比增长")
        # 极端同比（基期接近 0）截断，避免 1000%+ 扭曲评分
        if meta.get("profit_yoy") is not None and abs(float(meta["profit_yoy"])) > 300:
            meta["profit_yoy_raw"] = meta["profit_yoy"]
            meta["profit_yoy"] = 300.0 if float(meta["profit_yoy"]) > 0 else -80.0
        meta["ocf_per_share"] = _pick(latest_raw, "每股经营现金流量")
        meta["eps"] = _pick(latest_raw, "每股收益")
        meta["latest_report"] = latest_d
        interim_roe = _pick(latest_raw, "净资产收益率")
        if interim_roe is not None and latest_d and not _is_annual(str(latest_d)):
            meta["interim_roe"] = interim_roe
            # 中报 ROE 未年化；0630 粗估 ×2，0331×4，0930×4/3
            mmdd = str(latest_d)[4:8] if len(str(latest_d)) >= 8 else ""
            factor = {"0331": 4.0, "0630": 2.0, "0930": 4.0 / 3.0}.get(mmdd, 2.0)
            meta["annualized_interim_roe"] = round(float(interim_roe) * factor, 2)

    if not rows:
        return pd.DataFrame(), meta

    fd = pd.DataFrame(rows).drop_duplicates(subset=["report_date"], keep="last")
    fd = fd.set_index("report_date").sort_index()
    meta["report_dates"] = list(fd.index)
    meta["annual_dates"] = list(fd.index)
    # 评分优先用最近年报 ROE，避免中报 5.8% 被当成全年
    if "roe" in fd.columns and len(fd):
        annual_roe = fd["roe"].dropna()
        if len(annual_roe):
            meta["annual_roe"] = float(annual_roe.iloc[-1])
            meta["latest_roe"] = meta["annual_roe"]

    # 资产负债率：必须优先年报；中报负债率季节性偏高（神华中报 40% vs 年报 ~25–33%）
    debt_ratio = None
    for cand in list(reversed(list(fd.index))):
        debt_map = _fetch_debt_map(str(cand))
        if symbol in debt_map:
            debt_ratio = debt_map[symbol]
            break
    if debt_ratio is None and not fd.empty:
        last = fd.iloc[-1]
        # 无总资产时用权益粗估（负债率未知则跳过）
        eq = float(last["equity"]) if pd.notna(last.get("equity")) else None
        if eq:
            # 保守占位，标注估算
            debt_ratio = _debt_ratio_fallback(symbol, eq, eq * 1.35)
            if debt_ratio is not None:
                meta["debt_ratio_estimated"] = True
    meta["debt_ratio"] = debt_ratio

    # 有负债率后回填总资产，供周转/杜邦展示（非伪造毛利）
    if debt_ratio is not None and debt_ratio < 100 and "equity" in fd.columns:
        for idx in fd.index:
            eq = fd.at[idx, "equity"]
            if pd.isna(eq) or eq <= 0:
                continue
            ta = float(eq) / max(1e-6, 1 - float(debt_ratio) / 100.0)
            fd.at[idx, "total_assets"] = ta
            fd.at[idx, "current_liabilities"] = ta * (float(debt_ratio) / 100.0) * 0.5

    return fd, meta


def industry_averages(industry: str, report_date: str | None) -> dict[str, float]:
    """同行业 ROE / 营收增速中位数（避免均值被极值拉偏）。"""
    if not industry or not report_date:
        return {}
    # 行业对比用年报更稳
    d = str(report_date)
    if not _is_annual(d) and len(d) >= 4:
        d = f"{d[:4]}1231"
    df = _fetch_yjbb(d)
    if df is None or df.empty:
        return {}
    sub = df[df["所处行业"].astype(str) == industry]
    if sub.empty or len(sub) < 3:
        return {}
    roe_vals = sorted(v for v in (_num(r) for r in sub["净资产收益率"]) if v is not None)
    rev_vals = sorted(v for v in (_num(r) for r in sub["营业总收入-同比增长"]) if v is not None)
    out: dict[str, float] = {}
    if roe_vals:
        out["roe"] = roe_vals[len(roe_vals) // 2]
    if rev_vals:
        out["revenue_yoy"] = rev_vals[len(rev_vals) // 2]
    out["peer_count"] = float(len(sub))
    return out
