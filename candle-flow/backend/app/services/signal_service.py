from datetime import datetime
from decimal import Decimal
from typing import List, Optional, Tuple

from sqlalchemy.orm import Session

from app.core.nison_rules import (
    MIN_RISK_REWARD,
    NEEDS_NEXT_CONFIRM,
    NO_CHASE,
    WESTERN_NOT_CANDLES,
    is_extended_high,
    pattern_stop,
)
from app.core.confluence import evaluate_confluence
from app.core.price_targets import resolve_take_profits
from app.core.windows import window_still_open
from app.models.pattern import PatternRecord
from app.models.signal import TradingSignal
from app.schemas.signal import SignalOut
from app.services.kline_service import KlineService
from app.services.risk_service import RiskService

# 英文形态名 -> 中文（与前端 labels 一致，用于关联旧信号）
PATTERN_NAME_ZH = {
    "Hammer": "锤子线",
    "Hanging Man": "上吊线",
    "Shooting Star": "流星线",
    "Inverted Hammer": "倒锤子线",
    "Doji": "十字线",
    "Dragonfly Doji": "蜻蜓十字线",
    "Gravestone Doji": "墓碑十字线",
    "Bullish Engulfing": "看涨吞没",
    "Bearish Engulfing": "看跌吞没",
    "Morning Star": "启明星",
    "Evening Star": "黄昏星",
    "Three White Soldiers": "红三兵",
    "Three Black Crows": "三只乌鸦",
    "Piercing": "刺透",
    "Dark Cloud Cover": "乌云盖顶",
    "Bullish Harami": "看涨孕线",
    "Bearish Harami": "看跌孕线",
    "Bullish Harami Cross": "看涨十字孕线",
    "Bearish Harami Cross": "看跌十字孕线",
    "Bullish Belt Hold": "看涨捉腰带",
    "Bearish Belt Hold": "看跌捉腰带",
    "Bullish Counterattack": "看涨反击线",
    "Bearish Counterattack": "看跌反击线",
    "Tweezer Bottom": "平头底部",
    "Tweezer Top": "平头顶部",
    "Rising Window": "上升窗口",
    "Falling Window": "下降窗口",
    "Bullish Abandoned Baby": "看涨弃婴",
    "Bearish Abandoned Baby": "看跌弃婴",
    "Rising Three Methods": "上升三法",
    "Falling Three Methods": "下降三法",
    "Bullish Separating Lines": "看涨分手线",
    "Bearish Separating Lines": "看跌分手线",
    "Side by Side White": "跳空并列阳线",
    "Side by Side Black": "跳空并列阴线",
    "Two Crows": "两只乌鸦",
    "Tri-Star": "三星",
    "Tower Bottom": "塔形底部",
    "Tower Top": "塔形顶部",
    "Advance Block": "前进受阻",
    "Stalled": "停顿形态",
    "Rising Window Retest": "升窗回测",
    "Falling Window Retest": "降窗回测",
    "Bullish Kicker": "看涨脱离线",
    "Bearish Kicker": "看跌脱离线",
    "Unique Three River": "独特三川底部",
    "Concealing Baby Swallow": "藏婴吞没",
    "Upside Tasuki Gap": "向上跳空肩带",
    "Downside Tasuki Gap": "向下跳空肩带",
    "Bullish Breakaway": "看涨突破缺口",
    "Bearish Breakaway": "看跌突破缺口",
    "Downside Gap Side by Side White": "下跌跳空并列阳线",
    "Golden Cross": "黄金交叉",
    "Death Cross": "死亡交叉",
}


