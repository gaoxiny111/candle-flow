"""高股息筛选口径单测（不打网络）。"""

from __future__ import annotations

import time

import pandas as pd
import pytest

from app.services import high_dividend as hd


def test_thresholds_match_new_rules():
    assert hd.MIN_AVG_YIELD_3Y == 4.0
    assert hd.MIN_LAST_YIELD == 3.0
    assert hd.MIN_DIV_YEARS == 3
    assert hd.MAX_DIV_YEARS == 5
    assert hd.PAYOUT_MIN == 30.0
    assert hd.PAYOUT_MAX == 80.0
    assert hd.PE_MAX == 15.0
    assert hd.PB_MAX == 1.5
    assert hd.ROE_MIN == 10.0
    assert hd.OCF_NP_MIN == 0.8
    assert hd.MIN_MARKET_CAP == 200e8


def test_annual_cash_dps_from_sina_payout():
    hist = pd.DataFrame(
        {
            "公告日期": ["2024-07-01", "2024-12-01", "2025-07-01"],
            "派息": [10.0, 5.0, 12.0],
            "进度": ["实施", "实施", "实施"],
            "除权除息日": ["2024-07-10", "2024-12-10", "2025-07-10"],
        }
    )
    s = hd._annual_cash_dps(hist)
    assert s.loc[2024] == pytest.approx(1.5)
    assert s.loc[2025] == pytest.approx(1.2)


def test_annual_cash_dps_skips_plan_rows():
    hist = pd.DataFrame(
        {
            "公告日期": ["2025-06-01", "2025-06-15"],
            "派息": [10.0, 10.0],
            "进度": ["预案", "实施"],
            "除权除息日": [None, "2025-06-20"],
        }
    )
    s = hd._annual_cash_dps(hist)
    assert s.loc[2025] == pytest.approx(1.0)


def test_payout_ratio_3y_sum_dps_over_eps():
    cash = pd.Series({2023: 1.0, 2024: 1.2, 2025: 1.1})
    eps = pd.Series({2023: 2.0, 2024: 2.4, 2025: 2.2})
    # (1+1.2+1.1)/(2+2.4+2.2)=3.3/6.6=50%
    assert hd._payout_ratio_3y(cash, eps) == pytest.approx(50.0)


def test_payout_soft_when_missing():
    row = _base_ok(payout_ratio=None, payout_soft=True)
    assert hd._fail_reasons(row) == []
    row2 = _base_ok(payout_ratio=0.1, payout_soft=False)
    assert any("支付率" in r for r in hd._fail_reasons(row2))


def test_annual_cash_dps_legacy_columns():
    hist = pd.DataFrame(
        {
            "报告期": ["2023-12-31", "2024-12-31", "2025-12-31"],
            "每股分红": [0.8, 1.0, 1.2],
        }
    )
    s = hd._annual_cash_dps(hist)
    assert list(s.index) == [2023, 2024, 2025]
    assert s.loc[2025] == pytest.approx(1.2)


def test_consecutive_div_years_streak():
    s = pd.Series({y: 0.5 for y in range(2018, 2026)}, dtype=float)
    assert hd._consecutive_div_years(s) == 8
    # 近 5 年窗口 → 封顶 5，长期分红股仍可进入 3～5 带
    assert hd._consecutive_div_years(s, window=5) == 5


def test_consecutive_div_years_breaks_on_gap():
    s = pd.Series({2018: 0.5, 2019: 0.5, 2021: 0.6, 2022: 0.6, 2023: 0.7}, dtype=float)
    assert hd._consecutive_div_years(s) == 3
    assert hd._consecutive_div_years(s, window=5) == 3


def _base_ok(**over):
    row = {
        "code": "601088",
        "name": "命中",
        "price": 40.0,
        "avg_div_yield_3y": 5.5,
        "last_year_yield": 4.0,
        "consecutive_div_years": 4,
        "payout_ratio": 55.0,
        "pe_ttm": 12.0,
        "pb": 1.2,
        "roe": 12.0,
        "ocf_to_np": 1.1,
        "market_cap": 3e11,
        "data_ok": True,
    }
    row.update(over)
    return row


def test_apply_filters_new_rules():
    df = pd.DataFrame(
        [
            _base_ok(),
            _base_ok(code="000001", name="息不够", avg_div_yield_3y=3.0),
            _base_ok(code="000002", name="连续年数超5", consecutive_div_years=8),
            _base_ok(code="000003", name="支付率过高", payout_ratio=90.0),
            _base_ok(code="000004", name="ROE低", roe=8.0),
            _base_ok(code="000005", name="市值不够", market_cap=100e8),
        ]
    )
    out = hd._apply_filters(df)
    assert list(out["code"]) == ["601088"]


