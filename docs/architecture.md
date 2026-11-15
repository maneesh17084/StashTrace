# StashTrace architecture

## Tiers

| Tier | Stack | Owns |
|---|---|---|
| Frontend | HTML5, CSS, vanilla JS modules (`frontend/`) | Dashboard, filters, count entry, import status, alert detail, investigation screens. No business rules: it renders numbers the API computed. |
| Backend | Python 3.11, FastAPI, Pydantic, SQLAlchemy Core, Alembic, worker process (`backend/`) | API contracts, authentication/authorisation, import validation, stock posting, reconciliation, detection rules, case transitions. |
| Database | SQLite (WAL) via Alembic migrations (`migrations/`) | Master data, raw imports, trusted ledger, count snapshots, reconciliation versions, alert evidence, append-only history. |

HTML/CSS/JavaScript → FastAPI → SQLite. SQLite is a file, not a separate database server; it needs
no installation beyond Python.

The browser calls `/api/v1` on the same origin that serves `/` and `/static`. Batch imports and
detection runs are rows in the `job` table, so they keep running if the browser closes. Two ways
to process them:

* **Single process (the README's reviewer setup)**: `STASHTRACE_RUN_JOBS_INLINE=true` makes the API
  drain the job queue itself after each upload or detection request.
* **Separate worker**: `py -3.11 -m backend.worker` claims and processes jobs from the same SQLite file.
  This is how a deployment would run it, so a long import never blocks API requests.

```
Browser ──HTTP──> FastAPI (uvicorn) ──┐
                                        ├── SQLite file (WAL), on local disk
optional worker (backend.worker) ──────┘
```

## SQLite instead of PostgreSQL — what changes

| Concern | How it is handled |
|---|---|
| Concurrent writers | SQLite allows one writer. Every write transaction starts with `BEGIN IMMEDIATE` (`db.write()`), which takes the write lock up front. Postings for the same store/SKU are therefore serialised, and no transaction can deadlock upgrading a read lock. Readers continue in parallel thanks to WAL. |
| Long imports vs. API latency | Imports commit per chunk (`STASHTRACE_IMPORT_CHUNK`, default 5,000 rows), so the write lock is held for well under a second at a time. |
| Foreign keys | `PRAGMA foreign_keys=ON` on every connection. |
| Append-only history | Triggers (migration 0002) abort UPDATE/DELETE on the ledger, raw rows, row results, counts, reconciliations, alerts, evidence and investigation events. |
| Derived-value integrity | CHECK constraints enforce the stock equation and the variance on every reconciliation row. |
| Network storage | Not supported by WAL; the volume must be local disk. |
| Scaling limit | A single host. Moving to PostgreSQL needs the same SQL (no SQLite-specific SQL outside `db.py`, the triggers and `json_each` in two read queries). |

## Trusted ledger vs. raw input

```
upload ─> import_batch + import_raw_row (as received, untrusted)
            │ worker: validate each row against master data at the EVENT time
            ├─ QUARANTINED ─> import_row_result (error code + all errors)   no stock effect
            ├─ DUPLICATE / CONFLICT ─> import_row_result                     no stock effect
            └─ POSTED ─> inventory_movement + stock_balance (+ customer_return) in one transaction
                          └─ late movement? new count_reconciliation version
```

* Idempotency key: `(source_system, source_event_id)`. Same key + same business payload (delivery
  time excluded) = DUPLICATE, returning the earlier outcome. Same key + different payload after a
  POSTED row = CONFLICT. After a QUARANTINED row it is a correction, linked with `corrects_row_id`.
* Validation precedence (first error becomes `error_code`): format → master existence →
  authoritative reference (wrong store is caught even when the submitted store exists) → assortment
  at event time → movement rules (reason, approval, self-approval, return disposition, reversal
  target) → actor role at event time → shift.
* Corrections to posted entries are a linked `REVERSAL` plus a replacement, posted atomically
  (`POST /movements/{id}/reversal`). The original row is never edited.

## Stock equation

```
expected = opening + receipts + transfer_in + restocked_returns − sales − transfer_out + signed approved adjustments
variance = counted − expected
```

Each movement carries `quantity` (units in the event) and `stock_qty` (signed on-hand effect).
Returns that are not restocked, and transit write-offs (`TRANSFER_SHORTAGE`), have `stock_qty = 0`,
so a lost transfer never shows up as a variance at either store. A REVERSAL is bucketed with the
entry it reverses. Counts never post stock. Each reconciliation stores the ledger watermark
(`ledger_seq`) it used. A later posting with `occurred_at <= cutoff` creates a new version.

## Detection

Rules are rows in `rule_definition` (code + version + JSON params, frozen per version). A run
records the rules it used, the ledger watermark and the `as_of` horizon. Findings get a stable
`case_key`, so a rerun records `REDETECTED` in `detection_run_alert` instead of a second case.

| Rule | Grain | Logic (v1) |
|---|---|---|
| `COUNT_TIMING` | count | Cutoff after submission, submission before count, or stock movements between count time and cutoff (beyond 15 min). Routes to timing review; no shrinkage evaluation. |
| `UNEXPLAINED_SHORTAGE` | count | Reliable basis only. Remaining = shortage − units explained by unsuperseded quarantined rows for the same store/SKU before the cutoff. Alert if remaining ≥ 5 units. |
| `REPEATED_NEG_ADJ` | store/SKU window | ≥ 3 discretionary negative adjustments totalling ≥ 6 units within 14 days. |
| `TRANSFER_RECON` | transfer (lines inside) | Write-offs at both ends, or exceeding the shortfall = double count. A shortfall still open after expected arrival + 7 days = overdue. One case per transfer, linking both ends. |

## Investigation workflow

`OPEN → ASSIGNED → IN_REVIEW → RESOLVED`, enforced in `catalog.INVESTIGATION_TRANSITIONS`.
Every change is an append-only `investigation_event` with the actor, the role that authorised it,
the time and any note. `investigation_state` is a projection used for optimistic concurrency
(`expected_seq`). Assigning needs `ASSIGN_CASE`, and the assignee must hold `INVESTIGATE`.
Resolving needs `RESOLVE_CASE`, a resolution code and a reason of at least 10 characters. A
write-off needs `APPROVE_ADJUSTMENT` too, and is posted as a separate `INVESTIGATION_WRITE_OFF`
ledger entry, through normal validation, in the same transaction as the event.

## Authorisation

Bearer tokens (`/auth/login`, PBKDF2 passwords, hashed tokens). Permissions come from
effective-dated `role_assignment` rows: the API checks them at request time, and import validation
checks them at the event's `occurred_at`. Store-scoped users get `403` when they pass another
store's ID as a filter, and `404` when they fetch another store's record by ID. Uploads from
store-scoped users can only post rows for their own stores.
