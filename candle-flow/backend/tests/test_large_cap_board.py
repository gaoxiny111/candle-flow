"""大盘股基本面榜：拼榜逻辑与缺分调度（不打真网）。"""

from __future__ import annotations

from unittest.mock import patch

import app.services.large_cap_board as board


def setup_function() -> None:
    with board._LOCK:
        board._REFRESHING = False
        board._UNIVERSE_LOADING = False
        board._AUTO_SCORED = False
        board._LAST_BOARD = None
        board._LAST_TS = 0.0
        board._PROGRESS.update(
            running=False, planned=0, done=0, failed=0, started_at=0.0
        )


def test_board_groups_by_abcde() -> None:
    universe = [
        {
            "symbol": "600519.SH",
            "code": "600519",
            "name": "贵州茅台",
            "price": 1600.0,
            "pe_ttm": 25.0,
            "pb": 8.0,
            "market_cap": 2e12,
            "market_cap_yi": 20000.0,
        },
        {
            "symbol": "601318.SH",
            "code": "601318",
            "name": "中国平安",
            "price": 50.0,
            "pe_ttm": 10.0,
            "pb": 1.0,
            "market_cap": 9e11,
            "market_cap_yi": 9000.0,
        },
        {
            "symbol": "600036.SH",
            "code": "600036",
            "name": "招商银行",
            "price": 35.0,
            "pe_ttm": 6.0,
            "pb": 0.9,
            "market_cap": 8e11,
            "market_cap_yi": 8000.0,
        },
        {
            "symbol": "601668.SH",
            "code": "601668",
            "name": "中国建筑",
            "price": 5.0,
            "pe_ttm": 5.0,
            "pb": 0.4,
            "market_cap": 7e11,
            "market_cap_yi": 7000.0,
        },
    ]
    scored = {
        "600519.SH": {
            "composite_score": 83.0,
            "final_rating": "A-",
            "dim_scores": {},
            "sector_profile": {},
            "industry": "白酒",
            "name": "贵州茅台",
            "market": {},
        },
        "601318.SH": {
            "composite_score": 86.0,
            "final_rating": "A",
            "dim_scores": {},
            "sector_profile": {},
            "industry": "保险",
            "name": "中国平安",
            "market": {},
        },
        "601668.SH": {
            "composite_score": 42.0,
            "final_rating": "D",
            "dim_scores": {},
            "sector_profile": {},
            "industry": "建筑",
            "name": "中国建筑",
            "market": {},
        },
    }

    with (
        patch.object(board, "_load_universe_disk", return_value=universe),
        patch.object(board, "_load_scored_map", return_value=scored),
        patch.object(board, "_schedule_scoring", return_value=True) as sched,
    ):
        out = board.scan_large_cap_board(refresh=False)

    assert out.get("pool_note") == "股票池：A股全量，按 A/B/C/D/E 评级分组"
    assert out["scored_count"] == 3
    assert out["pending_count"] == 1
    assert out["grade_counts"]["A"] == 2  # A + A-
    assert out["grade_counts"]["D"] == 1
    assert out["grade_counts"]["pending"] == 1
    assert [x["symbol"] for x in out["groups"]["A"]] == ["601318.SH", "600519.SH"]
    assert out["groups"]["D"][0]["symbol"] == "601668.SH"
    # 默认 all：未评只计不落包，防超时
    assert out["groups"]["pending"] == []
    assert out["items"][0]["symbol"] == "601318.SH"
    assert out["count"] == 3
    assert "warnings" not in out["items"][0]
    sched.assert_called_once()

    with (
        patch.object(board, "_load_universe_disk", return_value=universe),
        patch.object(board, "_load_scored_map", return_value=scored),
        patch.object(board, "_schedule_scoring", return_value=False),
    ):
        with board._LOCK:
            board._AUTO_SCORED = True
        pend = board.scan_large_cap_board(refresh=False, grade="pending")
    assert len(pend["groups"]["pending"]) == 1
    assert pend["groups"]["pending"][0]["symbol"] == "600036.SH"


def test_board_does_not_reschedule_after_auto_round() -> None:
    universe = [
        {
            "symbol": "600036.SH",
            "code": "600036",
            "name": "招商银行",
            "price": 35.0,
            "pe_ttm": 6.0,
            "pb": 0.9,
            "market_cap": 8e11,
            "market_cap_yi": 8000.0,
        },
    ]
    with board._LOCK:
        board._AUTO_SCORED = True

    with (
        patch.object(board, "_load_universe_disk", return_value=universe),
        patch.object(board, "_load_scored_map", return_value={}),
        patch.object(board, "_schedule_scoring", return_value=True) as sched,
    ):
        out = board.scan_large_cap_board(refresh=False)

    assert out["status"] == "partial"
    sched.assert_not_called()


def test_cold_start_returns_computing_without_blocking() -> None:
    with (
        patch.object(board, "_load_universe_disk", return_value=None),
        patch.object(board, "_schedule_universe_load", return_value=True) as uni,
        patch.object(board, "_schedule_scoring") as score,
    ):
        out = board.scan_large_cap_board(refresh=False)

    assert out["status"] == "computing"
    assert out["items"] == []
    assert "A股全量" in (out.get("pool_note") or "")
    uni.assert_called_once()
    score.assert_not_called()


def test_grade_band_maps_fine_ratings() -> None:
    assert board._grade_band("A-") == "A"
    assert board._grade_band("B+") == "B"
    assert board._grade_band("B-") == "B"
    assert board._grade_band("C") == "C"
    assert board._grade_band(None) == "pending"
