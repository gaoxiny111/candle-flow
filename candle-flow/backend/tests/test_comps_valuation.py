"""Tests for comparable-company valuation calculator."""

from __future__ import annotations

import pandas as pd

from app.analysis.models.comps import (
    calculate_comparable_valuation,
    select_comparables,
    target_fundamentals,
)


def test_target_fundamentals_from_fin_and_market():
    fin = pd.DataFrame(
        [
            {"net_profit": 1e10, "equity": 5e10, "revenue": 3e10, "eps": 2.5},
        ],
        index=["20231231"],
    )
    market = {"price": 40.0, "market_cap": 40.0 * 4e9, "name": "测试"}
    meta = {"name": "测试", "industry": "煤炭开采", "eps": 2.5}
    t = target_fundamentals("601088.SH", market=market, meta=meta, fin_df=fin)
    assert t is not None
    assert t["net_profit"] == 1e10
    assert t["net_assets"] == 5e10
    assert abs(t["total_shares"] - 4e9) < 1
    assert t["industry"] == "煤炭开采"


def test_target_fundamentals_missing_profit_returns_none():
    assert (
        target_fundamentals(
            "601088.SH",
            market={"price": 10, "market_cap": 1e10},
            meta={"industry": "软件"},
            fin_df=pd.DataFrame(),
        )
        is None
    )


def test_calculate_comparable_valuation_pe_pb_range(monkeypatch):
    fin = pd.DataFrame(
        [{"net_profit": 10e8, "equity": 50e8, "revenue": 80e8, "eps": 1.0}],
        index=["20231231"],
    )
    market = {"price": 8.0, "market_cap": 80e8, "pe_ttm": 8.0, "pb": 1.6, "name": "目标"}
    meta = {
        "symbol": "600000.SH",
        "name": "目标",
        "industry": "测试行业",
        "latest_report": "20231231",
        "eps": 1.0,
    }

    peers = [
        {
            "symbol": "600001.SH",
            "name": "可比甲",
            "net_profit": 12e8,
            "net_assets": 60e8,
            "revenue": 90e8,
            "eps": 1.2,
            "roe": 20.0,
        },
        {
            "symbol": "600002.SH",
            "name": "可比乙",
            "net_profit": 9e8,
            "net_assets": 45e8,
            "revenue": 70e8,
            "eps": 0.9,
            "roe": 18.0,
        },
        {
            "symbol": "600003.SH",
            "name": "可比丙",
            "net_profit": 11e8,
            "net_assets": 55e8,
            "revenue": 85e8,
            "eps": 1.1,
            "roe": 19.0,
        },
    ]

    monkeypatch.setattr(
        "app.analysis.models.comps._industry_peer_rows",
        lambda *a, **k: peers,
    )
    monkeypatch.setattr(
        "app.analysis.models.comps._batch_quotes",
        lambda symbols, db=None: {
            "600001.SH": {"symbol": "600001.SH", "name": "可比甲", "pe_ttm": 10.0, "pb": 2.0, "market_cap": 90e8, "price": 12.0},
            "600002.SH": {"symbol": "600002.SH", "name": "可比乙", "pe_ttm": 12.0, "pb": 1.8, "market_cap": 70e8, "price": 10.0},
            "600003.SH": {"symbol": "600003.SH", "name": "可比丙", "pe_ttm": 8.0, "pb": 2.2, "market_cap": 85e8, "price": 11.0},
        },
    )

    result = calculate_comparable_valuation(
        "600000.SH",
        market=market,
        meta=meta,
        fin_df=fin,
        db=None,
    )
    assert result["peer_count"] == 3
    assert result["avg_pe"] == 10.0  # (10+12+8)/3
    assert abs(result["avg_pb"] - 2.0) < 1e-9
    pe_range = result["valuation_range"]["pe_based"]
    # net_profit 10e8 * avg_pe 10 / shares(80e8/8=10e8) = 10
    assert pe_range["mid"] == 10.0
    assert pe_range["low"] == 9.0
    assert pe_range["high"] == 11.0
    assert result["signal"] == "低估"  # price 8 < mid*0.9
    assert len(result["comparables"]) == 3


def test_select_comparables_filters_cap_and_limits(monkeypatch):
    peers = [
        {"symbol": f"60000{i}.SH", "name": f"P{i}", "net_profit": 1e8, "net_assets": 5e8, "revenue": 8e8, "eps": 1.0, "roe": 15}
        for i in range(1, 8)
    ]
    monkeypatch.setattr("app.analysis.models.comps._industry_peer_rows", lambda *a, **k: peers)

    def fake_quotes(symbols, db=None):
        # 600001 close; 600007 far (>50%)
        caps = {
            "600001.SH": 100e8,
            "600002.SH": 105e8,
            "600003.SH": 95e8,
            "600004.SH": 110e8,
            "600005.SH": 90e8,
            "600006.SH": 120e8,
            "600007.SH": 200e8,
        }
        out = {}
        for s in symbols:
            out[s.upper()] = {
                "symbol": s,
                "name": s,
                "pe_ttm": 10.0,
                "pb": 2.0,
                "market_cap": caps[s.upper()],
                "price": 10.0,
            }
        return out

    monkeypatch.setattr("app.analysis.models.comps._batch_quotes", fake_quotes)
    comps = select_comparables(
        "600000.SH",
        industry="测试",
        report_date="20231231",
        target_market_cap=100e8,
        limit=5,
    )
    assert len(comps) == 5
    assert all(c["symbol"] != "600007.SH" for c in comps)
    # closest first
    assert comps[0]["symbol"] == "600001.SH"