def test_fail_reasons_lists_missing_conditions():
    assert hd._fail_reasons(_base_ok()) == []
    reasons = hd._fail_reasons(_base_ok(avg_div_yield_3y=2.0, roe=5.0))
    assert any("近3年均息" in r for r in reasons)
    assert any("ROE" in r for r in reasons)


def test_hard_pe_pb_gate():
    assert hd._passes_hard_pe_pb(12.0, 1.2) is True
    assert hd._passes_hard_pe_pb(15.0, 1.5) is True
    assert hd._passes_hard_pe_pb(15.1, 1.2) is False
    assert hd._passes_hard_pe_pb(12.0, 1.6) is False
    assert hd._passes_hard_pe_pb(None, 1.2) is False
    assert hd._passes_hard_pe_pb(-1.0, 1.2) is False
    # 不满足仍进列表：标未命中并写入 fail_reasons
    pe_fail = hd._fail_reasons(_base_ok(pe_ttm=20.0))
    assert any("PE≤" in r for r in pe_fail)
    pb_fail = hd._fail_reasons(_base_ok(pb=2.0))
    assert any("PB≤" in r for r in pb_fail)
    item = hd._row_to_item(pd.Series(_base_ok(pe_ttm=20.0, pb=2.0)))
    assert item["passed"] is False
    assert any("PE≤" in r for r in item["fail_reasons"])
    assert any("PB≤" in r for r in item["fail_reasons"])


def test_to_symbol():
    assert hd._to_symbol("601088") == "601088.SH"
    assert hd._to_symbol("000001") == "000001.SZ"


def test_exclude_beijing_exchange_codes():
    assert hd._is_hs_a_code("601088") is True
    assert hd._is_hs_a_code("000001") is True
    assert hd._is_hs_a_code("300750") is True
    assert hd._is_hs_a_code("920000") is False
    assert hd._is_hs_a_code("830001") is False
    assert hd._is_hs_a_code("430047") is False


def test_scan_returns_computing_without_blocking(monkeypatch, tmp_path):
    """冷启动不得同步扫全池（否则 Cloudflare 524）。"""
    hd.invalidate_cache()
    monkeypatch.setattr(hd, "_DISK_CACHE_DIR", tmp_path)
    called = {"n": 0}

    def _fake_screen(**kwargs):
        called["n"] += 1
        time.sleep(0.05)
        return {
            "count": 1,
            "matched": 1,
            "rejected": 0,
            "scanned": 1,
            "universe": "csi_div",
            "pool_note": "test",
            "items": [hd._row_to_item(pd.Series(_base_ok()))],
            "notes": [],
            "thresholds": hd._thresholds(),
        }

    monkeypatch.setattr(hd, "screen_high_dividend_stocks", _fake_screen)

    t0 = time.time()
    out = hd.scan_high_dividend(universe="csi_div", top=50, refresh=False)
    assert time.time() - t0 < 1.0
    assert out["status"] == "computing"
    assert out["items"] == []

    # 等后台写完，再请求应命中缓存
    for _ in range(50):
        time.sleep(0.05)
        with hd._LOCK:
            if hd._CACHE["data"] is not None:
                break
    out2 = hd.scan_high_dividend(universe="csi_div", top=50, refresh=False)
    assert out2["status"] in ("ready", "refreshing")
    assert out2["matched"] == 1
    assert called["n"] >= 1
    hd.invalidate_cache()


def test_scan_serves_stale_disk_and_schedules_refresh(monkeypatch, tmp_path):
    hd.invalidate_cache()
    monkeypatch.setattr(hd, "_DISK_CACHE_DIR", tmp_path)
    key = f"{hd.CACHE_VERSION}:csi_div:None"
    payload = {
        "count": 1,
        "matched": 1,
        "rejected": 0,
        "scanned": 1,
        "universe": "csi_div",
        "pool_note": "disk",
        "items": [hd._row_to_item(pd.Series(_base_ok()))],
        "notes": [],
        "thresholds": hd._thresholds(),
    }
    hd._save_disk(key, payload, ts=time.time() - hd.CACHE_TTL_SEC - 10)

    scheduled = {"n": 0}

    def _fake_schedule(cache_key, universe, limit):
        scheduled["n"] += 1
        return True

    monkeypatch.setattr(hd, "_schedule_refresh", _fake_schedule)

    out = hd.scan_high_dividend(universe="csi_div", top=50, refresh=False)
    assert out["matched"] == 1
    assert out["stale"] is True
    assert out["status"] == "refreshing"
    assert scheduled["n"] == 1
    hd.invalidate_cache()
