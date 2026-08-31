from datetime import datetime, timedelta

from app.core.bull_tactics import (
    HEIMA,
    N_FAN,
    NIU_SAN,
    is_main_board,
    is_st_name,
    is_strict_limit_up,
    is_yizi_limit,
    kline_limit_for_tactics,
    limit_price,
    normalize_tactics,
    scan_heima_kualan,
    scan_n_fanbao,
    scan_niu_sanjue,
    scan_tactics,
)
from app.core.candle import Candle


def _c(day: int, o: float, h: float, l: float, c: float, v: float = 1_000_000) -> Candle:
    return Candle(o, h, l, c, v, datetime(2026, 1, 1) + timedelta(days=day))


def test_main_board_and_st_filter():
    assert is_main_board("600519.SH")
    assert is_main_board("000001.SZ")
    assert not is_main_board("300750.SZ")
    assert not is_main_board("688981.SH")
    assert is_st_name("*ST康美")
    assert is_st_name("ST海航")
    assert not is_st_name("贵州茅台")


def test_limit_up_helpers():
    lp = limit_price(10.0)
    assert lp == 11.0
    c = _c(0, 10.0, 11.0, 10.0, 11.0)
    assert is_strict_limit_up(c, 10.0)
    yizi = Candle(11.0, 11.0, 11.0, 11.0, 3_000_000, datetime(2026, 1, 2))
    assert is_yizi_limit(yizi, 10.0)


def test_heima_kualan_detects_pattern():
    candles = [_c(i, 10.0, 10.4, 9.9, 10.0, 900_000) for i in range(30)]
    candles.append(_c(30, 10.0, 11.0, 10.0, 11.0, 2_000_000))
    candles.append(_c(31, 11.0, 12.1, 11.0, 12.1, 2_100_000))
    candles.append(_c(32, 12.1, 13.31, 12.0, 13.0, 1_900_000))
    candles.append(_c(33, 12.9, 13.0, 12.55, 12.75, 1_100_000))
    candles.append(_c(34, 11.8, 12.0, 11.35, 11.55, 950_000))
    hits = scan_heima_kualan(candles)
    assert hits
    assert hits[0].tactic == HEIMA
    assert hits[0].details["day3_zhaban"] is True


def test_n_fanbao_rejects_yizi():
    candles = [_c(i, 10 + i * 0.05, 10.5 + i * 0.05, 9.8, 10 + i * 0.05, 1_000_000) for i in range(22)]
    pc = candles[-1].close
    lp = limit_price(pc)
    candles.append(Candle(lp, lp, lp, lp, 3_000_000, datetime(2026, 2, 1)))
    assert not scan_n_fanbao(candles)


def test_n_fanbao_detects_pullback():
    candles = [_c(i, 10.0, 10.5, 9.8, 10.0 + i * 0.05, 1_000_000) for i in range(22)]
    pc = candles[-1].close
    lp = limit_price(pc)
    limit_day = Candle(pc * 1.02, lp, pc * 1.01, lp, 2_500_000, datetime(2026, 2, 1))
    candles.append(limit_day)
    ref_open = limit_day.open
    candles.append(
        Candle(ref_open + 0.05, ref_open + 0.15, ref_open + 0.02, ref_open + 0.08, 1_200_000, datetime(2026, 2, 2))
    )
    hits = scan_n_fanbao(candles)
    assert hits
    assert hits[0].tactic == N_FAN


def test_niu_sanjue_gap_hold():
    candles = [_c(i, 10.0, 10.4, 9.9, 10.0 + i * 0.02, 1_000_000) for i in range(30)]
    prev = candles[-1]
    gap_open = prev.high + 0.2
    signal = Candle(gap_open, gap_open + 0.8, gap_open + 0.02, gap_open + 0.6, 2_500_000, datetime(2026, 3, 1))
    candles.append(signal)
    candles.append(
        Candle(signal.close - 0.1, signal.close, signal.close - 0.15, signal.close - 0.12, 1_100_000, datetime(2026, 3, 2))
    )
    hits = scan_niu_sanjue(candles)
    assert hits
    assert hits[0].tactic == NIU_SAN


