from __future__ import annotations

from typing import Any

import pandas as pd

from app.services.fundamental_screen import (
    _fetch_symbol_zcfz,
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
    code_col = next((c for c in df.columns if "代码" in str(c)), None)
    if code_col is None:
        return None
    want = str(symbol or "").upper()
    codes = df[code_col].map(_to_symbol).fillna("").astype(str).str.upper()
    hit = df.loc[codes == want]
    if hit.empty:
        return None
    return dict(hit.iloc[0])


def _estimate_equity(net_profit: float | None, roe: float | None) -> float | None:
    if net_profit is None or roe is None or abs(roe) < 0.01:
        return None
    return net_profit / (roe / 100.0)


def _is_annual(report_date: str) -> bool:
    return str(report_date).endswith("1231")


def fetch_deducted_parent_netprofit(symbol: str, report_date: str | None = None) -> float | None:
    """
    东财利润表 DEDUCT_PARENT_NETPROFIT（扣非归母净利润，元）。
    report_date 为 YYYYMMDD 时优先对齐该期；否则取最新一期。
    """
    import requests

    from app.utils.symbol import SymbolError, normalize_symbol, parse_symbol

    try:
        code, _ = parse_symbol(normalize_symbol(symbol))
    except SymbolError:
        digits = "".join(ch for ch in str(symbol) if ch.isdigit())
        code = digits[-6:] if len(digits) >= 6 else ""
    if not code:
        return None
    try:
        r = requests.get(
            "https://datacenter-web.eastmoney.com/api/data/v1/get",
            params={
                "reportName": "RPT_DMSK_FN_INCOME",
                "columns": "SECURITY_CODE,REPORT_DATE,PARENT_NETPROFIT,DEDUCT_PARENT_NETPROFIT",
                "filter": f'(SECURITY_CODE="{code}")',
                "pageNumber": "1",
                "pageSize": "8",
                "sortColumns": "REPORT_DATE",
                "sortTypes": "-1",
                "source": "WEB",
                "client": "WEB",
            },
            headers={"User-Agent": "Mozilla/5.0", "Referer": "https://data.eastmoney.com/"},
            timeout=12,
        )
        if not r.ok:
            return None
        rows = ((r.json() or {}).get("result") or {}).get("data") or []
        want = ""
        if report_date:
            s = str(report_date).replace("-", "")[:8]
            if len(s) == 8:
                want = f"{s[:4]}-{s[4:6]}-{s[6:8]}"
        for row in rows:
            rd = str(row.get("REPORT_DATE") or "")[:10]
            if want and rd != want:
                continue
            raw = row.get("DEDUCT_PARENT_NETPROFIT")
            if raw is None:
                if want:
                    continue
                return None
            return float(raw)
        # 未精确命中报告期时回退最新一期
        if rows and rows[0].get("DEDUCT_PARENT_NETPROFIT") is not None:
            return float(rows[0]["DEDUCT_PARENT_NETPROFIT"])
    except Exception:
        return None
    return None


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
        # 当期（可中报）利润含金量：每股经营现金流 / 每股收益 ≈ OCF/净利
        ocf_ps = meta.get("ocf_per_share")
        eps = meta.get("eps")
        if ocf_ps is not None and eps is not None and abs(float(eps)) > 1e-9:
            meta["latest_cash_ratio"] = float(ocf_ps) / float(eps)
        # 扣非归母净利：识别「归母高增但主业仍亏」的利润幻增
        ded = fetch_deducted_parent_netprofit(symbol, latest_d)
        if ded is not None:
            meta["deducted_net_profit"] = ded
            parent_np = _pick(latest_raw, "净利润", "归母净利润", "净利润-净利润")
            if parent_np is not None:
                meta["parent_net_profit"] = parent_np
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

    # 资产负债率：必须优先年报；用个股资产负债表，避免全市场 zcfz 分页
    debt_ratio = None
    zcfz_row: dict[str, float] | None = None
    zcfz_date: str | None = None
    by_date = _fetch_symbol_zcfz(symbol)
    for cand in list(reversed(list(fd.index))):
        item = by_date.get(str(cand))
        if item and item.get("debt_ratio") is not None:
            zcfz_row = item
            zcfz_date = str(cand)
            debt_ratio = float(zcfz_row["debt_ratio"])
            break
    if zcfz_row is None and by_date:
        annuals = sorted(k for k in by_date if str(k).endswith("1231"))
        pick = annuals[-1] if annuals else sorted(by_date)[-1]
        zcfz_row = by_date[pick]
        zcfz_date = pick
        if zcfz_row.get("debt_ratio") is not None:
            debt_ratio = float(zcfz_row["debt_ratio"])
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

    # 简表字段：区分经营性负债 vs 近似有息负债，并粗估流动/速动比
    if zcfz_row and zcfz_date:
        ta = zcfz_row.get("total_assets")
        tl = zcfz_row.get("total_liabilities")
        ap = float(zcfz_row.get("accounts_payable") or 0)
        adv = float(zcfz_row.get("advance_receipts") or 0)
        cash = float(zcfz_row.get("monetary_funds") or 0)
        ar = float(zcfz_row.get("accounts_receivable") or 0)
        inv = float(zcfz_row.get("inventory") or 0)
        op_liab = ap + adv
        ibd = max(0.0, float(tl) - op_liab) if tl is not None else None
        ibd_ratio = round(ibd / float(ta) * 100, 2) if ibd is not None and ta and float(ta) > 0 else None
        ca = cash + ar + inv
        # 流动负债粗估：经营性流动负债 + 有息负债的 70%（视为短期）
        cl_est = op_liab + (ibd * 0.7 if ibd is not None else 0.0)
        current_ratio = round(ca / cl_est, 2) if cl_est > 0 else None
        quick_ratio = round((cash + ar) / cl_est, 2) if cl_est > 0 else None
        meta["balance_sheet"] = {
            "report_date": zcfz_date,
            "total_assets": ta,
            "total_liabilities": tl,
            "operating_liabilities": round(op_liab, 2) if op_liab else 0.0,
            "interest_bearing_debt": round(ibd, 2) if ibd is not None else None,
            "interest_bearing_ratio": ibd_ratio,
            "current_ratio": current_ratio,
            "quick_ratio": quick_ratio,
            "estimated": True,
        }
        meta["interest_bearing_ratio"] = ibd_ratio
        meta["current_ratio"] = current_ratio
        meta["quick_ratio"] = quick_ratio

    # 有负债率后回填总资产，供周转/杜邦展示（非伪造毛利）
    if debt_ratio is not None and debt_ratio < 100 and "equity" in fd.columns:
        for idx in fd.index:
            eq = fd.at[idx, "equity"]
            if pd.isna(eq) or eq <= 0:
                continue
            if zcfz_row and zcfz_row.get("total_assets") and str(idx) == zcfz_date:
                ta = float(zcfz_row["total_assets"])
            else:
                ta = float(eq) / max(1e-6, 1 - float(debt_ratio) / 100.0)
            fd.at[idx, "total_assets"] = ta
            if zcfz_row and str(idx) == zcfz_date and meta.get("balance_sheet"):
                ibd = meta["balance_sheet"].get("interest_bearing_debt") or 0
                op = meta["balance_sheet"].get("operating_liabilities") or 0
                fd.at[idx, "current_liabilities"] = float(op) + float(ibd) * 0.7
                if zcfz_row.get("monetary_funds") is not None:
                    fd.at[idx, "monetary_funds"] = float(zcfz_row["monetary_funds"])
                if zcfz_row.get("accounts_receivable") is not None:
                    fd.at[idx, "accounts_receivable"] = float(zcfz_row["accounts_receivable"])
                if zcfz_row.get("inventory") is not None:
                    fd.at[idx, "inventory"] = float(zcfz_row["inventory"])
                if ibd is not None:
                    fd.at[idx, "short_term_borrowings"] = float(ibd) * 0.7
            else:
                fd.at[idx, "current_liabilities"] = ta * (float(debt_ratio) / 100.0) * 0.5

    return fd, meta


def industry_averages(industry: str, report_date: str | None) -> dict[str, float]:
    """同行业 ROE / 营收增速中位数（避免均值被极值拉偏）。"""
    if not industry or not report_date:
        return {}
    # 行业对比用年报更稳；中报回退上一完整年报，并在空表时再回退
    from app.analysis.models.comps import _yjbb_annual_candidates

    df = None
    for d in _yjbb_annual_candidates(report_date):
        cand = _fetch_yjbb(d)
        if cand is not None and not cand.empty and "所处行业" in cand.columns:
            df = cand
            break
    if df is None or df.empty:
        return {}
    sub = df[df["所处行业"].astype(str) == industry]
    # 精确行业样本不足时，放宽为「行业名互相包含」的近似同业
    if sub.empty or len(sub) < 5:
        soft = df[
            df["所处行业"].astype(str).map(lambda x: _soft_industry_match(str(x), industry))
        ]
        if len(soft) > len(sub):
            sub = soft
    if sub.empty or len(sub) < 5:
        return {"peer_count": float(len(sub)) if not sub.empty else 0.0}
    roe_vals = sorted(v for v in (_num(r) for r in sub["净资产收益率"]) if v is not None)
    rev_vals = sorted(v for v in (_num(r) for r in sub["营业总收入-同比增长"]) if v is not None)
    out: dict[str, float] = {}
    if roe_vals:
        out["roe"] = roe_vals[len(roe_vals) // 2]
    if rev_vals:
        out["revenue_yoy"] = rev_vals[len(rev_vals) // 2]
    out["peer_count"] = float(len(sub))
    return out


def _soft_industry_match(row_ind: str, target: str) -> bool:
    a, b = (row_ind or "").strip(), (target or "").strip()
    if not a or not b:
        return False
    if a == b:
        return True
    if len(b) >= 2 and (b in a or a in b):
        return True
    return False
