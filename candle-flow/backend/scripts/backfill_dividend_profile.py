"""分红档案全量回补：把东财分红历史压成 stock_dividend_profile 表。

用途（真高股息判据的数据底座）：
  读层「连续现金分红≥3年」「近三年平均股息率」两维必须有本地分红历史。
  分红历史来自实时接口（``dividend_data.fetch_dividend_history``），读层
  不能对全市场打网络，故先回补落库，之后由快照构建顺带刷新
  （``factor_db.upsert`` → ``dividend_store.refresh_profile``）。

用法（在 backend 目录下）：
  python scripts/backfill_dividend_profile.py                  # 全量回补
  python scripts/backfill_dividend_profile.py --only-missing   # 只补缺档案的
  python scripts/backfill_dividend_profile.py --limit 300      # 小样本试跑
  python scripts/backfill_dividend_profile.py --workers 6      # 并发数（默认 6）

为什么默认 6 并发：东财 datacenter 接口单只 ≈0.15~0.3s，6 并发下全市场
≈2~3 分钟；再高容易触发限流，反而更慢（失败重试有退避）。
"""

from __future__ import annotations

import argparse
import random
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sqlalchemy import text  # noqa: E402

from app.database import SessionLocal, init_db  # noqa: E402
from app.services import dividend_store  # noqa: E402


def _covered_symbols() -> list[str]:
    """回补范围 = 读层实际服务的标的（有综合分的快照）。"""
    db = SessionLocal()
    try:
        rows = db.execute(
            text("SELECT symbol FROM factor_snapshots WHERE composite_score IS NOT NULL")
        ).all()
        return [r[0] for r in rows]
    finally:
        db.close()


def _existing_symbols() -> set[str]:
    db = SessionLocal()
    try:
        return {r[0] for r in db.execute(text("SELECT symbol FROM stock_dividend_profile")).all()}
    finally:
        db.close()


def _fetch_one(symbol: str, retries: int = 2) -> tuple[str, dict | None, str | None]:
    """返回 (symbol, profile|None, error|None)。"""
    from app.analysis.dividend_data import fetch_dividend_history

    last_err: str | None = None
    for attempt in range(retries + 1):
        try:
            hist = fetch_dividend_history(symbol)
            return symbol, dividend_store.compute_profile(hist), None
        except Exception as exc:  # noqa: BLE001
            last_err = f"{type(exc).__name__}: {exc}"[:160]
            time.sleep(0.4 * (attempt + 1) + random.random() * 0.2)
    return symbol, None, last_err


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=0, help="只处理前 N 只（试跑用）")
    ap.add_argument("--workers", type=int, default=6)
    ap.add_argument("--only-missing", action="store_true", help="跳过已有档案的标的")
    ap.add_argument("--batch", type=int, default=200, help="每多少只提交一次")
    args = ap.parse_args()

    init_db()  # 确保 stock_dividend_profile 表存在（幂等）

    symbols = _covered_symbols()
    if args.only_missing:
        have = _existing_symbols()
        symbols = [s for s in symbols if s not in have]
    if args.limit:
        symbols = symbols[: args.limit]

    print(f"待回补 {len(symbols)} 只，并发 {args.workers}", flush=True)
    if not symbols:
        print("无待处理标的，退出。", flush=True)
        return 0

    t0 = time.time()
    ok = no_data = failed = 0
    pending: list[tuple[str, dict]] = []
    errors: list[tuple[str, str]] = []
    for i, (sym, prof, err) in enumerate(
        _run_pool(symbols, args.workers), start=1
    ):
        if err is not None:
            failed += 1
            errors.append((sym, err))
        elif prof is None:
            no_data += 1  # 从未分红/接口无记录：不写行（读层按「缺数据」不入选）
        else:
            ok += 1
            pending.append((sym, prof))
        if len(pending) >= args.batch:
            dividend_store.upsert_profiles(pending)
            pending.clear()
        if i % 200 == 0 or i == len(symbols):
            print(
                f"  {i}/{len(symbols)}  有效 {ok} / 无分红记录 {no_data} / 失败 {failed}"
                f"  用时 {time.time() - t0:.0f}s",
                flush=True,
            )
    if pending:
        dividend_store.upsert_profiles(pending)

    dividend_store.invalidate()
    st = dividend_store.stats()
    print(
        f"完成：有效 {ok} / 无分红记录 {no_data} / 失败 {failed}，"
        f"总耗时 {time.time() - t0:.0f}s",
        flush=True,
    )
    print(f"表内档案 {st['total']} 条，其中连续分红≥3 年 {st['continuous_ge3']} 条", flush=True)
    for sym, err in errors[:10]:
        print(f"  失败样例 {sym}: {err}", flush=True)
    return 0 if failed == 0 else 1


def _run_pool(symbols: list[str], workers: int):
    """并发抓取并按完成顺序产出（内部小批量 sleep 降低限流风险）。"""
    with ThreadPoolExecutor(max_workers=max(1, workers)) as ex:
        futs = {ex.submit(_fetch_one, s): s for s in symbols}
        for fut in as_completed(futs):
            try:
                yield fut.result()
            except Exception as exc:  # noqa: BLE001 - 单只异常不得中断整轮
                yield futs[fut], None, f"{type(exc).__name__}: {exc}"[:160]


if __name__ == "__main__":
    raise SystemExit(main())
