"""Init kline data script."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.database import SessionLocal, init_db
from app.services.kline_service import KlineService
from app.services.pattern_service import PatternService


def main():
    symbol = sys.argv[1] if len(sys.argv) > 1 else "000001.SZ"
    init_db()
    db = SessionLocal()
    try:
        kline_svc = KlineService(db)
        count, _ = kline_svc.sync(symbol)
        print(f"Synced {count} klines for {symbol}")
        pattern_svc = PatternService(db)
        found = pattern_svc.scan(symbol, lookback_days=120)
        print(f"Found {found} patterns")
    finally:
        db.close()


if __name__ == "__main__":
    main()
