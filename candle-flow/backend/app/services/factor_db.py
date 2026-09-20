"""盘后预构建因子库：将 run_full_analysis 结果持久化到 SQLite，盘中扫描零 API 调用。"""

from __future__ import annotations

import json
import logging
import os
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import text

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
# 单次批跑时间预算：全市场 ≈5400 只、单只 ≈13s、8 并发 ≈2.4h，
# 默认 1800s 会在 ~1100 只处截断（非披露期只补缺失，故可多日渐进补齐）。
# 需一次性补齐时可用环境变量放宽，或调 /fundamentals/factors/rebuild?budget_sec=。
_BUILD_BATCH_DEADLINE = float(os.environ.get("FACTOR_BUILD_DEADLINE_SEC", "1800"))


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


def _get_stale_symbols(
    existing_symbols: set[str],
    all_symbols: list[str],
    outdated_symbols: set[str] | None = None,
) -> list[str]:
    """找出缺失、过期或 payload 缺必需字段的股票。

    非披露期只补缺失条目；``outdated_symbols`` 来自 schema 守卫，
    把「已有行但缺新字段」的存量快照也纳入重建，否则新增字段永不回填。
    """
    in_window = _is_in_disclosure_window()
    ordered = sorted(all_symbols)
    if in_window:
        # 披露窗口期：全部重建
        return ordered
    # 非披露期：补缺失的 + 缺必需字段的
    outdated = outdated_symbols or set()
    return [s for s in ordered if s not in existing_symbols or s in outdated]


# 快照 payload 必需字段（schema 守卫）。
#
# 增量构建若只看「主键是否已存在」，新增字段在存量行上就永不回填：
# 2026-09-20 的 profit_yoy 即如此丢失（5211 条存量快照缺该键 →
# market_scan.technical_overlay 的 PEG 恒 None → 「强买入」档不可达）。
# 以后给 run_full_analysis 返回值加字段时，把键名追加到这里即可自愈。
_REQUIRED_SNAPSHOT_KEYS: tuple[str, ...] = ("profit_yoy",)


def _outdated_snapshot_symbols() -> set[str]:
    """返回 payload 缺少任一必需字段的 symbol 集合。

    判定依据是**键是否存在**（``json_type`` 为 NULL = 路径不存在），
    不是值是否为 null —— 合法的空值（如无同比数据）不应触发反复重建。
    SQLite 无 JSON1 时回退 Python 解析 payload。
    """
    if not _REQUIRED_SNAPSHOT_KEYS:
        return set()
    db = SessionLocal()
    try:
        try:
            conds = " OR ".join(
                f"json_type(payload, '$.{k}') IS NULL" for k in _REQUIRED_SNAPSHOT_KEYS
            )
            sql = text(f"SELECT symbol FROM factor_snapshots WHERE {conds}")
            return {row[0] for row in db.execute(sql)}
        except Exception:
            logger.warning(
                "factor_db schema guard SQL failed; falling back to python parse",
                exc_info=True,
            )
        outdated: set[str] = set()
        for sym, payload in db.query(
            FactorSnapshot.symbol, FactorSnapshot.payload
        ).all():
            try:
                data = json.loads(payload)
            except (json.JSONDecodeError, TypeError):
                outdated.add(sym)
                continue
            if not isinstance(data, dict) or any(
                k not in data for k in _REQUIRED_SNAPSHOT_KEYS
            ):
                outdated.add(sym)
        return outdated
    except Exception:
        logger.warning("factor_db schema guard failed", exc_info=True)
        return set()
    finally:
        db.close()


def build_all(
    *,
    force: bool = False,
    budget_sec: float | None = None,
    max_symbols: int | None = None,
) -> dict[str, Any]:
    """批量构建全市场因子库。

    Args:
        force: True 时无视披露窗口判断，全量重建。
        budget_sec: 本次批处理时间预算（秒）。缺省用 _BUILD_BATCH_DEADLINE。
            全市场约 5400 只、单只 ≈13s、8 并发 → 跑满约需 2.5h，
            默认 1800s 会在 ~1100 只处截断（非披露期只补缺失，故可多日渐进补齐）。
        max_symbols: 本次最多构建多少只（配合 budget_sec 做可控增量补齐）。

    Returns:
        {"built","failed","skipped","duration_sec","budget_sec",
         "truncated","coverage":{covered,universe,remaining,coverage_pct}}
    """
    from app.analysis.engine import analyze_symbol_full

    t0 = time.time()
    all_symbols = _get_all_symbols()
    if not all_symbols:
        logger.warning("factor_db.build_all: no symbols found in StockInfo")
        return {
            "built": 0,
            "failed": 0,
            "skipped": 0,
            "duration_sec": 0.0,
            "budget_sec": budget_sec or _BUILD_BATCH_DEADLINE,
            "truncated": False,
            "coverage": _coverage(len(all_symbols)),
        }

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
        # schema 守卫：已存在但 payload 缺必需字段的存量快照也要重建
        outdated = _outdated_snapshot_symbols()
        if outdated:
            logger.info(
                "factor_db.build_all: %d snapshots outdated by schema guard %s",
                len(outdated),
                _REQUIRED_SNAPSHOT_KEYS,
            )
        to_build = _get_stale_symbols(existing, all_symbols, outdated)

    if max_symbols is not None and max_symbols > 0:
        to_build = to_build[: int(max_symbols)]

    deadline = float(budget_sec) if budget_sec is not None else _BUILD_BATCH_DEADLINE
    total = len(to_build)
    skipped = len(all_symbols) - total
    logger.info(
        "factor_db.build_all: %d to build, %d skipped (force=%s, budget=%.0fs)",
        total,
        skipped,
        force,
        deadline,
    )

    if not to_build:
        coverage = _coverage(len(all_symbols))
        return {
            "built": 0,
            "failed": 0,
            "skipped": skipped,
            "duration_sec": time.time() - t0,
            "budget_sec": deadline,
            "truncated": False,
            "coverage": coverage,
        }

    built = 0
    failed = 0
    truncated = False
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
            if time.time() - batch_start > deadline:
                remaining = sum(1 for f in futures if not f.done())
                if remaining:
                    logger.warning(
                        "factor build BATCH DEADLINE %.0fs reached, %d remaining",
                        deadline,
                        remaining,
                    )
                    truncated = True
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
    coverage = _coverage(len(all_symbols))
    stats = {
        "built": built,
        "failed": failed,
        "skipped": skipped,
        "duration_sec": round(duration, 1),
        "budget_sec": deadline,
        "truncated": truncated,
        "coverage": coverage,
    }
    logger.info(
        "factor_db.build_all done: %s | coverage %s/%s (%.1f%%)",
        stats,
        coverage["covered"],
        coverage["universe"],
        coverage["coverage_pct"],
    )
    return stats


def _coverage(universe: int) -> dict[str, Any]:
    """覆盖率自证：已构建快照数 / SH·SZ 股票总数。"""
    db = SessionLocal()
    try:
        covered = int(
            db.query(FactorSnapshot)
            .filter(FactorSnapshot.composite_score.isnot(None))
            .count()
        )
    except Exception:
        logger.debug("coverage count failed", exc_info=True)
        covered = 0
    finally:
        db.close()
    return {
        "covered": covered,
        "universe": universe,
        "remaining": max(0, universe - covered),
        "coverage_pct": round(covered / universe * 100.0, 1) if universe else 0.0,
    }
