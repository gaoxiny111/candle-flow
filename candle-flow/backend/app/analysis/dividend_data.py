"""真实分红历史（东财 RPT_SHAREBONUS_DET）：用于 DDM 的 D0、分红率与分红确定性评分。"""

from __future__ import annotations

import threading
import time
from typing import Any

_TTL = 6 * 3600
_cache: dict[str, tuple[float, dict[str, Any]]] = {}
_lock = threading.Lock()


def _code6(symbol: str) -> str:
    from app.utils.symbol import SymbolError, normalize_symbol, parse_symbol

    try:
        code, _ = parse_symbol(normalize_symbol(symbol))
        return code
    except SymbolError:
        digits = "".join(ch for ch in str(symbol) if ch.isdigit())
        return digits[-6:] if len(digits) >= 6 else ""


def fetch_dividend_history(symbol: str, limit: int = 30) -> dict[str, Any]:
    """
    返回：
    {
      "plans": [{report_date, dps, cash, status, shares, ex_date, eps}],
      "latest_fy": {"year": 2025, "dps": 2.01, "cash_total": ..., "implemented": bool},
      "current_interim": {"report_date": "20260630", "dps": 0.98, "status": ...} | None,
      "consecutive_years": 18,
    }
    一个会计年度的 D0 = 该年度中期 + 末期每股分红之和（神华 2025: 0.98+1.03=2.01）。
    """
    import requests

    code = _code6(symbol)
    if not code:
        return {}
    now = time.time()
    with _lock:
        hit = _cache.get(code)
        if hit and now - hit[0] < _TTL:
            return hit[1]

    out: dict[str, Any] = {}
    try:
        resp = requests.get(
            "https://datacenter-web.eastmoney.com/api/data/v1/get",
            params={
                "reportName": "RPT_SHAREBONUS_DET",
                "columns": (
                    "SECURITY_CODE,REPORT_DATE,EX_DIVIDEND_DATE,PRETAX_BONUS_RMB,"
                    "ASSIGN_PROGRESS,TOTAL_SHARES,BASIC_EPS"
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
            timeout=15,
        )
        rows = ((resp.json() or {}).get("result") or {}).get("data") or []
    except Exception:
        rows = []

    plans: list[dict[str, Any]] = []
    for row in rows:
        rd = str(row.get("REPORT_DATE") or "")[:10].replace("-", "")
        if len(rd) != 8:
            continue
        pretax = row.get("PRETAX_BONUS_RMB")  # 每10股派现（元，含税）
        dps = float(pretax) / 10.0 if pretax is not None else None
        shares = row.get("TOTAL_SHARES")
        cash = dps * float(shares) if dps is not None and shares else None
        plans.append(
            {
                "report_date": rd,
                "dps": dps,
                "cash": cash,
                "status": str(row.get("ASSIGN_PROGRESS") or ""),
                "shares": float(shares) if shares else None,
                "ex_date": (str(row.get("EX_DIVIDEND_DATE") or "")[:10] or None),
                "eps": row.get("BASIC_EPS"),
            }
        )

    # 按会计年度归集（中期 0630 与末期 1231 同属一个 FY）
    fy_groups: dict[int, list[dict[str, Any]]] = {}
    for p in plans:
        if p.get("dps") is None:
            continue
        fy_groups.setdefault(int(p["report_date"][:4]), []).append(p)

    latest_fy: dict[str, Any] | None = None
    for fy in sorted(fy_groups, reverse=True):
        has_final = any(p["report_date"].endswith("1231") for p in fy_groups[fy])
        if not has_final:
            continue
        group = fy_groups[fy]
        dps_total = round(sum(float(p["dps"]) for p in group), 4)
        cash_total = sum(float(p["cash"]) for p in group if p.get("cash"))
        latest_fy = {
            "year": fy,
            "dps": dps_total,
            "cash_total": round(cash_total, 2) if cash_total else None,
            "implemented": all("实施" in p.get("status", "") or p.get("ex_date") for p in group),
            "plans": len(group),
        }
        break

    current_interim: dict[str, Any] | None = None
    if latest_fy:
        nxt = latest_fy["year"] + 1
        for p in plans:
            if p["report_date"] == f"{nxt}0630" and p.get("dps"):
                current_interim = {
                    "report_date": p["report_date"],
                    "dps": p["dps"],
                    "status": p.get("status"),
                }
                break

    consecutive_years = 0
    if latest_fy:
        for fy in range(latest_fy["year"], latest_fy["year"] - 30, -1):
            group = fy_groups.get(fy)
            if group and any(float(p["dps"]) > 0 for p in group):
                consecutive_years += 1
            else:
                break

    out = {
        "plans": plans,
        "latest_fy": latest_fy,
        "current_interim": current_interim,
        "consecutive_years": consecutive_years,
    }
    with _lock:
        _cache[code] = (now, out)
    return out
