"""个股资金面（东方财富口径）：主力资金日频净流入、融资融券、龙虎榜。

口径声明（改动前先读）：
- 「主力」= 超大单 + 大单（东方财富成交结构口径），**不是同花顺 DDX**；
  净额为「当日成交中主动大额资金净额」，属成交结构统计，不等于持仓变动。
- 单位统一为元，展示层再转万元/亿元；不做四舍五入以外的改写。
- 北向资金个股日频持股自 2024-08 起停止披露（改为季度）→ 本模块不含北向。
- 本模块只提供展示口径数值，**不参与任何打分与筛选**（项目铁律：资金面无评分口径）。

数据源特性：东财 push2his 对**新建连接有速率冷却**（短时间内连续新建连接会被 RST，
约 15 秒后恢复；push2delay 不提供日频数据）。故主力资金走「落库缓存 + 机会性增量同步」：
- 展示层优先读 `stock_fund_flow_daily`，页面加载不做同步等待（仅该标的首次无数据时才同步等一次）；
- 库内数据落后于最近交易日时，起后台线程刷新（同标的 2 分钟内最多尝试一次，全局串行）；
- 联网失败不影响展示，继续用库内已有数据。
"""

from __future__ import annotations

import logging
import threading
import time
from datetime import date, datetime, timedelta
from typing import Any, Optional

import requests
from cachetools import TTLCache
from sqlalchemy.orm import Session

from app.models.stock_fund_flow import StockFundFlowDaily
from app.utils.symbol import (
    SymbolError,
    is_etf_symbol,
    is_future,
    is_index_symbol,
    normalize_symbol,
    parse_symbol,
)

logger = logging.getLogger(__name__)

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    ),
    "Referer": "https://data.eastmoney.com/zjlx/",
}
UT = "b2884a393a59ad64002292a3e90d46a5"
# push2delay 不提供日频（klt=101）资金流，故只用 push2his
FFLOW_HOSTS = ("push2his.eastmoney.com",)
DATACENTER = "https://datacenter-web.eastmoney.com/api/data/v1/get"
FLOW_FIELDS2 = "f51,f52,f53,f54,f55,f56,f57,f58,f59,f60,f61,f62,f63,f64,f65"

FLOW_TTL = 600
MARGIN_TTL = 1800
LHB_TTL = 1800
FETCH_BUDGET_SEC = 5.0
FLOW_DB_DAYS = 90

_flow_cache: TTLCache = TTLCache(maxsize=512, ttl=FLOW_TTL)
_margin_cache: TTLCache = TTLCache(maxsize=512, ttl=MARGIN_TTL)
_lhb_cache: TTLCache = TTLCache(maxsize=512, ttl=LHB_TTL)
# 同一标的的联网尝试节流（含失败后 2 分钟重试）/ 已确认数据源无更新后的冷却
_sync_try: TTLCache = TTLCache(maxsize=1024, ttl=120)
_sync_fresh: TTLCache = TTLCache(maxsize=4096, ttl=7200)
# 抓取失败时的兜底（保留最近一次成功结果）
_stale: TTLCache = TTLCache(maxsize=256, ttl=3600)
# fflow 抓取串行化：并发新建连接会触发东财冷却（自己把自己打挂）
_FF_LOCK = threading.Lock()
_FF_LAST = [0.0]
FF_MIN_GAP_SEC = 1.5


def secid_for(symbol: str) -> Optional[str]:
    """东方财富 secid：沪市 1.xxxxxx，深市/北交所 0.xxxxxx。"""
    try:
        code, market = parse_symbol(symbol)
    except SymbolError:
        return None
    if market == "fut":
        return None
    return f"{1 if market == 'sh' else 0}.{code}"


def is_supported(symbol: str) -> bool:
    if not symbol:
        return False
    if is_future(symbol) or is_index_symbol(symbol) or is_etf_symbol(symbol):
        return False
    try:
        normalize_symbol(symbol)
    except SymbolError:
        return False
    return True


def _fmt_money(v: float | None) -> str:
    """元 → 万元/亿元（带符号）。"""
    if v is None:
        return "—"
    sign = "-" if v < 0 else "+"
    a = abs(float(v))
    if a >= 1e8:
        return f"{sign}{a / 1e8:.2f} 亿"
    if a >= 1e4:
        return f"{sign}{a / 1e4:.0f} 万"
    return f"{sign}{a:.0f} 元"


