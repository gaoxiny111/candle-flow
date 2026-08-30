import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sqlalchemy import create_engine, text

sym = "900948.SH"
engine = create_engine("sqlite:///./data/candle_flow.db")
with engine.connect() as c:
    dups = c.execute(
        text(
            "SELECT date, COUNT(*) FROM kline_data WHERE symbol=:s GROUP BY date HAVING COUNT(*)>1"
        ),
        {"s": sym},
    ).fetchall()
    print("duplicates", dups)
    # count rows > 10
    high = c.execute(
        text("SELECT COUNT(*) FROM kline_data WHERE symbol=:s AND close > 10"),
        {"s": sym},
    ).scalar()
    print("rows with close>10", high)
