"""Watchlist valuation (PE / PB / market cap) from Tencent / Eastmoney quotes."""

from __future__ import annotations

import json
import logging
import time
from datetime import date, datetime, timedelta
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Optional

from app.services.watchlist import MAX_WATCHLIST
from app.utils.symbol import SymbolError, is_b_share, is_future, normalize_symbol, parse_symbol

logger = logging.getLogger(__name__)

TENCENT_QUOTE = "https://qt.gtimg.cn/q="
EASTMONEY_ULIST = "https://push2.eastmoney.com/api/qt/ulist.np/get"
EASTMONEY_HISTORY = "https://datacenter-web.eastmoney.com/api/data/v1/get"
BAIDU_VALUATION = "https://gushitong.baidu.com/opendata"
# f2 最新价 f3 涨跌幅 f9 市盈率(动) f12 代码 f13 市场 f14 名称
# f20/f116 总市值 f23 市净率 f115 市盈率(TTM) f133 股息率(TTM %)
QUOTE_FIELDS = "f2,f3,f9,f12,f13,f14,f20,f23,f115,f116,f133"
CACHE_TTL_SEC = 180
HIST_TTL_SEC = 12 * 3600
HIST_YEARS = 10
_HEADERS = {
    "User-Agent": "Mozilla/5.0",
    "Referer": "https://finance.eastmoney.com",
}

_cache: dict[str, tuple[float, dict[str, Any]]] = {}
_hist_cache: dict[str, tuple[float, tuple[list[float], list[float]]]] = {}


def _num(value: Any) -> Optional[float]:
    if value in (None, "", "-"):
        return None
    try:
        n = float(value)
    except (TypeError, ValueError):
        return None
    if n != n:  # NaN
        return None
    return n


def _empty(symbol: str, name: str = "") -> dict[str, Any]:
    return {
        "symbol": symbol,
        "name": name,
        "price": None,
        "change_pct": None,
        "pe_ttm": None,
        "pe_dynamic": None,
        "pb": None,
        "market_cap": None,
        "dividend_yield": None,
        "pe_percentile": None,
        "pb_percentile": None,
        "percentiles_pending": False,
    }


def _secid(symbol: str) -> Optional[str]:
    if is_future(symbol):
        return None
    code, market = parse_symbol(symbol)
    if market == "sh":
        return f"1.{code}"
    if market in ("sz", "bj"):
        # 东财北交所 secid 市场位与深市同为 0
        return f"0.{code}"
    return None


def _symbol_from_row(row: dict[str, Any]) -> Optional[str]:
    code = str(row.get("f12") or "").zfill(6)
    if len(code) != 6 or not code.isdigit():
        return None
    market = row.get("f13")
    if market in (1, "1"):
        return f"{code}.SH"
    if market in (0, "0"):
        # 东财 f13=0 涵盖深市与北交所，按代码段区分
        if code.startswith("920") or code.startswith(("4", "8")):
            return f"{code}.BJ"
        return f"{code}.SZ"
    return None


def _row_to_item(symbol: str, row: dict[str, Any]) -> dict[str, Any]:
    return {
        "symbol": symbol,
        "name": str(row.get("f14") or ""),
        "price": _num(row.get("f2")),
        "change_pct": _num(row.get("f3")),
        "pe_ttm": _num(row.get("f115")),
        "pe_dynamic": _num(row.get("f9")),
        "pb": _num(row.get("f23")),
        "market_cap": _num(row.get("f116")) or _num(row.get("f20")),
        "dividend_yield": _num(row.get("f133")),
    }


def _tencent_code(symbol: str) -> Optional[str]:
    if is_future(symbol):
        return None
    code, market = parse_symbol(symbol)
    if market not in ("sh", "sz", "bj"):
        return None
    return f"{market}{code}"


def _parse_tencent(text: str, wanted: set[str]) -> dict[str, dict[str, Any]]:
    out: dict[str, dict[str, Any]] = {}
    for chunk in text.split(";"):
        chunk = chunk.strip()
        if not chunk or "=" not in chunk:
            continue
        _, _, rest = chunk.partition("=")
        fields = rest.strip().strip('"').split("~")
        if len(fields) < 47:
            continue
        code = (fields[2] or "").zfill(6)
        key = chunk.split("=")[0].replace("v_", "").strip()
        market = (
            "SH"
            if key.startswith("sh")
            else "SZ"
            if key.startswith("sz")
            else "BJ"
            if key.startswith("bj")
            else ""
        )
        if not market or len(code) != 6:
            continue
        symbol = f"{code}.{market}"
        if symbol not in wanted:
            continue
        cap_yi = _num(fields[45])
        out[symbol] = {
            "symbol": symbol,
            "name": fields[1] or "",
            "price": _num(fields[3]),
            "change_pct": _num(fields[32]),
            "pe_ttm": _num(fields[39]),
            "pe_dynamic": _num(fields[39]),
            "pb": _num(fields[46]),
            "market_cap": cap_yi * 1e8 if cap_yi is not None else None,
            "dividend_yield": None,
        }
    return out


