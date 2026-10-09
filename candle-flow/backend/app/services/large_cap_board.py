"""大盘股基本面榜：按个股引擎综合分排序。

默认池：**A股**（沪深 A 股全量，不设市值门槛；排除北交所/ST 等由行情层过滤）。
分数来自 ``FundamentalEngine`` / ``factor_snapshots``（与图表页基本面同源）。
HTTP 不阻塞拉行情/全量打分（避免 Cloudflare 524）：有结果立刻返回，缺池/缺分后台补。
"""

from __future__ import annotations

import json
import logging
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

# 0 = 不设市值门槛（全量沪深 A 股）
DEFAULT_MIN_CAP_YI = 0.0
# 全市场护栏（A 股约五千只量级）
MAX_UNIVERSE = 6000
DEFAULT_TOP = MAX_UNIVERSE
DEFAULT_UNIVERSE_LIMIT = MAX_UNIVERSE
GRADE_BANDS = ("A", "B", "C", "D", "E")
SCORE_WORKERS = 3
CACHE_TTL_SEC = 3600
UNIVERSE_DISK_TTL_SEC = 6 * 3600

_DISK_CACHE_DIR = Path("data") / "cache" / "large_cap_board"

_LOCK = threading.Lock()
_REFRESHING = False
_UNIVERSE_LOADING = False
_AUTO_SCORED = False  # 本轮已自动补算过；未点「重新打分」不反复开线程
_LAST_BOARD: dict[str, Any] | None = None
_LAST_TS = 0.0
_PROGRESS: dict[str, Any] = {
    "running": False,
    "planned": 0,
    "done": 0,
    "failed": 0,
    "started_at": 0.0,
}


def _to_float(v: Any) -> float | None:
    try:
        if v is None or v == "":
            return None
        return float(v)
    except (TypeError, ValueError):
        return None


def _disk_path(min_cap_yi: float) -> Path:
    # v4：取消市值门槛后的全量 A 股池；min_cap 仅兼容旧参数
    tag = "all" if float(min_cap_yi or 0) <= 0 else f"cap{int(min_cap_yi)}"
    return _DISK_CACHE_DIR / f"universe_{tag}_v4.json"


def _clear_universe_disk(min_cap_yi: float | None = None) -> None:
    try:
        if not _DISK_CACHE_DIR.is_dir():
            return
        if min_cap_yi is None:
            for p in _DISK_CACHE_DIR.glob("universe_*.json"):
                p.unlink(missing_ok=True)
        else:
            _disk_path(min_cap_yi).unlink(missing_ok=True)
    except Exception:  # noqa: BLE001
        logger.debug("large_cap universe disk clear failed", exc_info=True)


def _load_universe_disk(min_cap_yi: float) -> list[dict[str, Any]] | None:
    path = _disk_path(min_cap_yi)
    if not path.is_file():
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
        ts = float(payload.get("ts") or 0.0)
        if time.time() - ts > UNIVERSE_DISK_TTL_SEC:
            return None
        rows = payload.get("rows")
        if not isinstance(rows, list) or not rows:
            return None
        return rows
    except Exception:  # noqa: BLE001
        logger.debug("large_cap universe disk load failed", exc_info=True)
        return None


def _save_universe_disk(min_cap_yi: float, rows: list[dict[str, Any]]) -> None:
    try:
        _DISK_CACHE_DIR.mkdir(parents=True, exist_ok=True)
        path = _disk_path(min_cap_yi)
        path.write_text(
            json.dumps({"ts": time.time(), "rows": rows}, ensure_ascii=False),
            encoding="utf-8",
        )
    except Exception:  # noqa: BLE001
        logger.debug("large_cap universe disk save failed", exc_info=True)


