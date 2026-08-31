import pytest

from app.utils.symbol import (
    SymbolError,
    futures_sina_code,
    is_b_share,
    is_future,
    normalize_symbol,
    parse_symbol,
)


def test_normalize_with_market():
    assert normalize_symbol("000001.SZ") == "000001.SZ"
    assert normalize_symbol("600519.sh") == "600519.SH"


def test_normalize_code_only():
    assert normalize_symbol("600519") == "600519.SH"
    assert normalize_symbol("000001") == "000001.SZ"
    assert normalize_symbol("900948") == "900948.SH"


def test_normalize_alias():
    assert normalize_symbol("神华") == "601088.SH"
    assert normalize_symbol("中国神华") == "601088.SH"
    assert normalize_symbol("伊泰B股") == "900948.SH"


def test_normalize_rejects_futures():
    with pytest.raises(SymbolError):
        normalize_symbol("螺纹钢")
    with pytest.raises(SymbolError):
        normalize_symbol("RB0")
    with pytest.raises(SymbolError):
        normalize_symbol("RB0.FUT")
    assert parse_symbol("RB0.FUT") == ("RB0", "fut")
    assert is_future("RB0.FUT") is True
    assert is_future("600519.SH") is False
    assert futures_sina_code("RB0.FUT") == "RB0"
    assert is_b_share("RB0.FUT") is False


def test_invalid_symbol():
    with pytest.raises(SymbolError):
        normalize_symbol("akshare")


def test_parse_symbol():
    assert parse_symbol("600519") == ("600519", "sh")


def test_is_b_share():
    assert is_b_share("900948.SH") is True
    assert is_b_share("900948") is True
    assert is_b_share("601088.SH") is False


def test_price_outlier_filter():
    from app.utils.price_filter import filter_inliers, is_price_outlier, price_anchor

    # 神华真实价 ~47，混入 ~10 的模拟数据
    closes = [47.2] * 20 + [10.5, 10.6, 11.2]
    anchor = price_anchor(closes)
    assert anchor is not None and 40 < anchor < 55
    assert is_price_outlier(10.5, anchor)
    assert not is_price_outlier(47.2, anchor)

    rows = [{"c": c} for c in [46, 47, 48, 10.6, 47.5]]
    kept = filter_inliers(rows, lambda r: r["c"])
    assert all(r["c"] > 20 for r in kept)
    assert len(kept) == 4
