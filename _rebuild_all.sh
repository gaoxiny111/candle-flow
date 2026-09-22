#!/bin/bash
# 全量因子快照重建（可续跑）。每次迭代用 budget_sec 限制单次时长，
# 循环直到所有存量快照的 scoring_version 都等于当前 SCORING_VERSION。
cd /opt/candle-flow/backend || exit 1
.venv/bin/python -u - <<'PY'
import time
from sqlalchemy import text
from app.database import SessionLocal
from app.services.factor_db import SCORING_VERSION, build_all

def remaining():
    db = SessionLocal()
    try:
        n_bad = db.execute(text(
            "SELECT COUNT(*) FROM factor_snapshots WHERE "
            "json_type(payload,'$.scoring_version') IS NULL "
            "OR json_type(payload,'$.profit_yoy') IS NULL "
            "OR json_extract(payload,'$.scoring_version') IS NOT :v"
        ), {"v": SCORING_VERSION}).scalar()
        total = db.execute(text("SELECT COUNT(*) FROM factor_snapshots")).scalar()
        return int(n_bad or 0), int(total or 0)
    finally:
        db.close()

print(f"目标版本 = {SCORING_VERSION}", flush=True)
round_no = 0
while True:
    round_no += 1
    n_bad, total = remaining()
    print(f"\n===== 第 {round_no} 轮 [{time.strftime('%H:%M:%S')}] "
          f"存量 {total} 只，待重建 {n_bad} 只 =====", flush=True)
    if n_bad == 0:
        print("✅ ALL DONE：全部快照已是最新口径", flush=True)
        break
    if round_no > 30:
        print("⚠ 达到轮次上限，退出（可能有长期失败票，需单独排查）", flush=True)
        break
    t0 = time.time()
    try:
        stats = build_all(force=False, budget_sec=3300)
        print(f"    本轮结果: {stats}  用时 {time.time() - t0:.0f}s", flush=True)
    except Exception as e:
        print(f"    ⚠ build_all 异常: {type(e).__name__}: {e}", flush=True)
        time.sleep(5)
PY
echo "REBUILD_LOOP_EXIT=$?" >> /tmp/_rebuild_all.log