def _fetch_universe_rows(*, min_cap_yi: float = 0.0) -> list[dict[str, Any]]:
    """东财/新浪行情：沪深 A 股池（默认不设市值门槛，按市值降序）。"""
    from app.services.high_dividend import _fetch_spot, _to_symbol

    df = _fetch_spot()
    if df is None or df.empty:
        return []
    min_yi = float(min_cap_yi or 0)
    min_cap = min_yi * 1e8 if min_yi > 0 else 0.0
    rows: list[dict[str, Any]] = []
    for _, r in df.iterrows():
        cap = _to_float(r.get("market_cap"))
        if cap is not None and cap != cap:  # NaN
            cap = None
        if min_cap > 0 and (cap is None or cap < min_cap):
            continue
        code = str(r.get("code") or "").zfill(6)
        # 排除北交所
        if code.startswith(("4", "8", "92")):
            continue
        rows.append(
            {
                "symbol": _to_symbol(code),
                "code": code,
                "name": str(r.get("name") or ""),
                "price": _to_float(r.get("price")),
                "pe_ttm": _to_float(r.get("pe_ttm")),
                "pb": _to_float(r.get("pb")),
                "market_cap": cap,
                "market_cap_yi": round(cap / 1e8, 2) if cap is not None else None,
            }
        )
    rows.sort(key=lambda x: float(x.get("market_cap") or 0), reverse=True)
    if len(rows) > MAX_UNIVERSE:
        logger.warning("A股池异常偏大：%s，截断至 %s", len(rows), MAX_UNIVERSE)
        rows = rows[:MAX_UNIVERSE]
    if min_yi > 0:
        logger.info("A股池拉取完成：%s 只（≥%s亿）", len(rows), min_yi)
    else:
        logger.info("A股池拉取完成：%s 只（无市值门槛）", len(rows))
    return rows


def _universe_large_caps(*, min_cap_yi: float, limit: int) -> list[dict[str, Any]]:
    rows = _load_universe_disk(min_cap_yi)
    if rows is None:
        rows = _fetch_universe_rows(min_cap_yi=min_cap_yi)
        if rows:
            _save_universe_disk(min_cap_yi, rows)
    return rows[: max(1, min(int(limit), MAX_UNIVERSE))]


def _schedule_universe_load(min_cap_yi: float) -> bool:
    """冷启动：后台拉 A股大盘池，避免 HTTP 卡在行情。"""
    global _UNIVERSE_LOADING
    with _LOCK:
        if _UNIVERSE_LOADING:
            return False
        _UNIVERSE_LOADING = True

    def _job() -> None:
        global _UNIVERSE_LOADING, _LAST_BOARD, _LAST_TS
        try:
            rows = _fetch_universe_rows(min_cap_yi=min_cap_yi)
            if rows:
                _save_universe_disk(min_cap_yi, rows)
                logger.info("A股大盘池就绪：%s 只（≥%s亿）", len(rows), min_cap_yi)
        except Exception:  # noqa: BLE001
            logger.exception("A股大盘池拉取失败")
        finally:
            with _LOCK:
                _UNIVERSE_LOADING = False
                _LAST_BOARD = None
                _LAST_TS = 0.0

    threading.Thread(target=_job, name="large-cap-universe", daemon=True).start()
    return True


def _item_from_report(base: dict[str, Any], report: dict[str, Any] | None) -> dict[str, Any]:
    out = dict(base)
    if not report or report.get("skipped"):
        out.update(
            {
                "composite_score": None,
                "final_rating": None,
                "scored": False,
                "status": "pending",
                "sector_kind": None,
                "dim_scores": {},
                "warnings": [],
            }
        )
        return out
    market = report.get("market") or {}
    out.update(
        {
            "name": report.get("name") or base.get("name") or "",
            "composite_score": report.get("composite_score"),
            "final_rating": report.get("final_rating") or report.get("final_rating_letter"),
            "scored": report.get("composite_score") is not None,
            "status": "ready" if report.get("composite_score") is not None else "pending",
            "sector_kind": (report.get("sector_profile") or {}).get("kind"),
            "dim_scores": report.get("dim_scores") or {},
            "pe_ttm": market.get("pe_ttm", base.get("pe_ttm")),
            "pb": market.get("pb", base.get("pb")),
            "price": market.get("price", base.get("price")),
            "dividend_yield": market.get("dividend_yield"),
            "warnings": list(report.get("warnings") or [])[:5],
            "scoring_version": report.get("scoring_version"),
        }
    )
    return out