def _num(v: Any) -> Optional[float]:
    if v in (None, "", "-", "--"):
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _last_trading_date() -> date:
    """最近一个工作日（不处理法定节假日，节假日只会多尝试一次同步）。"""
    d = datetime.now().date()
    while d.weekday() >= 5:
        d -= timedelta(days=1)
    return d


# ── 主力资金日频 ─────────────────────────────────────────────


def parse_flow_rows(klines: list[str]) -> list[dict[str, Any]]:
    """东财 fflow daykline：
    f51 日期, f52 主力净额, f53 小单, f54 中单, f55 大单, f56 超大单,
    f57 主力净占比, f58 小单占比, f59 中单占比, f60 大单占比, f61 超大单占比,
    f62 收盘价, f63 涨跌幅
    """
    rows: list[dict[str, Any]] = []
    for raw in klines or []:
        parts = str(raw).split(",")
        if len(parts) < 13:
            continue
        vals = [_num(p) for p in parts[1:13]]
        if vals[0] is None or vals[10] is None:
            continue
        rows.append(
            {
                "date": parts[0].strip()[:10],
                "main": vals[0],
                "small": vals[1],
                "mid": vals[2],
                "large": vals[3],
                "xlarge": vals[4],
                "main_ratio": vals[5],
                "small_ratio": vals[6],
                "mid_ratio": vals[7],
                "large_ratio": vals[8],
                "xlarge_ratio": vals[9],
                "close": vals[10],
                "chg": vals[11],
            }
        )
    rows.sort(key=lambda r: r["date"])
    return rows


def _fetch_flow_raw(secid: str) -> list[str]:
    """抓取日频资金流。多主机重试，但受总预算约束；全局串行 + 最小间隔，避免自触发冷却。"""
    last_err: Exception | None = None
    deadline = time.monotonic() + FETCH_BUDGET_SEC
    with _FF_LOCK:
        gap = time.monotonic() - _FF_LAST[0]
        if 0 < gap < FF_MIN_GAP_SEC:
            time.sleep(FF_MIN_GAP_SEC - gap)
        session = requests.Session()
        session.headers.update(HEADERS)
        try:
            for host in FFLOW_HOSTS:
                for attempt in range(2):
                    if time.monotonic() > deadline:
                        raise last_err or TimeoutError("flow fetch budget exceeded")
                    try:
                        resp = session.get(
                            f"https://{host}/api/qt/stock/fflow/daykline/get",
                            params={
                                "lmt": "0",
                                "klt": "101",
                                "secid": secid,
                                "fields1": "f1,f2,f3,f7",
                                "fields2": FLOW_FIELDS2,
                                "ut": UT,
                                "_": int(time.time() * 1000),
                            },
                            timeout=5,
                        )
                        resp.raise_for_status()
                        payload = resp.json() or {}
                        klines = ((payload.get("data") or {}).get("klines")) or []
                        if klines:
                            return klines
                        last_err = ValueError(f"empty klines @ {host}")
                    except Exception as exc:  # noqa: BLE001
                        last_err = exc
                        logger.warning("stock fflow %s @ %s failed: %s", secid, host, exc)
                    time.sleep(0.8 * (attempt + 1))
        finally:
            _FF_LAST[0] = time.monotonic()
            session.close()
    raise last_err or RuntimeError(f"stock fflow failed: {secid}")


def _db_rows(db: Session, symbol: str, days: int = FLOW_DB_DAYS) -> list[dict[str, Any]]:
    since = datetime.now().date() - timedelta(days=days)
    recs = (
        db.query(StockFundFlowDaily)
        .filter(StockFundFlowDaily.symbol == symbol, StockFundFlowDaily.date >= since)
        .order_by(StockFundFlowDaily.date.asc())
        .all()
    )
    return [
        {
            "date": r.date.isoformat(),
            "main": _num(r.main),
            "small": _num(r.small),
            "mid": _num(r.mid),
            "large": _num(r.large),
            "xlarge": _num(r.xlarge),
            "main_ratio": _num(r.main_ratio),
            "small_ratio": None,
            "mid_ratio": None,
            "large_ratio": _num(r.large_ratio),
            "xlarge_ratio": _num(r.xlarge_ratio),
            "close": _num(r.close),
            "chg": _num(r.chg),
        }
        for r in recs
    ]


