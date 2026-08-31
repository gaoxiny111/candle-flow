"""Scan main-board bull tactics for symbols."""

from __future__ import annotations

import logging
import time
from datetime import date, datetime, timedelta

from sqlalchemy.orm import Session

from app.core.bull_tactics import (
    HEIMA,
    N_FAN,
    NIU_SAN,
    TACTIC_NAMES,
    is_main_board,
    is_st_name,
    scan_all_tactics,
)
from app.core.pattern_engine import kline_to_candles
from app.models.stock import StockInfo
from app.services.akshare_client import akshare_client
from app.services.kline_service import KlineService
from app.services.stock_universe import ensure_seeded, lookup_name, refresh_universe
from app.utils.symbol import normalize_symbol, SymbolError

logger = logging.getLogger(__name__)

MARKET_SCAN_START = (date.today() - timedelta(days=400)).strftime("%Y%m%d")
MARKET_SCAN_SLEEP_SEC = 0.08


TACTIC_RULES = {
    HEIMA: "连续三天涨停（第三天允许炸板）；十三日内缩量回踩，不破第三日收盘与十日线；七日线附近为买点。",
    N_FAN: "放量涨停且非一字板；八日内缩量回踩，不破涨停阳线开盘价。",
    NIU_SAN: "年内倍量跳空高开大阳线或涨停；缩量回踩且不补缺口。",
}


class BullTacticsService:
    def __init__(self, db: Session):
        self.db = db

    def _eligible(self, symbol: str) -> tuple[bool, str, str]:
        try:
            symbol = normalize_symbol(symbol)
        except SymbolError:
            return False, symbol, ""
        if not is_main_board(symbol):
            return False, symbol, ""
        name = lookup_name(self.db, symbol) or ""
        if is_st_name(name):
            return False, symbol, name
        return True, symbol, name

    def _main_board_stocks(self) -> list[StockInfo]:
        ensure_seeded(self.db)
        rows = self.db.query(StockInfo).filter(StockInfo.market.in_(("SH", "SZ"))).all()
        out: list[StockInfo] = []
        for row in rows:
            if not is_main_board(row.symbol):
                continue
            if is_st_name(row.name):
                continue
            out.append(row)
        out.sort(key=lambda r: r.symbol)
        return out

    def _scan_candles(self, symbol: str, name: str, candles, recent_bars: int) -> dict:
        hits = scan_all_tactics(candles, recent_bars=recent_bars)
        return {
            "symbol": symbol,
            "name": name,
            "hits": [_hit_out(h) for h in hits],
        }

    def scan_symbol(self, symbol: str, recent_bars: int = 30) -> dict | None:
        ok, symbol, name = self._eligible(symbol)
        if not ok:
            return None
        klines, _ = KlineService(self.db).get_recent_klines(symbol, limit=280)
        if len(klines) < 32:
            return None
        candles = kline_to_candles(klines)
        return self._scan_candles(symbol, name, candles, recent_bars)

    def scan_symbols(self, symbols: list[str], recent_bars: int = 30) -> dict:
        rows: list[dict] = []
        skipped: list[str] = []
        for raw in symbols:
            sym = (raw or "").strip()
            if not sym:
                continue
            row = self.scan_symbol(sym, recent_bars=recent_bars)
            if row and row["hits"]:
                rows.append(row)
            elif row is None:
                skipped.append(sym)
        rows.sort(key=lambda r: max((h["score"] for h in r["hits"]), default=0), reverse=True)
        return {"items": rows, "skipped": skipped, "count": len(rows)}

    def scan_market(self, recent_bars: int = 30, refresh_list: bool = True) -> dict:
        """Scan all main-board non-ST stocks; fetches klines on the fly (may take several minutes)."""
        ensure_seeded(self.db)
        if refresh_list:
            try:
                refresh_universe(self.db, force=False)
            except Exception as exc:
                logger.warning("universe refresh skipped: %s", exc)

        stocks = self._main_board_stocks()
        items: list[dict] = []
        scanned = 0
        skipped = 0
        errors = 0

        if not akshare_client.is_available():
            return {
                "items": [],
                "scanned": 0,
                "universe_size": len(stocks),
                "skipped": 0,
                "errors": 0,
                "count": 0,
                "error": "未安装 AKShare，无法拉取全市场行情",
            }

        end = date.today().strftime("%Y%m%d")
        for row in stocks:
            scanned += 1
            try:
                df = akshare_client.fetch_daily(
                    row.symbol,
                    start_date=MARKET_SCAN_START,
                    end_date=end,
                )
                if df is None or df.empty or len(df) < 32:
                    skipped += 1
                    continue
                candles = _df_to_candles(df)
                result = self._scan_candles(row.symbol, row.name, candles, recent_bars)
                if result["hits"]:
                    items.append(result)
            except Exception as exc:
                errors += 1
                logger.debug("market scan failed for %s: %s", row.symbol, exc)
            if scanned % 40 == 0:
                time.sleep(MARKET_SCAN_SLEEP_SEC)

        items.sort(key=lambda r: max((h["score"] for h in r["hits"]), default=0), reverse=True)
        return {
            "items": items,
            "scanned": scanned,
            "universe_size": len(stocks),
            "skipped": skipped,
            "errors": errors,
            "count": len(items),
        }


def _df_to_candles(df) -> list:
    from app.core.candle import Candle

    candles: list[Candle] = []
    for _, row in df.iterrows():
        ts = row["date"]
        if not isinstance(ts, datetime):
            ts = datetime.combine(ts, datetime.min.time())
        candles.append(
            Candle(
                open=float(row["open"]),
                high=float(row["high"]),
                low=float(row["low"]),
                close=float(row["close"]),
                volume=float(row["volume"] or 0),
                timestamp=ts,
            )
        )
    return candles


def _hit_out(hit) -> dict:
    return {
        "tactic": hit.tactic,
        "buy_date": hit.buy_date,
        "buy_price": hit.buy_price,
        "setup_date": hit.setup_date,
        "score": hit.score,
        "details": hit.details,
    }
