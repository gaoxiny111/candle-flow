"""全市场资金流数据源（东财 datacenter ``RPT_DMSK_TS_STOCKNEW``）。

**为什么需要它**：用户要求「主力资金持续净流入」作为趋势反转的**必要条件**
（真反转需要资金/消息/盘面共振）。项目原有 `stock_fund_flow.py` 走 push2his
单只日频接口，受 `_FF_LOCK` 全局串行 + 最小间隔约束，3000 只主板要数小时，
无法用于全库扫描。

本模块走 datacenter 报表接口：**11 次请求拿全市场 5199 只一整天**
（pageSize 500 × 11 页，实测 3.5s），按 TRADE_DATE 倒序翻页可取多日。

**字段语义（已实测核对，勿想当然）**：
  PRIME_INFLOW        主力净流入（元）  ← 判断「持续净流入」只用这个
  SUPERDEAL_INFLOW    超大单流入（元）
  BIGDEAL_INFLOW      大单流入（元）
  RATIO = BUY_BIGDEAL_RATIO  大单**买入占比**（0~1），**不是净占比**！
  RATIO_3DAYS         3 日大单买入占比均值（同上，非净额）
  ORG_PARTICIPATE     机构参与度（0~1）
  PRIME_COST          主力成本价

口径：东财「主力」= 超大单 + 大单（与 `stock_fund_flow.py` 一致，
**非**同花顺 DDX）。

**数据缺失必须放行**：取不到返回空 dict / None，调用方不得因缺失而淘汰标的
（项目惯例：字段缺失一律放行）。

只读，不写库；进程内缓存 TTL 10 分钟。
"""
from __future__ import annotations

import logging
import os
import time
from typing import Any, Optional

import requests

logger = logging.getLogger(__name__)

DC_URL = "https://datacenter-web.eastmoney.com/api/data/v1/get"
REPORT = "RPT_DMSK_TS_STOCKNEW"
HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
    ),
    "Referer": "https://data.eastmoney.com/",
}
PAGE_SIZE = 500
MAX_PAGES = 40          # 每天约 11 页；取 5 日 ≈ 11×5 = 55 页，留余量
CACHE_TTL_SEC = 600

_cache: dict[str, tuple[float, dict[str, Any]]] = {}


def _norm_code(raw: Any) -> str:
    """任意写法（'000612' / '000612.SZ' / 'SZ000612'）→ 6 位代码。"""
    c = "".join(ch for ch in str(raw or "") if ch.isdigit())
    return c[:6] if len(c) > 6 else c


def fetch_market_flow(days: int = 5, timeout: int = 20) -> dict[str, Any]:
    """抓取最近 ``days`` 个交易日的全市场资金流。

    返回 ``{"dates": [...], "by_date": {date: {code: row}}, "latest": {code: row}}``

    - ``by_date`` 按交易日分组，用于判断「连续 N 日净流入」；
    - ``latest`` 是最新交易日的快照（便于单点查询）；
    - 取不到时返回 ``{"dates": [], "by_date": {}, "latest": {}}``。
    """
    key = f"mktflow:v2:{days}"
    hit = _cache.get(key)
    if hit and time.monotonic() - hit[0] < CACHE_TTL_SEC:
        return hit[1]

    by_date: dict[str, dict[str, dict[str, Any]]] = {}
    first_date: str | None = None
    try:
        for page in range(1, MAX_PAGES + 1):
            params = {
                "reportName": REPORT,
                "columns": "ALL",
                "pageSize": PAGE_SIZE,
                "pageNumber": page,
                "sortColumns": "TRADE_DATE,SECURITY_CODE",
                "sortTypes": "-1,-1",
                "source": "WEB",
                "client": "WEB",
            }
            r = requests.get(DC_URL, params=params, headers=HEADERS, timeout=timeout)
            r.raise_for_status()
            rows = ((r.json() or {}).get("result") or {}).get("data") or []
            if not rows:
                break
            if first_date is None:
                first_date = str(rows[0].get("TRADE_DATE") or "")[:10]
            page_dates: set[str] = set()
            for x in rows:
                code = _norm_code(x.get("SECURITY_CODE"))
                d = str(x.get("TRADE_DATE") or "")[:10]
                if not code or not d:
                    continue
                page_dates.add(d)
                by_date.setdefault(d, {})[code] = {
                    "date": d,
                    "main": x.get("PRIME_INFLOW"),
                    "super_in": x.get("SUPERDEAL_INFLOW"),
                    "big_in": x.get("BIGDEAL_INFLOW"),
                    "big_buy_ratio": x.get("BUY_BIGDEAL_RATIO"),
                    "ratio_d1": x.get("RATIO"),
                    "ratio_d3": x.get("RATIO_3DAYS"),
                    "ratio_d50": x.get("RATIO_50DAYS"),
                    "org_participate": x.get("ORG_PARTICIPATE"),
                    "prime_cost": x.get("PRIME_COST"),
                    "close": x.get("CLOSE_PRICE"),
                    "chg": x.get("CHANGE_RATE"),
                }
            # 本页已出现更早日期 → 目标日已全部翻完，停。
            # 不能用 len(dates) >= days 判断：按日期倒序排时前面若干页全是
            # 同一天，该条件在第 1 页就成立，必然漏数。
            # 也不能用「不满一页就停」：最后一天的数据会跨页，实测 5199 只
            # = 10 个满页 + 第 11 页 199 行，第 12 页才是前一日。
            if first_date and min(page_dates) < first_date:
                break
            if len(by_date) > days:
                break
    except Exception as exc:  # noqa: BLE001
        logger.warning("market flow fetch failed: %s", exc)
        if not by_date:
            return {"dates": [], "by_date": {}, "latest": {}}

    dates = sorted(by_date.keys(), reverse=True)[:days]
    out = {
        "dates": dates,
        "by_date": {d: by_date[d] for d in dates},
        "latest": by_date.get(dates[0], {}) if dates else {},
    }
    logger.info("market flow: dates=%s symbols=%d", dates,
                len(out["latest"]))
    _cache[key] = (time.monotonic(), out)
    return out