def _db_upsert(db: Session, symbol: str, rows: list[dict[str, Any]]) -> int:
    if not rows:
        return 0
    dates = [date.fromisoformat(r["date"]) for r in rows]
    existing = {
        rec.date: rec
        for rec in db.query(StockFundFlowDaily)
        .filter(StockFundFlowDaily.symbol == symbol, StockFundFlowDaily.date.in_(dates))
        .all()
    }
    written = 0
    fields = (
        "main",
        "small",
        "mid",
        "large",
        "xlarge",
        "main_ratio",
        "large_ratio",
        "xlarge_ratio",
        "close",
        "chg",
    )
    for r in rows:
        d = date.fromisoformat(r["date"])
        obj = existing.get(d)
        if obj is None:
            obj = StockFundFlowDaily(symbol=symbol, date=d)
            db.add(obj)
        for f in fields:
            setattr(obj, f, r.get(f))
        written += 1
    db.commit()
    return written


def _merge_rows(base: list[dict[str, Any]], new: list[dict[str, Any]]) -> list[dict[str, Any]]:
    merged = {r["date"]: r for r in base}
    for r in new:
        merged[r["date"]] = r
    keys = sorted(merged)[-FLOW_DB_DAYS:]
    return [merged[d] for d in keys]


def _bg_sync(symbol: str) -> None:  # pragma: no cover - 后台线程
    """后台增量同步，不阻塞请求。"""
    from app.database import SessionLocal

    db = SessionLocal()
    try:
        secid = secid_for(symbol)
        if not secid:
            return
        fetched = parse_flow_rows(_fetch_flow_raw(secid))
        if not fetched:
            return
        wrote = _db_upsert(db, symbol, fetched)
        have = _db_rows(db, symbol)
        if wrote == 0 or (have and have[-1]["date"] >= fetched[-1]["date"]):
            _sync_fresh[f"fresh:{symbol}"] = 1
    except Exception as exc:  # noqa: BLE001
        logger.warning("bg fund flow sync failed for %s: %s", symbol, exc)
        try:
            db.rollback()
        except Exception:  # noqa: BLE001
            pass
    finally:
        db.close()


def _schedule_sync(symbol: str) -> None:
    if _sync_try.get(f"try:{symbol}"):
        return
    _sync_try[f"try:{symbol}"] = 1
    threading.Thread(target=_bg_sync, args=(symbol,), name=f"ffsync-{symbol}", daemon=True).start()


def sync_flow(symbol: str, db: Session) -> int:
    """同步拉取并增量落库（阻塞）。返回写入行数；失败抛异常由调用方兜底。"""
    secid = secid_for(symbol)
    if not secid:
        return 0
    rows = parse_flow_rows(_fetch_flow_raw(secid))
    if not rows:
        return 0
    written = _db_upsert(db, symbol, rows)
    logger.info("fund flow synced %s: fetched=%s", symbol, len(rows))
    return written


def _aggregate(rows: list[dict[str, Any]], days: int = 20) -> dict[str, Any]:
    recent = rows[-days:]
    last5 = rows[-5:]
    last10 = rows[-10:]
    prev5 = rows[-10:-5]

    net_5d = sum(r["main"] or 0 for r in last5)
    net_10d = sum(r["main"] or 0 for r in last10)
    net_5d_prev = sum(r["main"] or 0 for r in prev5) if len(prev5) == 5 else None
    ratios = [r["main_ratio"] for r in last5 if r["main_ratio"] is not None]
    ratio_5d_avg = sum(ratios) / len(ratios) if ratios else None

    streak_days = 0
    streak_dir = ""
    for r in reversed(rows):
        v = r["main"] or 0
        if v == 0:
            break
        d = "in" if v > 0 else "out"
        if not streak_dir:
            streak_dir, streak_days = d, 1
        elif d == streak_dir:
            streak_days += 1
        else:
            break

    latest = rows[-1]
    return {
        "as_of": latest["date"],
        "close": latest["close"],
        "chg": latest["chg"],
        "latest_net": latest["main"],
        "latest_ratio": latest["main_ratio"],
        "net_5d": net_5d,
        "net_10d": net_10d,
        "net_5d_prev": net_5d_prev,
        "ratio_5d_avg": ratio_5d_avg,
        "streak_days": streak_days,
        "streak_dir": streak_dir,
        "inflow_days_5d": sum(1 for r in last5 if (r["main"] or 0) > 0),
        "xlarge_5d": sum(r["xlarge"] or 0 for r in last5),
        "large_5d": sum(r["large"] or 0 for r in last5),
        "series": recent,
    }


