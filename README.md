# StashTrace — inventory reconciliation and shrinkage investigation

StashTrace finds unusual inventory loss by comparing receipts, sales, transfers, adjustments, returns
and physical counts. Every expected-stock number can be traced back to ledger rows. Shrinkage is
kept apart from data-quality problems, and each alert goes through an investigation workflow with
full history.

**Architecture**: HTML/CSS/JavaScript → FastAPI → SQLite. SQLite is a file, not a separate database
server, so Python is the only thing to install. Details: [docs/architecture.md](docs/architecture.md).

## Quick start (Windows, PowerShell, Python 3.11)

Run these from the repository root (the folder that contains this README):

```powershell
# 1. Install the Python packages the app and the tests need (FastAPI, SQLAlchemy, Alembic, pytest, ...).
#    --user installs them for your Windows account only, so no admin rights are needed.
py -3.11 -m pip install --user -r requirements-dev.txt

# 2. Tell StashTrace where its database lives. SQLite is a single file, created at var\stashtrace.db.
#    These settings last only for this PowerShell window; set them again in every new window.
$env:STASHTRACE_DB_URL = "sqlite:///var/stashtrace.db"

# 3. Make the web server process uploads and detection runs itself, so you need only one terminal.
#    (Without this, queued work waits for the separate worker: py -3.11 -m backend.worker)
$env:STASHTRACE_RUN_JOBS_INLINE = "true"

# 4. Write the demo data as CSV files into data\demo: 2 stores, 1 SKU, employees, shifts,
#    a feed of stock movements (including deliberately bad rows) and 3 physical counts.
py -3.11 -m data_tools.demo_seed --out data/demo

# 5. Build the demo database from those CSVs. In order, this:
#    creates the tables (Alembic migrations), loads the master data, imports the movements and counts
#    through the same validation the app uses, reconciles every count, runs shrinkage detection,
#    and sets the demo password for the demo accounts.
#    --fresh deletes any existing var\stashtrace.db first, so re-running this resets the demo.
py -3.11 -m data_tools.load --data data/demo --db var/stashtrace.db --fresh --password stashtrace-demo

# 6. Start the web server (API and web pages on the same address). Leave this window open;
#    press Ctrl+C to stop it.
py -3.11 -m uvicorn backend.app.main:app --host 127.0.0.1 --port 8000
```

Open <http://127.0.0.1:8000> and sign in as **EMP-000005** with password **stashtrace-demo**. This is
a seeded demo credential for the operations manager, who can see every store.

**Next time**: steps 1, 4 and 5 are one-off. To start the app again, open PowerShell in the repository
root and run only the server, with its two settings:

```powershell
# Point at the existing demo database and process jobs in the server, then start it.
$env:STASHTRACE_DB_URL = "sqlite:///var/stashtrace.db"
$env:STASHTRACE_RUN_JOBS_INLINE = "true"
py -3.11 -m uvicorn backend.app.main:app --host 127.0.0.1 --port 8000
```

**Run the tests** (works in any PowerShell window; each test builds its own temporary database and
never touches `var\stashtrace.db`):

```powershell
# Run the full automated test suite; -q prints one dot per passing test and a summary line.
py -3.11 -m pytest -q
```

## Reviewer walkthrough

What each screen should show on a freshly loaded demo:

| Reviewer action | Expected result |
|---|---|
| Sign in as EMP-000005 and open **Dashboard** | 3 counts reconciled (1 in timing review), 3 open cases, 4 quarantined rows, 5 units (US$122.50) of open unexplained loss |
| **Alerts** → the `UNEXPLAINED_SHORTAGE` alert | 79 expected, 74 counted, 5 units short; the stock calculation, the rule version and threshold, and linked evidence (reconciliation, count, contributing movements) |
| Open the other two alerts | `COUNT_TIMING_REVIEW` is a data-quality finding with no shrinkage alert; `TRANSFER_DOUBLE_COUNT` is one case linking the dispatch, the receipt and the write-offs at both ends |
| **Imports** → open a batch with quarantined rows | Wrong-store, missing-reason, expired-role and self-approval rows are quarantined with error codes and never change inventory. *Correct…* resubmits a fixed row, linked to the original |
| Open the linked **Case** from any alert | Event history (actor, role, time). Assign, start review, add a finding, and resolve. Resolution needs an authorised role and a reason; an approved write-off posts a separate ledger entry |
| **Reconciliations** → the STORE-000001 count | The full stock equation, the ledger watermark, the version history and every contributing movement |

The worked example behind the shortage (`STORE-000001 / SKU-000001`, count at 2026-03-06 06:00 UTC):

```
opening 100 + receipt 20 − sales 30 − dispatch 10 + restocked return 2 + approved damage −3 = expected 79
physical count 74  →  variance −5  →  meets the 5-unit threshold  →  UNEXPLAINED_SHORTAGE alert
```

