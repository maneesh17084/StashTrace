"""Render docs/data_dictionary.md (and docs/schema.json) from backend/app/models.py.

    python -m data_tools.build_data_dictionary
"""
from __future__ import annotations

import json
from pathlib import Path

from sqlalchemy import CheckConstraint, ForeignKeyConstraint, UniqueConstraint

from backend.app import catalog as C
from backend.app.models import metadata

OUT = Path(__file__).resolve().parents[1] / "docs"

RELATIONSHIPS = [
    ("Store", "StoreSKU", "1:N", "A store carries many SKUs (bridge for Store N:M SKU)", "active_from / active_to"),
    ("SKU", "StoreSKU", "1:N", "A SKU is carried by many stores", "active_from / active_to"),
    ("Store (warehouse)", "StoreSKU.source_warehouse_id", "1:N, optional", "DC that replenishes a store assortment", "–"),
    ("Employee", "RoleAssignment", "1:N", "Effective-dated grants; NULL store = all stores", "effective_from / effective_to"),
    ("EmployeeRole", "RoleAssignment", "1:N", "Role granted", "–"),
    ("EmployeeRole", "RolePermission", "1:N", "Bridge EmployeeRole N:M action", "–"),
    ("Store", "Shift", "1:N", "Staffed windows; postings must fall inside one", "start_at / end_at"),
    ("Promotion", "PromotionScope", "1:N", "Bridge Promotion N:M StoreSKU", "start_date / end_date"),
    ("Store", "SourceDocument", "1:N", "PO / POS batch; the authority for wrong-store checks", "business_date"),
    ("Transfer", "TransferLine", "1:N", "One line per SKU", "–"),
    ("Store", "Transfer", "1:N x2", "from_store_id and to_store_id (distinct)", "created_at, expected_arrival_at"),
    ("StoreSKU", "InventoryMovement", "1:N", "Ledger entries for a store/SKU", "occurred_at, received_at, posted_at"),
    ("Employee", "InventoryMovement", "1:N (actor), 1:N optional (approver)", "Who posted / approved", "role effective at occurred_at"),
    ("Shift", "InventoryMovement", "1:N, optional", "Shift covering the event (NULL only for internal write-offs)", "–"),
    ("SourceDocument", "InventoryMovement", "1:N, optional", "Receipts (PO) and sales/returns (POS)", "–"),
    ("Transfer", "InventoryMovement", "1:N, optional", "Dispatch leg, receipt leg, transit write-offs", "–"),
    ("InventoryMovement", "InventoryMovement", "1:0..1", "REVERSAL -> reversed entry (unique)", "–"),
    ("InventoryMovement (RETURN)", "Return", "1:1", "Return record; optional link to original SALE (N:1)", "returned_at"),
    ("ImportBatch", "ImportRawRow", "1:N", "Untrusted rows as received", "submitted_at"),
    ("ImportRawRow", "ImportRowResult", "1:1", "Outcome; corrects_row_id links a correction to the quarantined original", "processed_at"),
    ("ImportRowResult", "InventoryMovement", "1:0..1", "Only POSTED rows create ledger entries", "–"),
    ("StoreSKU", "CycleCount", "1:N", "Physical count snapshots", "counted_at, count_cutoff_at, submitted_at"),
    ("CycleCount", "CountReconciliation", "1:N", "Versions; highest = current", "created_at"),
    ("RuleDefinition", "ShrinkageAlert", "1:N", "Rule code + version that fired", "effective_from"),
    ("DetectionRun", "ShrinkageAlert", "N:M via DetectionRunAlert", "First detection and every re-detection", "requested_at"),
    ("ShrinkageAlert", "AlertEvidence", "1:N", "Links to movements, counts, reconciliations, import rows, transfer lines", "–"),
    ("Investigation", "ShrinkageAlert", "1:N (1:1 in practice)", "Case opened for the alert's stable case key", "opened_at"),
    ("Investigation", "InvestigationEvent", "1:N", "Append-only history; InvestigationState is the projection", "occurred_at"),
    ("InvestigationEvent", "InventoryMovement", "1:0..1", "Write-off approved with the resolution", "–"),
]

LIFECYCLES = [
    ("Store", "ACTIVE → CLOSED (closed_on set)"),
    ("SKU", "ACTIVE → DISCONTINUED"),
    ("StoreSKU", "active from active_from; inactive at/after active_to"),
    ("Employee / RoleAssignment", "ACTIVE → TERMINATED; assignments end at effective_to (authority checked at event time)"),
    ("ImportBatch", "RECEIVED → PROCESSING → COMPLETED | FAILED (FAILED → RECEIVED on retry; processing resumes)"),
    ("Import row", "QUARANTINED → (new linked row) POSTED | QUARANTINED; DUPLICATE / CONFLICT are terminal"),
    ("Transfer (derived)", "created → dispatched (TRANSFER_OUT) → received / partially received (TRANSFER_IN) → reconciled "
                           "(shortfall written off once) | discrepancy (overdue or double-counted)"),
    ("CountReconciliation", "v1 COUNT_SUBMITTED → vN LATE_MOVEMENT; basis RELIABLE | TIMING_REVIEW per version"),
    ("DetectionRun", "QUEUED → RUNNING → COMPLETED | FAILED"),
    ("Investigation", "OPEN → ASSIGNED → IN_REVIEW → RESOLVED (reassignment allowed in ASSIGNED / IN_REVIEW; no reopen)"),
    ("ShrinkageAlert", "OPEN while its investigation is not RESOLVED; CLOSED afterwards (derived, alert row immutable)"),
]


