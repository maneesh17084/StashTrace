# Detection evaluation

Dataset `data\full`, database `var\full.db`.

## Per-rule quality

| Rule | Grain | TP | FP | FN | Precision | Recall | FPR |
|---|---|---|---|---|---|---|---|
| UNEXPLAINED_SHORTAGE | count | 589 | 75 | 3 | 0.887 | 0.9949 | 0.0039 |
| COUNT_TIMING | count | 240 | 0 | 0 | 1.0 | 1.0 | 0.0 |
| COUNT_QUARANTINE | count | 40 | 0 | 0 | 1.0 | 1.0 | 0.0 |
| TRANSFER_RECON | transfer | 533 | 0 | 0 | 1.0 | 1.0 | 0.0 |
| REPEATED_NEG_ADJ | adjustment window | 80 | 11 | 0 | 0.8791 | 1.0 | 0.002 |
| quarantine ACTOR_NOT_AUTHORIZED | movement | 150 | 0 | 0 | 1.0 | 1.0 | - |
| quarantine ADJUSTMENT_REASON_MISSING | movement | 600 | 0 | 0 | 1.0 | 1.0 | - |
| quarantine WRONG_STORE_REFERENCE | movement | 600 | 0 | 0 | 1.0 | 1.0 | - |

## Movement scenarios (expected import outcome reached)

| Scenario | n | correct |
|---|---|---|
| ADJUSTMENT_REASON_MISSING | 600 | 600 |
| FEED_REPLAY | 2000 | 2000 |
| PAYLOAD_CONFLICT | 100 | 100 |
| POS_RERING_REVERSAL | 200 | 200 |
| ROLE_OUTSIDE_EFFECTIVE_DATES | 150 | 150 |
| WRONG_STORE | 600 | 600 |

Baseline rows posted cleanly: 993,440. Unexpected non-posted rows: none.

## Count scenarios -> predicted decision

| Scenario | predicted |
|---|---|
| BASELINE | NO_ALERT: 15547 |
| BENIGN_NOISE | NO_ALERT: 2817, SHRINKAGE_ALERT: 3 |
| BOUNDARY_ABOVE_THRESHOLD | SHRINKAGE_ALERT: 60 |
| BOUNDARY_AT_THRESHOLD | NO_ALERT: 1, SHRINKAGE_ALERT: 59 |
| BOUNDARY_BELOW_THRESHOLD | NO_ALERT: 53, SHRINKAGE_ALERT: 7 |
| COUNT_BY_UNAUTHORIZED_ACTOR | QUARANTINE: 40 |
| COUNT_TIMING_CONFLICT | TIMING_REVIEW: 240 |
| LATE_MOVEMENT_BENIGN | NO_ALERT: 239 |
| MISCOUNT_MISSED_LOCATION | NO_ALERT: 16, SHRINKAGE_ALERT: 64 |
| QUARANTINE_EXPLAINED | NO_ALERT: 377, SHRINKAGE_ALERT: 1 |
| UNEXPLAINED_LOSS | NO_ALERT: 2, SHRINKAGE_ALERT: 438 |
| UNEXPLAINED_LOSS_CARRYOVER | NO_ALERT: 4, SHRINKAGE_ALERT: 32 |

Reconciliation arithmetic vs generator oracle: 19,960/19,960 counts match.
Late-movement counts re-versioned: 239/239.

## Transfer scenarios -> predicted

| Scenario | predicted |
|---|---|
| TRANSFER_DELAYED_WITHIN_GRACE | NO_ALERT: 376 |
| TRANSFER_DOUBLE_COUNT | TRANSFER_DOUBLE_COUNT: 282 |
| TRANSFER_IN_TRANSIT_AT_END | NO_ALERT: 173 |
| TRANSFER_NORMAL | NO_ALERT: 14108 |
| TRANSFER_SHORT_UNRESOLVED | TRANSFER_OVERDUE_DISCREPANCY: 251 |
| TRANSFER_SHORT_WRITTEN_OFF_ONCE | NO_ALERT: 459 |

Alert-type agreement on true transfer positives: 1.0.