def fetch_main_flow(symbol: str, db: Session | None = None, days: int = 20) -> Optional[dict[str, Any]]:
    """近 N 日主力资金序列 + 汇总。

    有 db：优先读库。库内已有数据但落后于最近交易日 → 后台刷新（不阻塞）；
    库内完全没有该标的 → 同步等待一次（首次访问才付这个代价）。
    无 db：直接联网（诊断脚本用）。取不到返回 None。
    """
    if not is_supported(symbol):
        return None
    key = f"flow:{symbol}"
    cached = _flow_cache.get(key)
    if cached:
        return cached

    rows: list[dict[str, Any]] = []
    from_db = False
    net_attempted = False
    if db is not None:
        rows = _db_rows(db, symbol)
        from_db = bool(rows)
        if _sync_fresh.get(f"fresh:{symbol}") is None:
            last_d = _last_trading_date().isoformat()
            if not rows:
                # 首次访问：同步等一次（受预算约束）
                secid = secid_for(symbol)
                if secid and not _sync_try.get(f"try:{symbol}"):
                    _sync_try[f"try:{symbol}"] = 1
                    net_attempted = True
                    try:
                        fetched = parse_flow_rows(_fetch_flow_raw(secid))
                        if fetched:
                            _db_upsert(db, symbol, fetched)
                            rows = _merge_rows(rows, fetched)
                            from_db = True
                            if fetched[-1]["date"] < last_d:
                                _sync_fresh[f"fresh:{symbol}"] = 1
                    except Exception as exc:  # noqa: BLE001
                        logger.warning("main flow sync failed for %s: %s", symbol, exc)
                        try:
                            db.rollback()
                        except Exception:  # noqa: BLE001
                            pass
            elif rows[-1]["date"] < last_d:
                # 已有历史数据 → 后台刷新，本次先用库内数据
                _schedule_sync(symbol)

    if not rows and not net_attempted:
        secid = secid_for(symbol)
        if secid:
            try:
                rows = parse_flow_rows(_fetch_flow_raw(secid))
            except Exception as exc:  # noqa: BLE001
                logger.warning("main flow unavailable for %s: %s", symbol, exc)
    if not rows:
        return _stale.get(key)

    result = _aggregate(rows, days)
    result["source"] = "db+eastmoney" if from_db else "eastmoney"
    _flow_cache[key] = result
    _stale[key] = result
    return result


# ── 融资融券 ─────────────────────────────────────────────────


def _datacenter(report: str, filters: str, sort_col: str, page_size: int, referer: str) -> list[dict[str, Any]]:
    resp = requests.get(
        DATACENTER,
        params={
            "reportName": report,
            "columns": "ALL",
            "filter": filters,
            "sortColumns": sort_col,
            "sortTypes": "-1",
            "pageSize": str(page_size),
            "pageNumber": "1",
            "source": "WEB",
            "client": "WEB",
            "_": int(time.time() * 1000),
        },
        headers={**HEADERS, "Referer": referer},
        timeout=12,
    )
    resp.raise_for_status()
    payload = resp.json() or {}
    if not payload.get("success"):
        return []
    return ((payload.get("result") or {}).get("data")) or []


