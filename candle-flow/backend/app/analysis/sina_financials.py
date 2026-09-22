"""
新浪财经历史报表数据源（原始披露口径）。

背景：东财 F10 在同一控制下企业合并（如中国神华 2026 年资产注入）后，
会按 CAS 追溯重述比较期报表，导致历史年报的总资产/总负债/负债率被改写，
并非当时审计披露口径。新浪的报表页保留公司原始披露（年报为审计口径），
用于校准：
- 资产负债率 / 有息负债（含「一年内到期的非流动负债」）
- 真实资本开支（替代 OCF×0.25 的占位估算）与自由现金流
- 应收票据 / 应收账款 / 货币资金等明细
- 单季利润（累计值差分）

单位：新浪表格为万元，本模块统一换算为元。
"""

from __future__ import annotations

import threading
import time
from io import StringIO
from typing import Any

import pandas as pd

from app.utils.symbol import SymbolError, normalize_symbol, parse_symbol

_BASE = (
    "http://money.finance.sina.com.cn/corp/go.php/{page}/stockid/"
    "{code}/ctrl/{year}/displaytype/4.phtml"
)
_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)",
    "Referer": "https://finance.sina.com.cn/",
}

PAGE_BALANCE = "vFD_BalanceSheet"
PAGE_CASHFLOW = "vFD_CashFlow"
PAGE_INCOME = "vFD_ProfitStatement"

# 需要抽取的精确行标签
BS_FIELDS: dict[str, str] = {
    "monetary_funds": "货币资金",
    "notes_receivable": "应收票据",
    "accounts_receivable": "应收账款",
    "inventory": "存货",
    "goodwill": "商誉",
    "total_assets": "资产总计",
    "short_term_borrowings": "短期借款",
    "accounts_payable": "应付账款",
    "non_current_due_within_1y": "一年内到期的非流动负债",
    "short_term_bonds": "应付短期债券",
    "long_term_borrowings": "长期借款",
    "bonds_payable": "应付债券",
    "lease_liabilities": "租赁负债",
    "total_liabilities": "负债合计",
    "parent_equity": "归属于母公司股东权益合计",
    "total_current_liabilities": "流动负债合计",
    "advance_receipts": "预收款项",
    "tax_payable": "应交税费",
    "staff_salary_payable": "应付职工薪酬",
    "other_payable": "其他应付款",
}
CF_FIELDS: dict[str, str] = {
    "operating_cashflow": "经营活动产生的现金流量净额",
    "capex": "购建固定资产、无形资产和其他长期资产所支付的现金",
    "disposal_long_asset_cash": "处置固定资产、无形资产和其他长期资产所收回的现金净额",
    "dividend_interest_paid": "分配股利、利润或偿付利息所支付的现金",
    "net_profit": "净利润",
}
IS_FIELDS: dict[str, str] = {
    "revenue": "一、营业总收入",
    "cogs": "营业成本",
    "operating_profit": "三、营业利润",
    "net_profit": "五、净利润",
    "parent_net_profit": "归属于母公司所有者的净利润",
    "eps": "基本每股收益(元/股)",
}
# 说明：新浪利润表**不提供**「扣除非经常性损益后的净利润」行（实测 603519
# 2025 年报页无任何含「扣」的 <th> 标签）。扣非数据统一走东财
# ``financials.fetch_deducted_series``（RPT_DMSK_FN_INCOME.DEDUCT_PARENT_NETPROFIT），
# 不要在本字段表里添加会恒为 None 的「扣非」行。

_TTL = 12 * 3600
_cache: dict[str, tuple[float, dict[str, dict[str, float | None]]]] = {}
_cache_lock = threading.Lock()


def _code6(symbol: str) -> str | None:
    try:
        code, _ = parse_symbol(normalize_symbol(symbol))
        return code
    except SymbolError:
        digits = "".join(ch for ch in str(symbol) if ch.isdigit())
        return digits[-6:] if len(digits) >= 6 else None


def _to_ymd(raw: Any) -> str | None:
    s = str(raw or "").replace("-", "").replace("/", "")[:8]
    return s if len(s) == 8 and s.isdigit() else None


def _num_wan(v: Any) -> float | None:
    """新浪单元格 → 元（原始单位万元）。'--' / 空 → None。"""
    if v is None:
        return None
    s = str(v).strip().replace(",", "")
    if s in ("", "--", "nan", "None"):
        return None
    try:
        return float(s) * 1e4
    except ValueError:
        return None


def fetch_statement_year(symbol: str, page: str, year: int) -> dict[str, dict[str, float | None]]:
    """抓取某一年四个报告期的报表 → {YYYYMMDD: {field: 元}}。"""
    import requests

    code = _code6(symbol)
    if not code:
        return {}
    cache_key = f"{code}:{page}:{year}"
    now = time.time()
    with _cache_lock:
        hit = _cache.get(cache_key)
        if hit and now - hit[0] < _TTL:
            return hit[1]

    fields: dict[str, str]
    if page == PAGE_BALANCE:
        fields = BS_FIELDS
    elif page == PAGE_CASHFLOW:
        fields = CF_FIELDS
    else:
        fields = IS_FIELDS

    out: dict[str, dict[str, float | None]] = {}
    try:
        resp = requests.get(
            _BASE.format(page=page, code=code, year=year),
            headers=_HEADERS,
            timeout=15,
        )
        resp.encoding = "gb2312"
        if not resp.ok:
            raise ValueError(f"http {resp.status_code}")
        tables = pd.read_html(StringIO(resp.text))
        table = max(tables, key=lambda x: x.shape[0] * x.shape[1])
        # 首行：报表日期 + 各报告期列
        header = [str(x) for x in table.iloc[0].tolist()]
        date_cols: dict[str, int] = {}
        for col_idx, cell in enumerate(header):
            ymd = _to_ymd(cell)
            if ymd:
                date_cols[ymd] = col_idx
        label_to_field = {label: name for name, label in fields.items()}
        for _, row in table.iterrows():
            label = str(row.iloc[0]).strip() if pd.notna(row.iloc[0]) else ""
            field = label_to_field.get(label)
            if field is None:
                continue
            for ymd, col_idx in date_cols.items():
                val = _num_wan(row.iloc[col_idx]) if col_idx < len(row) else None
                out.setdefault(ymd, {})[field] = val
    except Exception:
        # 新浪反爬 / 编码异常时静默降级（调用方回退东财口径）
        out = {}

    with _cache_lock:
        _cache[cache_key] = (now, out)
    return out


def fetch_statements(
    symbol: str,
    page: str,
    years: list[int],
) -> dict[str, dict[str, float | None]]:
    """合并多年报告期 → {YYYYMMDD: {field: 元}}（新年份优先）。"""
    merged: dict[str, dict[str, float | None]] = {}
    for year in sorted(set(years), reverse=True):
        for ymd, vals in fetch_statement_year(symbol, page, year).items():
            merged.setdefault(ymd, {}).update(vals)
    return merged


def interest_bearing_debt(bs_row: dict[str, float | None]) -> float:
    """有息负债 = 短借 + 长借 + 应付债券 + 应付短期债券 + 一年内到期非流动负债。"""
    keys = (
        "short_term_borrowings",
        "long_term_borrowings",
        "bonds_payable",
        "short_term_bonds",
        "non_current_due_within_1y",
    )
    return float(sum(float(bs_row.get(k) or 0) for k in keys))


def free_cashflow(cf_row: dict[str, float | None]) -> float | None:
    ocf = cf_row.get("operating_cashflow")
    capex = cf_row.get("capex")
    if ocf is None or capex is None:
        return None
    return float(ocf) - abs(float(capex))
