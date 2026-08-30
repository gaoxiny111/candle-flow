"""A-share / B-share code-name lookup and search."""
from __future__ import annotations

import logging
import re
import threading
from datetime import datetime, timedelta
from typing import Iterable, Optional
from urllib.parse import quote

import requests
from sqlalchemy.orm import Session

from app.database import SessionLocal
from app.models.stock import StockInfo
from app.utils.symbol import (
    CODE_ONLY,
    FUTURE_ALIASES,
    FUTURE_NAMES,
    NAME_ALIASES,
    SYMBOL_NAMES,
    SYMBOL_WITH_MARKET,
    SymbolError,
    futures_symbol,
    normalize_symbol,
)

logger = logging.getLogger(__name__)

SINA_SUGGEST_URL = "https://suggest3.sinajs.cn/suggest/type=11,12,13,8&key={key}"
REFRESH_TTL = timedelta(hours=24)
_refresh_lock = threading.Lock()
_last_refresh: datetime | None = None

_MARKET_CODE = re.compile(r"^(sh|sz)(\d{6})$", re.I)


def code_to_symbol(code: str, market: str | None = None) -> str | None:
    raw = (code or "").strip()
    fut = futures_symbol(raw)
    if fut:
        return fut
    code = raw.zfill(6) if raw.isdigit() else raw
    if not CODE_ONLY.match(code):
        return None
    if market:
        m = market.strip().lower()
        if m in ("sh", "sz") or m.startswith(("sh", "sz")):
            suffix = "SH" if m.startswith("sh") else "SZ"
            return f"{code}.{suffix}"
        if m.startswith("bj"):
            return None
    try:
        return normalize_symbol(code)
    except SymbolError:
        return None


def parse_sina_suggest(text: str) -> list[dict]:
    """Parse sina suggest JS payload into {symbol, name, code, market}."""
    raw = (text or "").strip()
    if "='" in raw:
        raw = raw.split("='", 1)[1]
    if raw.endswith("';"):
        raw = raw[:-2]
    elif raw.endswith("'"):
        raw = raw[:-1]
    items: list[dict] = []
    seen: set[str] = set()
    for chunk in raw.split(";"):
        parts = [p.strip() for p in chunk.split(",") if p.strip()]
        if len(parts) < 4:
            continue
        kind = parts[1] if len(parts) > 1 else ""
        code = parts[2] if len(parts) > 2 else ""
        if kind == "8" or futures_symbol(code):
            symbol = futures_symbol(code)
            if not symbol or symbol in seen:
                continue
            name = parts[4] if len(parts) > 4 else parts[0]
            if not name or name.isdigit():
                name = parts[0]
            items.append(
                {
                    "symbol": symbol,
                    "name": name,
                    "code": symbol.split(".")[0],
                    "market": "FUT",
                }
            )
            seen.add(symbol)
            continue
        if kind not in ("11", "12", "13"):
            continue
        market_token = next((p for p in parts if _MARKET_CODE.match(p)), "")
        m = _MARKET_CODE.match(market_token)
        if m:
            market, code = m.group(1).lower(), m.group(2)
        else:
            market = None
        symbol = code_to_symbol(code, market)
        if not symbol or symbol in seen:
            continue
        name = parts[4] if len(parts) > 4 else parts[0]
        if not name or name.isdigit():
            name = parts[0]
        items.append({"symbol": symbol, "name": name, "code": symbol.split(".")[0], "market": symbol.split(".")[1]})
        seen.add(symbol)
    return items


def _seed_rows() -> list[dict]:
    rows: dict[str, dict] = {}
    for symbol, name in SYMBOL_NAMES.items():
        code, market = symbol.rsplit(".", 1)
        rows[symbol] = {"symbol": symbol, "code": code, "name": name, "market": market}
    for name, symbol in NAME_ALIASES.items():
        if symbol not in rows:
            code, market = symbol.rsplit(".", 1)
            rows[symbol] = {"symbol": symbol, "code": code, "name": name, "market": market}
    for symbol, name in FUTURE_NAMES.items():
        code = symbol.split(".")[0]
        rows[symbol] = {"symbol": symbol, "code": code, "name": name, "market": "FUT"}
    return list(rows.values())


def _upsert(db: Session, rows: Iterable[dict]) -> int:
    count = 0
    for row in rows:
        symbol = row["symbol"]
        existing = db.get(StockInfo, symbol)
        if existing:
            existing.name = row["name"]
            existing.code = row["code"]
            existing.market = row["market"]
        else:
            db.add(
                StockInfo(
                    symbol=symbol,
                    code=row["code"],
                    name=row["name"],
                    market=row["market"],
                )
            )
        count += 1
    db.commit()
    return count


def ensure_seeded(db: Session) -> None:
    seeds = _seed_rows()
    if db.query(StockInfo).count() == 0:
        _upsert(db, seeds)
        return
    missing = [row for row in seeds if row["market"] == "FUT" and db.get(StockInfo, row["symbol"]) is None]
    if missing:
        _upsert(db, missing)


def fetch_sina_suggest(query: str) -> list[dict]:
    q = (query or "").strip()
    if not q:
        return []
    try:
        resp = requests.get(
            SINA_SUGGEST_URL.format(key=quote(q)),
            headers={
                "Referer": "https://finance.sina.com.cn/",
                "User-Agent": "Mozilla/5.0",
            },
            timeout=5,
        )
        resp.encoding = "gbk"
        return parse_sina_suggest(resp.text)
    except Exception as e:
        logger.warning("sina suggest failed for %s: %s", q, e)
        return []


