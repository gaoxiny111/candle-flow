"""Independent daily worker: incremental kline sync + bull tactics report.

Usage (from backend dir, with venv):
  python -m app.workers.daily_scan

When using this worker, set ENABLE_INPROCESS_SCHEDULER=0 on the API service
to avoid duplicate runs.
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
    return 0 if report.get("status") in ("ok", "stale_data") else 1


if __name__ == "__main__":
    sys.exit(main())