def _fetch_tencent(symbols: list[str]) -> dict[str, dict[str, Any]]:
    import requests

    wanted: set[str] = set()
    codes: list[str] = []
    for symbol in symbols:
        code = _tencent_code(symbol)
        if not code:
            continue
        codes.append(code)
        wanted.add(symbol)
    if not codes:
        return {}
    try:
        r = requests.get(
            TENCENT_QUOTE + ",".join(codes),
            headers=_HEADERS,
            timeout=8,
        )
        text = r.content.decode("gbk", errors="replace")
    except Exception as e:
        logger.warning("tencent valuation failed for %s: %s", symbols, e)
        return {}
    return _parse_tencent(text, wanted)


def _fetch_eastmoney(symbols: list[str]) -> dict[str, dict[str, Any]]:
    import requests

    secids = []
    wanted: set[str] = set()
    for symbol in symbols:
        sid = _secid(symbol)
        if not sid:
            continue
        secids.append(sid)
        wanted.add(symbol)
    if not secids:
        return {}

    last_err: Exception | None = None
    for attempt in range(2):
        try:
            r = requests.get(
                EASTMONEY_ULIST,
                params={
                    "fltt": "2",
                    "invt": "2",
                    "fields": QUOTE_FIELDS,
                    "secids": ",".join(secids),
                },
                headers=_HEADERS,
                timeout=8,
            )
            payload = r.json() or {}
            rows = ((payload.get("data") or {}).get("diff")) or []
            out: dict[str, dict[str, Any]] = {}
            if isinstance(rows, dict):
                rows = list(rows.values())
            for row in rows:
                if not isinstance(row, dict):
                    continue
                symbol = _symbol_from_row(row)
                if not symbol or symbol not in wanted:
                    continue
                out[symbol] = _row_to_item(symbol, row)
            return out
        except Exception as e:
            last_err = e
            if attempt == 0:
                time.sleep(0.4)
    logger.warning("eastmoney valuation failed for %s: %s", symbols, last_err)
    return {}


def _fetch_quotes(symbols: list[str]) -> dict[str, dict[str, Any]]:
    # 东财优先：含股息率；腾讯补缺价/涨跌
    data = _fetch_eastmoney(symbols)
    missing = [s for s in symbols if s not in data]
    if missing:
        data.update(_fetch_tencent(missing))
    # 东财部分失败时，腾讯命中的票再试一次补股息
    need_dy = [s for s in symbols if data.get(s) and data[s].get("dividend_yield") is None]
    if need_dy:
        for s, item in _fetch_eastmoney(need_dy).items():
            dy = item.get("dividend_yield")
            if s in data and dy is not None:
                data[s]["dividend_yield"] = dy
            elif s not in data:
                data[s] = item
    return data


def percentile_rank(current: Optional[float], series: list[float]) -> Optional[float]:
    """Percent of historical values that are below *current* (lower = cheaper)."""
    if current is None or current <= 0:
        return None
    vals = [v for v in series if v is not None and v > 0]
    if len(vals) < 20:
        return None
    below = sum(1 for v in vals if v < current)
    equal = sum(1 for v in vals if v == current)
    return round(100.0 * (below + 0.5 * equal) / len(vals), 1)


def _cutoff_date() -> date:
    return date.today() - timedelta(days=365 * HIST_YEARS)


def _series_from_eastmoney_rows(rows: list[Any]) -> tuple[list[float], list[float]]:
    cutoff = _cutoff_date()
    pe_s: list[float] = []
    pb_s: list[float] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        raw_date = str(row.get("TRADE_DATE") or "")[:10]
        try:
            d = date.fromisoformat(raw_date)
        except ValueError:
            d = None
        if d is not None and d < cutoff:
            continue
        pe = _num(row.get("PE_TTM"))
        pb = _num(row.get("PB_MRQ"))
        if pe is not None and pe > 0:
            pe_s.append(pe)
        if pb is not None and pb > 0:
            pb_s.append(pb)
    return pe_s, pb_s


