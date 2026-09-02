from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.database import Base
from app.models.stock import StockInfo
from app.services.stock_universe import (
    code_to_symbol,
    ensure_market_consistent,
    ensure_seeded,
    lookup_name,
    parse_sina_suggest,
    pinyin_abbr,
    resolve_symbol,
    search_local,
    search_stocks,
    _upsert,
)
from app.utils.symbol import SymbolError, market_for_code, normalize_symbol


def test_parse_sina_suggest_ignores_futures():
    text = "var suggestvalue='螺纹钢连续,8,RB0,RB0,螺纹钢连续,lwg,螺纹钢连续;';"
    assert parse_sina_suggest(text) == []


def test_search_local_excludes_futures():
    db = _memory_db()
    from app.services.stock_universe import ensure_seeded

    ensure_seeded(db)
    _upsert(
        db,
        [{"symbol": "RB0.FUT", "code": "RB0", "name": "螺纹钢连续", "market": "FUT"}],
    )
    assert not search_local(db, "螺纹")
    assert not search_local(db, "RB0")


def test_parse_sina_suggest_maotai():
    text = "var suggestvalue='贵州茅台,11,600519,sh600519,贵州茅台,gzmt,贵州茅台;';"
    items = parse_sina_suggest(text)
    assert items[0]["symbol"] == "600519.SH"
    assert items[0]["name"] == "贵州茅台"
    assert items[0]["code"] == "600519"


def test_parse_sina_suggest_bank():
    text = (
        "var suggestvalue='平安,11,000001,sz000001,平安银行,payh,平安银行;"
        "平安,11,601318,sh601318,中国平安,zgpa,中国平安;';"
    )
    items = parse_sina_suggest(text)
    symbols = [i["symbol"] for i in items]
    assert "000001.SZ" in symbols
    assert "601318.SH" in symbols


def test_parse_sina_suggest_etf():
    text = (
        "var suggestvalue='沪深300ETF华泰柏瑞,22,510300,of510300,沪深300ETF华泰柏瑞,,"
        "沪深300ETF华泰柏瑞,99,1,,,;"
        "创业板ETF,22,159915,of159915,创业板ETF,,创业板ETF,99,1,,,';"
    )
    items = parse_sina_suggest(text)
    symbols = {i["symbol"] for i in items}
    assert "510300.SH" in symbols
    assert "159915.SZ" in symbols


def _memory_db():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine)()


def test_search_local_by_name():
    db = _memory_db()
    _upsert(
        db,
        [
            {"symbol": "300750.SZ", "code": "300750", "name": "宁德时代", "market": "SZ"},
            {"symbol": "600519.SH", "code": "600519", "name": "贵州茅台", "market": "SH"},
        ],
    )
    hits = search_local(db, "茅台")
    assert hits[0].symbol == "600519.SH"
    hits = search_local(db, "宁德")
    assert hits[0].symbol == "300750.SZ"
    hits = search_local(db, "300750")
    assert hits[0].symbol == "300750.SZ"


def test_resolve_seeded_chinese_name(monkeypatch):
    db = _memory_db()
    monkeypatch.setattr("app.services.stock_universe.fetch_sina_suggest", lambda q: [])
    monkeypatch.setattr("app.services.stock_universe.refresh_universe", lambda db, force=False: 0)
    assert resolve_symbol("贵州茅台", db) == "600519.SH"
    assert resolve_symbol("茅台", db) == "600519.SH"
    assert normalize_symbol("茅台") == "600519.SH"


def test_resolve_unknown_name(monkeypatch):
    db = _memory_db()
    monkeypatch.setattr("app.services.stock_universe.fetch_sina_suggest", lambda q: [])
    monkeypatch.setattr("app.services.stock_universe.refresh_universe", lambda db, force=False: 0)
    try:
        resolve_symbol("不存在的股票xyz", db)
        assert False, "should raise"
    except SymbolError as e:
        assert "未找到" in str(e)


