"""Load a generated dataset into a fresh database through the real import pipeline.

    python -m data_tools.load --data data/full --db var/full.db --fresh

Steps: migrate -> load master CSVs -> replay inputs day by day (counts submitted that day, then
movements received that day) through receive_batch/process_batch -> run detection twice (the second
run proves re-detection creates no duplicate cases) -> write load_report.json.

The replay mirrors production arrival order, so late movements naturally re-version reconciliations.
``ground_truth.*`` is never opened here.
"""
from __future__ import annotations

import argparse
import csv
import io
import json
import logging
import os
import platform
import time
from pathlib import Path

from sqlalchemy import text

from backend.app.db import Database, dispose_all
from backend.app.services import detection, posting
from data_tools.masters import EVALUATION_ONLY_FILES, load_masters, set_passwords

REPO = Path(__file__).resolve().parents[1]
log = logging.getLogger("stashtrace.load")


def migrate(url: str) -> None:
    from alembic import command
    from alembic.config import Config
    cfg = Config(str(REPO / "alembic.ini"))
    cfg.set_main_option("script_location", str(REPO / "migrations"))
    cfg.cmd_opts = argparse.Namespace(x=[f"db_url={url}"])
    command.upgrade(cfg, "head")


def _day_groups(path: Path, ts_col: str):
    """Yield (day, header_line, [lines]) from a CSV sorted by ``ts_col``."""
    with path.open(newline="", encoding="utf-8") as fh:
        header_line = fh.readline().rstrip("\n")
        header = next(csv.reader([header_line]))
        idx = header.index(ts_col)
        day, lines, last_ts = None, [], ""
        for line in fh:
            line = line.rstrip("\n")
            if not line:
                continue
            ts = next(csv.reader([line]))[idx]
            if ts < last_ts:
                raise ValueError(f"{path.name} must be sorted by {ts_col} for replay (row ts {ts} < {last_ts})")
            last_ts = ts
            d = ts[:10]
            if d != day and lines:
                yield day, header_line, lines
                lines = []
            day = d
            lines.append(line)
        if lines:
            yield day, header_line, lines


def replay(db: Database, data: Path, submitter: str, report: dict) -> None:
    mv = _day_groups(data / "movements_input.csv", "received_at")
    ct = _day_groups(data / "counts_input.csv", "submitted_at")
    nxt_m, nxt_c = next(mv, None), next(ct, None)
    totals = {"MOVEMENTS": dict.fromkeys(("received", "posted", "quarantined", "duplicate", "conflict"), 0),
              "COUNTS": dict.fromkeys(("received", "posted", "quarantined", "duplicate", "conflict"), 0)}
    reversioned = 0
    batches = 0
    t_import = time.perf_counter()
    while nxt_m or nxt_c:
        day = min(x[0] for x in (nxt_m, nxt_c) if x)
        for kind, cur, it in (("COUNTS", nxt_c, ct), ("MOVEMENTS", nxt_m, mv)):
            if cur and cur[0] == day:
                body = (cur[1] + "\n" + "\n".join(cur[2]) + "\n").encode()
                b = posting.receive_batch(db, kind, f"{kind.lower()}-{day}.csv", body, submitter,
                                          "STORE_OPS", enqueue=False)
                res = posting.process_batch(db, b["batch_id"])
                t = totals[kind]
                t["received"] += b["received_count"]
                for k in ("posted", "quarantined", "duplicate", "conflict"):
                    t[k] += res[k]
                reversioned += res["reconciliations_reversioned"]
                batches += 1
                if kind == "COUNTS":
                    nxt_c = next(ct, None)
                else:
                    nxt_m = next(mv, None)
        if day[-2:] in ("01", "08", "15", "22"):
            log.info("replayed through %s (%s batches, %s movements received, %.0fs)", day, batches,
                     totals["MOVEMENTS"]["received"], time.perf_counter() - t_import)
    report["import"] = {"batches": batches, "seconds": round(time.perf_counter() - t_import, 1),
                        "movements": totals["MOVEMENTS"], "counts": totals["COUNTS"],
                        "reconciliation_versions_from_late_movements": reversioned}


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--data", required=True)
    ap.add_argument("--db", required=True, help="SQLite file path")
    ap.add_argument("--fresh", action="store_true", help="delete the database file first")
    ap.add_argument("--password", default=os.environ.get("STASHTRACE_DEMO_PASSWORD", "stashtrace-demo"),
                    help="password set for the manifest's demo_logins")
    ap.add_argument("--no-detect", action="store_true")
    args = ap.parse_args()
    logging.basicConfig(level="INFO", format="%(asctime)s %(levelname)s %(name)s %(message)s")
    logging.getLogger("stashtrace.posting").setLevel("WARNING")
    data, db_path = Path(args.data), Path(args.db)
    for f in EVALUATION_ONLY_FILES:  # make the no-leak contract explicit
        log.info("not reading %s (evaluation only)", f)
    if args.fresh:
        dispose_all()
        for suffix in ("", "-wal", "-shm"):
            p = Path(str(db_path) + suffix)
            if p.exists():
                p.unlink()
    db_path.parent.mkdir(parents=True, exist_ok=True)
    url = f"sqlite:///{db_path.resolve().as_posix()}"
    report: dict = {"data": str(data), "db": str(db_path), "python": platform.python_version(),
                    "platform": platform.platform(), "processor": platform.processor(), "cpu_count": os.cpu_count()}
    t0 = time.perf_counter()
    migrate(url)
    db = Database(url)
    t = time.perf_counter()
    report["masters"] = load_masters(db, data)
    report["masters_seconds"] = round(time.perf_counter() - t, 1)
    manifest = json.loads((data / "manifest.json").read_text(encoding="utf-8"))
    logins = manifest.get("demo_logins", [])
    set_passwords(db, logins, args.password)
    submitter = logins[0] if logins else "EMP-000004"
    replay(db, data, submitter, report)
    if not args.no_detect:
        runs = []
        for _ in range(2):
            q = detection.queue_run(db, submitter, enqueue=False)
            runs.append(detection.execute_run(db, q["run_id"]))
        report["detection_runs"] = runs
    with db.read() as conn:
        report["db_rows"] = {t: conn.execute(text(f"SELECT COUNT(*) FROM {t}")).scalar_one() for t in (
            "inventory_movement", "import_row_result", "cycle_count", "count_reconciliation", "customer_return",
            "shrinkage_alert", "alert_evidence", "investigation")}
    report["total_seconds"] = round(time.perf_counter() - t0, 1)
    report["demo_logins"] = logins
    out = data / "load_report.json"
    out.write_text(json.dumps(report, indent=2), encoding="utf-8")
    print(json.dumps(report, indent=2))


if __name__ == "__main__":
    main()