def _parse_baidu_chart(payload: Any) -> list[float]:
    try:
        body = payload["Result"][0]["DisplayData"]["resultData"]["tplData"]["result"]["chartInfo"][0]["body"]
    except (KeyError, IndexError, TypeError):
        return []
    cutoff = _cutoff_date()
    out: list[float] = []
    for row in body or []:
        if not isinstance(row, (list, tuple)) or len(row) < 2:
            continue
        raw_date = str(row[0] or "")[:10]
        try:
            d = date.fromisoformat(raw_date)
        except ValueError:
            d = None
        if d is not None and d < cutoff:
            continue
        n = _num(row[1])
        if n is not None and n > 0:
            out.append(n)
    return out


def _fetch_eastmoney_history(code: str) -> tuple[list[float], list[float]]:
    import requests

    try:
        r = requests.get(
            EASTMONEY_HISTORY,
            params={
                "sortColumns": "TRADE_DATE",
                "sortTypes": -1,
                "pageSize": 2600,
                "pageNumber": 1,
                "reportName": "RPT_VALUEANALYSIS_DET",
                "columns": "TRADE_DATE,PE_TTM,PB_MRQ",
                "source": "WEB",
                "client": "WEB",
                "filter": f'(SECURITY_CODE="{code}")',
            },
            headers=_HEADERS,
            timeout=12,
        )
        rows = ((r.json() or {}).get("result") or {}).get("data") or []
    except Exception as e:
        logger.warning("eastmoney valuation history failed for %s: %s", code, e)
        return [], []
    return _series_from_eastmoney_rows(rows)


def _fetch_baidu_series(code: str, indicator: str) -> list[float]:
    import requests

    try:
        r = requests.get(
            BAIDU_VALUATION,
            params={
                "openapi": "1",
                "dspName": "iphone",
                "tn": "tangram",
                "client": "app",
                "query": indicator,
                "code": code,
                "word": "",
                "resource_id": "51171",
                "market": "ab",
                "tag": indicator,
                "chart_select": "近十年",
                "industry_select": "",
                "skip_industry": "1",
                "finClientType": "pc",
            },
            headers={"User-Agent": "Mozilla/5.0", "Referer": "https://gushitong.baidu.com"},
            timeout=12,
        )
        return _parse_baidu_chart(r.json())
    except Exception as e:
        logger.warning("baidu valuation history failed for %s %s: %s", code, indicator, e)
        return []


def _fetch_valuation_history(symbol: str) -> tuple[list[float], list[float]]:
    """Daily PE / PB history. B-shares skip Eastmoney (empty there) and use Baidu."""
    try:
        code, _ = parse_symbol(symbol)
    except SymbolError:
        return [], []
    if is_b_share(symbol):
        return (
            _fetch_baidu_series(code, "市盈率(TTM)"),
            _fetch_baidu_series(code, "市净率"),
        )
    pe_s, pb_s = _fetch_eastmoney_history(code)
    if len(pe_s) < 20:
        pe_s = _fetch_baidu_series(code, "市盈率(TTM)") or pe_s
    if len(pb_s) < 20:
        pb_s = _fetch_baidu_series(code, "市净率") or pb_s
    return pe_s, pb_s


_hist_pool = ThreadPoolExecutor(max_workers=4, thread_name_prefix="val-hist")
_inflight: set[str] = set()


def _parse_hist_json(raw: str | None) -> list[float]:
    try:
        data = json.loads(raw or "[]")
    except json.JSONDecodeError:
        return []
    if not isinstance(data, list):
        return []
    out: list[float] = []
    for item in data:
        n = _num(item)
        if n is not None and n > 0:
            out.append(n)
    return out


def upsert_history(db: Any, symbol: str, pe_s: list[float], pb_s: list[float]) -> None:
    from app.models.valuation import ValuationHistory

    row = db.get(ValuationHistory, symbol)
    payload_pe = json.dumps(pe_s)
    payload_pb = json.dumps(pb_s)
    now = datetime.utcnow()
    if row is None:
        db.add(ValuationHistory(symbol=symbol, pe_json=payload_pe, pb_json=payload_pb, updated_at=now))
    else:
        row.pe_json = payload_pe
        row.pb_json = payload_pb
        row.updated_at = now
    db.commit()