def refresh_universe(db: Session, force: bool = False) -> int:
    global _last_refresh
    with _refresh_lock:
        if not force and _last_refresh and datetime.now() - _last_refresh < REFRESH_TTL:
            if db.query(StockInfo).count() > 10:
                return 0
        rows: list[dict] = []
        try:
            import akshare as ak

            df = ak.stock_info_a_code_name()
            for _, rec in df.iterrows():
                code = str(rec.get("code", "")).zfill(6)
                name = str(rec.get("name", "")).strip()
                symbol = code_to_symbol(code)
                if not symbol or not name or name in ("nan", "None"):
                    continue
                rows.append(
                    {
                        "symbol": symbol,
                        "code": symbol.split(".")[0],
                        "name": name,
                        "market": symbol.split(".")[1],
                    }
                )
        except Exception as e:
            logger.warning("stock universe refresh failed: %s", e)
        rows.extend(_seed_rows())
        n = _upsert(db, rows) if rows else 0
        if n:
            _last_refresh = datetime.now()
        return n


def _rank(item: StockInfo, q: str) -> tuple[int, int, str]:
    name = item.name or ""
    code = item.code or ""
    symbol = item.symbol or ""
    qu = q.upper()
    alias = FUTURE_ALIASES.get(q) or next(
        (v for k, v in FUTURE_ALIASES.items() if k.upper() == qu),
        None,
    )
    if alias and symbol == alias:
        return (-1, 0, symbol)
    if name == q:
        return (0, 0, symbol)
    if name.startswith(q):
        return (1, len(name), symbol)
    if q in name:
        return (2, name.find(q), symbol)
    if code == q or symbol.upper() == qu or symbol.split(".")[0] == q:
        return (3, 0, symbol)
    if code.startswith(q) or symbol.upper().startswith(qu):
        return (4, 0, symbol)
    if q in code or qu in symbol.upper():
        return (5, 0, symbol)
    return (9, 0, symbol)


def search_local(db: Session, query: str, limit: int = 10) -> list[StockInfo]:
    q = (query or "").strip()
    if not q:
        return []
    like = f"%{q}%"
    rows = (
        db.query(StockInfo)
        .filter(
            (StockInfo.name.contains(q))
            | (StockInfo.code.contains(q))
            | (StockInfo.symbol.contains(q.upper()))
        )
        .all()
    )
    # SQLAlchemy contains is case-sensitive on SQLite for ASCII; also match case-insensitive code
    extra = []
    if q.isdigit() or any(c.isascii() for c in q):
        extra = (
            db.query(StockInfo)
            .filter(StockInfo.symbol.ilike(like) | StockInfo.code.ilike(like))
            .all()
        )
    by_symbol = {r.symbol: r for r in rows}
    for r in extra:
        by_symbol[r.symbol] = r
    alias = NAME_ALIASES.get(q) or NAME_ALIASES.get(q.lower()) or FUTURE_ALIASES.get(q)
    if not alias:
        alias = next((v for k, v in FUTURE_ALIASES.items() if k.upper() == q.upper()), None)
    if alias:
        row = db.get(StockInfo, alias)
        if row:
            by_symbol[row.symbol] = row
    ranked = sorted(by_symbol.values(), key=lambda r: _rank(r, q))
    return ranked[:limit]


def search_stocks(db: Session, query: str, limit: int = 10) -> list[dict]:
    q = (query or "").strip()
    if not q:
        return []
    ensure_seeded(db)
    local = search_local(db, q, limit=limit)
    # 本地已有结果时不要等新浪，否则输入「茅台」会卡住几秒看不到下拉
    if local:
        return [
            {"symbol": r.symbol, "name": r.name, "code": r.code, "market": r.market}
            for r in local[:limit]
        ]
    remote = fetch_sina_suggest(q)
    if remote:
        _upsert(db, remote)
        local = search_local(db, q, limit=limit)
    else:
        threading.Thread(target=_refresh_in_background, daemon=True).start()
    return [
        {"symbol": r.symbol, "name": r.name, "code": r.code, "market": r.market}
        for r in local[:limit]
    ]


def _refresh_in_background() -> None:
    db = SessionLocal()
    try:
        refresh_universe(db)
    except Exception as e:
        logger.warning("background universe refresh failed: %s", e)
    finally:
        db.close()


def lookup_name(db: Session, symbol: str) -> str:
    try:
        symbol = normalize_symbol(symbol)
    except SymbolError:
        return ""
    row = db.get(StockInfo, symbol)
    if row:
        return row.name
    return SYMBOL_NAMES.get(symbol, "")


def resolve_symbol(raw: str, db: Optional[Session] = None) -> str:
    """Resolve code or Chinese name to 000001.SZ form."""
    text = (raw or "").strip()
    if not text:
        raise SymbolError("股票代码不能为空")
    try:
        return normalize_symbol(text)
    except SymbolError:
        pass

    own_session = db is None
    if own_session:
        db = SessionLocal()
    try:
        ensure_seeded(db)
        hits = search_stocks(db, text, limit=8)
        if not hits:
            raise SymbolError(f"未找到标的: {text}，可输入股票如 茅台，或期货如 螺纹钢、RB0")
        exact = [h for h in hits if h["name"] == text]
        if len(exact) == 1:
            return exact[0]["symbol"]
        if len(hits) == 1:
            return hits[0]["symbol"]
        starts = [h for h in hits if h["name"].startswith(text)]
        if len(starts) == 1:
            return starts[0]["symbol"]
        # unambiguous if first is clearly better (exact/prefix vs others)
        top = hits[0]
        if top["name"] == text or top["name"].startswith(text):
            return top["symbol"]
        names = "、".join(f"{h['name']}({h['symbol']})" for h in hits[:5])
        raise SymbolError(f"「{text}」对应多只标的，请选择：{names}")
    finally:
        if own_session and db is not None:
            db.close()
