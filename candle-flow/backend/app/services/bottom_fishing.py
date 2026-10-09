"""抄底信号：缩量企稳 / 放量大阳线。

口径对齐用户脚本（AkShare 日线）：
- 缩量企稳：近 3 日至少 2 日成交量 < 20 日均量 × 0.7，且收盘不创新低
- 放量大阳线：涨幅 > 3%，成交量较前一日放大 ≥ 1.5 倍

注意：原稿 ``sum >= 2 & 不创新低`` 因运算符优先级会算错；这里用括号修正。
「不创新低」定为：收盘 ≥ 此前 3 日收盘最低（不含当日），避免 rolling(3).min() 恒真。
"""

from __future__ import annotations

import logging
from datetime import date
from typing import Any

import numpy as np
import pandas as pd
from sqlalchemy.orm import Session

from app.models.kline import KlineData
from app.services.stock_universe import lookup_name
from app.utils.symbol import SymbolError, normalize_symbol

logger = logging.getLogger(__name__)

MIN_BARS = 25
VOL_SHRINK_RATIO = 0.7
VOL_SURGE_RATIO = 1.5
BIG_YANG_PCT = 3.0
DEFAULT_LOOKBACK_DAYS = 5
MAX_SYMBOLS = 40


def compute_signals(df: pd.DataFrame) -> pd.DataFrame:
    """在已按日期升序的日线 DataFrame 上计算信号列。

    需要列：date, close, volume；可选 open/high/low。
    """
    out = df.copy()
    if out.empty:
        out["pct_chg"] = []
        out["volume_shrink"] = []
        out["no_new_low"] = []
        out["shrink_stabilize"] = []
        out["big_yang"] = []
        out["volume_surge"] = []
        out["yang_surge"] = []
        out["bottom_signal"] = []
        return out

    close = pd.to_numeric(out["close"], errors="coerce").astype(float)
    volume = pd.to_numeric(out["volume"], errors="coerce").astype(float)
    prev_close = close.shift(1)
    pct = np.where(prev_close > 0, (close / prev_close - 1.0) * 100.0, np.nan)
    out["pct_chg"] = pct

    ma20_vol = volume.rolling(20, min_periods=20).mean()
    shrink = volume < ma20_vol * VOL_SHRINK_RATIO
    # 收盘不低于此前 3 日收盘最低 → 企稳、不创新低
    prior_low = close.shift(1).rolling(3, min_periods=3).min()
    no_new_low = close >= prior_low
    shrink_ok = shrink.rolling(3, min_periods=3).sum() >= 2
    shrink_stabilize = shrink_ok & no_new_low.fillna(False)

    big_yang = out["pct_chg"] > BIG_YANG_PCT
    volume_surge = volume > volume.shift(1) * VOL_SURGE_RATIO
    yang_surge = big_yang.fillna(False) & volume_surge.fillna(False)

    out["volume_shrink"] = shrink.fillna(False)
    out["no_new_low"] = no_new_low.fillna(False)
    out["shrink_stabilize"] = shrink_stabilize.fillna(False)
    out["big_yang"] = big_yang.fillna(False)
    out["volume_surge"] = volume_surge.fillna(False)
    out["yang_surge"] = yang_surge
    out["bottom_signal"] = out["shrink_stabilize"] | out["yang_surge"]
    return out


def _bars_to_frame(rows: list[KlineData]) -> pd.DataFrame:
    if not rows:
        return pd.DataFrame(columns=["date", "close", "volume"])
    return pd.DataFrame(
        {
            "date": [r.date for r in rows],
            "close": [float(r.close) for r in rows],
            "volume": [float(r.volume) for r in rows],
        }
    ).sort_values("date").reset_index(drop=True)


def _signal_labels(shrink: bool, yang: bool) -> list[str]:
    labels: list[str] = []
    if shrink:
        labels.append("缩量企稳")
    if yang:
        labels.append("放量大阳线")
    return labels


