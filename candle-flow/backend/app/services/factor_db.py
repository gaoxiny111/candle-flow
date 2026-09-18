"""盘后预构建因子库：将 run_full_analysis 结果持久化到 SQLite，盘中扫描零 API 调用。"""

from __future__ import annotations

import json
import logging
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta, timezone
from typing import Any

from app.database import SessionLocal
from app.models.factor_snapshot import FactorSnapshot
from app.models.stock import StockInfo

logger = logging.getLogger(__name__)

# 非披露期 TTL：30 天内的快照视为有效
_DEFAULT_TTL_DAYS = 30
# 披露窗口期 TTL：3 天内视为有效（财报数据频繁更新）
_DISCLOSURE_TTL_DAYS = 3
# 批量构建参数
_BUILD_WORKERS = 8
_BUILD_PER_STOCK_TIMEOUT = 20.0
_BUILD_BATCH_DEADLINE = 1800.0  # 夜间批跑允许 30 分钟


def _is_in_disclosure_window(now: datetime | None = None) -> bool:
    """判断当前是否处于财报披露窗口期。

    年报/一季报: 1-4 月
    半年报: 7-8 月
    三季报: 10 月
    """
    today = now or datetime.now()
    month = today.month
    return month in (1, 2, 3, 4, 7, 8, 10)


def _ttl_days() -> int:
    """根据是否处于披露窗口返回 TTL。"""
    return _DISCLOSURE_TTL_DAYS if _is_in_disclosure_window() else _DEFAULT_TTL_DAYS


def is_stale(symbol: str, *, ttl_days: int | None = None) -> bool:
    """判断指定股票的因子快照是否过期或不存在。

    返回 True 表示需要重新构建（或数据不存在）。
    """
    ttl = ttl_days if ttl_days is not None else _ttl_days()
    db = SessionLocal()
    try:
        row = db.query(FactorSnapshot).filter(FactorSnapshot.symbol == symbol).first()
        if row is None:
            return True
        if row.built_at is None:
            return True
        # timezone-aware 比较
        built = row.built_at
        if built.tzinfo is None:
            built = built.replace(tzinfo=timezone.utc)
        now = datetime.now(timezone.utc)
        return (now - built) > timedelta(days=ttl)
    except Exception:
        logger.debug("is_stale check failed for %s", symbol, exc_info=True)
        return True
    finally:
        db.close()


def get(symbol: str) -> dict[str, Any] | None:
    """从因子库读取单只股票的完整分析结果。缺失返回 None。"""
    db = SessionLocal()
    try:
        row = db.query(FactorSnapshot).filter(FactorSnapshot.symbol == symbol).first()
        if row is None:
            return None
        return json.loads(row.payload)
    except Exception:
        logger.debug("factor_db.get failed for %s", symbol, exc_info=True)
        return None
    finally:
        db.close()


def get_many(symbols: list[str]) -> dict[str, dict[str, Any]]:
    """批量读取多只股票的因子快照。返回 {symbol: report_dict}。"""
    if not symbols:
        return {}
    db = SessionLocal()
    try:
        rows = db.query(FactorSnapshot).filter(FactorSnapshot.symbol.in_(symbols)).all()
        result: dict[str, dict[str, Any]] = {}
        for row in rows:
            try:
                result[row.symbol] = json.loads(row.payload)
            except (json.JSONDecodeError, TypeError):
                continue
        return result
    except Exception:
        logger.debug("factor_db.get_many failed", exc_info=True)
        return {}
    finally:
        db.close()


