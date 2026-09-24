"""Independent daily worker: incremental kline sync + bull tactics report
+ 盘后因子库预构建（榜单基本面分的唯一自动通道）。

Usage (from backend dir, with venv):
  python -m app.workers.daily_scan

When using this worker, set ENABLE_INPROCESS_SCHEDULER=0 on the API service
to avoid duplicate runs. **注意**：进程内调度器 `_scheduled_sync` 里含
`build_all()`，禁用后本 worker 必须补齐这一步，否则因子快照永无自动重建
（榜单会一直停在手动触发时的那一版）。
"""

from __future__ import annotations

import logging
import sys

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
logger = logging.getLogger("daily_scan_worker")


def main() -> int:
    from app.database import init_db
    from app.services.bull_tactics_daily import run_daily_bull_tactics_scan
    from app.services.main_board_kline_sync import sync_main_board_klines

    init_db()
    logger.info("daily worker: incremental kline sync…")
    sync_info = sync_main_board_klines(refresh_universe_list=True, incremental=True)
    logger.info("daily worker: sync done %s", sync_info)
    logger.info("daily worker: bull tactics scan…")
    report = run_daily_bull_tactics_scan(refresh_list=False, sync_klines=False)
    logger.info(
        "daily worker: report status=%s count=%s trade_date=%s",
        report.get("status"),
        report.get("count"),
        report.get("trade_date"),
    )

    # 因子库盘后预构建：与进程内调度器 _scheduled_sync 保持同一步骤与顺序。
    # 非 force：披露窗口期（1-4/7-8/10 月）全量重建，非披露期只补缺失与口径
    # 落后项 —— 故非披露期通常秒级返回，不额外占用机器。
    try:
        from app.services.factor_db import build_all

        stats = build_all()
        logger.info("daily worker: factor build %s", stats)
    except Exception:
        logger.exception("daily worker: factor build failed")

    return 0 if report.get("status") in ("ok", "stale_data") else 1


if __name__ == "__main__":
    sys.exit(main())