def flow_of(code: str, table: dict[str, Any]) -> Optional[dict[str, Any]]:
    """从 ``fetch_market_flow`` 结果里取单只最新一日的行。"""
    c = _norm_code(code)
    return (table.get("latest") or {}).get(c) if c else None


def inflow_streak(code: str, table: dict[str, Any], window: int = 5) -> dict[str, Any]:
    """近 ``window`` 个交易日的**主力净流入连续天数**统计。

    返回 ``{"days": N, "in_days": n, "out_days": n, "streak": k, "dir": "in"/"out",
    "net": sum, "latest": v, "series": [...]}``；无数据时 ``days=0``。

    「持续净流入」判据由调用方定：本项目取 ``dir == "in" and in_days >= X``。
    """
    c = _norm_code(code)
    dates = table.get("dates") or []
    if not c or not dates:
        return {"days": 0}
    series: list[float] = []
    for d in dates[:window]:
        row = (table.get("by_date") or {}).get(d, {}).get(c)
        if row is None or row.get("main") is None:
            continue
        series.append(float(row["main"]))
    if not series:
        return {"days": 0}
    in_days = sum(1 for v in series if v > 0)
    out_days = sum(1 for v in series if v < 0)
    streak, direction = 0, ""
    for v in series:                      # series[0] = 最新一日
        if v == 0:
            break
        d = "in" if v > 0 else "out"
        if not direction:
            direction, streak = d, 1
        elif d == direction:
            streak += 1
        else:
            break
    return {
        "days": len(series),
        "in_days": in_days,
        "out_days": out_days,
        "streak": streak,
        "dir": direction,
        "net": sum(series),
        "latest": series[0],
        "series": series,
    }


# ── 落库（逐日积累历史）───────────────────────────────────────────────
def persist_market_flow(table: dict[str, Any] | None = None) -> dict[str, Any]:
    """把当日全市场快照写入 ``market_fund_flow_daily``（幂等 upsert）。

    该报表**只给当日快照**，所以历史只能靠每日调用一次累积。重复调用同一日
    不会产生重复行（唯一索引 ``symbol+date``），只会刷新数值。
    """
    from sqlalchemy import select

    from app.database import SessionLocal
    from app.models.market_fund_flow import MarketFundFlowDaily

    tbl = table if table is not None else fetch_market_flow(days=1)
    rows_by_code = tbl.get("latest") or {}
    if not rows_by_code:
        return {"date": None, "inserted": 0, "updated": 0, "skipped": True}

    stats = {"date": None, "inserted": 0, "updated": 0, "skipped": False}
    db = SessionLocal()
    try:
        for code, row in rows_by_code.items():
            d = row.get("date")
            if not code or not d:
                continue
            stats["date"] = d
            from datetime import date as _date

            try:
                y, m, dd = (int(x) for x in str(d).split("-")[:3])
                trade_date = _date(y, m, dd)
            except Exception:  # noqa: BLE001 - 日期格式异常跳过
                continue

            def _f(v: Any) -> Optional[float]:
                try:
                    return float(v) if v is not None and v != "" else None
                except (TypeError, ValueError):
                    return None

            rec = db.execute(
                select(MarketFundFlowDaily).where(
                    MarketFundFlowDaily.symbol == code,
                    MarketFundFlowDaily.date == trade_date,
                )
            ).scalar_one_or_none()
            vals = {
                "main": _f(row.get("main")),
                "super_in": _f(row.get("super_in")),
                "big_in": _f(row.get("big_in")),
                "big_buy_ratio": _f(row.get("big_buy_ratio")),
                "org_participate": _f(row.get("org_participate")),
                "prime_cost": _f(row.get("prime_cost")),
                "close": _f(row.get("close")),
                "chg": _f(row.get("chg")),
            }
            if rec is None:
                db.add(MarketFundFlowDaily(symbol=code, date=trade_date, **vals))
                stats["inserted"] += 1
            else:
                for k, v in vals.items():
                    setattr(rec, k, v)
                stats["updated"] += 1
        db.commit()
    except Exception:  # noqa: BLE001
        db.rollback()
        logger.exception("persist market flow failed")
    finally:
        db.close()
    logger.info(
        "market flow persisted: date=%s inserted=%d updated=%d",
        stats["date"], stats["inserted"], stats["updated"],
    )
    return stats