def _load_scored_map(symbols: list[str]) -> dict[str, dict[str, Any]]:
    from app.services import factor_db
    from app.services.factor_db import SCORING_VERSION

    raw = factor_db.get_many(symbols)
    out: dict[str, dict[str, Any]] = {}
    for sym, rep in raw.items():
        if not isinstance(rep, dict):
            continue
        if rep.get("scoring_version") != SCORING_VERSION:
            continue
        if rep.get("composite_score") is None:
            continue
        out[sym.upper()] = rep
    return out


def _score_one(symbol: str) -> bool:
    from app.analysis.engine import analyze_symbol_full
    from app.services import factor_db

    try:
        report = analyze_symbol_full(db=None, symbol=symbol, use_cache=False)
        if report.get("composite_score") is None:
            return False
        factor_db.upsert(symbol, report)
        return True
    except Exception:
        logger.exception("large_cap score failed %s", symbol)
        return False


def _schedule_scoring(symbols: list[str]) -> bool:
    global _REFRESHING, _AUTO_SCORED
    with _LOCK:
        if _REFRESHING or not symbols:
            return False
        _REFRESHING = True
        _AUTO_SCORED = True
        _PROGRESS.update(
            running=True,
            planned=len(symbols),
            done=0,
            failed=0,
            started_at=time.time(),
        )

    def _job() -> None:
        global _REFRESHING, _LAST_BOARD, _LAST_TS
        done = failed = 0
        try:
            workers = min(SCORE_WORKERS, max(1, len(symbols)))
            logger.info("大盘股基本面打分开始：%s 只，workers=%s", len(symbols), workers)
            with ThreadPoolExecutor(max_workers=workers) as pool:
                futs = {pool.submit(_score_one, s): s for s in symbols}
                for fut in as_completed(futs):
                    ok = False
                    try:
                        ok = bool(fut.result())
                    except Exception:
                        ok = False
                    if ok:
                        done += 1
                    else:
                        failed += 1
                    with _LOCK:
                        _PROGRESS["done"] = done
                        _PROGRESS["failed"] = failed
            logger.info("大盘股基本面打分结束：ok=%s fail=%s", done, failed)
        finally:
            with _LOCK:
                _REFRESHING = False
                _PROGRESS["running"] = False
                _LAST_BOARD = None
                _LAST_TS = 0.0

    threading.Thread(target=_job, name="large-cap-score", daemon=True).start()
    return True


def _pool_note(min_yi: float) -> str:
    _ = min_yi
    return "股票池：A股全量，按 A/B/C/D/E 评级分组"


def _grade_band(letter: Any) -> str:
    """细档 A-/B+/B- 归入 A/B/C/D/E 主档。"""
    if letter is None:
        return "pending"
    s = str(letter).strip().upper()
    if not s:
        return "pending"
    ch = s[0]
    return ch if ch in GRADE_BANDS else "pending"


def _group_by_grade(items: list[dict[str, Any]]) -> dict[str, list[dict[str, Any]]]:
    groups: dict[str, list[dict[str, Any]]] = {g: [] for g in GRADE_BANDS}
    groups["pending"] = []
    for it in items:
        band = _grade_band(it.get("final_rating")) if it.get("scored") else "pending"
        it = {**it, "grade_band": band}
        groups.setdefault(band, []).append(it)
    for g in GRADE_BANDS:
        groups[g].sort(key=lambda x: float(x.get("composite_score") or 0), reverse=True)
    groups["pending"].sort(key=lambda x: float(x.get("market_cap") or 0), reverse=True)
    return groups


