"""Shared fixtures: a migrated demo database (masters only, or fully replayed), copied per test."""
from __future__ import annotations

import shutil
import sqlite3
from pathlib import Path

import pytest
from sqlalchemy import text
from fastapi.testclient import TestClient

from backend.app import db as dbmod
from backend.app.services import posting
from backend.app.services.context import invalidate
from data_tools import demo_seed
from data_tools.load import migrate
from data_tools.masters import load_masters, set_passwords

PASSWORD = "test-password"
S1, S2, SKU = "STORE-000001", "STORE-000002", "SKU-000001"
ADMIN, OPS, ANALYST, LP, MGR1, CLERK1, MGR2 = ("EMP-000004", "EMP-000005", "EMP-000006", "EMP-000007",
                                               "EMP-000010", "EMP-000011", "EMP-000020")


def _url(path: Path) -> str:
    return f"sqlite:///{path.resolve().as_posix()}"


@pytest.fixture(scope="session")
def demo_dir(tmp_path_factory) -> Path:
    d = tmp_path_factory.mktemp("demo")
    demo_seed.write(d)
    return d


@pytest.fixture(scope="session")
def masters_template(tmp_path_factory, demo_dir) -> Path:
    path = tmp_path_factory.mktemp("tpl") / "masters.db"
    migrate(_url(path))
    db = dbmod.Database(_url(path))
    load_masters(db, demo_dir)
    set_passwords(db, demo_seed.DEMO_LOGINS + ["EMP-000021", "EMP-000012"], PASSWORD)
    db.read_engine.dispose()
    _checkpoint(path)
    return path


def _checkpoint(path: Path) -> None:
    con = sqlite3.connect(path)
    con.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    con.close()


@pytest.fixture
def db(tmp_path, masters_template):
    """Fresh copy of the demo masters (no movements yet)."""
    path = tmp_path / "test.db"
    shutil.copy(masters_template, path)
    invalidate()
    d = dbmod.Database(_url(path))
    dbmod.set_default_db(d)
    yield d
    d.read_engine.dispose()
    dbmod._engines.pop(d.url, None)


def csv_bytes(fields: list[str], rows: list[dict]) -> bytes:
    import csv
    import io
    buf = io.StringIO()
    w = csv.writer(buf, lineterminator="\n")
    w.writerow(fields)
    for r in rows:
        w.writerow(["" if r.get(f) is None else r.get(f) for f in fields])
    return buf.getvalue().encode()


def import_rows(db, rows: list[dict], kind: str = "MOVEMENTS", submitter: str = ADMIN) -> dict:
    fields = posting.MOVEMENT_FIELDS if kind == "MOVEMENTS" else posting.COUNT_FIELDS
    b = posting.receive_batch(db, kind, "test.csv", csv_bytes(fields, rows), submitter, "STORE_OPS", enqueue=False)
    res = posting.process_batch(db, b["batch_id"])
    with db.read() as conn:
        res["rows"] = [dict(r._mapping) for r in conn.execute(text(
            "SELECT * FROM import_row_result WHERE batch_id=:b ORDER BY row_id"), {"b": b["batch_id"]})]
    res["batch_id"] = b["batch_id"]
    return res


def demo_movements(demo_dir: Path) -> list[dict]:
    from data_tools.masters import read_csv
    return list(read_csv(demo_dir / "movements_input.csv"))


def demo_counts(demo_dir: Path) -> list[dict]:
    from data_tools.masters import read_csv
    return list(read_csv(demo_dir / "counts_input.csv"))


def shift(store: str, ts: str) -> str:
    return demo_seed.sh(store, ts)


def pos(store: str, ts: str) -> str:
    return demo_seed.pos(store, ts)


_seq = [1000]


def mv(mtype: str, store: str, qty: int, ts: str, actor: str, **kw) -> dict:
    _seq[0] += 1
    sys_ = {"SALE": "POS", "RETURN": "POS", "RECEIPT": "WMS", "TRANSFER_IN": "WMS", "TRANSFER_OUT": "WMS"}.get(mtype, "STORE_OPS")
    row = {"source_system": sys_, "source_event_id": f"T-{_seq[0]}", "movement_type": mtype, "store_id": store,
           "sku_id": SKU, "quantity": qty, "occurred_at": ts, "received_at": kw.pop("received_at", ts), "actor_id": actor,
           "shift_id": kw.pop("shift_id") if "shift_id" in kw else shift(store, ts)}
    if mtype in ("SALE", "RETURN") and "source_doc_id" not in kw:
        row["source_doc_id"] = pos(store, ts)
    if mtype == "RECEIPT" and "source_doc_id" not in kw:
        row["source_doc_id"] = "PO-2026-000001"
    row.update(kw)
    return row


def count_row(cid: str, store: str, qty: int, ts: str, counter: str = CLERK1, cutoff: str | None = None,
              submitted: str | None = None) -> dict:
    from backend.app.services.common import add_minutes
    return {"source_system": "STORE_OPS", "count_id": cid, "store_id": store, "sku_id": SKU, "counted_qty": qty,
            "counted_at": ts, "count_cutoff_at": cutoff or ts, "submitted_at": submitted or add_minutes(ts, 10),
            "counted_by": counter, "shift_id": shift(store, ts), "count_type": "SCHEDULED"}


def balance(db, store: str = S1) -> int:
    with db.read() as conn:
        return conn.execute(text("SELECT on_hand FROM stock_balance WHERE store_id=:s AND sku_id=:k"),
                            {"s": store, "k": SKU}).scalar_one()


@pytest.fixture
def client(db):
    """API client with queued jobs left for the test to drain, whatever the shell sets.

    The README's reviewer setup exports STASHTRACE_RUN_JOBS_INLINE=true; tests must not depend on it.
    """
    from backend.app.config import settings
    from backend.app.main import app
    previous = settings.run_jobs_inline
    object.__setattr__(settings, "run_jobs_inline", False)  # Settings is a frozen dataclass
    yield TestClient(app)
    object.__setattr__(settings, "run_jobs_inline", previous)


def login(client, emp: str) -> dict:
    r = client.post("/api/v1/auth/login", json={"employee_id": emp, "password": PASSWORD})
    assert r.status_code == 200, r.text
    return {"Authorization": f"Bearer {r.json()['token']}"}
