"""Today's daily bar backfill from realtime quotes."""

from datetime import date, datetime
from zoneinfo import ZoneInfo

from app.services import akshare_client as spot_mod
from app.services.akshare_client import is_cn_weekday, trading_today
from app.services.kline_service import KlineService


CN = ZoneInfo("Asia/Shanghai")


def test_trading_today_uses_shanghai_not_utc():
    # 2026-08-31 01:30 China = 2026-08-30 17:30 UTC
    utc_evening = datetime(2026, 8, 30, 17, 30, tzinfo=ZoneInfo("UTC"))
    assert trading_today(utc_evening) == date(2026, 8, 31)
    assert is_cn_weekday(trading_today(utc_evening)) is True


def test_weekend_skipped_in_shanghai():
    sunday = datetime(2026, 8, 30, 12, 0, tzinfo=CN)
    assert is_cn_weekday(trading_today(sunday)) is False


def test_tencent_spot_parser(monkeypatch):
    payload = (
        'v_sh601088="1~中国神华~601088~48.78~47.63~48.00~396072~0~0~48.78~0~0~0~0~0~0~0~0~0~0~0~0~0~0~0~0~0~0~0~~'
        '20260831151132~1.15~2.41~48.97~47.55~48.78/396072/1~396072~192197~0.24~19.60~~48.97~47.55~2.98~'
        '8044.33~10580.11~2.35~52.39~42.87~1.74";'
    )

    class _Resp:
        content = payload.encode("gbk")

    monkeypatch.setattr(
        spot_mod.akshare_client,
        "_spot_from_eastmoney",
        lambda code, market: None,
    )

    import requests

    monkeypatch.setattr(requests, "get", lambda *a, **k: _Resp())
    spot = spot_mod.akshare_client._spot_from_tencent("601088", "sh")
    assert spot is not None
    assert spot["close"] == 48.78
    assert spot["open"] == 48.0
    assert spot["high"] == 48.97
    assert spot["low"] == 47.55
    assert spot["date"] == date(2026, 8, 31)
    assert spot["volume"] == 396072


def test_merge_today_spot_inserts_bar(monkeypatch):
    from app.database import SessionLocal, init_db
    from app.models.kline import KlineData

    init_db()
    db = SessionLocal()
    symbol = "601088.SH"
    try:
        db.query(KlineData).filter(KlineData.symbol == symbol).delete()
        db.add(
            KlineData(
                symbol=symbol,
                date=date(2026, 8, 28),
                open=47.5,
                high=47.8,
                low=47.4,
                close=47.63,
                volume=100000,
                source="akshare",
            )
        )
        db.commit()

        monkeypatch.setattr(spot_mod, "trading_today", lambda now=None: date(2026, 8, 31))
        monkeypatch.setattr(spot_mod, "is_cn_weekday", lambda d=None: True)
        monkeypatch.setattr(
            "app.services.kline_service.trading_today",
            lambda now=None: date(2026, 8, 31),
        )
        monkeypatch.setattr(
            "app.services.kline_service.is_cn_weekday",
            lambda d=None: True,
        )
        monkeypatch.setattr(
            spot_mod.akshare_client,
            "fetch_spot",
            lambda symbol: {
                "date": date(2026, 8, 31),
                "open": 48.0,
                "high": 48.97,
                "low": 47.55,
                "close": 48.78,
                "volume": 396072,
                "source": "tencent",
            },
        )

        svc = KlineService(db)
        assert svc.latest_is_stale(symbol) is True
        assert svc.ensure_today_bar(symbol) is True
        latest = svc.get_latest(symbol)
        assert latest is not None
        assert latest.date == date(2026, 8, 31)
        assert float(latest.close) == 48.78
        assert svc.latest_is_stale(symbol) is False

        # Second call refreshes OHLC and still reports success (so chart reloads).
        monkeypatch.setattr(
            spot_mod.akshare_client,
            "fetch_spot",
            lambda symbol: {
                "date": date(2026, 8, 31),
                "open": 48.0,
                "high": 49.10,
                "low": 47.55,
                "close": 49.00,
                "volume": 400000,
                "source": "tencent",
            },
        )
        assert svc.merge_today_spot(symbol) is True
        assert float(svc.get_latest(symbol).close) == 49.0
    finally:
        db.query(KlineData).filter(KlineData.symbol == symbol).delete()
        db.commit()
        db.close()