def _slim_row(it: dict[str, Any]) -> dict[str, Any]:
    """列表接口精简字段，避免 5000 只全量 JSON 拖垮 Cloudflare / 前端。"""
    dims = it.get("dim_scores") or {}
    scored = bool(it.get("scored"))
    out: dict[str, Any] = {
        "symbol": it.get("symbol"),
        "code": it.get("code"),
        "name": it.get("name") or "",
        "composite_score": it.get("composite_score"),
        "final_rating": it.get("final_rating"),
        "grade_band": it.get("grade_band"),
        "scored": scored,
        "price": it.get("price"),
        "pe_ttm": it.get("pe_ttm"),
        "pb": it.get("pb"),
        "dividend_yield": it.get("dividend_yield"),
        "market_cap_yi": it.get("market_cap_yi"),
        "industry": it.get("industry") or "",
    }
    if scored:
        out["dim_scores"] = {
            "盈利能力": dims.get("盈利能力"),
            "成长性": dims.get("成长性"),
            "现金流质量": dims.get("现金流质量"),
            "偿债能力": dims.get("偿债能力"),
            "估值合理性": dims.get("估值合理性"),
        }
    return out


def _computing_stub(*, min_yi: float, top_n: int, reason: str) -> dict[str, Any]:
    _ = top_n
    empty_groups = {g: [] for g in GRADE_BANDS}
    empty_groups["pending"] = []
    return {
        "count": 0,
        "universe_size": 0,
        "scored_count": 0,
        "pending_count": 0,
        "min_cap_yi": min_yi,
        "pool_note": _pool_note(min_yi),
        "items": [],
        "groups": empty_groups,
        "grade_counts": {**{g: 0 for g in GRADE_BANDS}, "pending": 0},
        "status": "computing",
        "cached": False,
        "refresh_started": True,
        "progress": dict(_PROGRESS),
        "notes": [
            reason,
            "分数口径：盈利/成长/偿债/现金流/估值加权（行业自适应权重）",
        ],
    }


