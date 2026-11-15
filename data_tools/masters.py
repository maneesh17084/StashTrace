"""CSV file contract shared by the generator, the demo seed and the loader."""
from __future__ import annotations

import csv
from pathlib import Path

from sqlalchemy import text

from backend.app.db import Database
from backend.app.services.authz import hash_password
from backend.app.services.context import invalidate

# file -> (table, columns) in load order (parents first)
MASTER_FILES: list[tuple[str, str, list[str]]] = [
    ("stores.csv", "store", ["store_id", "store_name", "store_type", "region_code", "timezone", "opened_on", "status", "closed_on"]),
    ("skus.csv", "sku", ["sku_id", "sku_name", "category", "brand", "uom", "unit_cost_cents", "unit_price_cents",
                         "shrink_risk", "status", "introduced_on"]),
    ("store_skus.csv", "store_sku", ["store_id", "sku_id", "active_from", "active_to", "opening_qty", "opening_as_of",
                                     "replenishment_source", "source_warehouse_id"]),
    ("employees.csv", "employee", ["employee_id", "display_name", "employee_type", "home_store_id", "hired_on",
                                   "terminated_on", "status"]),
    ("role_assignments.csv", "role_assignment", ["assignment_id", "employee_id", "role_code", "store_id",
                                                 "effective_from", "effective_to"]),
    ("shifts.csv", "shift", ["shift_id", "store_id", "shift_date", "shift_type", "start_at", "end_at"]),
    ("promotions.csv", "promotion", ["promotion_id", "promo_name", "promo_type", "discount_pct", "start_date", "end_date"]),
    ("promotion_scopes.csv", "promotion_scope", ["promotion_id", "store_id", "sku_id"]),
    ("source_documents.csv", "source_document", ["doc_id", "doc_type", "store_id", "business_date"]),
    ("transfers.csv", "transfer", ["transfer_id", "from_store_id", "to_store_id", "created_at", "expected_arrival_at"]),
    ("transfer_lines.csv", "transfer_line", ["transfer_id", "sku_id", "planned_qty"]),
]
INPUT_FILES = ["movements_input.csv", "counts_input.csv"]
# Never read by the application or loader: evaluation only.
EVALUATION_ONLY_FILES = ["ground_truth.csv", "ground_truth.json"]
INT_COLS = {"unit_cost_cents", "unit_price_cents", "opening_qty", "discount_pct", "planned_qty"}


def write_csv(path: Path, columns: list[str], rows) -> int:
    path.parent.mkdir(parents=True, exist_ok=True)
    n = 0
    with path.open("w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh, lineterminator="\n")
        w.writerow(columns)
        for r in rows:
            w.writerow(["" if r.get(c) is None else r.get(c) for c in columns] if isinstance(r, dict) else
                       ["" if v is None else v for v in r])
            n += 1
    return n


def read_csv(path: Path):
    with path.open(newline="", encoding="utf-8") as fh:
        yield from csv.DictReader(fh)


def load_masters(db: Database, data_dir: Path, chunk: int = 20000) -> dict[str, int]:
    counts: dict[str, int] = {}
    with db.write() as conn:
        for fname, table, cols in MASTER_FILES:
            path = data_dir / fname
            if not path.exists():
                counts[table] = 0
                continue
            sql = text(f"INSERT INTO {table} ({', '.join(cols)}) VALUES ({', '.join(':' + c for c in cols)})")
            buf, n = [], 0
            for r in read_csv(path):
                buf.append({c: (None if r.get(c, "") == "" else (int(r[c]) if c in INT_COLS else r[c])) for c in cols})
                if len(buf) >= chunk:
                    conn.execute(sql, buf)
                    n += len(buf)
                    buf = []
            if buf:
                conn.execute(sql, buf)
                n += len(buf)
            counts[table] = n
        # Opening balances seed the cached on-hand (derived from store_sku, not generated independently).
        conn.execute(text("INSERT INTO stock_balance (store_id, sku_id, on_hand, last_ledger_seq, updated_at)"
                          " SELECT store_id, sku_id, opening_qty, 0, opening_as_of FROM store_sku"))
    invalidate()
    return counts


def set_passwords(db: Database, employee_ids: list[str], password: str) -> None:
    if not password:
        return
    with db.write() as conn:
        for e in employee_ids:
            conn.execute(text("UPDATE employee SET password_hash=:h WHERE employee_id=:e"),
                         {"h": hash_password(password), "e": e})
