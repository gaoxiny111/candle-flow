"""In-process job progress for long scans / daily runs."""

from __future__ import annotations

import threading
import time
import uuid
from typing import Any, Callable

_lock = threading.Lock()
_jobs: dict[str, dict[str, Any]] = {}
_latest_by_kind: dict[str, str] = {}


def create_job(kind: str, *, total: int = 0, phase: str = "queued", message: str = "") -> str:
    job_id = uuid.uuid4().hex[:12]
    now = time.time()
    payload = {
        "job_id": job_id,
        "kind": kind,
        "status": "running",
        "phase": phase,
        "message": message,
        "done": 0,
        "total": int(total or 0),
        "pct": 0.0,
        "result": None,
        "error": None,
        "started_at": now,
        "updated_at": now,
    }
    with _lock:
        _jobs[job_id] = payload
        _latest_by_kind[kind] = job_id
    return job_id


def update_job(
    job_id: str,
    *,
    phase: str | None = None,
    message: str | None = None,
    done: int | None = None,
    total: int | None = None,
    status: str | None = None,
    result: Any = None,
    error: str | None = None,
) -> None:
    with _lock:
        job = _jobs.get(job_id)
        if not job:
            return
        if phase is not None:
            job["phase"] = phase
        if message is not None:
            job["message"] = message
        if done is not None:
            job["done"] = int(done)
        if total is not None:
            job["total"] = int(total)
        if status is not None:
            job["status"] = status
        if result is not None:
            job["result"] = result
        if error is not None:
            job["error"] = error
        tot = int(job.get("total") or 0)
        d = int(job.get("done") or 0)
        job["pct"] = round(100.0 * d / tot, 1) if tot > 0 else (100.0 if job.get("status") == "done" else 0.0)
        job["updated_at"] = time.time()


def finish_job(job_id: str, result: Any = None, *, error: str | None = None) -> None:
    if error:
        update_job(job_id, status="error", phase="error", message=error, error=error, result=result)
    else:
        with _lock:
            job = _jobs.get(job_id)
            if job and int(job.get("total") or 0) > 0:
                job["done"] = int(job["total"])
        update_job(job_id, status="done", phase="done", message="完成", result=result)


def get_job(job_id: str | None = None, *, kind: str | None = None) -> dict[str, Any] | None:
    with _lock:
        if job_id:
            job = _jobs.get(job_id)
        elif kind:
            jid = _latest_by_kind.get(kind)
            job = _jobs.get(jid) if jid else None
        else:
            job = None
        return dict(job) if job else None


def run_in_background(kind: str, target: Callable[[str], None], *, phase: str = "starting", message: str = "") -> str:
    """Create a job and run target(job_id) on a daemon thread."""
    job_id = create_job(kind, phase=phase, message=message)

    def _wrap():
        try:
            target(job_id)
        except Exception as exc:
            finish_job(job_id, error=str(exc))

    threading.Thread(target=_wrap, name=f"job-{kind}-{job_id}", daemon=True).start()
    return job_id
