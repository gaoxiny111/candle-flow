import logging
import time
from datetime import date, timedelta
from typing import Optional

import pandas as pd

from app.core.exceptions import DataSourceError
from app.utils.symbol import (
    SymbolError,
    futures_sina_code,
    is_future,
    normalize_symbol,
    parse_symbol,
)

logger = logging.getLogger(__name__)

MAX_RETRIES = 3
RETRY_DELAY_SEC = 1.5


def _normalize_df(df: pd.DataFrame) -> pd.DataFrame:
    if "日期" in df.columns:
        df = df.rename(
            columns={
                "日期": "date",
                "开盘": "open",
                "收盘": "close",
                "最高": "high",
                "最低": "low",
                "成交量": "volume",
            }
        )
    df["date"] = pd.to_datetime(df["date"]).dt.date
    return df[["date", "open", "high", "low", "close", "volume"]]


def _is_b_share(code: str) -> bool:
    return code.startswith("900") or code.startswith("200")


class AKShareClient:
    def __init__(self):
        self._available: Optional[bool] = None

    def is_available(self) -> bool:
        if self._available is not None:
            return self._available
        try:
            import akshare  # noqa: F401

            self._available = True
        except ImportError:
            self._available = False
        return self._available

    def _fetch_a_share(self, code: str, start: str, end: str) -> pd.DataFrame:
        import akshare as ak

        try:
            df = ak.stock_zh_a_hist(
                symbol=code,
                period="daily",
                start_date=start,
                end_date=end,
                adjust="qfq",
            )
            if df is not None and not df.empty:
                return df
        except Exception as e:
            logger.warning("eastmoney hist failed for %s: %s; fallback to sina", code, e)

        # 东财常被掐连接，回退新浪日线
        prefix = "sh" if code.startswith(("5", "6", "9")) else "sz"
        return ak.stock_zh_a_daily(
            symbol=f"{prefix}{code}",
            start_date=start,
            end_date=end,
            adjust="qfq",
        )

    def _fetch_b_share(self, code: str, market: str, start: str, end: str) -> pd.DataFrame:
        import akshare as ak

        prefix = "sh" if market == "sh" else "sz"
        return ak.stock_zh_b_daily(
            symbol=f"{prefix}{code}",
            start_date=start,
            end_date=end,
        )

    def _fetch_future(self, sina_code: str) -> pd.DataFrame:
        import akshare as ak

        for code in (sina_code, sina_code.lower(), sina_code.capitalize()):
            try:
                df = ak.futures_zh_daily_sina(symbol=code)
                if df is not None and not df.empty:
                    return df
            except Exception as e:
                logger.warning("futures daily failed for %s: %s", code, e)
        raise ValueError(f"期货行情为空: {sina_code}")

    def fetch_spot(self, symbol: str) -> Optional[dict]:
        """东财实时行情，补日线接口当天尚未入库的 K 线。"""
        import requests

        try:
            symbol = normalize_symbol(symbol)
        except SymbolError:
            return None
        if is_future(symbol):
            return None
        code, market = parse_symbol(symbol)
        secid = f"{1 if market == 'sh' else 0}.{code}"
        try:
            r = requests.get(
                "https://push2.eastmoney.com/api/qt/stock/get",
                params={
                    "fltt": "2",
                    "invt": "2",
                    "secid": secid,
                    "fields": "f43,f44,f45,f46,f47,f60",
                },
                timeout=8,
            )
            data = (r.json() or {}).get("data") or {}
        except Exception as e:
            logger.warning("spot fetch failed for %s: %s", symbol, e)
            return None

        def _num(key: str) -> Optional[float]:
            v = data.get(key)
            if v in (None, "", "-"):
                return None
            try:
                return float(v)
            except (TypeError, ValueError):
                return None

        close = _num("f43")
        high = _num("f44")
        low = _num("f45")
        open_ = _num("f46")
        volume = _num("f47")
        if close is None or close <= 0 or open_ is None or open_ <= 0:
            return None
        if high is None:
            high = max(open_, close)
        if low is None:
            low = min(open_, close)
        return {
            "date": date.today(),
            "open": open_,
            "high": high,
            "low": low,
            "close": close,
            "volume": int(volume or 0),
        }

    def fetch_daily(
        self,
        symbol: str,
        start_date: Optional[str] = None,
        end_date: Optional[str] = None,
    ) -> pd.DataFrame:
        if not self.is_available():
            raise DataSourceError("未安装 AKShare，无法获取真实行情")

        try:
            symbol = normalize_symbol(symbol)
        except SymbolError as e:
            raise DataSourceError(str(e)) from e

        code, market = parse_symbol(symbol)
        start = start_date or (date.today() - timedelta(days=365)).strftime("%Y%m%d")
        end = end_date or date.today().strftime("%Y%m%d")

        last_error: Exception | None = None
        for attempt in range(1, MAX_RETRIES + 1):
            try:
                if is_future(symbol):
                    df = self._fetch_future(futures_sina_code(symbol))
                elif _is_b_share(code):
                    df = self._fetch_b_share(code, market, start, end)
                else:
                    df = self._fetch_a_share(code, start, end)

                if df is not None and not df.empty:
                    return _normalize_df(df)
                last_error = ValueError("数据源返回空结果")
            except Exception as e:
                last_error = e
                logger.warning(
                    "AKShare fetch attempt %s/%s failed for %s: %s",
                    attempt,
                    MAX_RETRIES,
                    symbol,
                    e,
                )
            if attempt < MAX_RETRIES:
                time.sleep(RETRY_DELAY_SEC)

        msg = f"获取 {symbol} 真实行情失败"
        if last_error:
            msg = f"{msg}: {last_error}"
        raise DataSourceError(msg)


akshare_client = AKShareClient()
