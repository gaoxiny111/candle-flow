import logging
import time
from datetime import date, datetime, timedelta
from typing import Optional
from zoneinfo import ZoneInfo

import pandas as pd

from app.core.exceptions import DataSourceError
from app.utils.symbol import (
    SymbolError,
    futures_sina_code,
    is_etf_symbol,
    is_future,
    is_index_symbol,
    normalize_symbol,
    parse_symbol,
)

logger = logging.getLogger(__name__)

MAX_RETRIES = 3
RETRY_DELAY_SEC = 1.5
CN_TZ = ZoneInfo("Asia/Shanghai")
_SPOT_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Referer": "https://finance.eastmoney.com/",
}


def trading_today(now: datetime | None = None) -> date:
    """A-share calendar date in Asia/Shanghai (not the server local TZ)."""
    return (now or datetime.now(CN_TZ)).astimezone(CN_TZ).date()


def is_cn_weekday(d: date | None = None) -> bool:
    day = d or trading_today()
    return day.weekday() < 5


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
        if code.startswith("920") or code.startswith(("4", "8")):
            prefix = "bj"
        elif code.startswith(("5", "6", "9")):
            prefix = "sh"
        else:
            prefix = "sz"
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

    def _fetch_etf(self, code: str, market: str, start: str, end: str) -> pd.DataFrame:
        import akshare as ak

        sina_sym = f"{market}{code}"
        try:
            df = ak.fund_etf_hist_em(
                symbol=code,
                period="daily",
                start_date=start,
                end_date=end,
                adjust="qfq",
            )
            if df is not None and not df.empty:
                return df
        except Exception as e:
            logger.warning("eastmoney etf hist failed for %s: %s; fallback to sina", code, e)

        df = ak.fund_etf_hist_sina(symbol=sina_sym)
        if df is None or df.empty:
            raise ValueError(f"ETF 行情为空: {sina_sym}")
        out = df.copy()
        out["date"] = pd.to_datetime(out["date"]).dt.date
        start_d = date(int(start[:4]), int(start[4:6]), int(start[6:8]))
        end_d = date(int(end[:4]), int(end[4:6]), int(end[6:8]))
        out = out[(out["date"] >= start_d) & (out["date"] <= end_d)]
        return out[["date", "open", "high", "low", "close", "volume"]]

    def _fetch_index(self, code: str, market: str, start: str, end: str) -> pd.DataFrame:
        import akshare as ak

        sina_sym = f"{market}{code}"
        df = ak.stock_zh_index_daily(symbol=sina_sym)
        if df is None or df.empty:
            raise ValueError(f"指数行情为空: {sina_sym}")
        # sina returns full history with english columns
        out = df.copy()
        out["date"] = pd.to_datetime(out["date"]).dt.date
        start_d = date(int(start[:4]), int(start[4:6]), int(start[6:8]))
        end_d = date(int(end[:4]), int(end[4:6]), int(end[6:8]))
        out = out[(out["date"] >= start_d) & (out["date"] <= end_d)]
        return out

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

    def _spot_from_eastmoney(self, code: str, market: str) -> Optional[dict]:
        import requests

        secid = f"{1 if market == 'sh' else 0}.{code}"
        r = requests.get(
            "https://push2.eastmoney.com/api/qt/stock/get",
            params={
                "fltt": "2",
                "invt": "2",
                "secid": secid,
                "fields": "f43,f44,f45,f46,f47,f60",
            },
            headers=_SPOT_HEADERS,
            timeout=8,
        )
        data = (r.json() or {}).get("data") or {}

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
        prev = _num("f60")
        if close is None or close <= 0:
            return None
        if open_ is None or open_ <= 0:
            open_ = prev if prev and prev > 0 else close
        if high is None or high <= 0:
            high = max(open_, close)
        if low is None or low <= 0:
            low = min(open_, close)
        return {
            "date": trading_today(),
            "open": open_,
            "high": high,
            "low": low,
            "close": close,
            "volume": int(volume or 0),
            "source": "eastmoney",
        }

    def _spot_from_tencent(self, code: str, market: str) -> Optional[dict]:
        import requests

        r = requests.get(
            f"https://qt.gtimg.cn/q={market}{code}",
            headers={**_SPOT_HEADERS, "Referer": "https://finance.qq.com/"},
            timeout=8,
        )
        text = r.content.decode("gbk", errors="replace")
        if "=" not in text:
            return None
        fields = text.split("=", 1)[1].strip().strip(";").strip('"').split("~")
        if len(fields) < 37:
            return None

        def _num(idx: int) -> Optional[float]:
            if idx >= len(fields):
                return None
            v = fields[idx]
            if v in (None, "", "-"):
                return None
            try:
                return float(v)
            except (TypeError, ValueError):
                return None

        close = _num(3)
        prev = _num(4)
        open_ = _num(5)
        high = _num(33)
        low = _num(34)
        volume = _num(36)
        if close is None or close <= 0:
            return None
        if open_ is None or open_ <= 0:
            open_ = prev if prev and prev > 0 else close
        if high is None or high <= 0:
            high = max(open_, close)
        if low is None or low <= 0:
            low = min(open_, close)
        trade_date = trading_today()
        raw_dt = (fields[30] or "")[:8]
        if len(raw_dt) == 8 and raw_dt.isdigit():
            try:
                trade_date = date(int(raw_dt[:4]), int(raw_dt[4:6]), int(raw_dt[6:8]))
            except ValueError:
                pass
        return {
            "date": trade_date,
            "open": open_,
            "high": high,
            "low": low,
            "close": close,
            "volume": int(volume or 0),
            "source": "tencent",
        }

    def _spot_from_sina(self, code: str, market: str) -> Optional[dict]:
        import requests

        r = requests.get(
            f"https://hq.sinajs.cn/list={market}{code}",
            headers={**_SPOT_HEADERS, "Referer": "https://finance.sina.com.cn/"},
            timeout=8,
        )
        text = r.content.decode("gbk", errors="replace")
        if "=" not in text:
            return None
        payload = text.split("=", 1)[1].strip().strip(";").strip('"')
        fields = payload.split(",")
        if len(fields) < 10:
            return None

        def _num(idx: int) -> Optional[float]:
            if idx >= len(fields):
                return None
            v = fields[idx]
            if v in (None, "", "-"):
                return None
            try:
                return float(v)
            except (TypeError, ValueError):
                return None

        open_ = _num(1)
        prev = _num(2)
        close = _num(3)
        high = _num(4)
        low = _num(5)
        volume = _num(8)
        if close is None or close <= 0:
            return None
        if open_ is None or open_ <= 0:
            open_ = prev if prev and prev > 0 else close
        if high is None or high <= 0:
            high = max(open_, close)
        if low is None or low <= 0:
            low = min(open_, close)
        trade_date = trading_today()
        raw = (fields[30] if len(fields) > 30 else "") or ""
        if len(raw) >= 10:
            try:
                trade_date = date.fromisoformat(raw[:10])
            except ValueError:
                pass
        return {
            "date": trade_date,
            "open": open_,
            "high": high,
            "low": low,
            "close": close,
            "volume": int(volume or 0),
            "source": "sina",
        }

    def fetch_spot(self, symbol: str) -> Optional[dict]:
        """Realtime quote used to backfill today's daily bar when hist lags."""
        try:
            symbol = normalize_symbol(symbol)
        except SymbolError:
            return None
        if is_future(symbol):
            return None
        code, market = parse_symbol(symbol)
        errors: list[str] = []
        for name, fn in (
            ("eastmoney", self._spot_from_eastmoney),
            ("tencent", self._spot_from_tencent),
            ("sina", self._spot_from_sina),
        ):
            try:
                spot = fn(code, market)
            except Exception as e:
                errors.append(f"{name}:{e}")
                continue
            if spot:
                return spot
            errors.append(f"{name}:empty")
        if errors:
            logger.warning("spot fetch failed for %s: %s", symbol, "; ".join(errors))
        return None

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
        today = trading_today()
        start = start_date or (today - timedelta(days=365)).strftime("%Y%m%d")
        end = end_date or today.strftime("%Y%m%d")

        last_error: Exception | None = None
        for attempt in range(1, MAX_RETRIES + 1):
            try:
                if is_future(symbol):
                    df = self._fetch_future(futures_sina_code(symbol))
                elif is_index_symbol(symbol):
                    df = self._fetch_index(code, market, start, end)
                elif is_etf_symbol(symbol):
                    df = self._fetch_etf(code, market, start, end)
                elif _is_b_share(code):
                    df = self._fetch_b_share(code, market, start, end)
                else:
                    df = self._fetch_a_share(code, start, end)

                if df is not None and not df.empty:
                    # index path already english; a/b share may be Chinese headers
                    if "日期" in df.columns or "开盘" in df.columns:
                        return _normalize_df(df)
                    df = df.copy()
                    df["date"] = pd.to_datetime(df["date"]).dt.date
                    return df[["date", "open", "high", "low", "close", "volume"]]
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
