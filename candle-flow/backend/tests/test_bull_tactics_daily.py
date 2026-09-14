"""Tests for daily bull-tactics report flatten/persist helpers."""

from __future__ import annotations

import json
from pathlib import Path

from app.services import bull_tactics_daily as mod
from app.services.main_board_kline_sync import is_fresh_enough


def test_is_fresh_enough_ratio_or_abs():
    assert not is_fresh_enough({"universe_size": 1000, "fresh_to_trade_date": 100})
    assert is_fresh_enough({"universe_size": 1000, "fresh_to_trade_date": 500})
    assert is_fresh_enough({"universe_size": 2000, "fresh_to_trade_date": 1000})
    assert is_fresh_enough({"universe_size": 3000, "fresh_to_trade_date": 500})  # abs gate
    assert not is_fresh_enough({"universe_size": 0, "fresh_to_trade_date": 0})


def test_flatten_today_hits_only_keeps_trade_date():
    scan = {
        "items": [
            {
                "symbol": "600519.SH",
                "name": "贵州茅台",
                "hits": [
                    {"tactic": "黑马跨栏", "buy_date": "2026-09-14", "buy_price": 10.5, "score": 80, "setup_date": "2026-09-10"},
                    {"tactic": "N字反包", "buy_date": "2026-09-13", "buy_price": 10.0, "score": 70, "setup_date": "2026-09-08"},
                ],
            },
            {
                "symbol": "000001.SZ",
                "name": "平安银行",
                "hits": [
                    {"tactic": "牛股三绝", "buy_date": "2026-09-14", "buy_price": 12.0, "score": 75, "setup_date": "2026-09-12"},
                ],
            },
        ]
    }
    rows = mod._flatten_today_hits(scan, "2026-09-14")
    assert len(rows) == 2
    assert {r["symbol"] for r in rows} == {"600519.SH", "000001.SZ"}
    assert all(r["buy_date"] == "2026-09-14" for r in rows)


def test_run_daily_writes_json_and_csv(tmp_path, monkeypatch):
    monkeypatch.setattr(mod, "DATA_DIR", tmp_path)
    monkeypatch.setattr(mod, "_today", lambda: "2026-09-14")
    monkeypatch.setattr(
        mod,
        "kline_freshness",
        lambda db, trade_date: {
            "universe_size": 10,
            "with_bars": 10,
            "fresh_to_trade_date": 10,
            "db_latest_date": "2026-09-14",
            "trade_date": trade_date,
            "ratio": 1.0,
            "stale": False,
            "ready": True,
        },
    )

    class FakeSvc:
        def __init__(self, db):
            pass

        def scan_market(self, recent_bars=7, refresh_list=False, tactics=None):
            return {
                "items": [
                    {
                        "symbol": "600000.SH",
                        "name": "测试股",
                        "hits": [
                            {
                                "tactic": "黑马跨栏",
                                "buy_date": "2026-09-14",
                                "buy_price": 11.2,
                                "score": 88,
                                "setup_date": "2026-09-11",
                                "details": {},
                            }
                        ],
                    }
                ],
                "scanned": 10,
                "universe_size": 10,
                "errors": 0,
                "skipped": 0,
            }

    monkeypatch.setattr(mod, "BullTacticsService", FakeSvc)
    monkeypatch.setattr(mod, "SessionLocal", lambda: type("DB", (), {"close": lambda self: None})())

    out = mod.run_daily_bull_tactics_scan(trade_date="2026-09-14")
    assert out["status"] == "ok"
    assert out["ready"] is True
    assert out["count"] == 1
    assert out["counts"]["黑马跨栏"] == 1

    report = Path(tmp_path) / "daily_2026-09-14.json"
    latest = Path(tmp_path) / "latest.json"
    csv_file = Path(tmp_path) / "daily_2026-09-14.csv"
    assert report.is_file() and latest.is_file() and csv_file.is_file()
    loaded = json.loads(report.read_text(encoding="utf-8"))
    assert loaded["items"][0]["symbol"] == "600000.SH"
    assert mod.load_daily_report("2026-09-14")["count"] == 1


def test_run_daily_gates_when_stale(tmp_path, monkeypatch):
    monkeypatch.setattr(mod, "DATA_DIR", tmp_path)
    monkeypatch.setattr(mod, "_today", lambda: "2026-09-14")
    monkeypatch.setattr(
        mod,
        "kline_freshness",
        lambda db, trade_date: {
            "universe_size": 3000,
            "with_bars": 100,
            "fresh_to_trade_date": 3,
            "db_latest_date": "2026-09-10",
            "trade_date": trade_date,
            "ratio": 0.001,
            "stale": True,
            "ready": False,
        },
    )

    called = {"scan": 0}

    class FakeSvc:
        def __init__(self, db):
            pass

        def scan_market(self, *args, **kwargs):
            called["scan"] += 1
            return {"items": [], "scanned": 0}

    monkeypatch.setattr(mod, "BullTacticsService", FakeSvc)
    monkeypatch.setattr(mod, "SessionLocal", lambda: type("DB", (), {"close": lambda self: None})())

    out = mod.run_daily_bull_tactics_scan(trade_date="2026-09-14")
    assert out["status"] == "stale_data"
    assert out["ready"] is False
    assert out["count"] == 0
    assert out["items"] == []
    assert called["scan"] == 0
    assert "新鲜度" in (out.get("message") or "")
    assert (Path(tmp_path) / "latest.json").is_file()


def test_run_daily_uses_incremental_sync(tmp_path, monkeypatch):
    monkeypatch.setattr(mod, "DATA_DIR", tmp_path)
    monkeypatch.setattr(mod, "_today", lambda: "2026-09-14")
    monkeypatch.setattr(
        mod,
        "kline_freshness",
        lambda db, trade_date: {
            "universe_size": 10,
            "with_bars": 10,
            "fresh_to_trade_date": 10,
            "db_latest_date": "2026-09-14",
            "trade_date": trade_date,
            "ratio": 1.0,
            "stale": False,
            "ready": True,
        },
    )

    sync_calls: list[dict] = []

    def fake_sync(**kwargs):
        sync_calls.append(kwargs)
        return {"status": "ok", "needed": 2, "synced": 2, "incremental": True}

    class FakeSvc:
        def __init__(self, db):
            pass

        def scan_market(self, recent_bars=7, refresh_list=False, tactics=None):
            return {"items": [], "scanned": 10, "universe_size": 10, "errors": 0, "skipped": 0}

    import app.services.main_board_kline_sync as sync_mod

    monkeypatch.setattr(sync_mod, "sync_main_board_klines", fake_sync)
    monkeypatch.setattr(mod, "BullTacticsService", FakeSvc)
    monkeypatch.setattr(mod, "SessionLocal", lambda: type("DB", (), {"close": lambda self: None})())

    out = mod.run_daily_bull_tactics_scan(trade_date="2026-09-14", sync_klines=True)
    assert out["status"] == "ok"
    assert sync_calls and sync_calls[0].get("incremental") is True
    assert out["sync"]["needed"] == 2
