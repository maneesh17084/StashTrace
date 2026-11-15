"""Background worker: processes import batches and detection runs from the job table.

Run:  python -m backend.worker            (loops forever)
      python -m backend.worker --drain    (process the queue once and exit)
"""
from __future__ import annotations

import argparse
import logging
import signal
import time

from backend.app.config import settings
from backend.app.db import get_db
from backend.app.services import jobs

_stop = False


def _handle(_sig, _frm):
    global _stop
    _stop = True


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--drain", action="store_true", help="process queued jobs then exit")
    args = parser.parse_args()
    logging.basicConfig(level=settings.log_level, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    log = logging.getLogger("stashtrace.worker")
    db = get_db()
    signal.signal(signal.SIGINT, _handle)
    signal.signal(signal.SIGTERM, _handle)
    n = jobs.requeue_stale(db)
    if n:
        log.warning("requeued %s stale job(s)", n)
    log.info("worker %s started on %s", jobs.WORKER_ID, db.url)
    while not _stop:
        job = jobs.claim_next(db)
        if job is None:
            if args.drain:
                break
            time.sleep(settings.worker_poll_seconds)
            continue
        log.info("job %s %s %s (attempt %s)", job["job_id"], job["job_type"], job["ref_id"], job["attempts"] + 1)
        jobs.run_job(db, job)
    log.info("worker stopped")


if __name__ == "__main__":
    main()
