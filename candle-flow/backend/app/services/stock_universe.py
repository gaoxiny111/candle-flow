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
    NAME_ALIASES,
    SYMBOL_NAMES,
    SYMBOL_WITH_MARKET,
    SymbolError,
    normalize_symbol,
)

logger = logging.getLogger(__name__)

SINA_SUGGEST_URL = "https://suggest3.sinajs.cn/suggest/type=11,12,13,22&key={key}"
REFRESH_TTL = timedelta(hours=24)
_refresh_lock = threading.Lock()
_last_refresh: datetime | None = None

_MARKET_CODE = re.compile(r"^(sh|sz|bj)(\d{6})$", re.I)
_OF_CODE = re.compile(r"^of(\d{6})$", re.I)
# 11/12/13 股票，22 场内基金/ETF
_SINA_KINDS = {"11", "12", "13", "22"}


def pinyin_abbr(name: str) -> str:
    """中文名 -> 拼音首字母缩写（小写），非汉字字符原样保留。如 贵州茅台->gzmt，伊泰B股->ytbg。"""
    text = (name or "").strip()
    if not text:
        return ""
    try:
        from pypinyin import Style, lazy_pinyin

        return "".join(lazy_pinyin(text, style=Style.FIRST_LETTER, errors=lambda x: list(x))).lower()
    except Exception:
        return ""


def code_to_symbol(code: str, market: str | None = None) -> str | None:
    raw = (code or "").strip()
    code = raw.zfill(6) if raw.isdigit() else raw
    if not CODE_ONLY.match(code):
        return None
    if market:
        m = market.strip().lower()
        if m in ("sh", "sz", "bj") or m.startswith(("sh", "sz", "bj")):
            return f"{code}.{m[:2].upper()}"
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
        if kind not in _SINA_KINDS:
            continue
        market_token = next((p for p in parts if _MARKET_CODE.match(p) or _OF_CODE.match(p)), "")
        m = _MARKET_CODE.match(market_token)
        if m:
            market, code = m.group(1).lower(), m.group(2)
        else:
            ofm = _OF_CODE.match(market_token)
            if ofm:
                code = ofm.group(1)
                market = None
            else:
                market = None
        symbol = code_to_symbol(code, market)
        if not symbol or symbol in seen:
            continue
        name = parts[4] if len(parts) > 4 else parts[0]
        if not name or name.isdigit():
            name = parts[0]
        # 新浪第 6 个字段是拼音首字母（如 gzmt），直接利用
        py = parts[5].strip().lower() if len(parts) > 5 and parts[5].strip().isascii() else ""
        items.append(
            {
                "symbol": symbol,
                "name": name,
                "code": symbol.split(".")[0],
                "market": symbol.split(".")[1],
                "pinyin": py,
            }
        )
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
    return list(rows.values())


def _upsert(db: Session, rows: Iterable[dict]) -> int:
    count = 0
    for row in rows:
        symbol = row["symbol"]
        py = (row.get("pinyin") or "").strip().lower() or pinyin_abbr(row["name"])
        existing = db.get(StockInfo, symbol)
        if existing:
            existing.name = row["name"]
            existing.code = row["code"]
            existing.market = row["market"]
            if py:
                existing.pinyin = py
        else:
            db.add(
                StockInfo(
                    symbol=symbol,
                    code=row["code"],
                    name=row["name"],
                    market=row["market"],
                    pinyin=py,
                )
            )
        count += 1
    db.commit()
    return count


def ensure_seeded(db: Session) -> None:
    if db.query(StockInfo).count() == 0:
        _upsert(db, _seed_rows())
    ensure_pinyin(db)
    ensure_market_consistent(db)


def ensure_market_consistent(db: Session) -> None:
    """纠正历史脏数据：北交所代码段（920/4/8 开头）却被标成 SH/SZ 的行。"""
    bad_rows = [
        row
        for row in db.query(StockInfo).filter(StockInfo.market.in_(("SH", "SZ"))).all()
        if (row.code or "").startswith("920") or (row.code or "").startswith(("4", "8"))
    ]
    if not bad_rows:
        return
    for row in bad_rows:
        correct_symbol = f"{row.code}.BJ"
        existing = db.get(StockInfo, correct_symbol)
        if existing:
            existing.name = row.name
            if row.pinyin:
                existing.pinyin = row.pinyin
        else:
            db.add(
                StockInfo(
                    symbol=correct_symbol,
                    code=row.code,
                    name=row.name,
                    market="BJ",
                    pinyin=row.pinyin or pinyin_abbr(row.name),
                )
            )
        db.delete(row)
    db.commit()
    logger.info("fixed %d stock_info rows with wrong market (BJ code segment)", len(bad_rows))


