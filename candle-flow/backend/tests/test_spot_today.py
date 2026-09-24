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
        # merge_today_spot 只在 15:00 后允许合并（盘中 spot 是未完成快照）。
        # 本用例测「补今日 bar 的内容正确性」，因此把时钟推到收盘后。
        monkeypatch.setattr(
            KlineService,
            "_is_after_close",
            staticmethod(lambda now=None: True),
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


# ── 盘中污染防护（2026-09-23 事故回归）──────────────────────────────────────
#
# 事故：merge_today_spot 在 09:31 用实时行情伪造「今日日线」写入
#   （焦作万方 close=11.21，真实午盘 10.94；volume=14609，全库当日均量塌 -95%），
#   并且回写覆盖了 09-22 的正确数据。EMA/RSI 因此失真 → 误报右侧信号。


def test_is_after_close_boundary():
    """15:00 是分界：盘中拒绝，收盘后放行（用 Asia/Shanghai，不是服务器本地时区）。"""
    assert KlineService._is_after_close(datetime(2026, 9, 23, 9, 31, tzinfo=CN)) is False
    assert KlineService._is_after_close(datetime(2026, 9, 23, 11, 30, tzinfo=CN)) is False
    assert KlineService._is_after_close(datetime(2026, 9, 23, 14, 59, tzinfo=CN)) is False
    assert KlineService._is_after_close(datetime(2026, 9, 23, 15, 0, tzinfo=CN)) is True
    assert KlineService._is_after_close(datetime(2026, 9, 23, 16, 35, tzinfo=CN)) is True
    # UTC 07:31 == 北京 15:31 → 必须放行（旧实现用 naive now() 会判成 07:31 拒绝）
    assert KlineService._is_after_close(datetime(2026, 9, 23, 7, 31, tzinfo=ZoneInfo("UTC"))) is True


def test_merge_today_spot_rejected_intraday(monkeypatch):
    """盘中调用 merge_today_spot 必须直接返回 False，不写库。"""
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
                date=date(2026, 9, 22),
                open=48.0,
                high=48.5,
                low=47.9,
                close=48.2,
                volume=1_000_000,
                source="akshare",
            )
        )
        db.commit()

        monkeypatch.setattr("app.services.kline_service.trading_today", lambda now=None: date(2026, 9, 23))
        monkeypatch.setattr("app.services.kline_service.is_cn_weekday", lambda d=None: True)
        monkeypatch.setattr(KlineService, "_is_after_close", staticmethod(lambda now=None: False))
        monkeypatch.setattr(
            spot_mod.akshare_client,
            "fetch_spot",
            lambda symbol: {
                "date": date(2026, 9, 23),
                "open": 48.2,
                "high": 48.3,
                "low": 47.8,
                "close": 47.9,          # 盘中价，不是收盘价
                "volume": 14_609,        # 盘中累计量
                "source": "tencent",
            },
        )
        svc = KlineService(db)
        assert svc.merge_today_spot(symbol) is False
        latest = svc.get_latest(symbol)
        assert latest.date == date(2026, 9, 22), "盘中不得新增今日 bar"
        assert float(latest.close) == 48.2
        assert int(latest.volume) == 1_000_000, "历史 bar 不得被盘中快照覆盖"
    finally:
        db.query(KlineData).filter(KlineData.symbol == symbol).delete()
        db.commit()
        db.close()


