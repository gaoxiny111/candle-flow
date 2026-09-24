from datetime import date, datetime
from decimal import Decimal
from typing import List, Optional, Tuple
import logging

from sqlalchemy import func
from sqlalchemy.orm import Session

from app.core.exceptions import DataSourceError
from app.models.kline import KlineData
from app.models.pattern import PatternRecord
from app.models.signal import TradingSignal
from app.services.akshare_client import akshare_client, is_cn_weekday, trading_today
from app.utils.price_filter import filter_inliers, is_price_outlier, price_anchor
from app.utils.symbol import is_b_share, is_future, is_index_symbol, normalize_symbol, parse_symbol

logger = logging.getLogger(__name__)


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
        if is_index_symbol(symbol):
            # 指数误用个股行情时会出现 ~10 元与 ~3000 点混杂
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
        allow_history_overwrite: bool = False,
    ) -> bool:
        """写/更新一根 K 线。

        ``allow_history_overwrite=False``（默认）时**拒绝覆盖已完成交易日**：
        日线源的历史数据是权威值，只有当日 bar 允许被后续同步修正。
        旧行为下 ``merge_today_spot`` 的盘中 spot 曾把 09-22 的正确收盘价
        与成交量覆盖成 09:31 的实时快照（实测焦作万方 09-22 量 3805万→31.9万）。
        历史回补脚本（``allow_history_overwrite=True``）是唯一例外。
        """
        if not allow_history_overwrite and d < trading_today():
            logger.warning(
                "refuse history overwrite for %s: bar_date=%s today=%s",
                symbol,
                d,
                trading_today(),
            )
            return False
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

    def latest_is_stale(self, symbol: str) -> bool:
        """True when Shanghai trading day has no daily bar yet."""
        today = trading_today()
        if not is_cn_weekday(today):
            return False
        latest = self.get_latest(symbol)
        return latest is None or latest.date < today

    @staticmethod
    def _is_after_close(now: datetime | None = None) -> bool:
        """实时行情是否已可视为「今日日线」（收盘后）。

        盘中（09:30~15:00）的 spot 是**未完成的当日快照**：close 是当时价、
        volume 是当时累计量。把它写成日线会让 EMA/RSI/量比全部失真
        （实测：焦作万方今日 09:31 写入假 bar close=11.21，真实午盘 10.94；
        全库 09-23 均量因此塌 -95%）。所以只允许 15:00 之后合并。

        用 ``Asia/Shanghai`` 而非服务器本地时区（线上是 UTC，否则 15:00
        会被判成 07:00 之前）。
        """
        from app.services.akshare_client import CN_TZ

        now = now or datetime.now(CN_TZ)
        if now.tzinfo is None:
            now = now.replace(tzinfo=CN_TZ)
        now = now.astimezone(CN_TZ)
        return (now.hour, now.minute) >= (15, 0)

    def merge_today_spot(self, symbol: str, force: bool = False) -> bool:
        """日线源常不含当天，**收盘后**用现价补一根今日 K 线（东财 / 腾讯 / 新浪）。

        两条硬约束（防盘中污染）：
        1. 仅在 **15:00 之后** 调用（``force=True`` 可绕过，仅供离线回补脚本）；
        2. **绝不回写已完成交易日** —— ``spot_date < trading_today()`` 直接拒绝，
           否则会把历史日（如 09-22）的正确收盘/成交量覆盖成实时快照。
        """
        today = trading_today()
        if not is_cn_weekday(today):
            return False
        if not force and not self._is_after_close():
            return False
        spot = akshare_client.fetch_spot(symbol)
        if not spot:
            return False
        # Reject quotes stamped on a different calendar day (stale cache / holiday).
        spot_date = spot["date"]
        if isinstance(spot_date, datetime):
            spot_date = spot_date.date()
        if spot_date != today:
            logger.warning(
                "spot date mismatch for %s: quote=%s shanghai=%s",
                symbol,
                spot_date,
                today,
            )
            return False
        latest = self.get_latest(symbol)
        # 历史数据保护：spot 只能写「今天」，不能回头改已完成交易日。
        if spot_date < today:
            logger.warning(
                "refuse to overwrite finished session for %s: spot_date=%s today=%s",
                symbol,
                spot_date,
                today,
            )
            return False
        vol = int(spot["volume"] or 0)
        # 量纲对齐：日线源的成交量单位是「手」，部分 spot 通道返回「股」，差 100 倍。
        # 判据必须是「与昨日量的**量级**差 100 倍」而不是「差 50 倍就乘」——
        # 旧实现写 `vol * 50 < latest.volume`，对「今日缩量到 1%」的正常情形会
        # 误判为量纲不符并乘 100，把**真实成交量灌水 100 倍**（实测线上 09-22：
        # 3048 只中 1212 只（39.8%）的当日量被放大到 100 倍，avg 冲高到
        # 一个量级失真的 3794 万/50.6 亿）。改为「小于昨日量的 1%」才补乘。
        if latest and latest.volume and vol > 0 and vol < int(latest.volume) / 100:
            vol *= 100
        # Keep previous volume when spot reports 0 but we already have today's bar.
        if vol <= 0 and latest and latest.date == spot_date:
            vol = int(latest.volume or 0)
        if vol <= 0 and latest and latest.date == spot_date and float(latest.close) == float(spot["close"]):
            return False
        self._upsert_bar(
            symbol,
            spot_date,
            spot["open"],
            spot["high"],
            spot["low"],
            spot["close"],
            vol,
        )
        self.db.commit()
        return True

    def ensure_today_bar(self, symbol: str) -> bool:
        """Backfill today's bar when hist sync left the series one session behind."""
        symbol = normalize_symbol(symbol)
        if not self.latest_is_stale(symbol):
            return False
        return self.merge_today_spot(symbol)

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
                # merge_today_spot 自带收盘门槛，盘中会直接返回 False。
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
                allow_history_overwrite=True,  # 日线源是历史数据的权威值
            )
            if created:
                count += 1
        self.db.commit()
        self.merge_today_spot(symbol)
        synced = count if count else len(df)
        return synced, purged