def scan_large_cap_board(
    *,
    min_cap_yi: float = DEFAULT_MIN_CAP_YI,
    top: int = DEFAULT_TOP,
    universe_limit: int = DEFAULT_UNIVERSE_LIMIT,
    refresh: bool = False,
    grade: str | None = None,
    pending_limit: int = 200,
    pending_offset: int = 0,
) -> dict[str, Any]:
    """返回 A股榜；缺池/缺分时后台补算。

    默认只下发已评分 A–E（精简字段）；未评分仅给计数，点「未评」再分页拉取，
    避免 5000 条一次响应超时。
    """
    global _LAST_BOARD, _LAST_TS, _AUTO_SCORED

    top_n = max(1, min(int(top), MAX_UNIVERSE))
    uni_n = max(top_n, min(int(universe_limit), MAX_UNIVERSE))
    try:
        min_yi = float(min_cap_yi) if min_cap_yi is not None else DEFAULT_MIN_CAP_YI
    except (TypeError, ValueError):
        min_yi = DEFAULT_MIN_CAP_YI
    if min_yi < 0:
        min_yi = 0.0

    grade_key = (grade or "all").strip().lower()
    if grade_key in {"a", "b", "c", "d", "e"}:
        grade_key = grade_key.upper()
    elif grade_key not in {"all", "pending", "scored"}:
        grade_key = "all"

    if refresh:
        with _LOCK:
            _AUTO_SCORED = False
            _LAST_BOARD = None
            _LAST_TS = 0.0
        _clear_universe_disk(None)

    with _LOCK:
        refreshing = _REFRESHING
        uni_loading = _UNIVERSE_LOADING
        auto_done = _AUTO_SCORED
        cached_universe = (
            not refresh
            and _LAST_BOARD is not None
            and isinstance(_LAST_BOARD.get("_universe"), list)
            and time.time() - _LAST_TS < CACHE_TTL_SEC
        )
        cached_uni_rows: list[dict[str, Any]] | None = None
        if cached_universe:
            cached_uni_rows = list(_LAST_BOARD["_universe"])  # type: ignore[index]

    if cached_uni_rows is not None:
        universe = cached_uni_rows[:uni_n]
    else:
        disk_rows = _load_universe_disk(min_yi)
        if disk_rows:
            universe = disk_rows[:uni_n]
        elif uni_loading:
            return _computing_stub(
                min_yi=min_yi,
                top_n=top_n,
                reason="正在拉取 A股池，本页会自动刷新…",
            )
        else:
            _schedule_universe_load(min_yi)
            return _computing_stub(
                min_yi=min_yi,
                top_n=top_n,
                reason="正在拉取 A股池，本页会自动刷新…",
            )

    symbols = [u["symbol"] for u in universe]
    # 分批读因子库，避免单次 IN 5000 过慢
    scored_map: dict[str, dict[str, Any]] = {}
    chunk = 400
    for i in range(0, len(symbols), chunk):
        scored_map.update(_load_scored_map(symbols[i : i + chunk]))

    items: list[dict[str, Any]] = []
    pending_syms: list[str] = []
    for u in universe:
        sym = str(u["symbol"]).upper()
        rep = scored_map.get(sym)
        it = _item_from_report(u, rep)
        if rep:
            it["industry"] = rep.get("industry") or ""
            it["name"] = it.get("name") or rep.get("name") or u.get("name") or ""
        items.append(it)
        if not it.get("scored"):
            pending_syms.append(u["symbol"])

    started = False
    if pending_syms and not refreshing and (refresh or not auto_done):
        started = _schedule_scoring(pending_syms)

    groups_full = _group_by_grade(items)
    grade_counts = {g: len(groups_full[g]) for g in GRADE_BANDS}
    grade_counts["pending"] = len(groups_full["pending"])
    scored_count = sum(grade_counts[g] for g in GRADE_BANDS)

    # 按需裁剪下发内容
    lim = max(1, min(int(pending_limit), 500))
    off = max(0, int(pending_offset))
    groups_out: dict[str, list[dict[str, Any]]] = {g: [] for g in GRADE_BANDS}
    groups_out["pending"] = []

    if grade_key == "pending":
        page = groups_full["pending"][off : off + lim]
        groups_out["pending"] = [_slim_row(x) for x in page]
    elif grade_key in GRADE_BANDS:
        groups_out[grade_key] = [_slim_row(x) for x in groups_full[grade_key]]
    else:
        # all / scored：只下发已评分 A–E，未评仅计数（防超时）
        for g in GRADE_BANDS:
            groups_out[g] = [_slim_row(x) for x in groups_full[g]]

    shown: list[dict[str, Any]] = []
    if grade_key == "pending":
        shown = list(groups_out["pending"])
    elif grade_key in GRADE_BANDS:
        shown = list(groups_out[grade_key])
    else:
        for g in GRADE_BANDS:
            shown.extend(groups_out[g])

    status = "ready"
    if pending_syms:
        if started or refreshing or _REFRESHING:
            status = "computing"
        else:
            status = "partial"

    board = {
        "count": len(shown),
        "universe_size": len(universe),
        "scored_count": scored_count,
        "pending_count": len(pending_syms),
        "min_cap_yi": min_yi,
        "pool_note": _pool_note(min_yi),
        "items": shown,
        "groups": groups_out,
        "grade_counts": grade_counts,
        "grade": grade_key,
        "pending_limit": lim if grade_key == "pending" else None,
        "pending_offset": off if grade_key == "pending" else None,
        "status": status,
        "cached": cached_uni_rows is not None,
        "refresh_started": started,
        "progress": dict(_PROGRESS),
        "notes": [
            "股票池：沪深 A 股全量，不设市值门槛",
            "默认只返回已评分 A–E；未评分请点「未评」分页加载（避免超时）",
            "展示按 A/B/C/D/E 主档分组（A- 归 A，B+/B- 归 B）",
            "未评分标的后台补算，本页自动刷新",
        ],
    }
    with _LOCK:
        # 只缓存股票池，分数每次重拼（避免把巨大 JSON 锁进内存）
        _LAST_BOARD = {"_universe": list(universe)}
        _LAST_TS = time.time()
    return board