def test_upsert_bar_refuses_history_overwrite(monkeypatch):
    """_upsert_bar 默认拒绝覆盖已完成交易日（日线源才是历史权威值）。"""
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
                date=date(2026, 9, 22),
                open=48.0,
                high=48.5,
                low=47.9,
                close=48.2,
                volume=1_000_000,
                source="akshare",
            )
        )
        db.commit()
        monkeypatch.setattr("app.services.kline_service.trading_today", lambda now=None: date(2026, 9, 23))

        svc = KlineService(db)
        # 默认：拒写历史日
        svc._upsert_bar(symbol, date(2026, 9, 22), 1.0, 1.0, 1.0, 1.0, 1)
        db.commit()
        row = (
            db.query(KlineData)
            .filter(KlineData.symbol == symbol, KlineData.date == date(2026, 9, 22))
            .first()
        )
        assert float(row.close) == 48.2, "历史收盘价被篡改"
        assert int(row.volume) == 1_000_000, "历史成交量被篡改"

        # 显式放行（回补脚本 / sync 的日线源写入）才允许覆盖
        svc._upsert_bar(
            symbol, date(2026, 9, 22), 48.1, 48.6, 47.9, 48.4, 1_200_000,
            allow_history_overwrite=True,
        )
        db.commit()
        row = (
            db.query(KlineData)
            .filter(KlineData.symbol == symbol, KlineData.date == date(2026, 9, 22))
            .first()
        )
        assert float(row.close) == 48.4
        assert int(row.volume) == 1_200_000
    finally:
        db.query(KlineData).filter(KlineData.symbol == symbol).delete()
        db.commit()
        db.close()


# ── 量纲判据（旧 `vol * 50` 会灌水 100 倍）──────────────────────────────────
#
# 事故：旧判据 `if vol * 50 < latest.volume: vol *= 100` 从「缩量到 2%」起
# 就误判，把**已经是「手」口径**的正常量放大 100 倍。全库实测 09-22：
# 3048 只中 1212 只（39.8%）当日量被放大 100 倍，avg 冲到 51M（应为 ~31M）。


def test_volume_scale_rule_boundaries():
    """验证「小于昨日量 1% 才 ×100」的边界（真·股口径 100 倍差才触发）。"""
    from app.services.kline_service import KlineService as _K

    # 复刻现在的判据表达式（与 kline_service.py 保持同一写法）
    def scaled(vol: int, latest_vol: int) -> int:
        if latest_vol and vol > 0 and vol < latest_vol / 100:
            return vol * 100
        return vol

    base = 10_000_000
    # 缩量到 2%（很常见）→ 不得放大（旧判据在这里就已经误触发）
    assert scaled(int(base * 0.02), base) == int(base * 0.02)
    # 缩量到 1.5%（同样常见）→ 不得放大
    assert scaled(int(base * 0.015), base) == int(base * 0.015)
    # 缩量到 0.5%（真正的 100 倍口径差）→ 正确放大
    assert scaled(int(base * 0.005), base) == int(base * 0.005) * 100
    # 正常/放量 → 不动
    assert scaled(base, base) == base
    assert scaled(base * 2, base) == base * 2


def test_merge_today_spot_does_not_inflate_normal_shrink(monkeypatch):
    """真实场景：今日缩量到昨日 2% 时，量**不得**被 ×100 灌水。

    这是线上 09-22 灌水的直接复现：盘中 spot 量偏小（缩量）被当成量纲不符，
    乘以 100 后写库 → 全库 39.8% 的标的量级错误。
    """
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
                date=date(2026, 9, 22),
                open=48.0,
                high=48.5,
                low=47.9,
                close=48.2,
                volume=10_000_000,   # 昨日 1000 万
                source="akshare",
            )
        )
        db.commit()

        monkeypatch.setattr("app.services.kline_service.trading_today", lambda now=None: date(2026, 9, 23))
        monkeypatch.setattr("app.services.kline_service.is_cn_weekday", lambda d=None: True)
        monkeypatch.setattr(KlineService, "_is_after_close", staticmethod(lambda now=None: True))
        monkeypatch.setattr(
            spot_mod.akshare_client,
            "fetch_spot",
            lambda symbol: {
                "date": date(2026, 9, 23),
                "open": 48.1,
                "high": 48.3,
                "low": 47.7,
                "close": 47.9,
                "volume": 200_000,   # 缩量到 2%（20 万），是正常量、不是量纲差
                "source": "tencent",
            },
        )
        svc = KlineService(db)
        assert svc.merge_today_spot(symbol) is True
        latest = svc.get_latest(symbol)
        assert latest.date == date(2026, 9, 23)
        assert int(latest.volume) == 200_000, (
            f"缩量到 2% 不该被 ×100，实际写成 {int(latest.volume)}"
        )
    finally:
        db.query(KlineData).filter(KlineData.symbol == symbol).delete()
        db.commit()
        db.close()