def test_lookup_name_from_stock_info():
    db = _memory_db()
    _upsert(
        db,
        [{"symbol": "002812.SZ", "code": "002812", "name": "恩捷股份", "market": "SZ"}],
    )
    assert lookup_name(db, "002812.SZ") == "恩捷股份"
    assert lookup_name(db, "002812") == "恩捷股份"


def test_pinyin_abbr():
    assert pinyin_abbr("贵州茅台") == "gzmt"
    assert pinyin_abbr("宁德时代") == "ndsd"
    assert pinyin_abbr("") == ""


def test_search_local_by_pinyin():
    db = _memory_db()
    _upsert(
        db,
        [
            {"symbol": "600519.SH", "code": "600519", "name": "贵州茅台", "market": "SH"},
            {"symbol": "300750.SZ", "code": "300750", "name": "宁德时代", "market": "SZ"},
        ],
    )
    hits = search_local(db, "gzmt")
    assert hits and hits[0].symbol == "600519.SH"
    hits = search_local(db, "GZMT")
    assert hits and hits[0].symbol == "600519.SH"
    hits = search_local(db, "nd")
    assert hits and hits[0].symbol == "300750.SZ"


def test_search_stocks_pinyin_without_network(monkeypatch):
    """本地库有拼音时，不依赖新浪联想也能搜到。"""
    db = _memory_db()
    monkeypatch.setattr("app.services.stock_universe.fetch_sina_suggest", lambda q: [])
    monkeypatch.setattr("app.services.stock_universe.refresh_universe", lambda db, force=False: 0)
    _upsert(
        db,
        [{"symbol": "600519.SH", "code": "600519", "name": "贵州茅台", "market": "SH"}],
    )
    hits = search_stocks(db, "gzmt")
    assert hits and hits[0]["symbol"] == "600519.SH"


def test_ensure_seeded_backfills_pinyin():
    db = _memory_db()
    db.add(StockInfo(symbol="600519.SH", code="600519", name="贵州茅台", market="SH", pinyin=""))
    db.commit()
    ensure_seeded(db)
    hits = search_local(db, "gzmt")
    assert hits and hits[0].symbol == "600519.SH"


def test_bj_code_to_symbol():
    assert code_to_symbol("920047", "bj") == "920047.BJ"
    assert code_to_symbol("920047") == "920047.BJ"
    assert code_to_symbol("830799") == "830799.BJ"
    assert code_to_symbol("430047") == "430047.BJ"
    assert market_for_code("920047") == "BJ"
    assert normalize_symbol("920047") == "920047.BJ"
    assert normalize_symbol("920047.bj") == "920047.BJ"
    # 900 开头仍是上海 B 股
    assert market_for_code("900948") == "SH"


def test_parse_sina_suggest_bj():
    text = "var suggestvalue='诺思兰德,11,920047,bj920047,诺思兰德,nsld,诺思兰德,99,1,,,';"
    items = parse_sina_suggest(text)
    assert items and items[0]["symbol"] == "920047.BJ"
    assert items[0]["market"] == "BJ"
    assert items[0]["pinyin"] == "nsld"


def test_ensure_market_consistent_fixes_dirty_rows():
    db = _memory_db()
    db.add(StockInfo(symbol="920047.SH", code="920047", name="诺思兰德", market="SH", pinyin="nsld"))
    db.add(StockInfo(symbol="600519.SH", code="600519", name="贵州茅台", market="SH", pinyin="gzmt"))
    db.commit()
    ensure_market_consistent(db)
    assert db.get(StockInfo, "920047.SH") is None
    fixed = db.get(StockInfo, "920047.BJ")
    assert fixed and fixed.name == "诺思兰德" and fixed.pinyin == "nsld"
    # 正常行不受影响
    assert db.get(StockInfo, "600519.SH") is not None
    hits = search_local(db, "920047")
    assert hits and hits[0].symbol == "920047.BJ"
