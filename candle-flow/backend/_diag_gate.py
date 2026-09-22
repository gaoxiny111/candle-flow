"""端到端验证：减持窗口期闸门 + 快照字段打通。"""
import json
import sys

sys.path.insert(0, ".")

from app.analysis.engine import FundamentalEngine  # noqa
from app.services.market_confluence_service import _detect_buy_signal  # noqa
from app.services.kline_service import KlineService  # noqa
from app.database import SessionLocal  # noqa

SYM = "603519.SH"

print("=== 1. report 顶层是否带 reduce_window ===")
rep = FundamentalEngine().run_full_analysis(SYM)
print("reduce_window:", json.dumps(rep.get("reduce_window"), ensure_ascii=False))
print("reduce_remaining_days:", rep.get("reduce_remaining_days"))
print("latest_block_trade:", json.dumps(rep.get("latest_block_trade"), ensure_ascii=False))
risk = (rep.get("modules") or {}).get("risk") or {}
print("risk.metadata.reduce_window_deduct:", (risk.get("metadata") or {}).get("reduce_window_deduct"))
print("risk.metadata.block_trade_deduct:", (risk.get("metadata") or {}).get("block_trade_deduct"))

print("\n=== 2. _detect_buy_signal 闸门单测 ===")
sess = SessionLocal()
try:
    klines, _ = KlineService(sess).get_recent_klines(SYM, limit=180)
    print("klines:", len(klines))
    if len(klines) >= 60:
        win = rep.get("reduce_window")
        # 构造「强买入条件全满足」的场景：基本面 88 + PEG 1.0
        r_blocked = _detect_buy_signal(
            klines, 88.0, 1.0, None, pattern_name="红三兵",
            pattern_ready=True, reduce_window=win,
        )
        r_free = _detect_buy_signal(
            klines, 88.0, 1.0, None, pattern_name="红三兵",
            pattern_ready=True, reduce_window=None,
        )
        print("窗口期开启 ->", r_blocked.get("signal"), r_blocked.get("label"), "gate=", r_blocked.get("gate"))
        print("   理由:", r_blocked.get("reasons"))
        print("无窗口期  ->", r_free.get("signal"), r_free.get("label"))
        # 窗口已结束的窗口对象（end 在过去）
        past = {"start": "2025-01-01", "end": "2025-03-01", "in_window": False, "remaining_days": 0}
        r_past = _detect_buy_signal(
            klines, 88.0, 1.0, None, pattern_name="红三兵",
            pattern_ready=True, reduce_window=past,
        )
        print("窗口已结束 ->", r_past.get("signal"), r_past.get("label"))
finally:
    sess.close()
