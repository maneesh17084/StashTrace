"""Job queue on SQLite: claim, run, finish. Used by the worker process (and inline mode/tests)."""
from __future__ import annotations

import logging
import os
import socket
import traceback

from sqlalchemy import text

from ..db import Database
from .common import utcnow
from . import detection, posting

log = logging.getLogger("stashtrace.jobs")
WORKER_ID = f"{socket.gethostname()}:{os.getpid()}"
MAX_ATTEMPTS = 3


def claim_next(db: Database) -> dict | None:
    with db.write() as conn:  # BEGIN IMMEDIATE: only one worker can claim a given job
        r = conn.execute(text("SELECT * FROM job WHERE status='QUEUED' ORDER BY job_id LIMIT 1")).first()
        if r is None:
            return None
        conn.execute(text("UPDATE job SET status='RUNNING', attempts=attempts+1, worker_id=:w, started_at=:t"
                          " WHERE job_id=:j AND status='QUEUED'"), {"w": WORKER_ID, "t": utcnow(), "j": r.job_id})
        return dict(r._mapping)


def run_job(db: Database, job: dict) -> None:
    try:
        if job["job_type"] == "IMPORT":
            posting.process_batch(db, job["ref_id"])
        else:
            detection.execute_run(db, job["ref_id"])
        with db.write() as conn:
            conn.execute(text("UPDATE job SET status='DONE', finished_at=:t WHERE job_id=:j"),
                         {"t": utcnow(), "j": job["job_id"]})
    except Exception as exc:  # noqa: BLE001 - every failure must be recorded on the job
        log.error("job %s failed: %s", job["job_id"], exc)
        retry = job["attempts"] + 1 < MAX_ATTEMPTS
        with db.write() as conn:
            conn.execute(text("UPDATE job SET status=:s, finished_at=:t, error_message=:m WHERE job_id=:j"),
                         {"s": "QUEUED" if retry else "FAILED", "t": utcnow(),
                          "m": traceback.format_exc()[-2000:], "j": job["job_id"]})
        if not retry and job["job_type"] == "IMPORT":
            posting.mark_batch_failed(db, job["ref_id"], str(exc))


def drain(db: Database, max_jobs: int | None = None) -> int:
    """Run queued jobs until the queue is empty (inline mode, tests, data loader)."""
    n = 0
    while max_jobs is None or n < max_jobs:
        job = claim_next(db)
        if job is None:
            break
        run_job(db, job)
        n += 1
    return n


def requeue_stale(db: Database, older_than_minutes: int = 30) -> int:
    """Return RUNNING jobs abandoned by a crashed worker to the queue (processing is resumable)."""
    from .common import add_minutes
    cutoff = add_minutes(utcnow(), -older_than_minutes)
    with db.write() as conn:
        res = conn.execute(text("UPDATE job SET status='QUEUED' WHERE status='RUNNING' AND started_at < :c"), {"c": cutoff})
        return res.rowcount