def scan_symbol(
    db: Session,
    symbol: str,
    *,
    lookback_days: int = DEFAULT_LOOKBACK_DAYS,
    ensure_fresh: bool = True,
) -> dict[str, Any] | None:
    """扫描单票；无足够 K 线或无近期信号时返回 None（无命中）或带 status 的摘要。"""
    try:
        symbol = normalize_symbol(symbol)
    except SymbolError:
        return {
            "symbol": symbol,
            "name": "",
            "ok": False,
            "message": "代码无效",
            "hits": [],
            "latest": None,
        }

    if ensure_fresh:
        try:
            from app.services.kline_service import KlineService

            svc = KlineService(db)
            rows, total = svc.get_recent_klines(symbol, limit=80)
            if total < MIN_BARS or svc.latest_is_stale(symbol):
                try:
                    svc.sync(symbol)
                except Exception:
                    logger.warning("bottom-fishing sync failed for %s", symbol, exc_info=True)
            # 收盘后尽量并入今日 bar；盘中不污染批量量能口径
            try:
                svc.merge_today_spot(symbol)
            except Exception:
                logger.debug("merge_today_spot skipped for %s", symbol, exc_info=True)
        except Exception:
            logger.warning("bottom-fishing ensure failed for %s", symbol, exc_info=True)

    rows = (
        db.query(KlineData)
        .filter(KlineData.symbol == symbol)
        .order_by(KlineData.date.desc())
        .limit(120)
        .all()
    )
    rows = list(reversed(rows))
    name = lookup_name(db, symbol) or ""
    if len(rows) < MIN_BARS:
        return {
            "symbol": symbol,
            "name": name,
            "ok": False,
            "message": "K线不足",
            "hits": [],
            "latest": None,
        }

    frame = compute_signals(_bars_to_frame(rows))
    lookback = max(1, min(int(lookback_days), 20))
    recent = frame.tail(lookback)
    hits_df = recent[recent["bottom_signal"]]
    hits: list[dict[str, Any]] = []
    for _, row in hits_df.iterrows():
        d = row["date"]
        if isinstance(d, date):
            d_str = d.isoformat()
        else:
            d_str = str(d)[:10]
        shrink = bool(row["shrink_stabilize"])
        yang = bool(row["yang_surge"])
        hits.append(
            {
                "date": d_str,
                "close": round(float(row["close"]), 4),
                "pct_chg": round(float(row["pct_chg"]), 2) if pd.notna(row["pct_chg"]) else None,
                "shrink_stabilize": shrink,
                "yang_surge": yang,
                "labels": _signal_labels(shrink, yang),
            }
        )

    last = frame.iloc[-1]
    last_date = last["date"]
    last_date_str = last_date.isoformat() if isinstance(last_date, date) else str(last_date)[:10]
    latest = {
        "date": last_date_str,
        "close": round(float(last["close"]), 4),
        "pct_chg": round(float(last["pct_chg"]), 2) if pd.notna(last["pct_chg"]) else None,
        "shrink_stabilize": bool(last["shrink_stabilize"]),
        "yang_surge": bool(last["yang_surge"]),
        "bottom_signal": bool(last["bottom_signal"]),
        "labels": _signal_labels(bool(last["shrink_stabilize"]), bool(last["yang_surge"])),
    }

    return {
        "symbol": symbol,
        "name": name,
        "ok": True,
        "message": "",
        "hits": hits,
        "latest": latest,
        "has_signal": bool(hits) or bool(latest["bottom_signal"]),
    }


def scan_symbols(
    db: Session,
    symbols: list[str],
    *,
    lookback_days: int = DEFAULT_LOOKBACK_DAYS,
    only_hits: bool = True,
) -> dict[str, Any]:
    cleaned: list[str] = []
    seen: set[str] = set()
    for raw in symbols:
        item = (raw or "").strip()
        if not item:
            continue
        try:
            sym = normalize_symbol(item)
        except SymbolError:
            continue
        if sym in seen:
            continue
        seen.add(sym)
        cleaned.append(sym)
        if len(cleaned) >= MAX_SYMBOLS:
            break

    items: list[dict[str, Any]] = []
    for sym in cleaned:
        row = scan_symbol(db, sym, lookback_days=lookback_days, ensure_fresh=True)
        if not row:
            continue
        if only_hits and not row.get("has_signal"):
            continue
        items.append(row)

    items.sort(
        key=lambda x: (
            0 if x.get("latest", {}).get("bottom_signal") else 1,
            -(len(x.get("hits") or [])),
            x.get("symbol") or "",
        )
    )
    return {
        "lookback_days": lookback_days,
        "scanned": len(cleaned),
        "hit_count": sum(1 for x in items if x.get("has_signal")),
        "items": items,
    }