def _load_histories_from_db(db: Any, symbols: list[str]) -> dict[str, tuple[datetime, list[float], list[float]]]:
    from app.models.valuation import ValuationHistory

    if not symbols:
        return {}
    rows = db.query(ValuationHistory).filter(ValuationHistory.symbol.in_(symbols)).all()
    out: dict[str, tuple[datetime, list[float], list[float]]] = {}
    for row in rows:
        updated = row.updated_at or datetime.utcnow()
        out[row.symbol] = (updated, _parse_hist_json(row.pe_json), _parse_hist_json(row.pb_json))
    return out


def _refresh_history_job(symbol: str) -> None:
    from app.database import SessionLocal

    try:
        pe_s, pb_s = _fetch_valuation_history(symbol)
        db = SessionLocal()
        try:
            upsert_history(db, symbol, pe_s, pb_s)
        finally:
            db.close()
        _hist_cache[symbol] = (time.time(), (pe_s, pb_s))
    except Exception as e:
        logger.warning("background valuation history failed for %s: %s", symbol, e)
    finally:
        _inflight.discard(symbol)


def _schedule_history(symbols: list[str]) -> None:
    for symbol in symbols:
        if symbol in _inflight:
            continue
        _inflight.add(symbol)
        _hist_pool.submit(_refresh_history_job, symbol)


def _histories_for(
    symbols: list[str],
    now: float,
    db: Any = None,
) -> tuple[dict[str, tuple[list[float], list[float]]], list[str]]:
    """Return cached series immediately; missing/stale symbols are fetched in the background."""
    out: dict[str, tuple[list[float], list[float]]] = {}
    need: list[str] = []
    memory_hit: set[str] = set()
    for symbol in symbols:
        cached = _hist_cache.get(symbol)
        if cached and now - cached[0] < HIST_TTL_SEC:
            out[symbol] = cached[1]
            memory_hit.add(symbol)

    remaining = [s for s in symbols if s not in memory_hit]
    own_db = None
    session = db
    if remaining and session is None:
        from app.database import SessionLocal

        own_db = SessionLocal()
        session = own_db
    try:
        stored = _load_histories_from_db(session, remaining) if session is not None and remaining else {}
    finally:
        if own_db is not None:
            own_db.close()

    for symbol in remaining:
        row = stored.get(symbol)
        if not row:
            need.append(symbol)
            continue
        updated, pe_s, pb_s = row
        age = now - updated.replace(tzinfo=None).timestamp()
        out[symbol] = (pe_s, pb_s)
        _hist_cache[symbol] = (now, (pe_s, pb_s))
        if age > HIST_TTL_SEC:
            need.append(symbol)

    if need:
        _schedule_history(need)
    pending = [s for s in symbols if s not in out or (len(out[s][0]) < 20 and len(out[s][1]) < 20)]
    return out, pending


def clear_cache() -> None:
    _cache.clear()
    _hist_cache.clear()
    _inflight.clear()


def get_valuations(symbols: list[str], *, now: Optional[float] = None, db: Any = None) -> list[dict[str, Any]]:
    """Quotes first; percentiles from SQLite/memory, history refresh is background."""
    ts = time.time() if now is None else now
    ordered: list[str] = []
    seen: set[str] = set()
    for raw in symbols:
        item = (raw or "").strip()
        if not item:
            continue
        try:
            symbol = normalize_symbol(item)
        except SymbolError:
            continue
        if symbol in seen:
            continue
        seen.add(symbol)
        ordered.append(symbol)
        if len(ordered) >= MAX_WATCHLIST:
            break

    quotes: dict[str, dict[str, Any]] = {}
    need: list[str] = []
    for symbol in ordered:
        if is_future(symbol):
            quotes[symbol] = _empty(symbol)
            continue
        cached = _cache.get(symbol)
        if cached and ts - cached[0] < CACHE_TTL_SEC:
            quotes[symbol] = cached[1]
        else:
            need.append(symbol)

    if need:
        fetched = _fetch_quotes(need)
        for symbol in need:
            item = fetched.get(symbol) or _empty(symbol)
            _cache[symbol] = (ts, item)
            quotes[symbol] = item

    stocks = [s for s in ordered if not is_future(s)]
    hists, pending = _histories_for(stocks, ts, db=db) if stocks else ({}, [])
    pending_set = set(pending)

    rows: list[dict[str, Any]] = []
    for symbol in ordered:
        item = dict(quotes[symbol])
        pe_s, pb_s = hists.get(symbol, ([], []))
        item["pe_percentile"] = percentile_rank(item.get("pe_ttm"), pe_s)
        item["pb_percentile"] = percentile_rank(item.get("pb"), pb_s)
        item["percentiles_pending"] = symbol in pending_set
        rows.append(item)
    return rows
