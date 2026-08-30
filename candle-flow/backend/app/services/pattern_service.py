from datetime import date
from decimal import Decimal
from typing import List, Optional, Tuple

from sqlalchemy.orm import Session

from app.core.nison_rules import WESTERN_NOT_CANDLES
from app.core.indicators import is_downtrend, is_uptrend
from app.core.pattern_engine import PatternEngine, kline_to_candles
from app.models.pattern import PatternRecord
from app.services.kline_service import KlineService
from app.services.signal_service import SignalService


class PatternService:
    def __init__(self, db: Session):
        self.db = db
        self.engine = PatternEngine(min_score=50.0)

    def get_patterns(
        self,
        symbol: Optional[str] = None,
        direction: Optional[str] = None,
        status: Optional[str] = None,
        page: int = 1,
        page_size: int = 20,
        symbols: Optional[List[str]] = None,
    ) -> Tuple[List[PatternRecord], int]:
        q = self.db.query(PatternRecord).filter(~PatternRecord.pattern_name.in_(WESTERN_NOT_CANDLES))
        if symbols is not None:
            if not symbols:
                return [], 0
            q = q.filter(PatternRecord.symbol.in_(symbols))
        elif symbol:
            q = q.filter(PatternRecord.symbol == symbol)
        if direction:
            q = q.filter(PatternRecord.direction == direction)
        if status:
            q = q.filter(PatternRecord.confirmation_status == status)
        total = q.count()
        items = (
            q.order_by(PatternRecord.candle_date.desc())
            .offset((page - 1) * page_size)
            .limit(page_size)
            .all()
        )
        return items, total

    def get_by_id(self, pattern_id: int) -> Optional[PatternRecord]:
        return self.db.query(PatternRecord).filter(PatternRecord.id == pattern_id).first()

    def scan(self, symbol: str, lookback_days: int = 60) -> int:
        kline_svc = KlineService(self.db)
        if kline_svc.get_latest(symbol) is None or kline_svc.is_contaminated(symbol):
            kline_svc.sync(symbol, force=True)
        window = max(lookback_days, 120)
        klines, _ = kline_svc.get_recent_klines(symbol, limit=window)
        if len(klines) < 20:
            kline_svc.sync(symbol, force=True)
            klines, _ = kline_svc.get_recent_klines(symbol, limit=window)
        candles = kline_to_candles(klines)
        self._purge_western_records(symbol)
        results = self.engine.scan(candles)
        found = 0
        for r in results:
            candle_date = klines[r.candle_index].date
            existing = (
                self.db.query(PatternRecord)
                .filter(
                    PatternRecord.symbol == symbol,
                    PatternRecord.pattern_name == r.pattern_name,
                    PatternRecord.candle_date == candle_date,
                )
                .first()
            )
            if existing:
                continue
            prev_trend = None
            if is_uptrend(candles, r.candle_index):
                prev_trend = "up"
            elif is_downtrend(candles, r.candle_index):
                prev_trend = "down"
            else:
                prev_trend = "sideways"
            record = PatternRecord(
                symbol=symbol,
                pattern_name=r.pattern_name,
                direction=r.direction,
                score=Decimal(str(round(r.score, 2))),
                candle_date=candle_date,
                prev_trend=prev_trend,
                confirmation_status="confirmed" if r.score >= 60 else "pending",
            )
            self.db.add(record)
            found += 1
        self.db.commit()
        return found

    def _purge_western_records(self, symbol: str) -> None:
        """金叉/死叉已从形态引擎移除，清掉历史误当形态的记录和待确认信号。"""
        from app.models.signal import TradingSignal

        self.db.query(PatternRecord).filter(
            PatternRecord.symbol == symbol,
            PatternRecord.pattern_name.in_(WESTERN_NOT_CANDLES),
        ).delete(synchronize_session=False)
        self.db.query(TradingSignal).filter(
            TradingSignal.symbol == symbol,
            TradingSignal.pattern_name.in_(WESTERN_NOT_CANDLES),
            TradingSignal.status.in_(["pending", "dismissed"]),
        ).delete(synchronize_session=False)
