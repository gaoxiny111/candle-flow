"""Tests for scannable prefilter, job progress, and analysis cache helpers."""

from __future__ import annotations

from datetime import date

from app.services import job_progress as jp
from app.services.main_board_kline_sync import list_scannable_main_board


def test_job_progress_lifecycle():
    job_id = jp.create_job("unit_test", total=10, phase="scan", message="go")
    jp.update_job(job_id, done=5, message="half")
    mid = jp.get_job(job_id)
    assert mid is not None
    assert mid["pct"] == 50.0
    assert mid["status"] == "running"
    jp.finish_job(job_id, {"ok": True})
    done = jp.get_job(job_id)
    assert done["status"] == "done"
    assert done["result"]["ok"] is True
    assert jp.get_job(kind="unit_test")["job_id"] == job_id


def test_list_scannable_main_board_empty_db(monkeypatch):
    class FakeQ:
        def filter(self, *a, **k):
            return self

        def all(self):
            return []

        def group_by(self, *a, **k):
            return self

    class FakeDB:
        def query(self, *a, **k):
            return FakeQ()

    monkeypatch.setattr(
        "app.services.main_board_kline_sync._main_board_symbols",
        lambda db: [],
    )
    out = list_scannable_main_board(FakeDB(), as_of=date(2026, 9, 15), min_bars=45)
    assert out["scannable"] == 0
    assert out["symbols"] == []


def test_list_scannable_filters_stale_and_short(monkeypatch):
    day = date(2026, 9, 15)
    monkeypatch.setattr(
        "app.services.main_board_kline_sync._main_board_symbols",
        lambda db: ["600000.SH", "600001.SH", "600002.SH", "600003.SH"],
    )

    class FakeQuery:
        def __init__(self, rows):
            self._rows = rows

        def group_by(self, *a, **k):
            return self

        def all(self):
            return self._rows

    class FakeDB:
        def __init__(self):
            self.n = 0

        def query(self, *cols):
            self.n += 1
            if self.n == 1:
                return FakeQuery(
                    [
                        ("600000.SH", day),
                        ("600001.SH", date(2026, 9, 10)),  # stale
                        ("600002.SH", day),
                    ]
                )
            return FakeQuery(
                [
                    ("600000.SH", 100),
                    ("600001.SH", 100),
                    ("600002.SH", 10),  # short
                ]
            )

    out = list_scannable_main_board(FakeDB(), as_of=day, min_bars=45, require_fresh=True)
    assert out["symbols"] == ["600000.SH"]
    assert out["stale_kline"] == 1
    assert out["short_bars"] == 1
    assert out["no_kline"] == 1
