"""分红历史派生档案的读写（读层「真高股息」判据的数据底座）。

分层理由：
  ``app/analysis/dividend_data.fetch_dividend_history`` 是**实时网络接口**
  （东财 RPT_SHAREBONUS_DET，带 6h 内存缓存）——分析期取用没问题，但读层
  榜单**绝不允许**对全市场 3200 只逐个打网络。所以：分析期/回补期把结果
  压成与股价无关的量落进 ``stock_dividend_profile``，读层只读本地表。

维护路径（两条，互为兜底）：
  ① ``factor_db.upsert`` 写完快照后调用 ``refresh_profile(symbol)`` —— 每次
     快照构建顺带刷新该只的分红档案（分析期已把接口结果放进 6h 缓存，
     同进程内基本零额外网络开销）；
  ② ``scripts/backfill_dividend_profile.py`` 全量回补（首次建表/口径变更时）。

口径（与用户 2026-09-30 的「真高股息防御型」框架对齐）：
  - ``consecutive_years``：连续现金分红年数，直接取东财实现；
  - ``dps_3y_avg``：**近 3 个完整会计年度**的年均每股分红（元/股，含税）。
    为什么要求该年度有末期（1231）预案：只有中期的年度不构成一个完整的
    年度分红口径，算进去会让连续性与均值虚增。
  - 股息率不落库：读层用 ``dps_3y_avg ÷ 当前快照价`` 现算，行情更新即新鲜。
"""

from __future__ import annotations

import json
import logging
import threading
import time
from typing import Any

from sqlalchemy.orm import Session

from app.database import SessionLocal
from app.models.dividend_profile import StockDividendProfile

logger = logging.getLogger(__name__)

# 近三年均值取「最近 3 个完整会计年度」
DPS_AVG_YEARS = 3
# 序列保留的完整年度数（判连续性/算均值之外的滚动窗口）
DPS_SERIES_KEEP = 6

# 读层缓存：全表数千行、读取本身很快，但榜单每请求一次没必要。
# 写入侧同进程会 ``invalidate()``，TTL 只是跨进程/异常场景的兜底。
_CACHE_TTL_SEC = 120.0
_CACHE: dict[str, Any] = {"ts": 0.0, "data": None}
_LOCK = threading.Lock()


def _fy_totals(plans: list[dict[str, Any]] | None) -> dict[int, float]:
    """按会计年度归集每股分红合计，只保留**有末期(1231)预案**的年度。"""
    grouped: dict[int, list[float]] = {}
    has_final: set[int] = set()
    for p in plans or []:
        dps = p.get("dps")
        rd = str(p.get("report_date") or "")
        if dps is None or len(rd) != 8:
            continue
        try:
            fy = int(rd[:4])
            dps_f = float(dps)
        except (TypeError, ValueError):
            continue
        grouped.setdefault(fy, []).append(dps_f)
        if rd.endswith("1231"):
            has_final.add(fy)
    return {fy: round(sum(v), 4) for fy, v in grouped.items() if fy in has_final}


def compute_profile(hist: dict[str, Any] | None) -> dict[str, Any] | None:
    """把 ``fetch_dividend_history`` 的原始返回压成表行字段；无有效记录 → None。"""
    if not hist:
        return None
    fy_totals = _fy_totals(hist.get("plans"))
    if not fy_totals:
        return None
    years = sorted(fy_totals, reverse=True)
    recent = [fy_totals[y] for y in years[:DPS_AVG_YEARS]]
    return {
        "consecutive_years": int(hist.get("consecutive_years") or 0),
        "latest_fy": years[0],
        "dps_latest_fy": fy_totals[years[0]],
        "dps_3y_avg": round(sum(recent) / len(recent), 4) if recent else None,
        "dps_series": {y: fy_totals[y] for y in years[:DPS_SERIES_KEEP]},
    }


def upsert_profiles(items: list[tuple[str, dict[str, Any]]], *, source: str = "eastmoney") -> int:
    """批量写入/更新档案。返回成功写入条数（异常不抛出，构建链路不得被拖挂）。"""
    if not items:
        return 0
    db = SessionLocal()
    written = 0
    try:
        for symbol, prof in items:
            row = db.query(StockDividendProfile).filter(
                StockDividendProfile.symbol == symbol
            ).first()
            if row is None:
                row = StockDividendProfile(symbol=symbol)
                db.add(row)
            row.consecutive_years = int(prof.get("consecutive_years") or 0)
            row.latest_fy = prof.get("latest_fy")
            row.dps_latest_fy = prof.get("dps_latest_fy")
            row.dps_3y_avg = prof.get("dps_3y_avg")
            series = prof.get("dps_series")
            row.dps_series = (
                json.dumps(series, ensure_ascii=False) if isinstance(series, dict) else None
            )
            row.source = source
            written += 1
        db.commit()
    except Exception:  # noqa: BLE001 - 档案写入失败不影响快照与榜单
        logger.warning("dividend profile upsert failed", exc_info=True)
        db.rollback()
        written = 0
    finally:
        db.close()
    if written:
        invalidate()
    return written


