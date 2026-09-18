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


def normalize_profit_yoy(raw: float | None) -> dict[str, Any]:
    """
    净利润同比的口径拆分（展示 vs 外推）：

    - profit_yoy        : 真实披露同比，原样保留，用于展示 / 成长性评分 / 风险提示
    - profit_yoy_forward: 仅用于前瞻外推（动态PE 等）。基期接近 0 或并表暴增时
                          按 ±300% 上限折减，避免把一次性增速当成可持续增速
                          （例如 493% 直接外推会算出 4x 的前瞻PE）

    历史行为是把 profit_yoy 本身硬截断为 300，导致 2026 中报 493.25% 显示成 300%，
    且 PEG/前瞻成长性都建立在错误分母上。
    """
    if raw is None:
        return {
            "profit_yoy": None,
            "profit_yoy_raw": None,
            "profit_yoy_forward": None,
            "profit_yoy_extreme": False,
        }
    v = float(raw)
    extreme = abs(v) > 300
    return {
        "profit_yoy": v,
        "profit_yoy_raw": v,
        "profit_yoy_extreme": extreme,
        "profit_yoy_forward": (300.0 if v > 0 else -80.0) if extreme else v,
    }


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


def fetch_deducted_yoy(symbol: str, report_date: str | None = None) -> float | None:
    """扣非归母净利同比（%）：取最近两期 DEDUCT_PARENT_NETPROFIT 计算（支持中报对比）。"""
    import requests

    code = _income_code(symbol)
    if not code:
        return None
    try:
        r = requests.get(
            "https://datacenter-web.eastmoney.com/api/data/v1/get",
            params={
                "reportName": "RPT_DMSK_FN_INCOME",
                "columns": "SECURITY_CODE,REPORT_DATE,DEDUCT_PARENT_NETPROFIT",
                "filter": f'(SECURITY_CODE="{code}")',
                "pageNumber": "1",
                "pageSize": "12",
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
    except Exception:
        return None

    by_date: dict[str, float] = {}
    for row in rows:
        rd = str(row.get("REPORT_DATE") or "")[:10].replace("-", "")
        v = row.get("DEDUCT_PARENT_NETPROFIT")
        if len(rd) == 8 and v is not None:
            by_date[rd] = float(v)

    cur = report_date.replace("-", "")[:8] if report_date else None
    if cur and cur in by_date:
        y_ago = f"{int(cur[:4]) - 1}{cur[4:]}"
        cur_v, prev_v = by_date.get(cur), by_date.get(y_ago)
    elif rows:
        cur_v = rows[0].get("DEDUCT_PARENT_NETPROFIT")
        rd0 = str(rows[0].get("REPORT_DATE") or "")[:10].replace("-", "")
        prev_v = by_date.get(f"{int(rd0[:4]) - 1}{rd0[4:]}") if len(rd0) == 8 else None
    else:
        return None
    if cur_v is None or prev_v is None or abs(float(prev_v)) < 1e-6:
        return None
    return round((float(cur_v) / float(prev_v) - 1) * 100, 2)


def _income_code(symbol: str) -> str:
    from app.utils.symbol import SymbolError, normalize_symbol, parse_symbol

    try:
        code, _ = parse_symbol(normalize_symbol(symbol))
        return code
    except SymbolError:
        digits = "".join(ch for ch in str(symbol) if ch.isdigit())
        return digits[-6:] if len(digits) >= 6 else ""


def fetch_income_by_period(symbol: str, limit: int = 16) -> dict[str, dict[str, float]]:
    """
    东财利润表：按报告期 YYYYMMDD → 营业利润 / 营收 / 成本 / 毛利率。
    用于修正 ROIC（勿用净利×1.15）与真实毛利率。
    """
    import requests

    code = _income_code(symbol)
    if not code:
        return {}
    try:
        r = requests.get(
            "https://datacenter-web.eastmoney.com/api/data/v1/get",
            params={
                "reportName": "RPT_DMSK_FN_INCOME",
                "columns": (
                    "SECURITY_CODE,REPORT_DATE,TOTAL_OPERATE_INCOME,OPERATE_INCOME,"
                    "OPERATE_COST,OPERATE_PROFIT,PARENT_NETPROFIT"
                ),
                "filter": f'(SECURITY_CODE="{code}")',
                "pageNumber": "1",
                "pageSize": str(limit),
                "sortColumns": "REPORT_DATE",
                "sortTypes": "-1",
                "source": "WEB",
                "client": "WEB",
            },
            headers={"User-Agent": "Mozilla/5.0", "Referer": "https://data.eastmoney.com/"},
            timeout=12,
        )
        if not r.ok:
            return {}
        rows = ((r.json() or {}).get("result") or {}).get("data") or []
    except Exception:
        return {}

    out: dict[str, dict[str, float]] = {}
    for row in rows:
        rd = str(row.get("REPORT_DATE") or "")[:10].replace("-", "")
        if len(rd) != 8:
            continue
        rev = row.get("TOTAL_OPERATE_INCOME")
        if rev is None:
            rev = row.get("OPERATE_INCOME")
        cost = row.get("OPERATE_COST")
        op = row.get("OPERATE_PROFIT")
        item: dict[str, float] = {}
        if rev is not None:
            item["revenue"] = float(rev)
        if cost is not None:
            item["cogs"] = float(cost)
        if op is not None:
            item["operating_profit"] = float(op)
        if rev is not None and cost is not None and float(rev) > 0:
            item["gross_margin"] = round((float(rev) - float(cost)) / float(rev) * 100, 2)
        if item:
            out[rd] = item
    return out


def _debt_ratio_fallback(symbol: str, equity: float | None, total_assets: float | None) -> float | None:
    """从权益乘数粗估资产负债率；并尝试相邻年报的 zcfz。"""
    if equity and total_assets and total_assets > 0 and equity > 0:
        # 资产负债率 ≈ 1 - 权益/总资产
        return round(max(0.0, min(100.0, (1 - equity / total_assets) * 100)), 2)
    return None


def _sina_to_zcfz(row: dict[str, Any]) -> dict[str, Any] | None:
    """新浪原始资产负债表行 → 与 _parse_em_zcfz_row 同构的偿债简表。"""
    from app.analysis.sina_financials import interest_bearing_debt

    ta = row.get("total_assets")
    tl = row.get("total_liabilities")
    if not ta or not tl or float(ta) <= 0:
        return None
    ibd = interest_bearing_debt(row)
    return {
        "debt_ratio": round(float(tl) / float(ta) * 100, 2),
        "total_assets": float(ta),
        "total_liabilities": float(tl),
        "monetary_funds": row.get("monetary_funds"),
        "accounts_receivable": row.get("accounts_receivable"),
        "inventory": row.get("inventory"),
        "accounts_payable": row.get("accounts_payable"),
        "advance_receipts": row.get("advance_receipts"),
        "tax_payable": row.get("tax_payable"),
        "staff_salary_payable": row.get("staff_salary_payable"),
        "other_payable": row.get("other_payable"),
        "interest_bearing_debt": float(ibd),
        "short_term_borrowings": float(row.get("short_term_borrowings") or 0),
        "long_term_borrowings": float(row.get("long_term_borrowings") or 0),
        "non_current_due_within_1y": row.get("non_current_due_within_1y"),
        "bonds_payable": row.get("bonds_payable"),
        "interest_bearing_explicit": 1.0,
        "notes_receivable": row.get("notes_receivable"),
        "source": "sina_original",
    }


_QUARTER_PREV_MD = {"0331": None, "0630": "0331", "0930": "0630", "1231": "0930"}
_QUARTER_NO = {"0331": 1, "0630": 2, "0930": 3, "1231": 4}


def _single_quarter_from_sina(
    is_map: dict[str, dict[str, float | None]], report_date: str | None
) -> dict[str, Any] | None:
    """累计利润表差分 → 最新单季归母净利及同比/环比（如 2026Q2 同比+42%、环比+69%）。"""
    ymd = str(report_date or "").replace("-", "")[:8]
    if len(ymd) != 8:
        return None
    y, md = int(ymd[:4]), ymd[4:]
    q = _QUARTER_NO.get(md)
    if q is None:
        return None

    def _np(year: int, mdx: str) -> float | None:
        row = is_map.get(f"{year}{mdx}")
        if not row:
            return None
        v = row.get("parent_net_profit")
        return float(v) if v is not None else None

    cur_cum = _np(y, md)
    prev_cum = _np(y - 1, md)
    if cur_cum is None or prev_cum is None:
        return None
    if q == 1:
        cur_sq, prev_sq = cur_cum, prev_cum
    else:
        prev_md = _QUARTER_PREV_MD[md]
        a, b = _np(y, prev_md), _np(y - 1, prev_md)
        if a is None or b is None:
            return None
        cur_sq, prev_sq = cur_cum - a, prev_cum - b
    # 环比上一单季
    if q == 1:
        q4_prev_year = _np(y - 1, "1231")
        q3_prev_year = _np(y - 1, "0930")
        prev_sq_qoq = (
            q4_prev_year - q3_prev_year
            if q4_prev_year is not None and q3_prev_year is not None
            else None
        )
    else:
        # 上一单季净利：Q2→Q1累计（即Q1单季）；Q3→Q2单季=Q2累计−Q1累计；Q4→Q3单季
        prev_md = _QUARTER_PREV_MD[md]
        prev_cum_for_qoq = _np(y, prev_md)
        if q == 2:
            prev_sq_qoq = prev_cum_for_qoq
        else:
            prev_prev_md = _QUARTER_PREV_MD[prev_md]
            prev_prev_cum = _np(y, prev_prev_md)
            if prev_cum_for_qoq is not None and prev_prev_cum is not None:
                prev_sq_qoq = prev_cum_for_qoq - prev_prev_cum
            else:
                prev_sq_qoq = None
    yoy = (cur_sq / prev_sq - 1) * 100 if prev_sq else None
    qoq = (cur_sq / prev_sq_qoq - 1) * 100 if prev_sq_qoq else None
    return {
        "label": f"{y}Q{q}",
        "report_date": ymd,
        "net_profit": cur_sq,
        "yoy_pct": round(yoy, 1) if yoy is not None else None,
        "qoq_pct": round(qoq, 1) if qoq is not None else None,
    }


def _ar_metrics_from_sina(
    bs_map: dict[str, dict[str, float | None]],
    is_map: dict[str, dict[str, float | None]],
    annual_dates: list[str],
) -> dict[str, Any] | None:
    """应收账款周转天数（期末口径，AR/营收×365）五年变化 + 应收票据同比。"""
    ann = sorted(d for d in annual_dates if str(d).endswith("1231"))
    if not ann:
        return None

    def _days(ymd: str) -> float | None:
        b, i = bs_map.get(ymd), is_map.get(ymd)
        if not b or not i:
            return None
        ar, rev = b.get("accounts_receivable"), i.get("revenue")
        if ar is None or rev is None or float(rev) <= 0:
            return None
        return float(ar) / float(rev) * 365.0

    latest = ann[-1]
    days_latest = _days(latest)
    if days_latest is None:
        return None
    out: dict[str, Any] = {
        "period": f"{latest[:4]}年报",
        "days_latest": round(days_latest, 1),
        "days_5y_ago": None,
        "days_delta_5y": None,
        "notes_yoy_pct": None,
    }
    old = f"{int(latest[:4]) - 5}1231"
    days_old = _days(old)
    if days_old is not None:
        out["days_5y_ago"] = round(days_old, 1)
        out["days_delta_5y"] = round(days_latest - days_old, 1)
    b_now, b_prev = bs_map.get(latest), bs_map.get(ann[-2]) if len(ann) >= 2 else None
    if b_now and b_prev:
        n_now, n_prev = b_now.get("notes_receivable"), b_prev.get("notes_receivable")
        if n_now and n_prev and float(n_prev) > 0:
            out["notes_yoy_pct"] = round((float(n_now) / float(n_prev) - 1) * 100, 1)
    return out


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
                "operating_profit_estimated": 1.0 if net_profit else None,
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
        # 真实披露同比一律原样保留（展示 / 成长性评分 / 风险提示都以它为准）；
        # 只有前瞻外推场景才用折减后口径，见 normalize_profit_yoy。
        meta.update(normalize_profit_yoy(_pick(latest_raw, "净利润-同比增长")))
        meta["ocf_per_share"] = _pick(latest_raw, "每股经营现金流量")
        meta["eps"] = _pick(latest_raw, "每股收益")
        meta["latest_report"] = latest_d
        # 当期（可中报）利润含金量：每股经营现金流 / 每股收益 ≈ OCF/净利。
        # 注意这是近似口径（每股指标各自四舍五入），下面拿到同报告期绝对值后会覆盖。
        ocf_ps = meta.get("ocf_per_share")
        eps = meta.get("eps")
        if ocf_ps is not None and eps is not None and abs(float(eps)) > 1e-9:
            meta["latest_cash_ratio"] = float(ocf_ps) / float(eps)
            meta["latest_cash_ratio_source"] = "每股经营现金流/每股收益（近似口径）"
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

    # 利润表补全：真实营业利润 / 营业成本 / 毛利率（修正 ROIC=净利×1.15 偏差）
    income_by = fetch_income_by_period(symbol)
    latest_gm: float | None = None
    if income_by:
        for idx in fd.index:
            key = str(idx)
            inc = income_by.get(key)
            if not inc:
                continue
            if inc.get("operating_profit") is not None:
                fd.at[idx, "operating_profit"] = float(inc["operating_profit"])
                fd.at[idx, "operating_profit_estimated"] = 0.0
            if inc.get("cogs") is not None:
                fd.at[idx, "cogs"] = float(inc["cogs"])
            if inc.get("gross_margin") is not None:
                fd.at[idx, "gross_margin"] = float(inc["gross_margin"])
                latest_gm = float(inc["gross_margin"])
        # 最新报告期（可中报）毛利率优先用于高成长画像
        snap_key = str(meta.get("latest_report") or "")
        if snap_key and snap_key in income_by and income_by[snap_key].get("gross_margin") is not None:
            latest_gm = float(income_by[snap_key]["gross_margin"])
            meta["latest_gross_margin"] = latest_gm
        elif latest_gm is not None:
            meta["latest_gross_margin"] = latest_gm
        meta["income_enriched"] = True

    # 新浪原始披露口径校准（东财在同一控制下企业合并后会追溯重述历史报表，
    # 如神华 2026 年资产注入后，东财 2025 年报被改写为总资产 9038 亿/负债率 33.2%）
    import datetime as _dt

    from app.analysis.sina_financials import (
        PAGE_BALANCE as _SINA_BS,
        PAGE_CASHFLOW as _SINA_CF,
        PAGE_INCOME as _SINA_IS,
        fetch_statements as _sina_fetch,
        free_cashflow as _sina_fcf,
    )

    _sina_years = list(range(_dt.date.today().year - 7, _dt.date.today().year + 1))
    sina_bs_all = _sina_fetch(symbol, _SINA_BS, _sina_years)
    sina_cf_all = _sina_fetch(symbol, _SINA_CF, _sina_years)
    sina_is_all = _sina_fetch(symbol, _SINA_IS, _sina_years)
    sina_bs_map = {
        k: adapted
        for k, v in sina_bs_all.items()
        if (adapted := _sina_to_zcfz(v)) is not None
    }

    # 年报序列覆盖：真实 OCF / CapEx（替代 OCF×0.25 占位）与明细资产项
    for idx in fd.index:
        key = str(idx)
        cf_r = sina_cf_all.get(key)
        if cf_r:
            if cf_r.get("operating_cashflow") is not None:
                fd.at[idx, "operating_cashflow"] = float(cf_r["operating_cashflow"])
            if cf_r.get("capex") is not None:
                fd.at[idx, "capital_expenditure"] = abs(float(cf_r["capex"]))
        b = sina_bs_map.get(key)
        if b:
            for col in (
                "total_assets",
                "monetary_funds",
                "accounts_receivable",
                "notes_receivable",
                "inventory",
                "goodwill",
                "interest_bearing_debt",
                "short_term_borrowings",
            ):
                val = b.get(col)
                if val is not None:
                    fd.at[idx, col] = float(val)

    # 最新报告期（可中报）：经营现金流/资本开支/FCF + 单季利润 + 扣非同比
    latest_ymd = str(meta.get("latest_report") or "")
    if latest_ymd:
        cf_latest = sina_cf_all.get(latest_ymd)
        if cf_latest and cf_latest.get("operating_cashflow") is not None:
            meta["latest_operating_cashflow"] = float(cf_latest["operating_cashflow"])
            if cf_latest.get("capex") is not None:
                meta["latest_capex"] = abs(float(cf_latest["capex"]))
            fcf_latest = _sina_fcf(cf_latest)
            if fcf_latest is not None:
                meta["latest_fcf"] = float(fcf_latest)
        # ── 当期利润含金量：用「同报告期绝对值」统一口径（分子 OCF / 分母归母净利）──
        # 原先用 每股经营现金流/每股收益 估算，两个每股指标各自四舍五入后
        # 会带来 ~2pct 偏差（如 -288.2% vs 实际 -289.9%）。
        _np_period = (sina_is_all.get(latest_ymd) or {}).get("parent_net_profit")
        if _np_period is None:
            _np_period = _pick(latest_raw, "净利润", "归母净利润") if latest_raw else None
        if (
            meta.get("latest_operating_cashflow") is not None
            and _np_period is not None
            and abs(float(_np_period)) > 1e-6
        ):
            meta["latest_cash_ratio"] = round(
                float(meta["latest_operating_cashflow"]) / float(_np_period), 4
            )
            meta["latest_cash_ratio_source"] = (
                f"经营现金流/归母净利（{latest_ymd}同期绝对值口径）"
            )
            meta["latest_period_net_profit"] = float(_np_period)
        bs_latest = sina_bs_all.get(latest_ymd)
        if bs_latest and bs_latest.get("total_assets") and bs_latest.get("total_liabilities"):
            from app.analysis.sina_financials import interest_bearing_debt as _ibd

            _ta = float(bs_latest["total_assets"])
            _ibd_v = _ibd(bs_latest)
            meta["interim_balance_sheet"] = {
                "report_date": latest_ymd,
                "debt_ratio": round(float(bs_latest["total_liabilities"]) / _ta * 100, 2),
                "interest_bearing_ratio": round(_ibd_v / _ta * 100, 2),
                "total_assets": _ta,
                "interest_bearing_debt": _ibd_v,
                "source": "sina_original",
            }
        sq = _single_quarter_from_sina(sina_is_all, latest_ymd)
        if sq:
            meta["single_quarter"] = sq
        if latest_ymd and not latest_ymd.endswith("1231"):
            ded_yoy = fetch_deducted_yoy(symbol, latest_ymd)
            if ded_yoy is not None:
                meta["deducted_yoy_pct"] = ded_yoy
        meta["ar_metrics"] = _ar_metrics_from_sina(sina_bs_all, sina_is_all, list(fd.index))

    # 真实分红历史：D0 / 分红率 / 连续分红年限（红利框架与 DDM 输入）
    try:
        from app.analysis.dividend_data import fetch_dividend_history

        div_hist = fetch_dividend_history(symbol)
    except Exception:
        div_hist = {}
    latest_fy = div_hist.get("latest_fy") if div_hist else None
    if latest_fy and latest_fy.get("dps"):
        fy_ymd = f"{int(latest_fy['year'])}1231"
        np_fy = (sina_is_all.get(fy_ymd) or {}).get("parent_net_profit")
        if np_fy is None and fy_ymd in fd.index:
            np_fy = float(fd.at[fy_ymd, "net_profit"]) if pd.notna(fd.at[fy_ymd, "net_profit"]) else None
        cf_fy = sina_cf_all.get(fy_ymd)
        fcf_fy = _sina_fcf(cf_fy) if cf_fy else None
        cash_total = latest_fy.get("cash_total")
        payout = (
            round(float(cash_total) / float(np_fy) * 100, 1)
            if cash_total and np_fy and float(np_fy) > 0
            else None
        )
        meta["dividend"] = {
            "d0": float(latest_fy["dps"]),
            "fy": int(latest_fy["year"]),
            "cash_total": float(cash_total) if cash_total else None,
            "payout_ratio_pct": payout,
            "consecutive_years": int(div_hist.get("consecutive_years") or 0),
            "current_interim": div_hist.get("current_interim"),
            "fcf": float(fcf_fy) if fcf_fy is not None else None,
            "fcf_dividend_gap": (
                round(float(fcf_fy) - float(cash_total), 2)
                if fcf_fy is not None and cash_total
                else None
            ),
        }
        if payout is not None:
            meta["payout_ratio_pct"] = payout

    # 资产负债率：新浪原始审计口径优先，东财个股表兜底
    debt_ratio = None
    zcfz_row: dict[str, float] | None = None
    zcfz_date: str | None = None
    zcfz_source: str | None = None
    by_date = _fetch_symbol_zcfz(symbol)

    def _pick_bs(ymd: str) -> tuple[dict | None, str | None]:
        item = sina_bs_map.get(ymd)
        if item and item.get("debt_ratio") is not None:
            return item, "sina_original"
        item = by_date.get(ymd)
        if item and item.get("debt_ratio") is not None:
            return item, "eastmoney"
        return None, None

    # 「以最新报告期为锚」：中报/季报已披露时，优先采用最新报告期的资产负债表，
    # 否则会出现「2026 中报已出，但仍用 2025 年报负债率」的滞后
    # （例：商络电子 2025 年报 72.96% → 2026 中报 75.76%，+2.8pct 风险信号被掩盖）。
    if latest_ymd and not latest_ymd.endswith("1231"):
        _item, _src = _pick_bs(latest_ymd)
        if _item:
            zcfz_row, zcfz_date, zcfz_source = _item, latest_ymd, _src
            debt_ratio = float(zcfz_row["debt_ratio"])
            meta["debt_ratio_period"] = latest_ymd
            meta["debt_ratio_scope"] = "最新报告期"

    for cand in list(reversed(list(fd.index))):
        if zcfz_row is not None:
            break
        item, src = _pick_bs(str(cand))
        if item:
            zcfz_row, zcfz_date, zcfz_source = item, str(cand), src
            debt_ratio = float(zcfz_row["debt_ratio"])
            meta["debt_ratio_period"] = str(cand)
            meta["debt_ratio_scope"] = "最近年报"
            break
    if zcfz_row is None:
        for pool in (sina_bs_map, by_date):
            annuals = sorted(k for k in pool if str(k).endswith("1231"))
            if annuals:
                pick = annuals[-1]
                item, src = _pick_bs(pick)
                if item:
                    zcfz_row, zcfz_date, zcfz_source = item, pick, src
                    debt_ratio = float(zcfz_row["debt_ratio"])
                    meta["debt_ratio_period"] = pick
                    meta["debt_ratio_scope"] = "最近年报"
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

    # 简表字段：明示有息负债 vs 经营性负债；禁止把税费/薪酬等残差当成借款
    if zcfz_row and zcfz_date:
        ta = zcfz_row.get("total_assets")
        tl = zcfz_row.get("total_liabilities")
        ap = float(zcfz_row.get("accounts_payable") or 0)
        adv = float(zcfz_row.get("advance_receipts") or 0)
        tax_p = float(zcfz_row.get("tax_payable") or 0)
        staff_p = float(zcfz_row.get("staff_salary_payable") or 0)
        other_p = float(zcfz_row.get("other_payable") or 0)
        cash = float(zcfz_row.get("monetary_funds") or 0)
        ar = float(zcfz_row.get("accounts_receivable") or 0)
        inv = float(zcfz_row.get("inventory") or 0)
        op_liab = ap + adv + tax_p + staff_p + other_p
        if zcfz_row.get("interest_bearing_explicit"):
            ibd = float(zcfz_row.get("interest_bearing_debt") or 0)
            short_borrow = float(zcfz_row.get("short_term_borrowings") or 0)
        else:
            # 旧数据源兜底：残差前尽量扣掉经营性负债
            ibd = max(0.0, float(tl) - op_liab) if tl is not None else 0.0
            short_borrow = ibd * 0.7
        ibd_ratio = round(ibd / float(ta) * 100, 2) if ta and float(ta) > 0 else None
        ca = cash + ar + inv
        cl_est = op_liab + short_borrow
        # 分子为 0 表示该数据源没映射到货币资金/应收账款/存货（银行等金融股
        # 用「现金及存放央行款项」「客户贷款」等科目），属「不可得」而非
        # 「流动性为 0」。此前按 0 参与打分，20 家 A 股银行的流动/速动比率
        # 全部是 0.0 → 各自拿到 0 分（最差档），把存款类机构误判成流动性枯竭。
        current_ratio = round(ca / cl_est, 2) if cl_est > 0 and ca > 0 else None
        quick_ratio = (
            round((cash + ar) / cl_est, 2) if cl_est > 0 and (cash + ar) > 0 else None
        )
        meta["balance_sheet"] = {
            "report_date": zcfz_date,
            "total_assets": ta,
            "total_liabilities": tl,
            "operating_liabilities": round(op_liab, 2) if op_liab else 0.0,
            "interest_bearing_debt": round(ibd, 2),
            "short_term_borrowings": round(short_borrow, 2),
            "interest_bearing_ratio": ibd_ratio,
            "interest_bearing_explicit": bool(zcfz_row.get("interest_bearing_explicit")),
            "current_ratio": current_ratio,
            "quick_ratio": quick_ratio,
            "estimated": not bool(zcfz_row.get("interest_bearing_explicit")),
            "source": zcfz_source or zcfz_row.get("source") or "eastmoney",
            "notes_receivable": zcfz_row.get("notes_receivable"),
        }
        meta["interest_bearing_ratio"] = ibd_ratio
        meta["interest_bearing_debt"] = ibd
        meta["short_term_borrowings"] = short_borrow
        meta["current_ratio"] = current_ratio
        meta["quick_ratio"] = quick_ratio

    # 有负债率后回填总资产，供周转/杜邦展示（非伪造毛利）
    if debt_ratio is not None and debt_ratio < 100 and "equity" in fd.columns:
        for idx in fd.index:
            eq = fd.at[idx, "equity"]
            if pd.isna(eq) or eq <= 0:
                continue
            z_item = sina_bs_map.get(str(idx)) or (by_date.get(str(idx)) if by_date else None)
            if z_item and z_item.get("total_assets"):
                ta = float(z_item["total_assets"])
            elif zcfz_row and zcfz_row.get("total_assets") and str(idx) == zcfz_date:
                ta = float(zcfz_row["total_assets"])
            else:
                ta = float(eq) / max(1e-6, 1 - float(debt_ratio) / 100.0)
            fd.at[idx, "total_assets"] = ta
            if z_item:
                if z_item.get("interest_bearing_explicit"):
                    ibd_y = float(z_item.get("interest_bearing_debt") or 0)
                    short_b = float(z_item.get("short_term_borrowings") or 0)
                else:
                    ibd_y = float(z_item.get("interest_bearing_debt") or 0)
                    short_b = ibd_y * 0.7
                op = (
                    float(z_item.get("accounts_payable") or 0)
                    + float(z_item.get("advance_receipts") or 0)
                    + float(z_item.get("tax_payable") or 0)
                    + float(z_item.get("staff_salary_payable") or 0)
                    + float(z_item.get("other_payable") or 0)
                )
                fd.at[idx, "current_liabilities"] = op + short_b
                fd.at[idx, "interest_bearing_debt"] = ibd_y
                if z_item.get("monetary_funds") is not None:
                    fd.at[idx, "monetary_funds"] = float(z_item["monetary_funds"])
                if z_item.get("accounts_receivable") is not None:
                    fd.at[idx, "accounts_receivable"] = float(z_item["accounts_receivable"])
                if z_item.get("inventory") is not None:
                    fd.at[idx, "inventory"] = float(z_item["inventory"])
                fd.at[idx, "short_term_borrowings"] = short_b
            elif zcfz_row and str(idx) == zcfz_date and meta.get("balance_sheet"):
                short_b = float(meta["balance_sheet"].get("short_term_borrowings") or 0)
                op = float(meta["balance_sheet"].get("operating_liabilities") or 0)
                fd.at[idx, "current_liabilities"] = op + short_b
                fd.at[idx, "interest_bearing_debt"] = float(
                    meta["balance_sheet"].get("interest_bearing_debt") or 0
                )
                if zcfz_row.get("monetary_funds") is not None:
                    fd.at[idx, "monetary_funds"] = float(zcfz_row["monetary_funds"])
                if zcfz_row.get("accounts_receivable") is not None:
                    fd.at[idx, "accounts_receivable"] = float(zcfz_row["accounts_receivable"])
                if zcfz_row.get("inventory") is not None:
                    fd.at[idx, "inventory"] = float(zcfz_row["inventory"])
                fd.at[idx, "short_term_borrowings"] = short_b
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