def ensure_pinyin(db: Session) -> None:
    """为存量数据回填拼音首字母（一次性，迁移后旧行 pinyin 为空）。"""
    missing = db.query(StockInfo).filter((StockInfo.pinyin == None) | (StockInfo.pinyin == "")).all()  # noqa: E711
    if not missing:
        return
    for row in missing:
        row.pinyin = pinyin_abbr(row.name)
    db.commit()


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
        if not rows:
            # 后备：东财全市场快照（含北交所）
            try:
                import akshare as ak

                df = ak.stock_zh_a_spot_em()
                for _, rec in df.iterrows():
                    code = str(rec.get("代码", "")).zfill(6)
                    name = str(rec.get("名称", "")).strip()
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
                if rows:
                    logger.info("stock universe refreshed via eastmoney spot fallback: %d rows", len(rows))
            except Exception as e:
                logger.warning("eastmoney spot fallback failed: %s", e)
        rows.extend(_seed_rows())
        n = _upsert(db, rows) if rows else 0
        if n:
            _last_refresh = datetime.now()
        return n


def _is_stock(item: StockInfo | dict) -> bool:
    """Supported chart symbols: A/B shares and CN indices (not futures)."""
    if isinstance(item, dict):
        sym = str(item.get("symbol", "")).upper()
        market = str(item.get("market", "")).upper()
    else:
        sym = (item.symbol or "").upper()
        market = (item.market or "").upper()
    if market == "FUT" or sym.endswith(".FUT"):
        return False
    return True


def _rank(item: StockInfo, q: str) -> tuple[int, int, str]:
    name = item.name or ""
    code = item.code or ""
    symbol = item.symbol or ""
    py = (item.pinyin or "").lower()
    qu = q.upper()
    ql = q.lower()
    if name == q:
        return (0, 0, symbol)
    if py and py == ql:
        return (1, 0, symbol)
    if name.startswith(q):
        return (2, len(name), symbol)
    if py and py.startswith(ql):
        return (3, len(py), symbol)
    if q in name:
        return (4, name.find(q), symbol)
    if code == q or symbol.upper() == qu or symbol.split(".")[0] == q:
        return (5, 0, symbol)
    if code.startswith(q) or symbol.upper().startswith(qu):
        return (6, 0, symbol)
    if py and ql in py:
        return (7, py.find(ql), symbol)
    if q in code or qu in symbol.upper():
        return (8, 0, symbol)
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
            | (StockInfo.pinyin.contains(q.lower()))
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
    alias = NAME_ALIASES.get(q) or NAME_ALIASES.get(q.lower())
    if alias:
        row = db.get(StockInfo, alias)
        if row:
            by_symbol[row.symbol] = row
    ranked = sorted(
        (r for r in by_symbol.values() if _is_stock(r)),
        key=lambda r: _rank(r, q),
    )
    return ranked[:limit]


def _stock_hit(row: StockInfo) -> dict:
    return {"symbol": row.symbol, "name": row.name, "code": row.code, "market": row.market}


def search_stocks(db: Session, query: str, limit: int = 10) -> list[dict]:
    q = (query or "").strip()
    if not q:
        return []
    ensure_seeded(db)
    local = search_local(db, q, limit=limit)
    if local:
        return [_stock_hit(r) for r in local[:limit]]
    remote = fetch_sina_suggest(q)
    if remote:
        stock_remote = [r for r in remote if _is_stock(r)]
        if stock_remote:
            _upsert(db, stock_remote)
        local = search_local(db, q, limit=limit)
    else:
        threading.Thread(target=_refresh_in_background, daemon=True).start()
    return [_stock_hit(r) for r in local[:limit]]


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
        hits = [h for h in search_stocks(db, text, limit=8) if _is_stock(h)]
        if not hits:
            raise SymbolError(f"未找到标的: {text}，可输入股票如 茅台、600519，或指数如 上证指数")
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
