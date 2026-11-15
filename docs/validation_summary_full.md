# Validation summary — full

Overall: **PASS** (67/67 checks)

| Area | Check | Checked | Failures | Status |
|---|---|---|---|---|
| files | primary key unique: store.store_id | 100 | 0 | PASS |
| files | primary key unique: sku.sku_id | 5,000 | 0 | PASS |
| files | primary key unique: store_sku(store_id, sku_id) | 65,786 | 0 | PASS |
| files | primary key unique: employee.employee_id | 1,024 | 0 | PASS |
| files | primary key unique: role_assignment.assignment_id | 1,116 | 0 | PASS |
| files | primary key unique: shift.shift_id | 36,200 | 0 | PASS |
| files | primary key unique: promotion.promotion_id | 120 | 0 | PASS |
| files | primary key unique: promotion_scope | 7,675 | 0 | PASS |
| files | primary key unique: source_document.doc_id | 34,290 | 0 | PASS |
| files | primary key unique: transfer.transfer_id | 15,735 | 0 | PASS |
| files | primary key unique: transfer_line(transfer_id, sku_id) | 53,983 | 0 | PASS |
| files | primary key unique: count input count_id | 20,000 | 0 | PASS |
| files | primary key unique: ground truth gt_id | 39,379 | 0 | PASS |
| files | foreign keys: store_sku -> store, sku, source warehouse | 65,786 | 0 | PASS |
| files | foreign keys: employee -> home store | 1,024 | 0 | PASS |
| files | foreign keys: role_assignment -> employee, role, store | 1,116 | 0 | PASS |
| files | foreign keys: shift -> store | 36,200 | 0 | PASS |
| files | foreign keys: promotion_scope -> promotion, store_sku | 7,675 | 0 | PASS |
| files | foreign keys: source_document -> store | 34,290 | 0 | PASS |
| files | foreign keys: transfer -> stores (distinct ends) | 15,735 | 0 | PASS |
| files | foreign keys: transfer_line -> transfer, assortment at both ends | 53,983 | 0 | PASS |
| files | allowed values: store.store_type | 100 | 0 | PASS |
| files | allowed values: sku.category | 5,000 | 0 | PASS |
| files | allowed values: sku.shrink_risk | 5,000 | 0 | PASS |
| files | allowed values: shift.shift_type | 36,200 | 0 | PASS |
| files | allowed values: promotion.promo_type | 120 | 0 | PASS |
| files | allowed values: source_document.doc_type | 34,290 | 0 | PASS |
| files | allowed values: movement.movement_type | 1,000,000 | 0 | PASS |
| files | allowed values: movement.disposition | 1,000,000 | 0 | PASS |
| files | allowed values: count.count_type | 20,000 | 0 | PASS |
| files | ID formats (movement, count, store, sku) | 1,020,000 | 0 | PASS |
| files | date ordering: store_sku active_to > active_from | 65,786 | 0 | PASS |
| files | date ordering: role_assignment effective_to > effective_from | 1,116 | 0 | PASS |
| files | date ordering: shift end_at > start_at | 36,200 | 0 | PASS |
| files | date ordering: promotion end_date >= start_date | 120 | 0 | PASS |
| files | date ordering: transfer expected_arrival >= created | 15,735 | 0 | PASS |
| files | date ordering: movement received_at >= occurred_at | 1,000,000 | 0 | PASS |
| files | date ordering: count submitted_at >= counted_at (except labelled SUBMIT_EARLY) | 20,000 | 0 | PASS |
| files | date ordering: movement sorted by received_at (feed order) | 999,999 | 0 | PASS |
| files | repeated event keys are all labelled (replay/conflict/correction) | 997,090 | 0 | PASS |
| files | exact duplicate rows == labelled feed replays | 1,000,000 | 0 | PASS |
| files | unlabelled movement rows have valid references (store/doc/transfer/shift/actor/reason) | 1,000,000 | 0 | PASS |
| files | lifecycle: transfer receipt follows dispatch | 53,731 | 0 | PASS |
| files | lifecycle: return follows its original sale | 13,988 | 0 | PASS |
| files | lifecycle: leavers' role assignments end by termination | 40 | 0 | PASS |
| files | ground truth not leaked into input columns | 29 | 0 | PASS |
| files | ground truth rows reference existing inputs | 39,379 | 0 | PASS |
| files | volume: movement inputs == profile target | 1,000,000 | 0 | PASS |
| files | volume: count inputs == profile target | 20,000 | 0 | PASS |
| database | SQLite integrity_check | 1 | 0 | PASS |
| database | foreign_key_check | 996,550 | 0 | PASS |
| database | ledger rebuild == cached stock_balance | 65,786 | 0 | PASS |
| database | stock equation holds on every reconciliation | 20,340 | 0 | PASS |
| database | current reconciliations recompute from the ledger | 19,960 | 0 | PASS |
| database | no count is missing a late movement (re-versioning complete) | 19,960 | 0 | PASS |
| database | quarantined/duplicate/conflict rows never reach the ledger | 3,490 | 0 | PASS |
| database | every ledger row traces to a POSTED import row | 996,550 | 0 | PASS |
| database | batch counters add up | 357 | 0 | PASS |
| database | no duplicate ledger events (source key) or double reversals | 996,550 | 0 | PASS |
| database | transfer legs posted only by the referenced end | 107,941 | 0 | PASS |
| database | adjustments carry reason and an approver other than the actor | 13,661 | 0 | PASS |
| database | every posting's actor role was effective at the event time and store | 996,550 | 0 | PASS |
| database | every posting with a shift is inside that shift at that store | 996,550 | 0 | PASS |
| database | case state == last event; event seq gapless | 1,528 | 0 | PASS |
| database | every alert has rule version, run, case and evidence | 1,528 | 0 | PASS |
| database | no shrinkage alert on a count under timing review | 664 | 0 | PASS |
| database | every movement input row has exactly one outcome | 1,000,000 | 0 | PASS |