def main() -> None:
    OUT.mkdir(exist_ok=True)
    schema = {}
    L = ["# StashTrace data dictionary", "",
         "Generated from `backend/app/models.py` by `py -3.11 -m data_tools.build_data_dictionary` — do not edit by hand.", "",
         "Conventions: timestamps are TEXT `YYYY-MM-DDTHH:MM:SSZ` (UTC); dates `YYYY-MM-DD`; money INTEGER cents; "
         "quantities INTEGER units. Public IDs are zero-padded, immutable and identical across files and tables. "
         "Tables marked *append-only* reject UPDATE/DELETE via triggers.", "",
         "## ID conventions", "", "| Entity | Pattern | Allocated by |", "|---|---|---|"]
    alloc = {"movement": "source feed or backend", "count": "source feed or backend", "alert": "backend",
             "investigation": "backend", "recon": "backend", "batch": "backend", "run": "backend", "return": "source feed"}
    for k, pat in C.ID_PATTERNS.items():
        L.append(f"| {k} | `{pat.pattern}` | {alloc.get(k, 'master data')} |")
    L += ["", "## Relationships and cardinality", "", "| Parent | Child | Cardinality | Meaning | Effective dates |",
          "|---|---|---|---|---|"]
    L += [f"| {a} | {b} | {c} | {d} | {e} |" for a, b, c, d, e in RELATIONSHIPS]
    L += ["", "## Lifecycle states", "", "| Entity | States / transitions |", "|---|---|"]
    L += [f"| {a} | {b} |" for a, b in LIFECYCLES]
    L += ["", "## Tables", ""]
    for t in metadata.tables.values():  # declaration order: masters first, then events
        info = t.info
        pk = [c.name for c in t.primary_key.columns]
        uniques = sorted(", ".join(c.name for c in u.columns) for u in t.constraints if isinstance(u, UniqueConstraint))
        uniques += [c.name for c in t.columns if c.unique]
        uniques = list(dict.fromkeys(uniques))
        fks = []
        for c in t.foreign_key_constraints:
            fks.append(f"({', '.join(c.column_keys)}) → {c.referred_table.name}({', '.join(e.column.name for e in c.elements)})")
        fks.sort()
        checks = sorted(str(c.sqltext) for c in t.constraints if isinstance(c, CheckConstraint))
        L += [f"### `{t.name}` — {info.get('entity', t.name)}{' *(append-only)*' if info.get('append_only') else ''}", "",
              info.get("desc", ""), "", f"- Primary key: `{', '.join(pk)}`"]
        if uniques:
            L.append(f"- Unique: {'; '.join(f'`{u}`' for u in uniques)}")
        if fks:
            L.append(f"- Foreign keys: {'; '.join(f'`{x}`' for x in fks)}")
        if checks:
            L.append(f"- Checks: {'; '.join(f'`{x}`' for x in checks)}")
        L += ["", "| Field | Type | Required | Units | Allowed values | Validation | Description |", "|---|---|---|---|---|---|---|"]
        cols = []
        for c in t.columns:
            i = c.info
            req = "PK" if c.primary_key else ("yes" if not c.nullable else "optional")
            uq = " (unique)" if c.unique else ""
            L.append(f"| `{c.name}` | {c.type} | {req}{uq} | {i.get('units', '')} | {i.get('allowed', '')} | "
                     f"{i.get('validation', '')} | {i.get('desc', '')} |")
            cols.append({"name": c.name, "type": str(c.type), "nullable": c.nullable, "primary_key": c.primary_key,
                         "unique": bool(c.unique), **i})
        L.append("")
        schema[t.name] = {"entity": info.get("entity"), "description": info.get("desc"), "append_only": bool(info.get("append_only")),
                          "primary_key": pk, "unique": uniques, "foreign_keys": fks, "checks": checks, "columns": cols}
    L += ["## Import error codes (precedence order)", "", "| Code | Meaning |", "|---|---|"]
    L += [f"| `{k}` | {v} |" for k, v in C.ERROR_CODES.items()]
    L += ["", "## Adjustment reasons", "", "| Reason | Required sign | Store stock effect |", "|---|---|---|"]
    for k, v in C.ADJUSTMENT_REASONS.items():
        L.append(f"| `{k}` | {'+' if v > 0 else ('−' if v < 0 else 'either')} | {'none (transit write-off)' if k in C.ZERO_STOCK_REASONS else 'signed quantity'} |")
    L += ["", "## Roles and permissions", "", "| Role | Scope | Actions |", "|---|---|---|"]
    L += [f"| `{k}` | {v['scope']} | {', '.join(v['actions'])} |" for k, v in C.ROLES.items()]
    L += ["", "## Input file contracts", "",
          "`movements_input.csv` columns: " + ", ".join(f"`{c}`" for c in __import__('backend.app.services.posting', fromlist=['x']).MOVEMENT_FIELDS),
          "", "`counts_input.csv` columns: " + ", ".join(f"`{c}`" for c in __import__('backend.app.services.posting', fromlist=['x']).COUNT_FIELDS),
          "", "Master CSVs use exactly the column names of their tables above.", ""]
    (OUT / "data_dictionary.md").write_text("\n".join(L), encoding="utf-8")
    (OUT / "schema.json").write_text(json.dumps(schema, indent=2), encoding="utf-8")
    print(f"wrote {OUT / 'data_dictionary.md'} ({len(schema)} tables)")


if __name__ == "__main__":
    main()
