"""收盘后自动扫描主板战法，输出当日符合条件的股票列表。"""

from __future__ import annotations

import csv
import json
import logging
import threading
from datetime import date, datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from sqlalchemy import func

from app.core.bull_tactics import TACTIC_NAMES, is_main_board, is_st_name
from app.database import SessionLocal
from app.models.kline import KlineData
from app.models.stock import StockInfo
from app.services.bull_tactics_service import BullTacticsService
from app.services.main_board_kline_sync import is_fresh_enough, target_trade_date

logger = logging.getLogger(__name__)

TZ = ZoneInfo("Asia/Shanghai")
DATA_DIR = Path("data") / "bull_tactics"
_run_lock = threading.Lock()
_running = False


def _today() -> str:
    return target_trade_date().isoformat()


def _now_iso() -> str:
    return datetime.now(TZ).isoformat(timespec="seconds")


def report_path(trade_date: str | None = None) -> Path:
    d = trade_date or _today()
    return DATA_DIR / f"daily_{d}.json"


def latest_path() -> Path:
    return DATA_DIR / "latest.json"


def csv_path(trade_date: str | None = None) -> Path:
    d = trade_date or _today()
    return DATA_DIR / f"daily_{d}.csv"


def load_daily_report(trade_date: str | None = None) -> dict[str, Any] | None:
    """读取指定日期或最新战法日报。"""
    paths: list[Path] = []
    if trade_date:
        paths.append(report_path(trade_date))
    else:
        paths.extend([latest_path(), report_path(_today())])
    for path in paths:
        if not path.is_file():
            continue
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except Exception as exc:
            logger.warning("failed to read bull tactics report %s: %s", path, exc)
    return None


def _parse_day(trade_date: str) -> date:
    return date.fromisoformat(str(trade_date)[:10])


def kline_freshness(db, trade_date: str) -> dict[str, Any]:
    """统计主板宇宙里，K 线是否已更新到目标交易日。"""
    day = _parse_day(trade_date)
    rows = db.query(StockInfo).filter(StockInfo.market.in_(("SH", "SZ"))).all()
    universe = [r for r in rows if is_main_board(r.symbol) and not is_st_name(r.name)]
    symbols = {r.symbol for r in universe}

    max_dates = dict(db.query(KlineData.symbol, func.max(KlineData.date)).group_by(KlineData.symbol).all())

    with_bars = 0
    fresh = 0
    for sym in symbols:
        mx = max_dates.get(sym)
        if mx is None:
            continue
        with_bars += 1
        if mx >= day:
            fresh += 1

    db_latest = max(max_dates.values()) if max_dates else None
    payload = {
        "universe_size": len(symbols),
        "with_bars": with_bars,
        "fresh_to_trade_date": fresh,
        "db_latest_date": db_latest.isoformat() if db_latest else None,
        "trade_date": trade_date,
        "ratio": round(fresh / len(symbols), 4) if symbols else 0.0,
    }
    payload["stale"] = not is_fresh_enough(payload)
    payload["ready"] = not payload["stale"]
    return payload


def _flatten_today_hits(scan: dict[str, Any], trade_date: str) -> list[dict[str, Any]]:
    """只保留买点日期为当日的命中，展平为列表。"""
    rows: list[dict[str, Any]] = []
    for item in scan.get("items") or []:
        symbol = item.get("symbol") or ""
        name = item.get("name") or ""
        for hit in item.get("hits") or []:
            buy_date = str(hit.get("buy_date") or "")[:10]
            if buy_date != trade_date:
                continue
            rows.append(
                {
                    "symbol": symbol,
                    "name": name,
                    "tactic": hit.get("tactic"),
                    "score": hit.get("score"),
                    "buy_date": buy_date,
                    "buy_price": hit.get("buy_price"),
                    "setup_date": hit.get("setup_date"),
                    "details": hit.get("details") or {},
                }
            )
    rows.sort(key=lambda r: (str(r.get("tactic") or ""), -float(r.get("score") or 0), r.get("symbol") or ""))
    return rows


def _count_all_hits(scan: dict[str, Any]) -> int:
    return sum(len(item.get("hits") or []) for item in (scan.get("items") or []))