def upsert(symbol: str, report: dict[str, Any]) -> None:
    """写入或更新单条因子快照。"""
    db = SessionLocal()
    try:
        payload = json.dumps(report, ensure_ascii=False, default=str)
        composite = report.get("composite_score")
        market = report.get("market") or {}
        pe_ttm = market.get("pe_ttm")
        row = db.query(FactorSnapshot).filter(FactorSnapshot.symbol == symbol).first()
        if row is None:
            row = FactorSnapshot(symbol=symbol)
            db.add(row)
        row.payload = payload
        row.composite_score = float(composite) if composite is not None else None
        row.pe_ttm = float(pe_ttm) if pe_ttm is not None else None
        row.built_at = datetime.now(timezone.utc)
        db.commit()
    except Exception:
        logger.debug("factor_db.upsert failed for %s", symbol, exc_info=True)
        db.rollback()
    finally:
        db.close()


def _get_all_symbols() -> list[str]:
    """获取全部 SH/SZ 主板股票代码。"""
    db = SessionLocal()
    try:
        rows = db.query(StockInfo).filter(StockInfo.market.in_(("SH", "SZ"))).all()
        return [r.symbol for r in rows]
    finally:
        db.close()


def _get_stale_symbols(existing_symbols: set[str], all_symbols: list[str]) -> list[str]:
    """找出缺失或过期的股票（非披露期只补建缺失条目）。"""
    in_window = _is_in_disclosure_window()
    if in_window:
        # 披露窗口期：全部重建
        return all_symbols
    # 非披露期：只补缺失的
    return [s for s in all_symbols if s not in existing_symbols]


def build_all(*, force: bool = False) -> dict[str, Any]:
    """批量构建全市场因子库。

    Args:
        force: True 时无视披露窗口判断，全量重建。

    Returns:
        {"built": int, "failed": int, "skipped": int, "duration_sec": float}
    """
    from app.analysis.engine import analyze_symbol_full

    t0 = time.time()
    all_symbols = _get_all_symbols()
    if not all_symbols:
        logger.warning("factor_db.build_all: no symbols found in StockInfo")
        return {"built": 0, "failed": 0, "skipped": 0, "duration_sec": 0.0}

    # 确定需要构建的股票列表
    if force:
        to_build = all_symbols
    else:
        # 查已有快照的 symbol 集合
        db = SessionLocal()
        try:
            existing = {r.symbol for r in db.query(FactorSnapshot.symbol).all()}
        finally:
            db.close()
        to_build = _get_stale_symbols(existing, set(all_symbols))

    total = len(to_build)
    skipped = len(all_symbols) - total
    logger.info("factor_db.build_all: %d to build, %d skipped (force=%s)", total, skipped, force)

    if not to_build:
        return {"built": 0, "failed": 0, "skipped": skipped, "duration_sec": time.time() - t0}

    built = 0
    failed = 0
    batch_start = time.time()

    def _process_one(sym: str) -> bool:
        try:
            result = analyze_symbol_full(db=None, symbol=sym, use_cache=True)
            if result.get("composite_score") is not None:
                upsert(sym, result)
                return True
            return False
        except Exception:
            logger.debug("factor build failed for %s", sym, exc_info=True)
            return False

    workers = min(_BUILD_WORKERS, max(1, len(to_build)))
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = {pool.submit(_process_one, sym): sym for sym in to_build}
        for fut in as_completed(futures):
            # 批级 deadline
            if time.time() - batch_start > _BUILD_BATCH_DEADLINE:
                remaining = sum(1 for f in futures if not f.done())
                if remaining:
                    logger.warning(
                        "factor build BATCH DEADLINE %.0fs reached, %d remaining",
                        _BUILD_BATCH_DEADLINE, remaining,
                    )
                    for f in futures:
                        if not f.done():
                            f.cancel()
                break
            sym = futures[fut]
            try:
                if fut.result(timeout=_BUILD_PER_STOCK_TIMEOUT):
                    built += 1
                else:
                    failed += 1
            except Exception:
                failed += 1
                logger.debug("factor build timeout/error for %s", sym)

    duration = time.time() - t0
    stats = {"built": built, "failed": failed, "skipped": skipped, "duration_sec": round(duration, 1)}
    logger.info("factor_db.build_all done: %s", stats)
    return stats