def fetch_margin(symbol: str, days: int = 10) -> Optional[dict[str, Any]]:
    """融资融券明细（交易所口径，T+1）。取不到返回 None。"""
    try:
        code, _market = parse_symbol(symbol)
    except SymbolError:
        return None
    key = f"margin:{code}"
    cached = _margin_cache.get(key)
    if cached:
        return cached
    try:
        rows = _datacenter(
            "RPTA_WEB_RZRQ_GGMX",
            f'(scode="{code}")',
            "DATE",
            30,
            f"https://data.eastmoney.com/rzrq/detail/{code}.html",
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning("margin unavailable for %s: %s", symbol, exc)
        return _stale.get(key)
    if not rows:
        return _stale.get(key)

    def _g(row: dict, k: str) -> Optional[float]:
        return _num(row.get(k))

    latest = rows[0]
    balance = _g(latest, "RZYE")
    balance_5d_ago = _g(rows[5], "RZYE") if len(rows) > 5 else None
    short_vol = _g(latest, "RQYL")
    short_vol_5d_ago = _g(rows[5], "RQYL") if len(rows) > 5 else None

    result = {
        "date": str(latest.get("DATE") or "")[:10],
        "balance": balance,
        "balance_chg_5d": (balance - balance_5d_ago) if (balance is not None and balance_5d_ago is not None) else None,
        "net_5d": _g(latest, "RZJME5D"),
        "net_10d": _g(latest, "RZJME10D"),
        "net_latest": _g(latest, "RZJME"),
        "buy_latest": _g(latest, "RZMRE"),
        "balance_ratio": _g(latest, "RZYEZB"),
        "short_volume": short_vol,
        "short_chg_5d": (short_vol - short_vol_5d_ago)
        if (short_vol is not None and short_vol_5d_ago is not None)
        else None,
        "short_balance": _g(latest, "RQYE"),
        "series": [
            {
                "date": str(r.get("DATE") or "")[:10],
                "balance": _g(r, "RZYE"),
                "net": _g(r, "RZJME"),
                "short_volume": _g(r, "RQYL"),
            }
            for r in reversed(rows[:days])
        ],
    }
    _margin_cache[key] = result
    _stale[key] = result
    return result


# ── 龙虎榜 ───────────────────────────────────────────────────


def fetch_lhb(symbol: str, recent_days: int = 180) -> Optional[dict[str, Any]]:
    """最近一次龙虎榜上榜记录（近 N 日）。无记录返回 None。"""
    try:
        code, _market = parse_symbol(symbol)
    except SymbolError:
        return None
    key = f"lhb:{code}"
    cache_key = f"lhb_cache:{code}"
    cached = _lhb_cache.get(cache_key)
    if cached is not None:
        return cached or None
    try:
        rows = _datacenter(
            "RPT_DAILYBILLBOARD_DETAILSNEW",
            f'(SECURITY_CODE="{code}")',
            "TRADE_DATE",
            20,
            "https://data.eastmoney.com/stock/lhb.html",
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning("lhb unavailable for %s: %s", symbol, exc)
        return _stale.get(key)
    today = date.today()
    found: Optional[dict[str, Any]] = None
    for row in rows:
        raw = str(row.get("TRADE_DATE") or "")[:10]
        try:
            d = date.fromisoformat(raw)
        except ValueError:
            continue
        if (today - d).days > recent_days:
            break
        found = {
            "date": raw,
            "reason": (row.get("EXPLANATION") or row.get("EXPLAIN") or "").strip(),
            "net_buy": _num(row.get("BILLBOARD_NET_AMT")),
            "buy_amt": _num(row.get("BILLBOARD_BUY_AMT")),
            "sell_amt": _num(row.get("BILLBOARD_SELL_AMT")),
            "days_ago": (today - d).days,
        }
        break
    _lhb_cache[cache_key] = found or {}
    if found:
        _stale[key] = found
        return found
    return _stale.get(key)


# ── 汇总 ─────────────────────────────────────────────────────


def build_fund_flow(symbol: str, db: Session | None = None) -> dict[str, Any]:
    """个股资金面汇总。任何一段取不到都不影响其余段（降级返回 available=False）。"""
    if not is_supported(symbol):
        return {"ok": False, "reason": "该标的无个股资金面数据（仅支持 A/B 股个股）"}
    main = fetch_main_flow(symbol, db)
    margin = fetch_margin(symbol)
    lhb = fetch_lhb(symbol)
    if not main and not margin:
        return {"ok": False, "reason": "资金面数据源暂不可用"}
    return {
        "ok": True,
        "symbol": symbol,
        "as_of": (main or {}).get("as_of") or (margin or {}).get("date") or "",
        "main": main,
        "margin": margin,
        "lhb": lhb,
        "source": "eastmoney",
        "updated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
    }