def _by_tactic(rows: list[dict[str, Any]]) -> dict[str, list[dict[str, Any]]]:
    out: dict[str, list[dict[str, Any]]] = {name: [] for name in TACTIC_NAMES}
    for row in rows:
        tactic = str(row.get("tactic") or "")
        if tactic not in out:
            out[tactic] = []
        out[tactic].append(
            {
                "symbol": row["symbol"],
                "name": row["name"],
                "score": row["score"],
                "buy_price": row["buy_price"],
                "setup_date": row["setup_date"],
            }
        )
    return out


def _write_csv(path: Path, rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8-sig", newline="") as f:
        writer = csv.DictWriter(
            f,
            fieldnames=["symbol", "name", "tactic", "score", "buy_date", "buy_price", "setup_date"],
        )
        writer.writeheader()
        for row in rows:
            writer.writerow(
                {
                    "symbol": row.get("symbol"),
                    "name": row.get("name"),
                    "tactic": row.get("tactic"),
                    "score": row.get("score"),
                    "buy_date": row.get("buy_date"),
                    "buy_price": row.get("buy_price"),
                    "setup_date": row.get("setup_date"),
                }
            )


def _freshness_message(fresh: dict[str, Any], today_hits: int, recent_hits: int) -> str:
    if fresh.get("stale"):
        return (
            f"K 线未达新鲜度门槛（{fresh.get('fresh_to_trade_date')}/{fresh.get('universe_size')}"
            f"，比例 {float(fresh.get('ratio') or 0):.0%}，需≥50%或≥500只）："
            f"不生成「今日战法列表」。库内最新日 {fresh.get('db_latest_date') or '—'}。"
            f"请先增量同步主板 K 线后再试。"
        )
    if today_hits == 0 and recent_hits > 0:
        return f"当日无新买点；近几日扫描到 {recent_hits} 个买点（已按「买点日=当日」过滤）。"
    if today_hits == 0:
        return "数据已达标，但当日暂无符合三条战法条件的标的。"
    return ""


def _persist_report(day: str, payload: dict[str, Any]) -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    text = json.dumps(payload, ensure_ascii=False, indent=2)
    report_path(day).write_text(text, encoding="utf-8")
    latest_path().write_text(text, encoding="utf-8")
    _write_csv(csv_path(day), payload.get("items") or [])


def run_daily_bull_tactics_scan(
    *,
    trade_date: str | None = None,
    recent_bars: int = 7,
    refresh_list: bool = False,
    sync_klines: bool = False,
    job_id: str | None = None,
) -> dict[str, Any]:
    """
    全市场扫描三条战法，筛选当日买点，写入 JSON/CSV。
    新鲜度不达标时不宣称今日列表（status=stale_data，items 为空）。
    """
    from app.services.job_progress import finish_job, update_job

    global _running
    with _run_lock:
        if _running:
            payload = {"status": "already_running", "trade_date": trade_date or _today()}
            if job_id:
                finish_job(job_id, payload, error="already_running")
            return payload
        _running = True

    day = trade_date or _today()
    started = datetime.now(TZ)
    sync_info: dict[str, Any] | None = None

    def _progress(done: int, total: int, phase: str) -> None:
        if not job_id:
            return
        labels = {"scan": "扫描战法", "sync": "同步K线", "tier": "分层", "fundamentals": "基本面", "done": "完成"}
        update_job(
            job_id,
            phase=phase,
            done=done,
            total=total,
            message=f"{labels.get(phase, phase)} {done}/{total}" if total else labels.get(phase, phase),
        )

    try:
        if sync_klines:
            from app.services.main_board_kline_sync import sync_main_board_klines

            if job_id:
                update_job(job_id, phase="sync", message="增量同步主板 K 线…", done=0, total=0)
            logger.info("daily bull tactics: incremental kline sync before scan…")
            sync_info = sync_main_board_klines(
                refresh_universe_list=refresh_list,
                incremental=True,
                as_of=_parse_day(day),
            )
            if job_id and sync_info:
                needed = int(sync_info.get("needed") or 0)
                synced = int(sync_info.get("synced") or 0)
                update_job(
                    job_id,
                    phase="sync",
                    done=synced,
                    total=max(needed, synced),
                    message=f"K线同步完成 {synced}/{needed or synced}",
                )

        db = SessionLocal()
        try:
            fresh = kline_freshness(db, day)
            if fresh.get("stale"):
                elapsed = (datetime.now(TZ) - started).total_seconds()
                message = _freshness_message(fresh, 0, 0)
                payload: dict[str, Any] = {
                    "status": "stale_data",
                    "ready": False,
                    "trade_date": day,
                    "generated_at": _now_iso(),
                    "elapsed_sec": round(elapsed, 1),
                    "scanned": 0,
                    "universe_size": fresh.get("universe_size", 0),
                    "scan_skipped": 0,
                    "scan_errors": 0,
                    "recent_hit_count": 0,
                    "kline": fresh,
                    "sync": sync_info,
                    "message": message,
                    "count": 0,
                    "counts": {name: 0 for name in TACTIC_NAMES},
                    "items": [],
                    "by_tactic": {name: [] for name in TACTIC_NAMES},
                }
                _persist_report(day, payload)
                logger.warning("daily bull tactics gated: %s", message)
                if job_id:
                    finish_job(job_id, payload)
                return payload

            if job_id:
                update_job(job_id, phase="scan", message="扫描主板战法…", done=0, total=0)
            scan = BullTacticsService(db).scan_market(
                recent_bars=recent_bars,
                refresh_list=False if sync_klines else refresh_list,
                tactics=None,
                progress=_progress if job_id else None,
            )
        finally:
            db.close()

        recent_hit_count = _count_all_hits(scan)
        rows = _flatten_today_hits(scan, day)
        by_tactic = _by_tactic(rows)
        elapsed = (datetime.now(TZ) - started).total_seconds()
        message = _freshness_message(fresh, len(rows), recent_hit_count)
        payload = {
            "status": "ok",
            "ready": True,
            "trade_date": day,
            "generated_at": _now_iso(),
            "elapsed_sec": round(elapsed, 1),
            "scanned": scan.get("scanned", 0),
            "universe_size": scan.get("universe_size", 0),
            "prefiltered": scan.get("prefiltered"),
            "prefilter": scan.get("prefilter"),
            "scan_skipped": scan.get("skipped", 0),
            "scan_errors": scan.get("errors", 0),
            "recent_hit_count": recent_hit_count,
            "kline": fresh,
            "sync": sync_info,
            "message": message,
            "count": len(rows),
            "counts": {name: len(by_tactic.get(name) or []) for name in TACTIC_NAMES},
            "items": rows,
            "by_tactic": by_tactic,
        }
        _persist_report(day, payload)

        summary = ", ".join(f"{k} {v}" for k, v in payload["counts"].items())
        logger.info(
            "daily bull tactics %s: %s hits (%s); fresh %s/%s; scanned %s; %.1fs → %s",
            day,
            payload["count"],
            summary,
            fresh.get("fresh_to_trade_date"),
            fresh.get("universe_size"),
            payload["scanned"],
            elapsed,
            report_path(day),
        )
        if message:
            logger.warning("daily bull tactics note: %s", message)
        if job_id:
            finish_job(job_id, payload)
        return payload
    except Exception as exc:
        logger.exception("daily bull tactics scan failed")
        fail = {
            "status": "error",
            "ready": False,
            "trade_date": day,
            "generated_at": _now_iso(),
            "error": str(exc),
            "message": str(exc),
            "count": 0,
            "items": [],
            "by_tactic": {name: [] for name in TACTIC_NAMES},
            "counts": {name: 0 for name in TACTIC_NAMES},
        }
        try:
            _persist_report(day, fail)
        except Exception:
            pass
        if job_id:
            finish_job(job_id, fail, error=str(exc))
        return fail
    finally:
        with _run_lock:
            _running = False


def start_daily_bull_tactics_async(
    *,
    trade_date: str | None = None,
    recent_bars: int = 7,
    refresh_list: bool = False,
    sync_klines: bool = True,
) -> dict[str, Any]:
    """后台启动日报生成，立即返回 job_id。"""
    from app.services.job_progress import get_job, run_in_background

    existing = get_job(kind="bull_tactics_daily")
    if existing and existing.get("status") == "running":
        return {"status": "already_running", "job_id": existing.get("job_id"), "progress": existing}

    def _run(job_id: str) -> None:
        run_daily_bull_tactics_scan(
            trade_date=trade_date,
            recent_bars=recent_bars,
            refresh_list=refresh_list,
            sync_klines=sync_klines,
            job_id=job_id,
        )

    job_id = run_in_background(
        "bull_tactics_daily",
        _run,
        phase="starting",
        message="任务已启动",
    )
    return {"status": "started", "job_id": job_id, "progress": get_job(job_id)}