class SignalService:
    def __init__(self, db: Session):
        self.db = db
        self.risk_svc = RiskService()

    def get_signals(
        self,
        symbol: Optional[str] = None,
        status: Optional[str] = None,
        page: int = 1,
        page_size: int = 20,
        symbols: Optional[List[str]] = None,
    ) -> Tuple[List[TradingSignal], int]:
        q = self.db.query(TradingSignal)
        if symbols is not None:
            if not symbols:
                return [], 0
            q = q.filter(TradingSignal.symbol.in_(symbols))
        elif symbol:
            q = q.filter(TradingSignal.symbol == symbol)
        q = q.filter(~TradingSignal.pattern_name.in_(WESTERN_NOT_CANDLES))
        if status:
            q = q.filter(TradingSignal.status == status)
        total = q.count()
        items = (
            q.order_by(
                TradingSignal.pattern_date.desc().nulls_last(),
                TradingSignal.created_at.desc(),
            )
            .offset((page - 1) * page_size)
            .limit(page_size)
            .all()
        )
        return items, total

    def get_by_id(self, signal_id: int) -> Optional[TradingSignal]:
        return self.db.query(TradingSignal).filter(TradingSignal.id == signal_id).first()

    def confirm(self, signal_id: int, action: str) -> Optional[TradingSignal]:
        signal = self.get_by_id(signal_id)
        if not signal:
            return None
        if action == "confirm":
            signal.status = "active"
            signal.confirmed_at = datetime.utcnow()
        elif action == "dismiss":
            signal.status = "closed"
            signal.notes = "dismissed by user"
            signal.closed_at = datetime.utcnow()
        self.db.commit()
        self.db.refresh(signal)
        return signal

    def refresh_open_positions(self, symbol: Optional[str] = None) -> int:
        """Mark active signals closed when later bars hit stop (wick) or 2R (close)."""
        q = self.db.query(TradingSignal).filter(TradingSignal.status == "active")
        if symbol:
            q = q.filter(TradingSignal.symbol == symbol)
        closed = 0
        for signal in q.all():
            klines = self._recent_klines(signal.symbol)
            if not klines:
                continue
            start = 0
            if signal.pattern_date:
                start = next((i for i, k in enumerate(klines) if k.date > signal.pattern_date), None)
                if start is None:
                    continue
            else:
                start = max(0, len(klines) - 20)
            entry = float(signal.entry_price)
            stop = float(signal.stop_loss)
            tp1 = float(signal.take_profit_1) if signal.take_profit_1 is not None else None
            size = int(signal.position_size or 0)
            buy = signal.signal_type == "buy"
            for k in klines[start:]:
                close_px = None
                reason = None
                if buy:
                    if float(k.low) <= stop:
                        close_px, reason = stop, "止损"
                    elif tp1 is not None and float(k.close) >= tp1:
                        close_px, reason = tp1, "风控建议2R"
                else:
                    if float(k.high) >= stop:
                        close_px, reason = stop, "止损"
                    elif tp1 is not None and float(k.close) <= tp1:
                        close_px, reason = tp1, "风控建议2R"
                if close_px is None:
                    continue
                pnl = (close_px - entry) * size if buy else (entry - close_px) * size
                signal.status = "closed"
                signal.closed_at = datetime.utcnow()
                signal.close_price = Decimal(str(round(close_px, 4)))
                signal.pnl = Decimal(str(round(pnl, 2)))
                extra = f"平仓：{reason} {k.date}"
                signal.notes = f"{signal.notes} {extra}".strip() if signal.notes else extra
                closed += 1
                break
        if closed:
            self.db.commit()
        return closed

    def _pattern_name_variants(self, name: str) -> set[str]:
        variants = {name}
        zh = PATTERN_NAME_ZH.get(name)
        if zh:
            variants.add(zh)
        for en, cn in PATTERN_NAME_ZH.items():
            if cn == name:
                variants.add(en)
        return variants

    def _recent_klines(self, symbol: str):
        cache = getattr(self, "_kline_cache", None)
        if cache is None:
            self._kline_cache = {}
            cache = self._kline_cache
        if symbol not in cache:
            cache[symbol], _ = KlineService(self.db).get_recent_klines(symbol, limit=250)
        return cache[symbol]

    def _date_from_entry(self, signal: TradingSignal):
        entry = float(signal.entry_price)
        if entry <= 0:
            return None
        best_date = None
        best_diff = None
        for k in self._recent_klines(signal.symbol):
            close = float(k.close)
            diff = abs(close - entry)
            if entry and diff / entry <= 0.003 and (best_diff is None or diff < best_diff):
                best_diff = diff
                best_date = k.date
        return best_date

    def _find_pattern_for_signal(self, signal: TradingSignal) -> Optional[PatternRecord]:
        if signal.pattern_id:
            pattern = self.db.query(PatternRecord).filter(PatternRecord.id == signal.pattern_id).first()
            if pattern:
                return pattern
        names = self._pattern_name_variants(signal.pattern_name)
        q = self.db.query(PatternRecord).filter(
            PatternRecord.symbol == signal.symbol,
            PatternRecord.pattern_name.in_(list(names)),
        )
        if signal.pattern_date:
            hit = q.filter(PatternRecord.candle_date == signal.pattern_date).first()
            if hit:
                return hit
        entry_date = self._date_from_entry(signal)
        if entry_date:
            hit = q.filter(PatternRecord.candle_date == entry_date).first()
            if hit:
                return hit
        return None

    def to_signal_out(self, signal: TradingSignal, quote: Optional[dict] = None) -> SignalOut:
        if quote is None:
            quote = KlineService(self.db).get_quote(signal.symbol)
        base = SignalOut.model_validate(signal)
        pattern = self._find_pattern_for_signal(signal)
        inferred_date = (
            signal.pattern_date
            or (pattern.candle_date if pattern else None)
            or self._date_from_entry(signal)
        )
        updates: dict = {}
        dirty = False
        if inferred_date:
            updates["pattern_date"] = inferred_date
            if signal.pattern_date != inferred_date:
                signal.pattern_date = inferred_date
                dirty = True
        if pattern:
            updates["pattern_id"] = signal.pattern_id or pattern.id
            updates["pattern_direction"] = pattern.direction
            if not signal.pattern_id:
                signal.pattern_id = pattern.id
                dirty = True
        if dirty:
            self.db.add(signal)
            self.db.commit()
        if quote:
            updates.update(
                {
                    "last_price": quote["last_price"],
                    "prev_close": quote["prev_close"],
                    "change_amount": quote["change_amount"],
                    "change_pct": quote["change_pct"],
                    "quote_date": quote["quote_date"],
                }
            )
        return base.model_copy(update=updates) if updates else base

    def _level_from_score(self, score: float) -> str:
        if score >= 80:
            return "strong"
        if score >= 60:
            return "medium"
        return "weak"

    def clear_pending(self, symbol: str) -> None:
        self.db.query(TradingSignal).filter(
            TradingSignal.symbol == symbol,
            TradingSignal.status == "pending",
        ).delete()
        self.db.commit()

    def _create_signal_for_pattern(
        self,
        pattern: PatternRecord,
        entry: float,
        stop: float,
        capital: float,
        risk_pct: float,
        confluence_count: int = 0,
        confluence_hits: str = "",
        confluence_detail: str = "",
        klines: Optional[list] = None,
        kline_index: Optional[int] = None,
    ) -> bool:
        signal_type = "buy" if pattern.direction == "bullish" else "sell"
        if signal_type == "buy" and stop >= entry:
            return False
        if signal_type == "sell" and stop <= entry:
            return False
        risk_distance = abs(entry - stop)
        if risk_distance <= 0:
            return False

        if klines is not None and kline_index is not None:
            tp1, tp2, notes = resolve_take_profits(
                klines, kline_index, pattern.direction, pattern.pattern_name, entry, stop
            )
        elif signal_type == "buy":
            tp1, tp2 = entry + risk_distance * 2, entry + risk_distance * 3
            notes = "蜡烛图不提供目标价；止盈按风险回报 2R/3R。"
        else:
            tp1, tp2 = entry - risk_distance * 2, entry - risk_distance * 3
            notes = "蜡烛图不提供目标价；止盈按风险回报 2R/3R。"

        reward = abs(tp1 - entry)
        if reward / risk_distance < MIN_RISK_REWARD:
            return False

        risk_result = self.risk_svc.calculate(
            entry_price=Decimal(str(entry)),
            stop_loss=Decimal(str(round(stop, 4))),
            capital=Decimal(str(capital)),
            risk_per_trade=Decimal(str(risk_pct)),
            take_profit=Decimal(str(round(tp1, 4))),
        )

        combined = float(pattern.score) + confluence_count * 6
        signal = TradingSignal(
            symbol=pattern.symbol,
            signal_type=signal_type,
            signal_level=self._level_from_score(combined),
            pattern_name=pattern.pattern_name,
            pattern_id=pattern.id,
            pattern_date=pattern.candle_date,
            confluence_count=confluence_count,
            confluence_hits=confluence_hits or None,
            confluence_detail=confluence_detail or None,
            entry_price=Decimal(str(round(entry, 4))),
            stop_loss=Decimal(str(round(stop, 4))),
            take_profit_1=Decimal(str(round(tp1, 4))),
            take_profit_2=Decimal(str(round(tp2, 4))),
            risk_reward_ratio=risk_result.risk_reward_ratio,
            position_size=risk_result.position_size,
            capital_at_risk=risk_result.capital_at_risk,
            status="pending",
            notes=notes,
        )
        self.db.add(signal)
        return True

    def generate_for_patterns(
        self,
        patterns: List[PatternRecord],
        klines_by_date: dict,
        capital: float = 100000,
        risk_pct: float = 1.0,
    ) -> int:
        if not patterns:
            return 0
        ordered = sorted(klines_by_date.values(), key=lambda k: k.date)
        date_to_idx = {k.date: i for i, k in enumerate(ordered)}
        created = 0
        for p in patterns:
            if p.pattern_name in WESTERN_NOT_CANDLES:
                continue
            if p.direction == "neutral" or float(p.score) < 60:
                continue
            idx = date_to_idx.get(p.candle_date)
            if idx is None:
                continue
            if p.pattern_name in NO_CHASE and is_extended_high(ordered, idx):
                continue
            if p.pattern_name in NEEDS_NEXT_CONFIRM and idx >= len(ordered) - 1:
                continue
            if p.pattern_name == "上升窗口" and not window_still_open(ordered, idx, "rising"):
                continue
            if p.pattern_name == "下降窗口" and not window_still_open(ordered, idx, "falling"):
                continue
            confluence = evaluate_confluence(ordered, idx, p.direction)
            same_day = [
                x
                for x in patterns
                if x.candle_date == p.candle_date
                and x.direction == p.direction
                and x.pattern_name != p.pattern_name
                and float(x.score) >= 50
            ]
            if same_day:
                names = "、".join(x.pattern_name for x in same_day[:3])
                confluence.add("形态互证", f"同日还有 {names}")
            if not confluence.ok:
                continue
            existing = (
                self.db.query(TradingSignal)
                .filter(
                    TradingSignal.symbol == p.symbol,
                    TradingSignal.pattern_name == p.pattern_name,
                    TradingSignal.pattern_date == p.candle_date,
                    TradingSignal.status.in_(["pending", "confirmed", "active"]),
                )
                .first()
            )
            if existing:
                continue
            existing_name = (
                self.db.query(TradingSignal)
                .filter(
                    TradingSignal.symbol == p.symbol,
                    TradingSignal.pattern_name == p.pattern_name,
                    TradingSignal.status.in_(["pending", "confirmed", "active"]),
                )
                .first()
            )
            if existing_name:
                continue
            entry = float(ordered[idx].close)
            stop = pattern_stop(ordered, idx, p.direction, p.pattern_name)
            if stop is None:
                continue
            if self._create_signal_for_pattern(
                p,
                entry,
                stop,
                capital,
                risk_pct,
                confluence.count,
                confluence.label,
                confluence.details_json,
                ordered,
                idx,
            ):
                created += 1
        if created:
            self.db.commit()
        return created

    def _patterns_in_window(self, symbol: str, dates: set) -> List[PatternRecord]:
        rows = (
            self.db.query(PatternRecord)
            .filter(
                PatternRecord.symbol == symbol,
                PatternRecord.score >= 60,
                PatternRecord.candle_date.in_(dates),
            )
            .order_by(PatternRecord.candle_date.desc(), PatternRecord.score.desc())
            .all()
        )
        # 每种形态只保留最近一次，避免横盘时平头顶部天天出信号
        best_by_name: dict[str, PatternRecord] = {}
        for p in rows:
            if p.pattern_name in WESTERN_NOT_CANDLES:
                continue
            if p.pattern_name not in best_by_name:
                best_by_name[p.pattern_name] = p
        return list(best_by_name.values())

    SIGNAL_LOOKBACK_BARS = 20

    def generate_from_patterns(self, symbol: str, capital: float = 100000, risk_pct: float = 1.0):
        """为最近形态生成信号（近 20 个交易日）"""
        kline_svc = KlineService(self.db)
        klines, _ = kline_svc.get_recent_klines(symbol, limit=120)
        if not klines:
            return
        klines_by_date = {k.date: k for k in klines}
        recent_dates = {k.date for k in klines[-self.SIGNAL_LOOKBACK_BARS :]}
        patterns = self._patterns_in_window(symbol, recent_dates)
        self.generate_for_patterns(patterns, klines_by_date, capital, risk_pct)
        self.refresh_open_positions(symbol)

    def regenerate(self, symbol: str, capital: float = 100000, risk_pct: float = 1.0) -> int:
        """清除 pending 信号并按最新形态重新生成"""
        self.clear_pending(symbol)
        kline_svc = KlineService(self.db)
        klines, _ = kline_svc.get_recent_klines(symbol, limit=120)
        if not klines:
            return 0
        klines_by_date = {k.date: k for k in klines}
        recent_dates = {k.date for k in klines[-self.SIGNAL_LOOKBACK_BARS :]}
        patterns = self._patterns_in_window(symbol, recent_dates)
        return self.generate_for_patterns(patterns, klines_by_date, capital, risk_pct)
