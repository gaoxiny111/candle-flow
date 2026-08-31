from datetime import date, datetime
from decimal import Decimal

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.database import Base
from app.models.pattern import PatternRecord
from app.models.signal import TradingSignal
from app.services.pattern_service import PatternService
from app.services.signal_service import SignalService
from app.services.watchlist import (
    MAX_WATCHLIST,
    add_symbol,
    add_symbol_to_groups,
    create_group,
    default_groups,
    dump_watchlist,
    find_group,
    flatten_groups,
    move_symbol,
    parse_watchlist,
    parse_watchlist_groups,
    remove_symbol,
)


def test_parse_watchlist_empty():
    assert parse_watchlist(None) == []
    assert parse_watchlist("") == []
    assert parse_watchlist("not-json") == []
    assert parse_watchlist("{}") == []


def test_parse_watchlist_dedupes_and_uppercases():
    raw = dump_watchlist(["600519.sh", " 000001.SZ ", "600519.SH", ""])
    assert parse_watchlist(raw) == ["600519.SH", "000001.SZ"]


def test_parse_legacy_flat_list():
    raw = '["600519.SH", "000001.SZ"]'
    assert parse_watchlist(raw) == ["600519.SH", "000001.SZ"]
    groups = parse_watchlist_groups(raw)
    assert len(groups) == 1
    assert groups[0].id == "default"
    assert groups[0].symbols == ["600519.SH", "000001.SZ"]


def test_groups_add_move_create():
    groups = default_groups(["600519.SH"])
    groups = create_group(groups, "半导体")
    groups = add_symbol_to_groups(groups, "688981.SH", group_name="半导体")
    assert flatten_groups(groups) == ["600519.SH", "688981.SH"]
    chip = next(g for g in groups if g.name == "半导体")
    assert chip.symbols == ["688981.SH"]
    groups = move_symbol(groups, "600519.SH", chip.id)
    assert chip.symbols == ["688981.SH", "600519.SH"]
    assert find_group(groups, group_id="default").symbols == []


def test_add_and_remove_symbol():
    items = add_symbol([], "600519.sh")
    items = add_symbol(items, "000001.SZ")
    items = add_symbol(items, "600519.SH")
    assert items == ["600519.SH", "000001.SZ"]
    items = remove_symbol(items, "600519.sh")
    assert items == ["000001.SZ"]


def test_watchlist_max():
    items = [f"{i:06d}.SZ" for i in range(MAX_WATCHLIST)]
    try:
        add_symbol(items, "600519.SH")
        assert False, "should raise"
    except ValueError as e:
        assert "最多" in str(e)


def _memory_db():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine)()


def _signal(symbol: str, name: str = "锤子线") -> TradingSignal:
    return TradingSignal(
        symbol=symbol,
        signal_type="buy",
        signal_level="medium",
        pattern_name=name,
        pattern_date=date(2026, 8, 26),
        entry_price=Decimal("10"),
        stop_loss=Decimal("9.5"),
        risk_reward_ratio=Decimal("2"),
        position_size=100,
        capital_at_risk=Decimal("50"),
        status="pending",
        created_at=datetime(2026, 8, 26),
    )


def test_get_signals_filters_watchlist_symbols():
    db = _memory_db()
    db.add_all(
        [
            _signal("000001.SZ"),
            _signal("600519.SH"),
            _signal("601088.SH"),
        ]
    )
    db.commit()
    svc = SignalService(db)
    items, total = svc.get_signals(symbols=["000001.SZ", "600519.SH"])
    assert total == 2
    assert {i.symbol for i in items} == {"000001.SZ", "600519.SH"}
    empty, empty_total = svc.get_signals(symbols=[])
    assert empty == []
    assert empty_total == 0


def test_get_patterns_filters_watchlist_symbols():
    db = _memory_db()
    db.add_all(
        [
            PatternRecord(
                symbol="000001.SZ",
                pattern_name="锤子线",
                direction="bullish",
                score=Decimal("70"),
                candle_date=date(2026, 8, 20),
                confirmation_status="confirmed",
            ),
            PatternRecord(
                symbol="600519.SH",
                pattern_name="看涨吞没",
                direction="bullish",
                score=Decimal("80"),
                candle_date=date(2026, 8, 21),
                confirmation_status="confirmed",
            ),
            PatternRecord(
                symbol="601088.SH",
                pattern_name="乌云盖顶",
                direction="bearish",
                score=Decimal("75"),
                candle_date=date(2026, 8, 22),
                confirmation_status="confirmed",
            ),
        ]
    )
    db.commit()
    svc = PatternService(db)
    items, total = svc.get_patterns(symbols=["000001.SZ", "600519.SH"])
    assert total == 2
    assert {i.symbol for i in items} == {"000001.SZ", "600519.SH"}
    empty, empty_total = svc.get_patterns(symbols=[])
    assert empty == []
    assert empty_total == 0
