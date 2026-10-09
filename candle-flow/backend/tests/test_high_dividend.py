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
    assert hd.ROE_AVG_3Y_MIN == 8.0
    assert hd.ROE_LATEST_MIN == 6.0
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
        "roe_avg_3y": 11.0,
        "roe_report_date": "2024-12-31",
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
            _base_ok(code="000004", name="ROE均低", roe=7.0, roe_avg_3y=7.0),
            _base_ok(code="000005", name="市值不够", market_cap=100e8),
        ]
    )
    out = hd._apply_filters(df)
    assert list(out["code"]) == ["601088"]


def test_fail_reasons_lists_missing_conditions():
    assert hd._fail_reasons(_base_ok()) == []
    reasons = hd._fail_reasons(_base_ok(avg_div_yield_3y=2.0, roe=5.0, roe_avg_3y=5.0))
    assert any("近3年均息" in r for r in reasons)
    assert any("近3年ROE均" in r for r in reasons)
    assert any("最新ROE" in r for r in reasons)


def test_roe_dual_thresholds():
    # 均值够、最新够 → 通过
    assert hd._fail_reasons(_base_ok(roe=6.0, roe_avg_3y=8.0)) == []
    # 均值不够
    assert any("近3年ROE均" in r for r in hd._fail_reasons(_base_ok(roe=10.0, roe_avg_3y=7.5)))
    # 最新不够
    assert any("最新ROE" in r for r in hd._fail_reasons(_base_ok(roe=5.5, roe_avg_3y=9.0)))
    # 缺近3年均
    assert any("近3年ROE均" in r for r in hd._fail_reasons(_base_ok(roe=10.0, roe_avg_3y=None)))
    item = hd._row_to_item(pd.Series(_base_ok(roe=6.5, roe_avg_3y=8.5)))
    assert item["passed"] is True
    assert item["roe"] == 6.5
    assert item["roe_avg_3y"] == 8.5


def test_latest_annual_fundamentals_roe_avg_3y(monkeypatch):
    """年报加权 ROE：最新一期 + 近 3 年均值；不足 3 年年报则均值缺失。"""

    class _FakeAk:
        @staticmethod
        def stock_financial_analysis_indicator(symbol: str):
            if symbol == "601088":
                return pd.DataFrame(
                    {
                        "日期": [
                            "2022-12-31",
                            "2023-12-31",
                            "2024-12-31",
                            "2024-06-30",
                            "2021-12-31",
                        ],
                        "加权净资产收益率(%)": [8.0, 9.0, 13.5, 20.0, 7.0],
                        "净资产收益率(%)": [7.5, 8.5, 10.0, 19.0, 6.5],
                        "摊薄每股收益(元)": [0.8, 1.0, 1.2, 0.6, 0.7],
                        "经营现金净流量与净利润的比率(%)": [1.0, 1.1, 1.2, 0.9, 1.0],
                    }
                )
            # 仅 2 年年报 → 无近3年均
            return pd.DataFrame(
                {
                    "日期": ["2023-12-31", "2024-12-31", "2024-06-30"],
                    "加权净资产收益率(%)": [9.0, 10.0, 15.0],
                    "净资产收益率(%)": [8.0, 9.0, 14.0],
                    "摊薄每股收益(元)": [1.0, 1.1, 0.5],
                    "经营现金净流量与净利润的比率(%)": [1.0, 1.0, 1.0],
                }
            )

    monkeypatch.setitem(__import__("sys").modules, "akshare", _FakeAk)
    fund = hd._latest_annual_fundamentals("601088")
    assert fund["report_date"] == "2024-12-31"
    assert fund["roe"] == 13.5
    assert fund["roe_avg_3y"] == pytest.approx((8.0 + 9.0 + 13.5) / 3, abs=0.01)
    assert fund["eps"] == 1.2

    short = hd._latest_annual_fundamentals("000001")
    assert short["roe"] == 10.0
    assert short["roe_avg_3y"] is None


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

    def _fake_quick(**kwargs):
        return {
            "count": 1,
            "matched": 0,
            "rejected": 1,
            "scanned": 1,
            "deep_scanned": 1,
            "spot_rejected": 0,
            "universe": "csi_div",
            "pool_note": "quick",
            "items": [hd._row_to_item(pd.Series(_base_ok()))],
            "notes": [],
            "thresholds": hd._thresholds(),
            "partial": True,
        }

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
            "partial": False,
        }

    monkeypatch.setattr(hd, "build_quick_spot_board", _fake_quick)
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
            if hd._CACHE["data"] is not None and not hd._CACHE["data"].get("partial"):
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
def test_large_cap_universe_filters_by_market_cap(monkeypatch):
    spot = pd.DataFrame(
        {
            "code": ["600000", "600001", "000001"],
            "name": ["大盘A", "小盘B", "大盘C"],
            "price": [10.0, 5.0, 8.0],
            "pe_ttm": [8.0, 20.0, 9.0],
            "pb": [0.8, 2.0, 1.0],
            "market_cap": [300e8, 50e8, 250e8],
        }
    )
    monkeypatch.setattr(hd, "_fetch_spot", lambda: spot)
    monkeypatch.setattr(
        hd,
        "_calc_div_metrics",
        lambda code, price: hd._empty_metrics(str(code), "mock"),
    )
    out = hd.screen_high_dividend_stocks(universe="large_cap")
    assert out["universe"] == "large_cap"
    assert "市值≥200亿" in out["pool_note"]
    assert out["scanned"] == 2
    codes = {it["code"] for it in out["items"]}
    assert codes == {"600000", "000001"}


def test_default_universe_is_large_cap():
    assert hd.DEFAULT_UNIVERSE == "large_cap"


def test_spot_reject_skips_deep_fetch(monkeypatch):
    """PE/PB 未过门不进深扫，仍出现在未命中列表。"""
    spot = pd.DataFrame(
        {
            "code": ["600000", "600010"],
            "name": ["估值OK", "PE过高"],
            "price": [10.0, 12.0],
            "pe_ttm": [8.0, 40.0],
            "pb": [0.9, 1.0],
            "market_cap": [300e8, 280e8],
        }
    )
    deep_codes: list[str] = []

    def _fake_calc(code, price):
        deep_codes.append(str(code))
        return hd._empty_metrics(str(code), "mock")

    monkeypatch.setattr(hd, "_fetch_spot", lambda: spot)
    monkeypatch.setattr(hd, "_calc_div_metrics", _fake_calc)
    out = hd.screen_high_dividend_stocks(universe="large_cap")
    assert out["deep_scanned"] == 1
    assert out["spot_rejected"] == 1
    assert deep_codes == ["600000"]
    pe_hi = next(it for it in out["items"] if it["code"] == "600010")
    assert pe_hi["passed"] is False
    assert any("PE" in r for r in pe_hi["fail_reasons"])


def test_quick_spot_board_lists_pending(monkeypatch):
    spot = pd.DataFrame(
        {
            "code": ["600000", "600010"],
            "name": ["估值OK", "PE过高"],
            "price": [10.0, 12.0],
            "pe_ttm": [8.0, 40.0],
            "pb": [0.9, 1.0],
            "market_cap": [300e8, 280e8],
        }
    )
    monkeypatch.setattr(hd, "_fetch_spot", lambda: spot)
    out = hd.build_quick_spot_board(universe="large_cap")
    assert out["partial"] is True
    assert out["scanned"] == 2
    assert len(out["items"]) == 2
    ok = next(it for it in out["items"] if it["code"] == "600000")
    assert "分红计算中" in (ok.get("fail_reasons") or [])