The damaged-return unit has no stock effect, and the damage write-off is not deducted a second time.

**Count Entry gotcha**: the form defaults to today's date, but the demo's shifts cover
2026-03-01 to 2026-03-10 only. To submit a count, sign in as **EMP-000011** (stock clerk, same
password), choose STORE-000001 and SKU-000001, set the date to **2026-03-06** and the time to
**06:00**, pick the OPEN shift (05:00–13:00 UTC), and enter a quantity. The result links to the new
reconciliation.

### Negative scenarios in the demo feed

| Scenario | Outcome |
|---|---|
| Sale submitted for STORE-000002 against STORE-000001's POS batch | Quarantined `WRONG_STORE_REFERENCE` (the submitted store exists) |
| Adjustment with no reason | Quarantined `ADJUSTMENT_REASON_MISSING`; can be corrected through a linked resubmission |
| Count whose cutoff is 14 h after it was taken and after submission | Reconciliation basis `TIMING_REVIEW`, data-quality alert, **no** shrinkage alert |
| Transfer shortfall written off at both ends | One `TRANSFER_DOUBLE_COUNT` case linking both ends |
| Receipt by a clerk whose role ended | Quarantined `ACTOR_NOT_AUTHORIZED` |
| Manager approving their own adjustment | Quarantined `SELF_APPROVAL` |
| Feed retry / same ID with a different quantity | `DUPLICATE` (no effect) / `CONFLICT` |

## Demo accounts and permissions

Every human account below signs in with the password given to `data_tools.load --password`
(`stashtrace-demo` in the Quick start). Permissions come from effective-dated role assignments and are
checked on the server for every request. A store-scoped user cannot see or change another store's
records, even by typing its ID into the URL.

| Employee ID | Name | Role | Stores | Can sign in? |
|---|---|---|---|---|
| EMP-000004 | Ada Admin | Administrator | All | Yes |
| EMP-000005 | Omar Ops | Operations manager | All | Yes (recommended for reviewing) |
| EMP-000006 | Ana Analyst | Merchandising / e-commerce analyst | All stores. Can view, export, and run detection, but cannot manage cases. | Yes |
| EMP-000007 | Lee Prevention | Loss prevention investigator | STORE-000001, STORE-000002 (region R01) | Yes |
| EMP-000010 | Morgan Manager | Store manager | STORE-000001 | Yes |
| EMP-000011 | Casey Clerk | Stock clerk | STORE-000001 | Yes |
| EMP-000012 | Riley Cashier | Cashier | STORE-000001 | Yes |
| EMP-000020 | Jordan Manager | Store manager | STORE-000002 | Yes |
| EMP-000021 | Taylor Clerk | Stock clerk | STORE-000002 | Yes |
| EMP-000013 | Former Clerk | Stock clerk until 2026-03-03, then left | STORE-000001 | No: terminated; their late receipt in the feed is quarantined `ACTOR_NOT_AUTHORIZED` |
| EMP-000001 | POS integration feed | Service identity (`SERVICE_FEED`) | All | No: posts sales and returns from the POS feed |
| EMP-000002 | WMS integration feed | Service identity (`SERVICE_FEED`) | All | No: posts receipts and transfers from the warehouse system |
| EMP-000003 | StashTrace detection | Service identity (`SYSTEM`) | All | No: opens cases when detection finds something |

What each role can do in the app:

| Action | Admin | Ops manager | Analyst | Loss prevention | Store manager | Stock clerk | Cashier |
|---|---|---|---|---|---|---|---|
| View dashboard, reconciliations, alerts and cases for their stores (store-scoped users see only the import batches they uploaded) | ✓ all | ✓ all | ✓ all | ✓ | ✓ own | ✓ own | ✓ own |
| Export reconciliations to CSV | ✓ | ✓ | ✓ | ✓ | ✓ own | – | – |
| Submit a cycle count (Count entry) | ✓ | – | – | – | ✓ own | ✓ own | – |
| Upload an import file | ✓ | ✓ | – | – | ✓ own | ✓ own rows only | – |
| Correct a quarantined import row | ✓ | ✓ | – | – | ✓ own | – | – |
| Run detection (Alerts → Run detection) | ✓ | ✓ | ✓ | – | – | – | – |
| Assign or reassign a case | ✓ | ✓ | – | ✓ | – | – | – |
| Start review on a case | ✓ | ✓ | – | ✓ | only when assigned to them | – | – |
| Add a finding to a case | ✓ | ✓ | – | ✓ | ✓ own | – | – |
| Resolve a case | ✓ | ✓ | – | ✓ | – | – | – |
| Approve a write-off when resolving | ✓ | ✓ | – | – | – | – | – |
| Reverse a posted movement (with replacement) | ✓ | ✓ | – | – | ✓ own | – | – |