def test_normalize_tactics():
    assert normalize_tactics(None) == [HEIMA, N_FAN, NIU_SAN]
    assert normalize_tactics([]) == [HEIMA, N_FAN, NIU_SAN]
    assert normalize_tactics([HEIMA]) == [HEIMA]
    assert normalize_tactics([HEIMA, "无效", N_FAN]) == [HEIMA, N_FAN]
    assert normalize_tactics(["  N字反包  "]) == [N_FAN]
    assert normalize_tactics(["无效"]) == []


def test_kline_limit_for_tactics():
    assert kline_limit_for_tactics([HEIMA]) == 80
    assert kline_limit_for_tactics([N_FAN]) == 80
    assert kline_limit_for_tactics([NIU_SAN]) == 280
    assert kline_limit_for_tactics([HEIMA, NIU_SAN]) == 280
    assert kline_limit_for_tactics(None) == 280


def test_scan_tactics_filters_by_name():
    candles = [_c(i, 10.0, 10.4, 9.9, 10.0, 900_000) for i in range(30)]
    candles.append(_c(30, 10.0, 11.0, 10.0, 11.0, 2_000_000))
    candles.append(_c(31, 11.0, 12.1, 11.0, 12.1, 2_100_000))
    candles.append(_c(32, 12.1, 13.31, 12.0, 13.0, 1_900_000))
    candles.append(_c(33, 12.9, 13.0, 12.55, 12.75, 1_100_000))
    candles.append(_c(34, 11.8, 12.0, 11.35, 11.55, 950_000))
    heima_hits = scan_tactics(candles, recent_bars=30, tactics=[HEIMA])
    assert heima_hits
    assert all(h.tactic == HEIMA for h in heima_hits)
    n_fan_hits = scan_tactics(candles, recent_bars=30, tactics=[N_FAN])
    assert n_fan_hits
    assert all(h.tactic == N_FAN for h in n_fan_hits)
    assert not scan_tactics(candles, recent_bars=30, tactics=[NIU_SAN])
    assert not scan_tactics(candles, recent_bars=30, tactics=["无效"])


def test_scan_market_filters_main_board(monkeypatch):
    from datetime import date as dt

    from app.models.kline import KlineData
    from app.models.stock import StockInfo
    from app.services.bull_tactics_service import BullTacticsService
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker
    from sqlalchemy.pool import StaticPool
    import app.models  # noqa: F401
    from app.database import Base

    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Session = sessionmaker(bind=engine)
    Base.metadata.create_all(engine)
    db = Session()
    db.add_all(
        [
            StockInfo(symbol="600519.SH", code="600519", name="贵州茅台", market="SH"),
            StockInfo(symbol="300750.SZ", code="300750", name="宁德时代", market="SZ"),
            StockInfo(symbol="000001.SZ", code="000001", name="ST测试", market="SZ"),
        ]
    )
    for i in range(40):
        db.add(
            KlineData(
                symbol="600519.SH",
                date=dt(2026, 1, 1) + timedelta(days=i),
                open=10.0,
                high=10.4,
                low=9.9,
                close=10.0,
                volume=1_000_000,
            )
        )
    db.commit()

    monkeypatch.setattr("app.services.bull_tactics_service.refresh_universe", lambda db, force=False: 0)
    monkeypatch.setattr(
        "app.services.bull_tactics_service.scan_tactics",
        lambda candles, recent_bars=30, tactics=None: [],
    )

    svc = BullTacticsService(db)
    out = svc.scan_market(recent_bars=30, refresh_list=False)
    assert out["universe_size"] == 1
    assert out["scanned"] == 1
    db.close()