def history_of(code: str, days: int = 20) -> list[dict[str, Any]]:
    """从库中读单只近 ``days`` 个交易日的主力净流入（倒序）。"""
    from sqlalchemy import select

    from app.database import SessionLocal
    from app.models.market_fund_flow import MarketFundFlowDaily

    c = _norm_code(code)
    if not c:
        return []
    db = SessionLocal()
    try:
        recs = db.execute(
            select(MarketFundFlowDaily)
            .where(MarketFundFlowDaily.symbol == c)
            .order_by(MarketFundFlowDaily.date.desc())
            .limit(days)
        ).scalars().all()
    except Exception:  # noqa: BLE001
        logger.exception("market flow history read failed")
        return []
    finally:
        db.close()
    return [
        {
            "date": r.date.isoformat(),
            "main": float(r.main) if r.main is not None else None,
            "close": float(r.close) if r.close is not None else None,
            "chg": float(r.chg) if r.chg is not None else None,
        }
        for r in recs
    ]


def streak_from_history(code: str, window: int = 5) -> dict[str, Any]:
    """基于**落库历史**统计主力净流入连续天数（与 ``inflow_streak`` 同语义）。

    差异：``inflow_streak`` 依赖报表支持多日（当前不支持，只会返回 1 天），
    本函数依赖每日采集累积的库表，是「持续净流入」判据真正可用的数据来源。
    """
    recs = history_of(code, days=window)
    series = [r["main"] for r in recs if r["main"] is not None]
    if not series:
        return {"days": 0}
    in_days = sum(1 for v in series if v > 0)
    out_days = sum(1 for v in series if v < 0)
    streak, direction = 0, ""
    for v in series:                      # series[0] = 最新一日
        if v == 0:
            break
        d = "in" if v > 0 else "out"
        if not direction:
            direction, streak = d, 1
        elif d == direction:
            streak += 1
        else:
            break
    return {
        "days": len(series),
        "in_days": in_days,
        "out_days": out_days,
        "streak": streak,
        "dir": direction,
        "net": sum(series),
        "latest": series[0],
        "series": series,
        "dates": [r["date"] for r in recs],
    }


# ── 每日采集调度 ──────────────────────────────────────────────────────
_scheduler = None


def _scheduled_collect() -> None:
    """调度入口：抓当日快照并落库。"""
    try:
        persist_market_flow()
    except Exception:  # noqa: BLE001
        logger.exception("scheduled market flow collection failed")


def start_market_flow_scheduler() -> None:
    """工作日 16:40（Asia/Shanghai）采集当日全市场资金流。

    排在 K 线同步（16:35）之后，确保当日行情已定盘。设
    ``ENABLE_INPROCESS_SCHEDULER=0`` 时跳过（改由独立 worker/cron 跑
    ``python -m app.services.market_flow collect``）。
    """
    global _scheduler
    if _scheduler is not None:
        return
    enabled = os.environ.get("ENABLE_INPROCESS_SCHEDULER", "1").strip().lower()
    if enabled in ("0", "false", "no", "off"):
        logger.info("in-process market-flow scheduler disabled (ENABLE_INPROCESS_SCHEDULER=%s)", enabled)
        return
    from apscheduler.schedulers.background import BackgroundScheduler
    from apscheduler.triggers.cron import CronTrigger
    from zoneinfo import ZoneInfo

    _scheduler = BackgroundScheduler(timezone=ZoneInfo("Asia/Shanghai"))
    _scheduler.add_job(
        _scheduled_collect,
        CronTrigger(hour=16, minute=40, day_of_week="mon-fri"),
        id="market_fund_flow_daily",
        replace_existing=True,
    )
    _scheduler.start()
    logger.info("market-flow daily collection scheduler started (weekdays 16:40 Asia/Shanghai)")


def stop_market_flow_scheduler() -> None:
    global _scheduler
    if _scheduler is None:
        return
    _scheduler.shutdown(wait=False)
    _scheduler = None


if __name__ == "__main__":  # pragma: no cover - 手工/独立 worker 入口
    import sys

    if len(sys.argv) > 1 and sys.argv[1] == "collect":
        _scheduled_collect()
        print("collected:", persist_market_flow.__name__)
    else:
        print("usage: python -m app.services.market_flow collect")