Postings from imported files are also checked against the role the actor held **at the time of the
event**:

| Movement in a feed | Who may post it |
|---|---|
| Sale, return | Cashier at that store, POS service feed |
| Receipt, transfer leg | Stock clerk or store manager at that store, WMS service feed |
| Adjustment | Stock clerk or store manager at that store, **approved by a different person** who holds approval rights (store manager, ops manager, admin) |
| Reversal | Store manager at that store, ops manager, service feeds |

Count entry as EMP-000005 is refused (403) on purpose: operations managers supervise but do not
count stock. Use EMP-000011 for counts (see the gotcha above).

## Full dataset and evaluation

The full dataset is **100% synthetic**, generated by `data_tools/generate.py` with seed **20261022**. No
external data was used, and no personal or confidential data is included.

| | |
|---|---|
| Inventory locations | 100 (92 retail stores + 8 distribution warehouses, one per region) |
| SKUs | 5,000 |
| Movement inputs | 1,000,000 |
| Count inputs | 20,000 |
| Period | 2026-01-01 to 2026-06-30 |

Generate it and run it through the application (about 3 minutes to generate and 16 minutes to load on
the development laptop):

```powershell
# 1. Generate the full synthetic dataset as CSV/JSON/SQL files in data\full (about 3 minutes).
#    The same --seed always produces byte-identical files.
py -3.11 -m data_tools.generate --profile full --seed 20261022 --out data/full

# 2. Load it into its own database, var\full.db, through the real import pipeline, replaying the feed
#    day by day, then run detection (about 16 minutes; progress is logged every simulated week).
#    The ground-truth files are NOT read here.
py -3.11 -m data_tools.load     --data data/full --db var/full.db --fresh --password stashtrace-demo

# 3. Check the files and the database: unique keys, valid references, allowed values, date order,
#    ledger arithmetic, reconciliation versions, role validity at each event, case history.
#    Writes data\full\validation_summary.md.
py -3.11 -m data_tools.validate --data data/full --db var/full.db

# 4. Score detection against the ground truth (the only step that reads it): precision, recall
#    and false-positive rate per rule. Writes data\full\evaluation_report.md.
py -3.11 -m data_tools.evaluate --data data/full --db var/full.db
```

The generated CSV/JSON/SQL files are not committed, because the same command and seed reproduce them
byte for byte. [docs/full_manifest.json](docs/full_manifest.json) is the manifest of the run reported
below: command, parameters, provenance, and row count plus SHA-256 for every file, so a regenerated
copy can be checked against it. `--profile starter` (10 locations, 60,000 movements) is a quicker
alternative.

**Ground truth is for evaluation only.** `ground_truth.csv` / `ground_truth.json` label the planted
scenarios and the expected decision for each. Only `data_tools/evaluate.py` reads them. The loader and
the application never do, and a test hides the files during a load to prove it.

Results of the full run:

* [docs/full_run.md](docs/full_run.md): volumes, import and detection timings, and a summary of everything below
* [docs/validation_summary_full.md](docs/validation_summary_full.md): 67/67 file and database checks pass
* [docs/evaluation_report_full.md](docs/evaluation_report_full.md): precision, recall and false-positive rate per rule, plus missed and falsely flagged cases by scenario
* [docs/reproducibility_full.md](docs/reproducibility_full.md): two generations with the same seed are byte-identical
* [docs/performance.md](docs/performance.md): p50/p95 API latency on the full database (measured on its restored backup copy)
* [docs/backup_restore.json](docs/backup_restore.json): online backup and restore with integrity and checksum comparison
* [docs/data_dictionary.md](docs/data_dictionary.md) and [docs/schema.json](docs/schema.json): every table and field, with type, required/optional, units, allowed values and validation
* [docs/release_validation.md](docs/release_validation.md): each release check mapped to its test or report

How the data is made:

1. **Masters first**: regions, stores and warehouses, SKUs (12 categories, Zipf popularity, log-normal
   costs), effective-dated assortments with opening balances, employees with effective-dated roles
   (including clerks who leave mid-period), shifts, promotions, POS-batch and PO source documents.
2. **Physical simulation per store/SKU**: weekday- and promotion-driven sales capped by what is on the
   shelf; vendor or DC replenishment; customer returns (restocked or not); approved damage/expiry
   write-offs; small unrecorded shrink; cycle counts with miscount noise; and planted hidden theft.
3. **Reporting anomalies** are injected into the feed: the four required edge cases (wrong store,
   adjustment without a reason, count timestamp that contradicts the movement sequence, transfer
   counted as shrinkage at both ends), plus feed replays, payload conflicts, expired roles and POS
   re-rings. A share of them is corrected by a later resubmission.
