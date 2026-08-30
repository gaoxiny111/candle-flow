"""Check whether kline data is real or mock."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sqlalchemy import create_engine, text

engine = create_engine("sqlite:///./data/candle_flow.db")

with engine.connect() as c:
    print("=== 数据库各标的来源 ===")
    for r in c.execute(
        text(
            "SELECT symbol, source, COUNT(*), MIN(date), MAX(date) "
            "FROM kline_data GROUP BY symbol, source"
        )
    ):
        print(r)

    for sym in ["000001.SZ", "601088.SH", "900948.SH"]:
        print(f"\n=== {sym} 最近3条(DB) ===")
        rows = c.execute(
            text(
                "SELECT date, close, volume, source FROM kline_data "
                "WHERE symbol=:s ORDER BY date DESC LIMIT 3"
            ),
            {"s": sym},
        ).fetchall()
        for r in rows:
            print(r)

print("\\n=== 尝试 AKShare 拉取对比 ===")
try:
    import akshare as ak

    df = ak.stock_zh_a_hist(
        symbol="000001",
        period="daily",
        start_date="20260801",
        end_date="20260828",
        adjust="qfq",
    )
    if df is not None and not df.empty:
        print("AKShare 成功，最近3条:")
        print(df[["日期", "收盘", "成交量"]].tail(3).to_string(index=False))
    else:
        print("AKShare 返回空数据")
except Exception as e:
    print(f"AKShare 请求失败: {e}")
