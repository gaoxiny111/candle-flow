from datetime import date
from decimal import Decimal
from typing import List, Optional, Tuple

from sqlalchemy import func
from sqlalchemy.orm import Session

from app.core.exceptions import DataSourceError
from app.models.kline import KlineData
from app.models.pattern import PatternRecord
from app.models.signal import TradingSignal
from app.services.akshare_client import akshare_client
from app.utils.price_filter import filter_inliers, is_price_outlier, price_anchor
from app.utils.symbol import is_b_share, is_future, normalize_symbol, parse_symbol


class KlineService:
    B_SHARE_MAX_PRICE = 15.0  # 沪B/深B 股价通常不超过此范围（元）

    def __init__(self, db: Session):
        self.db = db

    def get_klines(
        self,
        symbol: str,
        start_date: Optional[str] = None,
        end_date: Optional[str] = None,
        page: int = 1,
        page_size: int = 100,
    ) -> Tuple[List[KlineData], int]:
        q = self.db.query(KlineData).filter(KlineData.symbol == symbol)
        if start_date:
            from datetime import datetime

            q = q.filter(KlineData.date >= datetime.strptime(start_date, "%Y-%m-%d").date())
        if end_date:
            from datetime import datetime

            q = q.filter(KlineData.date <= datetime.strptime(end_date, "%Y-%m-%d").date())
        total = q.count()
        if not start_date and not end_date:
            items = (
                q.order_by(KlineData.date.desc())
                .offset((page - 1) * page_size)
                .limit(page_size)
                .all()
            )
            return list(reversed(items)), total
        items = (
            q.order_by(KlineData.date.asc())
            .offset((page - 1) * page_size)
            .limit(page_size)
            .all()
        )
        return items, total

    def get_recent_klines(self, symbol: str, limit: int = 120) -> Tuple[List[KlineData], int]:
        """Return the most recent *limit* bars in ascending date order."""
        symbol = normalize_symbol(symbol)
        items = (
            self.db.query(KlineData)
            .filter(KlineData.symbol == symbol)
            .order_by(KlineData.date.desc())
            .limit(limit)
            .all()
        )
        return list(reversed(items)), len(items)

    def get_latest(self, symbol: str) -> Optional[KlineData]:
        return (
            self.db.query(KlineData)
            .filter(KlineData.symbol == symbol)
            .order_by(KlineData.date.desc())
            .first()
        )

    def get_quote(self, symbol: str) -> Optional[dict]:
        """Latest close, previous close, and change %."""
        symbol = normalize_symbol(symbol)
        rows = (
            self.db.query(KlineData)
            .filter(KlineData.symbol == symbol)
            .order_by(KlineData.date.desc())
            .limit(2)
            .all()
        )
        if not rows:
            return None
        last = rows[0]
        prev = rows[1] if len(rows) > 1 else rows[0]
        last_p = float(last.close)
        prev_p = float(prev.close)
        change = last_p - prev_p
        pct = (change / prev_p * 100) if prev_p else 0.0
        return {
            "last_price": round(last_p, 4),
            "prev_close": round(prev_p, 4),
            "change_amount": round(change, 4),
            "change_pct": round(pct, 2),
            "quote_date": last.date,
        }

    def is_contaminated(self, symbol: str) -> bool:
        """检测是否混入了历史模拟数据"""
        symbol = normalize_symbol(symbol)
        if is_future(symbol):
            return False
        code, _ = parse_symbol(symbol)
        if is_b_share(symbol):
            max_close = (
                self.db.query(func.max(KlineData.close))
                .filter(KlineData.symbol == symbol)
                .scalar()
            )
            if max_close is not None and float(max_close) > self.B_SHARE_MAX_PRICE:
                return True
        # A 股：混入 <20 元的脏数据且正常价 >40
        if code.startswith(("6", "0", "3")):
            max_close = (
                self.db.query(func.max(KlineData.close))
                .filter(KlineData.symbol == symbol)
                .scalar()
            )
            bad_low = (
                self.db.query(func.count(KlineData.id))
                .filter(KlineData.symbol == symbol, KlineData.close < 20)
                .scalar()
            )
            if max_close and bad_low and float(max_close) > 40 and bad_low > 0:
                return True
        # 通用：同一标的收盘价极差异常（高低比 > 8）
        stats = (
            self.db.query(
                func.min(KlineData.close),
                func.max(KlineData.close),
            )
            .filter(KlineData.symbol == symbol)
            .first()
        )
        if stats and stats[0] and stats[1]:
            lo, hi = float(stats[0]), float(stats[1])
            if lo > 0 and hi / lo > 3:
                return True
        return False

    def price_anchor_close(self, symbol: str) -> float | None:
        recent, _ = self.get_recent_klines(symbol, limit=40)
        if not recent:
            return None
        return price_anchor([float(k.close) for k in recent])

    def purge_outliers(self, symbol: str) -> int:
        """Delete bars far from the recent price cluster. Does not need AKShare."""
        symbol = normalize_symbol(symbol)
        anchor = self.price_anchor_close(symbol)
        if anchor is None:
            return 0
        rows = self.db.query(KlineData).filter(KlineData.symbol == symbol).all()
        ids = [r.id for r in rows if is_price_outlier(float(r.close), anchor)]
        if not ids:
            return 0
        self.db.query(KlineData).filter(KlineData.id.in_(ids)).delete(synchronize_session=False)
        self.db.commit()
        return len(ids)

    def sanitize_rows(self, items: List[KlineData]) -> List[KlineData]:
        return filter_inliers(items, lambda k: float(k.close))

    def purge_symbol(self, symbol: str) -> None:
        """清除标的相关的 K 线、形态、信号（用于全量重同步）"""
        symbol = normalize_symbol(symbol)
        self.db.query(KlineData).filter(KlineData.symbol == symbol).delete()
        self.db.query(PatternRecord).filter(PatternRecord.symbol == symbol).delete()
        self.db.query(TradingSignal).filter(TradingSignal.symbol == symbol).delete()
        self.db.commit()

    def _upsert_bar(
        self,
        symbol: str,
        d: date,
        open_: float,
        high: float,
        low: float,
        close: float,
        volume: int,
    ) -> bool:
        existing = (
            self.db.query(KlineData)
            .filter(KlineData.symbol == symbol, KlineData.date == d)
            .first()
        )
        o = Decimal(str(round(open_, 4)))
        h = Decimal(str(round(high, 4)))
        l = Decimal(str(round(low, 4)))
        c = Decimal(str(round(close, 4)))
        vol = int(volume)
        if existing:
            existing.open = o
            existing.high = h
            existing.low = l
            existing.close = c
            existing.volume = vol
            existing.source = "akshare"
            return False
        self.db.add(
            KlineData(
                symbol=symbol,
                date=d,
                open=o,
                high=h,
                low=l,
                close=c,
                volume=vol,
                source="akshare",
            )
        )
        return True

    def merge_today_spot(self, symbol: str) -> bool:
        """日线源常不含当天，用东财现价补一根今日 K 线。"""
        if date.today().weekday() >= 5:
            return False
        spot = akshare_client.fetch_spot(symbol)
        if not spot:
            return False
        latest = self.get_latest(symbol)
        vol = int(spot["volume"] or 0)
        if latest and latest.volume and vol > 0 and vol * 50 < int(latest.volume):
            vol *= 100
        if vol <= 0 and latest and latest.date == spot["date"]:
            return False
        if vol <= 0 and latest and float(latest.close) == float(spot["close"]):
            return False
        created = self._upsert_bar(
            symbol,
            spot["date"],
            spot["open"],
            spot["high"],
            spot["low"],
            spot["close"],
            vol,
        )
        self.db.commit()
        return created

    def sync(self, symbol: str, force: bool = False) -> tuple[int, bool]:
        symbol = normalize_symbol(symbol)
        purged = False
        contaminated = self.is_contaminated(symbol)
        need_full = force or contaminated

        if need_full:
            start = None
        else:
            latest = self.get_latest(symbol)
            start = latest.date.strftime("%Y%m%d") if latest else None

        df = None
        hist_error: Exception | None = None
        try:
            df = akshare_client.fetch_daily(symbol, start_date=start)
        except DataSourceError as e:
            hist_error = e

        if df is None or df.empty:
            if self.get_latest(symbol) is not None:
                self.merge_today_spot(symbol)
                if hist_error and not self.get_latest(symbol):
                    raise hist_error
                return 0, False
            if hist_error:
                raise hist_error
            raise DataSourceError(f"{symbol} 无可用行情数据")

        if is_b_share(symbol) and float(df["close"].max()) > self.B_SHARE_MAX_PRICE:
            raise DataSourceError(f"{symbol} 行情数据异常，请稍后重试")

        if need_full:
            self.purge_symbol(symbol)
            purged = True

        count = 0
        for _, row in df.iterrows():
            d = row["date"] if isinstance(row["date"], date) else row["date"].date()
            created = self._upsert_bar(
                symbol,
                d,
                float(row["open"]),
                float(row["high"]),
                float(row["low"]),
                float(row["close"]),
                int(float(row["volume"])),
            )
            if created:
                count += 1
        self.db.commit()
        self.merge_today_spot(symbol)
        synced = count if count else len(df)
        return synced, purged
