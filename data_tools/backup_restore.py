"""Back up a live SQLite database with the online backup API, restore it, and prove the restore.

    python -m data_tools.backup_restore --db var/full.db --backup var/backup/full.db

Checks on the restored copy: integrity_check, foreign_key_check, schema revision, per-table row counts,
and ledger/balance checksums identical to the source.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import sqlite3
import time
from pathlib import Path

TABLES = ["inventory_movement", "stock_balance", "cycle_count", "count_reconciliation", "import_row_result",
          "shrinkage_alert", "alert_evidence", "investigation_event", "store_sku", "role_assignment"]


def fingerprint(path: Path) -> dict:
    con = sqlite3.connect(path)
    fp = {"revision": con.execute("SELECT version_num FROM alembic_version").fetchone()[0],
          "rows": {t: con.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0] for t in TABLES}}
    h = hashlib.sha256()
    for r in con.execute("SELECT movement_id, store_id, sku_id, stock_qty, occurred_at FROM inventory_movement ORDER BY ledger_seq"):
        h.update(repr(r).encode())
    fp["ledger_sha256"] = h.hexdigest()
    h = hashlib.sha256()
    for r in con.execute("SELECT store_id, sku_id, on_hand FROM stock_balance ORDER BY store_id, sku_id"):
        h.update(repr(r).encode())
    fp["balance_sha256"] = h.hexdigest()
    con.close()
    return fp


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", required=True)
    ap.add_argument("--backup", required=True)
    ap.add_argument("--out", default="docs")
    a = ap.parse_args()
    src, bak = Path(a.db), Path(a.backup)
    bak.parent.mkdir(parents=True, exist_ok=True)
    restored = bak.with_name(bak.stem + "-restored.db")
    for p in (bak, restored):
        p.unlink(missing_ok=True)
    t = time.perf_counter()
    with sqlite3.connect(src) as s, sqlite3.connect(bak) as d:
        s.backup(d)  # consistent snapshot, safe while the API and worker are running
    t_backup = time.perf_counter() - t
    t = time.perf_counter()
    with sqlite3.connect(bak) as s, sqlite3.connect(restored) as d:
        s.backup(d)
    t_restore = time.perf_counter() - t
    con = sqlite3.connect(restored)
    integrity = con.execute("PRAGMA integrity_check").fetchone()[0]
    fk = len(con.execute("PRAGMA foreign_key_check").fetchall())
    con.close()
    a_fp, b_fp = fingerprint(src), fingerprint(restored)
    report = {"source": str(src), "backup": str(bak), "restored": str(restored), "backup_seconds": round(t_backup, 1),
              "restore_seconds": round(t_restore, 1), "integrity_check": integrity, "foreign_key_violations": fk,
              "identical": a_fp == b_fp, "source_fingerprint": a_fp, "restored_fingerprint": b_fp}
    Path(a.out).mkdir(exist_ok=True)
    (Path(a.out) / "backup_restore.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
    print(json.dumps({k: v for k, v in report.items() if not k.endswith("fingerprint")}, indent=2))
    raise SystemExit(0 if report["identical"] and integrity == "ok" and fk == 0 else 1)


if __name__ == "__main__":
    main()