4. **Derived values**: the generator independently recomputes the ledger the backend should end up
   with. Labels come from the planted cause, for example "hidden loss outstanding ≥ 5 units", not
   from re-running the rules.
5. **Benign look-alikes**: approved damage, returns, transfers in transit or delayed within grace,
   transit losses written off once, late POS uploads, shortages explained by quarantined rows, and
   counts that missed a backroom location.

## Screens and API

| Screen | Route | API |
|---|---|---|
| Dashboard | `#/dashboard` | `GET /api/v1/dashboard/summary` |
| Reconciliations / detail | `#/reconciliations`, `#/reconciliations/{id}` | `GET /api/v1/reconciliations`, `GET /api/v1/reconciliations/{id}` (CSV export too) |
| Cycle count entry | `#/counts/new` | `POST /api/v1/counts` |
| Imports and exceptions | `#/imports`, `#/imports/{id}` | `POST /api/v1/imports`, `GET /api/v1/imports/{id}`, `/rows`, `POST /imports/rows/{row}/correction` |
| Alert queue and detail | `#/alerts`, `#/alerts/{id}` | `GET /api/v1/alerts`, `GET /api/v1/alerts/{id}`, `POST /api/v1/detection-runs` |
| Investigation | `#/investigations/{id}` | `GET /api/v1/investigations/{id}`, `POST /api/v1/investigations/{id}/events` |

Interactive API docs are at `/api/docs`. Filtering and paging happen on the server. Every screen has
loading, empty, error and success states, and status is shown with text as well as colour.

## Repository layout

```
frontend/      index.html, css/, js/api.js (shared fetch client), js/views/*.js (one module per screen)
backend/       app/ (API routes, services, schema, config), worker.py (optional separate job worker)
migrations/    Alembic: 0001 schema, 0002 append-only triggers + reference data, 0003-0004 indexes
data_tools/    generator, demo seed, loader, validator, evaluator, reproducibility, perf, backup/restore, reports
tests/         pytest suite
docs/          architecture, data dictionary, schema, full-run reports, full-data manifest
```

`var/` (databases, backups) and `data/` (generated datasets) are created locally and ignored by Git.

## Tests

`py -3.11 -m pytest -q` runs the full test suite. It covers:

* the end-to-end API flow and each negative scenario
* idempotent replay and conflicts, event-time role checks, and the guarantee that no quarantined row touches stock
* reversal and replacement, count versioning, timing review, the 4/5/6-unit threshold boundary, and benign cases
* double-counted transfers, repeated adjustments, and rerun idempotency
* store-scoped access by direct ID, unauthorised case transitions, and append-only triggers
* 20 concurrent postings to one store/SKU, and a forced failure mid-transaction followed by a retry
* generator determinism and a small generate → load → validate → evaluate pipeline

## Operations

* Every response carries `X-Request-ID` and `X-Response-Time-ms`, and the API logs one line per request.
* `GET /api/v1/health` reports the schema revision and the queued/failed job counts. Failed import
  jobs retry up to 3 times. `POST /api/v1/imports/{id}/retry` resumes from the first unprocessed
  row, so a retry never double-posts.
* Backups: `data_tools.backup_restore` takes an online backup, restores it to a new file, and
  compares row counts and ledger checksums (command below).

## Other useful commands

```powershell
# Run the job worker as a separate process (the deployment setup). Start it in a second window
# with the same STASHTRACE_DB_URL, and do NOT set STASHTRACE_RUN_JOBS_INLINE in the server window.
py -3.11 -m backend.worker

# Generate and load the smaller "starter" dataset (10 locations, 60,000 movements, about 1 minute to load).
py -3.11 -m data_tools.generate --profile starter --seed 20261022 --out data/starter
py -3.11 -m data_tools.load --data data/starter --db var/starter.db --fresh --password stashtrace-demo

# Prove reproducibility: generate a profile twice with the same seed and compare SHA-256 checksums
# of every file. Writes docs\reproducibility_<profile>.md.
py -3.11 -m data_tools.repro --profile starter

# Measure API response times (p50/p95) against a loaded database. Writes docs\performance.md.
py -3.11 -m data_tools.perf --db var/full.db --data data/full --password stashtrace-demo

# Back up a database while it is in use, restore the backup to a new file, and verify the restore.
py -3.11 -m data_tools.backup_restore --db var/stashtrace.db --backup var/backup/stashtrace.db

# Regenerate docs\data_dictionary.md and docs\schema.json from the database schema definition.
py -3.11 -m data_tools.build_data_dictionary

# Apply database migrations to the database in STASHTRACE_DB_URL (data_tools.load already does this).
py -3.11 -m alembic upgrade head
```

To use the app against another database, such as the full one, point `STASHTRACE_DB_URL` at it, for
example `$env:STASHTRACE_DB_URL = "sqlite:///var/full.db"`, and start the server as usual.
