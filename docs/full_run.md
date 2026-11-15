# Full-volume run

Profile `full`, seed `20261022`, generator 1.0.0. Command: `python -m data_tools.generate --profile full --seed 20261022 --out data/full`.

## Volumes and import

Hardware: Intel64 Family 6 Model 142 Stepping 10, GenuineIntel, 8 logical CPUs, Windows-10-10.0.26300-SP0, Python 3.11.0 (shared machine; other workloads were running).

| | Received | Posted (trusted) | Quarantined | Duplicate | Conflict |
|---|---|---|---|---|---|
| movements | 1,000,000 | 996,550 | 1,350 | 2,000 | 100 |
| counts | 20,000 | 19,960 | 40 | 0 | 0 |

* Masters loaded in 6.9 s: store 100, sku 5,000, store_sku 65,786, employee 1,024, role_assignment 1,116, shift 36,200, promotion 120, promotion_scope 7,675, source_document 34,290, transfer 15,735, transfer_line 53,983.
* Day-by-day replay through the import pipeline: 357 batches in 941.9 s (1,062 movement rows/s including validation, posting, balance updates and reconciliation).
* Late arrivals created 380 additional reconciliation versions.
* Store `COUNT_CORRECTION` write-offs in the feed: 3,453.
* Detection run 1: 1,528 alerts in 6.9 s. Rerun: 0 new, 1,528 re-detected (5.9 s). Stable case keys prevent duplicate cases.
* Database rows: inventory_movement 996,550, import_row_result 1,020,000, cycle_count 19,960, count_reconciliation 20,340, customer_return 18,612, shrinkage_alert 1,528, alert_evidence 11,358, investigation 1,528.

## Validation

**PASS**: 67/67 checks on files and database ([full table](validation_summary_full.md)). Highlights:

* unlabelled movement rows have valid references (store/doc/transfer/shift/actor/reason): 1,000,000 checked, 0 failures
* foreign_key_check: 996,550 checked, 0 failures
* ledger rebuild == cached stock_balance: 65,786 checked, 0 failures
* current reconciliations recompute from the ledger: 19,960 checked, 0 failures
* no count is missing a late movement (re-versioning complete): 19,960 checked, 0 failures
* quarantined/duplicate/conflict rows never reach the ledger: 3,490 checked, 0 failures
* every posting's actor role was effective at the event time and store: 996,550 checked, 0 failures

## Detection quality (held-out ground truth)

Labels come from the planted cause; the application never saw them. Grain per rule as shown.

| Rule | Grain | TP | FP | FN | Precision | Recall | False-positive rate |
|---|---|---|---|---|---|---|---|
| UNEXPLAINED_SHORTAGE | count | 589 | 75 | 3 | 0.887 | 0.9949 | 0.0039 |
| COUNT_TIMING | count | 240 | 0 | 0 | 1.0 | 1.0 | 0.0 |
| COUNT_QUARANTINE | count | 40 | 0 | 0 | 1.0 | 1.0 | 0.0 |
| TRANSFER_RECON | transfer | 533 | 0 | 0 | 1.0 | 1.0 | 0.0 |
| REPEATED_NEG_ADJ | store/SKU window | 80 | 11 | 0 | 0.8791 | 1.0 | 0.002 |
| import: ACTOR_NOT_AUTHORIZED | movement | 150 | 0 | 0 | 1.0 | 1.0 | – |
| import: ADJUSTMENT_REASON_MISSING | movement | 600 | 0 | 0 | 1.0 | 1.0 | – |
| import: WRONG_STORE_REFERENCE | movement | 600 | 0 | 0 | 1.0 | 1.0 | – |

* Reconciliation arithmetic matches the generator's independent ledger for 19,960/19,960 counts.
* Late-movement counts re-versioned: 239/239; none raised an alert.
* Baseline movement rows posted cleanly: 993,440; unexpected non-posted rows: none.

Missed planted cases and benign cases flagged, by scenario:

| Count scenario | Predicted |
|---|---|
| BASELINE | NO_ALERT 15547 |
| BENIGN_NOISE | NO_ALERT 2817, SHRINKAGE_ALERT 3 |
| BOUNDARY_ABOVE_THRESHOLD | SHRINKAGE_ALERT 60 |
| BOUNDARY_AT_THRESHOLD | NO_ALERT 1, SHRINKAGE_ALERT 59 |
| BOUNDARY_BELOW_THRESHOLD | NO_ALERT 53, SHRINKAGE_ALERT 7 |
| COUNT_BY_UNAUTHORIZED_ACTOR | QUARANTINE 40 |
| COUNT_TIMING_CONFLICT | TIMING_REVIEW 240 |
| LATE_MOVEMENT_BENIGN | NO_ALERT 239 |
| MISCOUNT_MISSED_LOCATION | NO_ALERT 16, SHRINKAGE_ALERT 64 |
| QUARANTINE_EXPLAINED | NO_ALERT 377, SHRINKAGE_ALERT 1 |
| UNEXPLAINED_LOSS | NO_ALERT 2, SHRINKAGE_ALERT 438 |
| UNEXPLAINED_LOSS_CARRYOVER | NO_ALERT 4, SHRINKAGE_ALERT 32 |

| Transfer scenario | Predicted |
|---|---|
| TRANSFER_DELAYED_WITHIN_GRACE | NO_ALERT 376 |
| TRANSFER_DOUBLE_COUNT | TRANSFER_DOUBLE_COUNT 282 |
| TRANSFER_IN_TRANSIT_AT_END | NO_ALERT 173 |
| TRANSFER_NORMAL | NO_ALERT 14108 |
| TRANSFER_SHORT_UNRESOLVED | TRANSFER_OVERDUE_DISCREPANCY 251 |
| TRANSFER_SHORT_WRITTEN_OFF_ONCE | NO_ALERT 459 |

## API latency

In-process ASGI client against the full database, 30 requests per endpoint.

| Endpoint | p50 ms | p95 ms |
|---|---|---|
| GET /dashboard/summary (all stores) | 326.8 | 408.9 |
| GET /dashboard/summary (one store) | 275.0 | 359.3 |
| GET /alerts (open queue, page 1) | 56.6 | 71.4 |
| GET /alerts/{id} | 24.7 | 50.2 |
| GET /reconciliations (store filter) | 37.8 | 57.4 |
| GET /reconciliations/{id} | 39.6 | 60.2 |
| GET /movements (store + sku) | 33.5 | 84.0 |
| GET /imports | 41.0 | 50.7 |
| GET /investigations (open) | 98.6 | 200.1 |

## Reproducibility

Generated twice with seed 20261022: **identical** (19 files, SHA-256 compared; [details](reproducibility_full.md)). Generation time per run: 146.7 s / 130.4 s.

## Backup and restore

Online backup 16.5 s, restore 20.5 s; restored copy integrity `ok`, 0 FK violations, row counts and ledger/balance checksums identical: **True**.
