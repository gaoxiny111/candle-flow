"""Eastmoney index main-force minute fund flow (累计净流入)."""

from __future__ import annotations

import logging
import time
from datetime import datetime
from typing import Any

import requests
from cachetools import TTLCache

logger = logging.getLogger(__name__)

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    ),
    "Referer": "https://data.eastmoney.com/zjlx/",
}

BROAD_INDICES = [
    {"code": "000300", "name": "沪深300", "secid": "1.000300", "color": "#1677ff"},
    {"code": "000905", "name": "中证500", "secid": "1.000905", "color": "#52c41a"},
    {"code": "000852", "name": "中证1000", "secid": "1.000852", "color": "#fa8c16"},
    {"code": "932000", "name": "中证2000", "secid": "2.932000", "color": "#722ed1"},
    {"code": "000688", "name": "科创50", "secid": "1.000688", "color": "#eb2f96"},
]

_cache: TTLCache = TTLCache(maxsize=8, ttl=120)
_last_ok: dict[str, Any] | None = None


def parse_fflow_klines(klines: list[str]) -> list[dict[str, Any]]:
    points: list[dict[str, Any]] = []
    for row in klines or []:
        parts = str(row).split(",")
        if len(parts) < 2:
            continue
        stamp = parts[0].strip()
        try:
            value = float(parts[1])
        except (TypeError, ValueError):
            continue
        if " " in stamp:
            date_part, time_part = stamp.split(" ", 1)
            hhmm = time_part[:5]
        else:
            date_part, hhmm = stamp[:10], stamp[11:16] or "15:00"
        points.append({"date": date_part, "time": hhmm, "value": value})
    return points


def _new_session() -> requests.Session:
    session = requests.Session()
    session.headers.update(HEADERS)
    return session


def _fetch_one(session: requests.Session, secid: str) -> list[str]:
    urls = [
        "https://push2delay.eastmoney.com/api/qt/stock/fflow/kline/get",
        "https://push2.eastmoney.com/api/qt/stock/fflow/kline/get",
        "https://push2his.eastmoney.com/api/qt/stock/fflow/kline/get",
    ]
    params = {
        "lmt": "0",
        "klt": "1",
        "secid": secid,
        "fields1": "f1,f2,f3,f7",
        "fields2": "f51,f52,f53,f54,f55,f56,f57,f58,f59,f60,f61,f62,f63,f64,f65",
        "ut": "b2884a393a59ad64002292a3e90d46a5",
        "_": int(time.time() * 1000),
    }
    last_err: Exception | None = None
    for url in urls:
        for attempt in range(2):
            try:
                resp = session.get(url, params=params, timeout=8)
                resp.raise_for_status()
                payload = resp.json() or {}
                data = payload.get("data") or {}
                klines = data.get("klines") or []
                if not klines:
                    raise ValueError("empty klines")
                return klines
            except Exception as exc:
                last_err = exc
                time.sleep(0.25 * (attempt + 1))
    raise last_err or RuntimeError(f"fetch failed: {secid}")


def fetch_broad_index_flow() -> dict[str, Any]:
    cached = _cache.get("broad")
    if cached:
        return cached

    series: list[dict[str, Any]] = []
    errors: list[str] = []
    session = _new_session()
    for item in BROAD_INDICES:
        try:
            points = parse_fflow_klines(_fetch_one(session, item["secid"]))
        except Exception as exc:
            logger.warning("fund flow fetch failed for %s: %s", item["name"], exc)
            errors.append(item["name"])
            points = []
        latest = points[-1]["value"] if points else None
        series.append(
            {
                "code": item["code"],
                "name": item["name"],
                "color": item["color"],
                "latest": latest,
                "points": points,
            }
        )
        time.sleep(0.25)

    order = {item["code"]: i for i, item in enumerate(BROAD_INDICES)}
    series.sort(key=lambda s: order.get(s["code"], 99))
    date = ""
    for s in series:
        if s["points"]:
            date = s["points"][-1]["date"]
            break

    result = {
        "date": date,
        "updated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "series": series,
        "partial": bool(errors) and any(s["points"] for s in series),
        "failed": errors,
    }
    if any(s["points"] for s in series):
        _cache["broad"] = result
        global _last_ok
        _last_ok = result
        return result
    if _last_ok:
        stale = dict(_last_ok)
        stale["partial"] = True
        return stale
    return result