def refresh_profile(symbol: str) -> bool:
    """抓取并写单只分红档案（供快照构建顺带刷新）。失败返回 False，不抛。"""
    try:
        from app.analysis.dividend_data import fetch_dividend_history

        prof = compute_profile(fetch_dividend_history(symbol))
    except Exception:  # noqa: BLE001 - 网络/解析异常都不得影响快照构建
        logger.debug("refresh dividend profile failed for %s", symbol, exc_info=True)
        return False
    if not prof:
        return False
    return upsert_profiles([(symbol, prof)]) > 0


def invalidate() -> None:
    """清读层缓存（写入后调用）。"""
    with _LOCK:
        _CACHE["ts"] = 0.0
        _CACHE["data"] = None


def load_all(db: Session | None = None) -> dict[str, dict[str, Any]]:
    """读取全部分红档案 {symbol: {consecutive_years, dps_3y_avg, ...}}。

    传入 ``db`` 时**不走进程缓存**（调用方自管会话与生命周期，测试用内存库
    也走这条路，避免读到全局文件库）；不传则走带 TTL 的全表缓存。
    """
    if db is not None:
        return _read_all(db)
    with _LOCK:
        if _CACHE["data"] is not None and time.time() - float(_CACHE["ts"]) < _CACHE_TTL_SEC:
            return _CACHE["data"]
    own = SessionLocal()
    try:
        data = _read_all(own)
    finally:
        own.close()
    with _LOCK:
        _CACHE.update(ts=time.time(), data=data)
    return data


def _read_all(db: Session) -> dict[str, dict[str, Any]]:
    data: dict[str, dict[str, Any]] = {}
    try:
        for r in db.query(StockDividendProfile).all():
            data[r.symbol] = {
                "consecutive_years": r.consecutive_years,
                "latest_fy": r.latest_fy,
                "dps_latest_fy": r.dps_latest_fy,
                "dps_3y_avg": r.dps_3y_avg,
            }
    except Exception:  # noqa: BLE001 - 表未建/读失败时退化为「无档案」，判据按缺数据走
        logger.warning("dividend profile load failed", exc_info=True)
        data = {}
    return data


def attach_profiles(rows: list[dict[str, Any]], db: Session | None = None) -> int:
    """把分红档案合并进轻量行（就地写 ``div_years`` / ``dps_3y_avg`` / ``div_latest_fy``）。

    注意：这几列**不在** ``_LIGHT_SQL`` / ``_from_payload`` 里，来源是本表。
    铁律 17（新增读层字段三处同步）对它不适用，但**必须**放在 ``load_covered``
    的浅拷贝之后（与 ``attach_realtime_quotes`` 同层），否则会写脏行缓存。

    ``div_latest_fy``（档案最新会计年度）是「真高股息」判据的**新鲜度闸门**输入：
    分红记录停更的标的（股价崩塌型，如金科股份 latest_fy=2020 却因分母塌陷显示
    36.58% 股息率）必须按「缺数据」处理，不能凭冻结的历史每股分红入选。
    判据侧见 ``high_dividend.hd_required_fy``。
    """
    profiles = load_all(db)
    if not profiles:
        return 0
    hit = 0
    for r in rows:
        p = profiles.get(str(r.get("symbol") or ""))
        if not p:
            continue
        r["div_years"] = p.get("consecutive_years")
        r["dps_3y_avg"] = p.get("dps_3y_avg")
        r["div_latest_fy"] = p.get("latest_fy")
        hit += 1
    return hit


def stats() -> dict[str, Any]:
    """档案覆盖自证：条数 / 连续分红≥3 年占比（供验收与 notes）。"""
    db = SessionLocal()
    try:
        total = db.query(StockDividendProfile).count()
        good = (
            db.query(StockDividendProfile)
            .filter(StockDividendProfile.consecutive_years >= 3)
            .count()
        )
    except Exception:  # noqa: BLE001
        return {"total": 0, "continuous_ge3": 0}
    finally:
        db.close()
    return {"total": total, "continuous_ge3": good}
