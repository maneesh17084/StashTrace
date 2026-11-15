# StashTrace data dictionary

Generated from `backend/app/models.py` by `py -3.11 -m data_tools.build_data_dictionary` — do not edit by hand.

Conventions: timestamps are TEXT `YYYY-MM-DDTHH:MM:SSZ` (UTC); dates `YYYY-MM-DD`; money INTEGER cents; quantities INTEGER units. Public IDs are zero-padded, immutable and identical across files and tables. Tables marked *append-only* reject UPDATE/DELETE via triggers.

## ID conventions

| Entity | Pattern | Allocated by |
|---|---|---|
| store | `^STORE-\d{6}$` | master data |
| sku | `^SKU-\d{6}$` | master data |
| employee | `^EMP-\d{6}$` | master data |
| assignment | `^RA-\d{6}$` | master data |
| shift | `^SHF-2026-\d{7}$` | master data |
| promotion | `^PRM-2026-\d{6}$` | master data |
| doc | `^(PO-2026-\d{6}|POS-2026-\d{7})$` | master data |
| transfer | `^TRF-2026-\d{6}$` | master data |
| movement | `^MOV-2026-\d{8}$` | source feed or backend |
| return | `^RET-2026-\d{6}$` | source feed |
| count | `^CNT-2026-\d{6}$` | source feed or backend |
| recon | `^REC-2026-\d{6}$` | backend |
| alert | `^SHR-2026-\d{6}$` | backend |
| investigation | `^INV-2026-\d{6}$` | backend |
| batch | `^IMP-2026-\d{6}$` | backend |
| run | `^RUN-2026-\d{6}$` | backend |

## Relationships and cardinality

| Parent | Child | Cardinality | Meaning | Effective dates |
|---|---|---|---|---|
| Store | StoreSKU | 1:N | A store carries many SKUs (bridge for Store N:M SKU) | active_from / active_to |
| SKU | StoreSKU | 1:N | A SKU is carried by many stores | active_from / active_to |
| Store (warehouse) | StoreSKU.source_warehouse_id | 1:N, optional | DC that replenishes a store assortment | – |
| Employee | RoleAssignment | 1:N | Effective-dated grants; NULL store = all stores | effective_from / effective_to |
| EmployeeRole | RoleAssignment | 1:N | Role granted | – |
| EmployeeRole | RolePermission | 1:N | Bridge EmployeeRole N:M action | – |
| Store | Shift | 1:N | Staffed windows; postings must fall inside one | start_at / end_at |
| Promotion | PromotionScope | 1:N | Bridge Promotion N:M StoreSKU | start_date / end_date |
| Store | SourceDocument | 1:N | PO / POS batch; the authority for wrong-store checks | business_date |
| Transfer | TransferLine | 1:N | One line per SKU | – |
| Store | Transfer | 1:N x2 | from_store_id and to_store_id (distinct) | created_at, expected_arrival_at |
| StoreSKU | InventoryMovement | 1:N | Ledger entries for a store/SKU | occurred_at, received_at, posted_at |
| Employee | InventoryMovement | 1:N (actor), 1:N optional (approver) | Who posted / approved | role effective at occurred_at |
| Shift | InventoryMovement | 1:N, optional | Shift covering the event (NULL only for internal write-offs) | – |
| SourceDocument | InventoryMovement | 1:N, optional | Receipts (PO) and sales/returns (POS) | – |
| Transfer | InventoryMovement | 1:N, optional | Dispatch leg, receipt leg, transit write-offs | – |
| InventoryMovement | InventoryMovement | 1:0..1 | REVERSAL -> reversed entry (unique) | – |
| InventoryMovement (RETURN) | Return | 1:1 | Return record; optional link to original SALE (N:1) | returned_at |
| ImportBatch | ImportRawRow | 1:N | Untrusted rows as received | submitted_at |
| ImportRawRow | ImportRowResult | 1:1 | Outcome; corrects_row_id links a correction to the quarantined original | processed_at |
| ImportRowResult | InventoryMovement | 1:0..1 | Only POSTED rows create ledger entries | – |
| StoreSKU | CycleCount | 1:N | Physical count snapshots | counted_at, count_cutoff_at, submitted_at |
| CycleCount | CountReconciliation | 1:N | Versions; highest = current | created_at |
| RuleDefinition | ShrinkageAlert | 1:N | Rule code + version that fired | effective_from |
| DetectionRun | ShrinkageAlert | N:M via DetectionRunAlert | First detection and every re-detection | requested_at |
| ShrinkageAlert | AlertEvidence | 1:N | Links to movements, counts, reconciliations, import rows, transfer lines | – |
| Investigation | ShrinkageAlert | 1:N (1:1 in practice) | Case opened for the alert's stable case key | opened_at |
| Investigation | InvestigationEvent | 1:N | Append-only history; InvestigationState is the projection | occurred_at |
| InvestigationEvent | InventoryMovement | 1:0..1 | Write-off approved with the resolution | – |

## Lifecycle states

| Entity | States / transitions |
|---|---|
| Store | ACTIVE → CLOSED (closed_on set) |
| SKU | ACTIVE → DISCONTINUED |
| StoreSKU | active from active_from; inactive at/after active_to |
| Employee / RoleAssignment | ACTIVE → TERMINATED; assignments end at effective_to (authority checked at event time) |
| ImportBatch | RECEIVED → PROCESSING → COMPLETED | FAILED (FAILED → RECEIVED on retry; processing resumes) |
| Import row | QUARANTINED → (new linked row) POSTED | QUARANTINED; DUPLICATE / CONFLICT are terminal |
| Transfer (derived) | created → dispatched (TRANSFER_OUT) → received / partially received (TRANSFER_IN) → reconciled (shortfall written off once) | discrepancy (overdue or double-counted) |
| CountReconciliation | v1 COUNT_SUBMITTED → vN LATE_MOVEMENT; basis RELIABLE | TIMING_REVIEW per version |
| DetectionRun | QUEUED → RUNNING → COMPLETED | FAILED |
| Investigation | OPEN → ASSIGNED → IN_REVIEW → RESOLVED (reassignment allowed in ASSIGNED / IN_REVIEW; no reopen) |
| ShrinkageAlert | OPEN while its investigation is not RESOLVED; CLOSED afterwards (derived, alert row immutable) |

## Tables

### `store` — Store

A selling location or distribution warehouse.

- Primary key: `store_id`
- Checks: `status IN ('ACTIVE', 'CLOSED')`; `store_type IN ('STORE', 'WAREHOUSE')`

| Field | Type | Required | Units | Allowed values | Validation | Description |
|---|---|---|---|---|---|---|
| `store_id` | TEXT | PK |  |  | ^STORE-\d{6}$, immutable | Store or warehouse public ID. |
| `store_name` | TEXT | yes |  |  |  | Display name. |
| `store_type` | TEXT | yes |  | STORE, WAREHOUSE |  | Retail store or distribution warehouse. |
| `region_code` | TEXT | yes |  |  | ^R\d{2}$ | Operating region (drives loss-prevention scope). |
| `timezone` | TEXT | yes |  |  |  | IANA timezone (informational; all stored timestamps are UTC). |
| `opened_on` | TEXT | yes | date |  |  | Opening date. |
| `status` | TEXT | yes |  | ACTIVE, CLOSED |  | Lifecycle state. |
| `closed_on` | TEXT | optional | date |  | >= opened_on | Closure date when status=CLOSED. |

### `sku` — SKU

A stock-keeping unit.

- Primary key: `sku_id`
- Checks: `category IN ('GROCERY', 'BEVERAGE', 'SPIRITS', 'HEALTH_BEAUTY', 'COSMETICS', 'ELECTRONICS', 'ACCESSORIES', 'HOME', 'TOYS', 'APPAREL', 'FRESH', 'HARDWARE')`; `shrink_risk IN ('LOW', 'MEDIUM', 'HIGH')`; `status IN ('ACTIVE', 'DISCONTINUED')`; `unit_cost_cents > 0`; `unit_price_cents >= unit_cost_cents`

| Field | Type | Required | Units | Allowed values | Validation | Description |
|---|---|---|---|---|---|---|
| `sku_id` | TEXT | PK |  |  | ^SKU-\d{6}$, immutable | Product public ID. |
| `sku_name` | TEXT | yes |  |  |  | Product description. |
| `category` | TEXT | yes |  | GROCERY, BEVERAGE, SPIRITS, HEALTH_BEAUTY, COSMETICS, ELECTRONICS, ACCESSORIES, HOME, TOYS, APPAREL, FRESH, HARDWARE |  | Merchandise category. |
| `brand` | TEXT | yes |  |  |  | Synthetic brand name. |
| `uom` | TEXT | yes |  | EA |  | Unit of measure; all quantities are counted in this unit. |
| `unit_cost_cents` | INTEGER | yes | USD cents |  | > 0 | Standard cost per unit. |
| `unit_price_cents` | INTEGER | yes | USD cents |  | >= unit_cost_cents | Regular selling price per unit. |
| `shrink_risk` | TEXT | yes |  | LOW, MEDIUM, HIGH |  | Merchandising risk class used for count targeting. |
| `status` | TEXT | yes |  | ACTIVE, DISCONTINUED |  | Lifecycle state. |
| `introduced_on` | TEXT | yes | date |  |  | Date the SKU entered the catalogue. |

### `store_sku` — StoreSKU

Bridge Store N:M SKU (assortment) with effective dates and opening balance.

- Primary key: `store_id, sku_id`
- Foreign keys: `(sku_id) → sku(sku_id)`; `(source_warehouse_id) → store(store_id)`; `(store_id) → store(store_id)`
- Checks: `active_to IS NULL OR active_to > active_from`; `opening_qty >= 0`; `replenishment_source IN ('VENDOR', 'DC')`

| Field | Type | Required | Units | Allowed values | Validation | Description |
|---|---|---|---|---|---|---|
| `store_id` | TEXT | PK |  |  |  | Store carrying the SKU. |
| `sku_id` | TEXT | PK |  |  |  | SKU carried. |
| `active_from` | TEXT | yes | ISO-8601 UTC, YYYY-MM-DDTHH:MM:SSZ |  |  | Assortment effective start (inclusive). |
| `active_to` | TEXT | optional | ISO-8601 UTC, YYYY-MM-DDTHH:MM:SSZ |  | > active_from | Assortment effective end (exclusive); NULL = open-ended. |
| `opening_qty` | INTEGER | yes | units |  | >= 0 | Opening on-hand balance at opening_as_of. |
| `opening_as_of` | TEXT | yes | ISO-8601 UTC, YYYY-MM-DDTHH:MM:SSZ |  | = active_from | Timestamp of the opening balance; movements before it are rejected. |
| `replenishment_source` | TEXT | yes |  | VENDOR, DC |  | How the assortment is replenished. |
| `source_warehouse_id` | TEXT | optional |  |  |  | Supplying warehouse when replenishment_source=DC. |

### `employee` — Employee

Actors who post movements, count, approve or investigate.

- Primary key: `employee_id`
- Foreign keys: `(home_store_id) → store(store_id)`
- Checks: `employee_type IN ('HUMAN', 'SERVICE')`; `status IN ('ACTIVE', 'TERMINATED')`

| Field | Type | Required | Units | Allowed values | Validation | Description |
|---|---|---|---|---|---|---|
| `employee_id` | TEXT | PK |  |  | ^EMP-\d{6}$ | Person or service identity. |
| `display_name` | TEXT | yes |  |  |  | Synthetic display name (no real personal data). |
| `employee_type` | TEXT | yes |  | HUMAN, SERVICE |  | HUMAN or SERVICE (automated feed identity). |
| `home_store_id` | TEXT | optional |  |  |  | Home store for store staff. |
| `hired_on` | TEXT | yes | date |  |  | Start date. |
| `terminated_on` | TEXT | optional | date |  | >= hired_on | Leaving date; NULL while employed. |
| `status` | TEXT | yes |  | ACTIVE, TERMINATED |  | Lifecycle state. |
| `password_hash` | TEXT | optional |  |  |  | PBKDF2 hash; only set for demo log-ins. Never exported. |

### `employee_role` — EmployeeRole

Role catalogue. Permissions via RolePermission; people via RoleAssignment.

- Primary key: `role_code`

| Field | Type | Required | Units | Allowed values | Validation | Description |
|---|---|---|---|---|---|---|
| `role_code` | TEXT | PK |  | CASHIER, STOCK_CLERK, STORE_MANAGER, LOSS_PREVENTION, OPS_MANAGER, ANALYST, SERVICE_FEED, SYSTEM, ADMIN |  | Role identifier. |
| `role_name` | TEXT | yes |  |  |  | Display name. |
| `default_scope` | TEXT | yes |  | STORE, REGION, ALL |  | Typical assignment scope. |

### `role_permission` — RolePermission

Bridge EmployeeRole N:M action.

- Primary key: `role_code, action`
- Foreign keys: `(role_code) → employee_role(role_code)`
- Checks: `action IN ('VIEW_STORE', 'VIEW_ALL_STORES', 'POST_SALE', 'POST_RETURN', 'POST_RECEIPT', 'POST_TRANSFER', 'POST_ADJUSTMENT', 'POST_REVERSAL', 'APPROVE_ADJUSTMENT', 'SUBMIT_COUNT', 'IMPORT_BATCH', 'RUN_DETECTION', 'ASSIGN_CASE', 'INVESTIGATE', 'RESOLVE_CASE', 'EXPORT_DATA')`

| Field | Type | Required | Units | Allowed values | Validation | Description |
|---|---|---|---|---|---|---|
| `role_code` | TEXT | PK |  |  |  | Role. |
| `action` | TEXT | PK |  | VIEW_STORE, VIEW_ALL_STORES, POST_SALE, POST_RETURN, POST_RECEIPT, POST_TRANSFER, POST_ADJUSTMENT, POST_REVERSAL, APPROVE_ADJUSTMENT, SUBMIT_COUNT, IMPORT_BATCH, RUN_DETECTION, ASSIGN_CASE, INVESTIGATE, RESOLVE_CASE, EXPORT_DATA |  | Permitted action. |

### `role_assignment` — RoleAssignment

Effective-dated grant of a role to an employee for a store (or all stores).

- Primary key: `assignment_id`
- Foreign keys: `(employee_id) → employee(employee_id)`; `(role_code) → employee_role(role_code)`; `(store_id) → store(store_id)`
- Checks: `effective_to IS NULL OR effective_to > effective_from`

| Field | Type | Required | Units | Allowed values | Validation | Description |
|---|---|---|---|---|---|---|
| `assignment_id` | TEXT | PK |  |  | ^RA-\d{6}$ | Assignment ID. |
| `employee_id` | TEXT | yes |  |  |  | Employee holding the role. |
| `role_code` | TEXT | yes |  |  |  | Role granted. |
| `store_id` | TEXT | optional |  |  |  | Store scope; NULL = all stores. |
| `effective_from` | TEXT | yes | ISO-8601 UTC, YYYY-MM-DDTHH:MM:SSZ |  |  | Start of authority (inclusive). |
| `effective_to` | TEXT | optional | ISO-8601 UTC, YYYY-MM-DDTHH:MM:SSZ |  | > effective_from | End of authority (exclusive); NULL = open-ended. |

### `shift` — Shift

A staffed time window at a store; every posting is tied to one.

- Primary key: `shift_id`
- Unique: `store_id, start_at`
- Foreign keys: `(store_id) → store(store_id)`
- Checks: `end_at > start_at`; `shift_type IN ('OPEN', 'CLOSE', 'DAY', 'SWING')`

| Field | Type | Required | Units | Allowed values | Validation | Description |
|---|---|---|---|---|---|---|
| `shift_id` | TEXT | PK |  |  | ^SHF-2026-\d{7}$ | Shift ID. |
| `store_id` | TEXT | yes |  |  |  | Store the shift runs at. |
| `shift_date` | TEXT | yes | date |  |  | Business date. |
| `shift_type` | TEXT | yes |  | OPEN, CLOSE, DAY, SWING |  | Shift pattern. |
| `start_at` | TEXT | yes | ISO-8601 UTC, YYYY-MM-DDTHH:MM:SSZ |  |  | Shift start (inclusive). |
| `end_at` | TEXT | yes | ISO-8601 UTC, YYYY-MM-DDTHH:MM:SSZ |  | > start_at | Shift end (exclusive). |

### `promotion` — Promotion

Explains sales uplift; never posts stock itself.

- Primary key: `promotion_id`
- Checks: `discount_pct BETWEEN 1 AND 90`; `end_date >= start_date`; `promo_type IN ('PERCENT_OFF', 'MULTI_BUY', 'CLEARANCE')`

| Field | Type | Required | Units | Allowed values | Validation | Description |
|---|---|---|---|---|---|---|
| `promotion_id` | TEXT | PK |  |  | ^PRM-2026-\d{6}$ | Promotion ID. |
| `promo_name` | TEXT | yes |  |  |  | Display name. |
| `promo_type` | TEXT | yes |  | PERCENT_OFF, MULTI_BUY, CLEARANCE |  | Mechanic. |
| `discount_pct` | INTEGER | yes | percent |  | 1..90 | Headline discount. |
| `start_date` | TEXT | yes | date |  |  | First promo day (inclusive). |
| `end_date` | TEXT | yes | date |  | >= start_date | Last promo day (inclusive). |

### `promotion_scope` — PromotionScope

Bridge Promotion N:M StoreSKU.

- Primary key: `promotion_id, store_id, sku_id`
- Foreign keys: `(promotion_id) → promotion(promotion_id)`; `(store_id, sku_id) → store_sku(store_id, sku_id)`

| Field | Type | Required | Units | Allowed values | Validation | Description |
|---|---|---|---|---|---|---|
| `promotion_id` | TEXT | PK |  |  |  | Promotion. |
| `store_id` | TEXT | PK |  |  |  | Store in scope. |
| `sku_id` | TEXT | PK |  |  |  | SKU in scope. |

### `source_document` — SourceDocument

Order/POS references that movements must match.

- Primary key: `doc_id`
- Foreign keys: `(store_id) → store(store_id)`
- Checks: `doc_type IN ('PURCHASE_ORDER', 'POS_BATCH')`

| Field | Type | Required | Units | Allowed values | Validation | Description |
|---|---|---|---|---|---|---|
| `doc_id` | TEXT | PK |  |  | PO-2026-NNNNNN | POS-2026-NNNNNNN | Authoritative upstream document. |
| `doc_type` | TEXT | yes |  | PURCHASE_ORDER, POS_BATCH |  | Purchase order (receipts) or POS day batch (sales/returns). |
| `store_id` | TEXT | yes |  |  |  | Store the document belongs to — the authority for wrong-store checks. |
| `business_date` | TEXT | yes | date |  |  | Business date. |

### `transfer` — Transfer

Inter-location transfer order; links dispatch and receipt legs. Status is derived from legs.

- Primary key: `transfer_id`
- Foreign keys: `(from_store_id) → store(store_id)`; `(to_store_id) → store(store_id)`
- Checks: `expected_arrival_at >= created_at`; `from_store_id <> to_store_id`

| Field | Type | Required | Units | Allowed values | Validation | Description |
|---|---|---|---|---|---|---|
| `transfer_id` | TEXT | PK |  |  | ^TRF-2026-\d{6}$ | Transfer ID. |
| `from_store_id` | TEXT | yes |  |  |  | Dispatching location. |
| `to_store_id` | TEXT | yes |  |  | <> from_store_id | Receiving location. |
| `created_at` | TEXT | yes | ISO-8601 UTC, YYYY-MM-DDTHH:MM:SSZ |  |  | Transfer order creation time. |
| `expected_arrival_at` | TEXT | yes | ISO-8601 UTC, YYYY-MM-DDTHH:MM:SSZ |  | >= created_at | Planned arrival. |

### `transfer_line` — TransferLine

Transfer 1:N lines (one per SKU).

- Primary key: `transfer_id, sku_id`
- Foreign keys: `(sku_id) → sku(sku_id)`; `(transfer_id) → transfer(transfer_id)`
- Checks: `planned_qty > 0`

| Field | Type | Required | Units | Allowed values | Validation | Description |
|---|---|---|---|---|---|---|
| `transfer_id` | TEXT | PK |  |  |  | Transfer. |
| `sku_id` | TEXT | PK |  |  |  | SKU on the transfer. |
| `planned_qty` | INTEGER | yes | units |  | > 0 | Ordered quantity. |

### `import_batch` — ImportBatch

One uploaded file. Immutable; processing state lives in import_batch_status.

- Primary key: `batch_id`
- Foreign keys: `(submitted_by) → employee(employee_id)`

| Field | Type | Required | Units | Allowed values | Validation | Description |
|---|---|---|---|---|---|---|
| `batch_id` | TEXT | PK |  |  | ^IMP-2026-\d{6}$ | Import batch / job reference. |
| `batch_kind` | TEXT | yes |  | MOVEMENTS, COUNTS |  | What the file contains. |
| `source_system` | TEXT | yes |  | POS, WMS, STORE_OPS, STASHTRACE |  | Default upstream system for rows. |
| `file_name` | TEXT | yes |  |  |  | Uploaded file name. |
| `file_sha256` | TEXT | yes |  |  |  | SHA-256 of the uploaded bytes. |
| `header_csv` | TEXT | yes |  |  |  | Header line of the CSV (raw rows are stored without it). |
| `submitted_by` | TEXT | yes |  |  |  | Uploader. |
| `submitted_at` | TEXT | yes | ISO-8601 UTC, YYYY-MM-DDTHH:MM:SSZ |  |  | Receive time. |

### `import_batch_status` — ImportBatchStatus

Mutable progress projection of a batch (1:1).

- Primary key: `batch_id`
- Foreign keys: `(batch_id) → import_batch(batch_id)`
- Checks: `posted_count + quarantined_count + duplicate_count + conflict_count = processed_count`; `status IN ('RECEIVED', 'PROCESSING', 'COMPLETED', 'FAILED')`

| Field | Type | Required | Units | Allowed values | Validation | Description |
|---|---|---|---|---|---|---|
| `batch_id` | TEXT | PK |  |  |  | Batch. |
| `status` | TEXT | yes |  | RECEIVED, PROCESSING, COMPLETED, FAILED |  | Processing state. |
| `received_count` | INTEGER | yes | rows |  |  | Rows saved on receive. |
| `processed_count` | INTEGER | yes | rows |  |  | Rows validated so far. |
| `posted_count` | INTEGER | yes | rows |  |  | Rows posted to the ledger / counts. |
| `quarantined_count` | INTEGER | yes | rows |  |  | Rows rejected with an error code. |
| `duplicate_count` | INTEGER | yes | rows |  |  | Exact replays of earlier rows. |
| `conflict_count` | INTEGER | yes | rows |  |  | Same event ID, different payload. |
| `started_at` | TEXT | optional | ISO-8601 UTC, YYYY-MM-DDTHH:MM:SSZ |  |  | Worker start. |
| `finished_at` | TEXT | optional | ISO-8601 UTC, YYYY-MM-DDTHH:MM:SSZ |  |  | Worker finish. |
| `error_message` | TEXT | optional |  |  |  | Fatal error if FAILED. |

### `import_raw_row` — ImportRawRow *(append-only)*

Untrusted input as received (append-only). Never read by stock calculations.

- Primary key: `row_id`
- Unique: `batch_id, row_number`
- Foreign keys: `(batch_id) → import_batch(batch_id)`

| Field | Type | Required | Units | Allowed values | Validation | Description |
|---|---|---|---|---|---|---|
| `row_id` | INTEGER | PK |  |  |  | Surrogate key. |
| `batch_id` | TEXT | yes |  |  |  | Batch. |
| `row_number` | INTEGER | yes |  |  | unique per batch | 1-based data row number in the file. |
| `raw_line` | TEXT | yes |  |  |  | Row exactly as received (CSV line or JSON). |

### `import_row_result` — ImportRowResult *(append-only)*

Validation outcome per raw row (append-only, 1:1 with ImportRawRow).

- Primary key: `row_id`
- Foreign keys: `(batch_id) → import_batch(batch_id)`; `(corrects_row_id) → import_raw_row(row_id)`; `(count_id) → cycle_count(count_id)`; `(duplicate_of_row_id) → import_raw_row(row_id)`; `(movement_id) → inventory_movement(movement_id)`; `(row_id) → import_raw_row(row_id)`
- Checks: `outcome IN ('POSTED', 'QUARANTINED', 'DUPLICATE', 'CONFLICT')`

| Field | Type | Required | Units | Allowed values | Validation | Description |
|---|---|---|---|---|---|---|
| `row_id` | INTEGER | PK |  |  |  | Raw row validated. |
| `batch_id` | TEXT | yes |  |  |  | Batch (denormalised for filtering). |
| `source_system` | TEXT | optional |  |  |  | Upstream system of the row. |
| `source_event_id` | TEXT | optional |  |  |  | Upstream event / count ID (idempotency key with source_system). |
| `payload_sha256` | TEXT | optional |  |  |  | SHA-256 of the canonical business payload (excludes received_at). |
| `outcome` | TEXT | yes |  | POSTED, QUARANTINED, DUPLICATE, CONFLICT |  | Validation outcome. |
| `error_code` | TEXT | optional |  | see catalog.ERROR_CODES |  | Primary (highest-precedence) error. |
| `errors_json` | TEXT | optional |  |  |  | All errors found, JSON list of {code, field, message}. |
| `movement_type` | TEXT | optional |  |  |  | Submitted movement type (movement batches). |
| `submitted_store_id` | TEXT | optional |  |  |  | Store as submitted (may be wrong). |
| `authoritative_store_id` | TEXT | optional |  |  |  | Store from the authoritative reference when it could be resolved. |
| `sku_id` | TEXT | optional |  |  |  | SKU as submitted. |
| `occurred_at` | TEXT | optional | ISO-8601 UTC, YYYY-MM-DDTHH:MM:SSZ |  |  | Event time as submitted. |
| `explain_qty` | INTEGER | yes | units |  |  | Units this row would have removed from stock had it posted (quarantined rows explain count shortages). |
| `corrects_row_id` | INTEGER | optional |  |  |  | Earlier quarantined row this submission corrects. |
| `duplicate_of_row_id` | INTEGER | optional |  |  |  | Earlier row this replays/conflicts with. |
| `movement_id` | TEXT | optional |  |  |  | Ledger entry created (POSTED) or matched (DUPLICATE). |
| `count_id` | TEXT | optional |  |  |  | Count created or matched (count batches). |
| `processed_at` | TEXT | yes | ISO-8601 UTC, YYYY-MM-DDTHH:MM:SSZ |  |  | Validation time. |

### `ingest_key` — IngestKey

Idempotency index (derived, rebuildable from import_row_result).

- Primary key: `source_system, source_event_id`
- Foreign keys: `(latest_row_id) → import_raw_row(row_id)`

| Field | Type | Required | Units | Allowed values | Validation | Description |
|---|---|---|---|---|---|---|
| `source_system` | TEXT | PK |  |  |  | Upstream system. |
| `source_event_id` | TEXT | PK |  |  |  | Upstream event ID. |
| `latest_row_id` | INTEGER | yes |  |  |  | Row that currently defines this key's outcome. |
| `payload_sha256` | TEXT | yes |  |  |  | Payload hash of latest_row_id. |
| `outcome` | TEXT | yes |  | POSTED, QUARANTINED |  | POSTED or QUARANTINED. |
| `movement_id` | TEXT | optional |  |  |  | Ledger entry when POSTED (movement keys). |
| `count_id` | TEXT | optional |  |  |  | Count when POSTED (count keys). |

### `inventory_movement` — InventoryMovement *(append-only)*

The trusted, append-only inventory ledger. Corrections are linked REVERSAL + replacement entries.

- Primary key: `ledger_seq`
- Unique: `movement_id`; `reverses_movement_id`; `source_system, source_event_id`
- Foreign keys: `(actor_id) → employee(employee_id)`; `(actor_role_code) → employee_role(role_code)`; `(approved_by) → employee(employee_id)`; `(approver_role_code) → employee_role(role_code)`; `(batch_id) → import_batch(batch_id)`; `(count_ref) → cycle_count(count_id)`; `(import_row_id) → import_raw_row(row_id)`; `(investigation_id) → investigation(investigation_id)`; `(reverses_movement_id) → inventory_movement(movement_id)`; `(shift_id) → shift(shift_id)`; `(source_doc_id) → source_document(doc_id)`; `(store_id, sku_id) → store_sku(store_id, sku_id)`; `(transfer_id) → transfer(transfer_id)`
- Checks: `movement_type <> 'ADJUSTMENT' OR (reason_code IS NOT NULL AND approved_by IS NOT NULL)`; `movement_type <> 'REVERSAL' OR reverses_movement_id IS NOT NULL`; `movement_type = 'ADJUSTMENT' OR quantity > 0`; `movement_type IN ('RECEIPT', 'SALE', 'TRANSFER_OUT', 'TRANSFER_IN', 'RETURN', 'ADJUSTMENT', 'REVERSAL')`; `movement_type NOT IN ('TRANSFER_IN','TRANSFER_OUT') OR transfer_id IS NOT NULL`; `quantity <> 0`; `received_at >= occurred_at`

| Field | Type | Required | Units | Allowed values | Validation | Description |
|---|---|---|---|---|---|---|
| `ledger_seq` | INTEGER | PK |  |  |  | Monotonic posting sequence; used as the ledger watermark. |
| `movement_id` | TEXT | yes (unique) |  |  | ^MOV-2026-\d{8}$, immutable | Public movement ID. |
| `source_system` | TEXT | yes |  | POS, WMS, STORE_OPS, STASHTRACE |  | Upstream system. |
| `source_event_id` | TEXT | yes |  |  |  | Upstream event ID; unique with source_system (idempotency). |
| `movement_type` | TEXT | yes |  | RECEIPT, SALE, TRANSFER_OUT, TRANSFER_IN, RETURN, ADJUSTMENT, REVERSAL |  | Kind of movement. |
| `store_id` | TEXT | yes |  |  |  | Store whose on-hand is affected. |
| `sku_id` | TEXT | yes |  |  |  | SKU. |
| `quantity` | INTEGER | yes | units |  | <> 0 | Units in the event. Positive, except ADJUSTMENT which is signed. |
| `stock_qty` | INTEGER | yes | units |  |  | Signed effect on store on-hand (derived from type/disposition/reason). |
| `occurred_at` | TEXT | yes | ISO-8601 UTC, YYYY-MM-DDTHH:MM:SSZ |  |  | When it physically happened. |
| `received_at` | TEXT | yes | ISO-8601 UTC, YYYY-MM-DDTHH:MM:SSZ |  | >= occurred_at | When the upstream system emitted it / we received it. |
| `posted_at` | TEXT | yes | ISO-8601 UTC, YYYY-MM-DDTHH:MM:SSZ |  |  | When StashTrace posted it to the ledger. |
| `actor_id` | TEXT | yes |  |  |  | Who performed it (service identity for feeds). |
| `actor_role_code` | TEXT | yes |  |  |  | Role that authorised the actor at occurred_at. |
| `shift_id` | TEXT | optional |  |  |  | Shift covering occurred_at at this store. |
| `source_doc_id` | TEXT | optional |  |  |  | PO (receipts) or POS batch (sales/returns). |
| `transfer_id` | TEXT | optional |  |  |  | Transfer for TRANSFER_* legs and transit write-offs. |
| `count_ref` | TEXT | optional |  |  |  | Count that a COUNT_CORRECTION adjustment follows. |
| `investigation_id` | TEXT | optional |  |  |  | Case that approved an INVESTIGATION_WRITE_OFF. |
| `reason_code` | TEXT | optional |  | DAMAGE, EXPIRED, SAMPLING, OTHER, THEFT_CONFIRMED, INVESTIGATION_WRITE_OFF, TRANSFER_SHORTAGE, VENDOR_CREDIT, FOUND, COUNT_CORRECTION |  | Adjustment reason (required for ADJUSTMENT). |
| `approved_by` | TEXT | optional |  |  |  | Approver for adjustments (must differ from actor). |
| `approver_role_code` | TEXT | optional |  |  |  | Role that authorised the approval. |
| `reverses_movement_id` | TEXT | optional (unique) |  |  |  | Original entry a REVERSAL negates (at most one reversal each). |
| `memo` | TEXT | optional |  |  | <= 500 chars | Free-text justification; required for REVERSAL. |
| `batch_id` | TEXT | yes |  |  |  | Import batch (API postings use a one-row batch). |
| `import_row_id` | INTEGER | yes |  |  |  | Raw row that produced the posting. |

### `customer_return` — Return *(append-only)*

Customer return; posted with a RETURN movement whose stock effect depends on disposition.

- Primary key: `return_id`
- Unique: `movement_id`
- Foreign keys: `(movement_id) → inventory_movement(movement_id)`; `(original_sale_movement_id) → inventory_movement(movement_id)`; `(sku_id) → sku(sku_id)`; `(store_id) → store(store_id)`
- Checks: `disposition IN ('RESTOCK', 'DAMAGED', 'VENDOR_RETURN', 'DISPOSE')`; `quantity > 0`

| Field | Type | Required | Units | Allowed values | Validation | Description |
|---|---|---|---|---|---|---|
| `return_id` | TEXT | PK |  |  | ^RET-2026-\d{6}$ | Return ID. |
| `store_id` | TEXT | yes |  |  |  | Store receiving the return. |
| `sku_id` | TEXT | yes |  |  |  | SKU returned. |
| `quantity` | INTEGER | yes | units |  | > 0 | Units returned. |
| `disposition` | TEXT | yes |  | RESTOCK, DAMAGED, VENDOR_RETURN, DISPOSE |  | What happened to the goods. Only RESTOCK adds sellable stock. |
| `return_reason` | TEXT | optional |  | CHANGED_MIND, DEFECTIVE, WRONG_ITEM, NOT_AS_DESCRIBED |  | Customer reason. |
| `returned_at` | TEXT | yes | ISO-8601 UTC, YYYY-MM-DDTHH:MM:SSZ |  |  | Return time. |
| `original_sale_movement_id` | TEXT | optional |  |  |  | Optional link to the original SALE (receipted return). |
| `restocked` | INTEGER | yes |  | 0, 1 |  | 1 when disposition permits restocking. |
| `movement_id` | TEXT | yes (unique) |  |  |  | RETURN ledger entry (1:1). |

### `stock_balance` — StockBalance

Cached on-hand per StoreSKU, updated in the same transaction as each posting.

- Primary key: `store_id, sku_id`
- Foreign keys: `(store_id, sku_id) → store_sku(store_id, sku_id)`

| Field | Type | Required | Units | Allowed values | Validation | Description |
|---|---|---|---|---|---|---|
| `store_id` | TEXT | PK |  |  |  | Store. |
| `sku_id` | TEXT | PK |  |  |  | SKU. |
| `on_hand` | INTEGER | yes | units |  |  | opening_qty + SUM(stock_qty) over the ledger. Cached; rebuildable. |
| `last_ledger_seq` | INTEGER | yes |  |  |  | Highest ledger_seq included. |
| `updated_at` | TEXT | yes | ISO-8601 UTC, YYYY-MM-DDTHH:MM:SSZ |  |  | Last update. |

### `cycle_count` — CycleCount *(append-only)*

Physical count snapshot. Never posts stock.

- Primary key: `count_id`
- Foreign keys: `(batch_id) → import_batch(batch_id)`; `(counted_by) → employee(employee_id)`; `(counter_role_code) → employee_role(role_code)`; `(shift_id) → shift(shift_id)`; `(store_id, sku_id) → store_sku(store_id, sku_id)`
- Checks: `count_type IN ('SCHEDULED', 'AD_HOC', 'BLIND')`; `counted_qty >= 0`

| Field | Type | Required | Units | Allowed values | Validation | Description |
|---|---|---|---|---|---|---|
| `count_id` | TEXT | PK |  |  | ^CNT-2026-\d{6}$ | Count ID. |
| `store_id` | TEXT | yes |  |  |  | Store counted. |
| `sku_id` | TEXT | yes |  |  |  | SKU counted. |
| `counted_qty` | INTEGER | yes | units |  | >= 0 | Physical quantity found. |
| `counted_at` | TEXT | yes | ISO-8601 UTC, YYYY-MM-DDTHH:MM:SSZ |  |  | When the physical count was performed. |
| `count_cutoff_at` | TEXT | yes | ISO-8601 UTC, YYYY-MM-DDTHH:MM:SSZ |  |  | Ledger cutoff the count represents (movements with occurred_at <= cutoff are included). |
| `submitted_at` | TEXT | yes | ISO-8601 UTC, YYYY-MM-DDTHH:MM:SSZ |  |  | When the count was submitted. |
| `counted_by` | TEXT | yes |  |  |  | Counter. |
| `counter_role_code` | TEXT | yes |  |  |  | Role authorising the count. |
| `shift_id` | TEXT | optional |  |  |  | Shift covering counted_at. |
| `count_type` | TEXT | yes |  | SCHEDULED, AD_HOC, BLIND |  | Count programme. |
| `batch_id` | TEXT | optional |  |  |  | Import batch (NULL for API submissions). |

### `count_reconciliation` — CountReconciliation *(append-only)*

Versioned expected-vs-counted calculation for a count (CycleCount 1:N).

- Primary key: `recon_id`
- Unique: `count_id, version`
- Foreign keys: `(count_id) → cycle_count(count_id)`; `(supersedes_recon_id) → count_reconciliation(recon_id)`
- Checks: `basis_status IN ('RELIABLE', 'TIMING_REVIEW')`; `expected_qty = opening_qty + receipts_qty + transfer_in_qty + restocked_returns_qty - sales_qty - transfer_out_qty + adjustments_qty`; `trigger IN ('COUNT_SUBMITTED', 'LATE_MOVEMENT', 'MANUAL_RECOMPUTE')`; `variance_qty = counted_qty - expected_qty`

| Field | Type | Required | Units | Allowed values | Validation | Description |
|---|---|---|---|---|---|---|
| `recon_id` | TEXT | PK |  |  | ^REC-2026-\d{6}$ | Reconciliation version ID. |
| `count_id` | TEXT | yes |  |  |  | Count reconciled. |
| `version` | INTEGER | yes |  |  | unique per count | 1, 2, ... per count; highest is current. |
| `supersedes_recon_id` | TEXT | optional |  |  |  | Previous version. |
| `trigger` | TEXT | yes |  | COUNT_SUBMITTED, LATE_MOVEMENT, MANUAL_RECOMPUTE |  | Why this version was computed. |
| `opening_qty` | INTEGER | yes | units |  |  | StoreSKU opening balance. |
| `receipts_qty` | INTEGER | yes | units |  |  | + RECEIPT stock (net of reversals). |
| `transfer_in_qty` | INTEGER | yes | units |  |  | + TRANSFER_IN stock. |
| `restocked_returns_qty` | INTEGER | yes | units |  |  | + RETURN stock (restockable dispositions only). |
| `sales_qty` | INTEGER | yes | units |  |  | - SALE units (positive number). |
| `transfer_out_qty` | INTEGER | yes | units |  |  | - TRANSFER_OUT units (positive number). |
| `adjustments_qty` | INTEGER | yes | units |  |  | + signed approved ADJUSTMENT stock. |
| `expected_qty` | INTEGER | yes | units |  |  | Derived expected on-hand at cutoff. |
| `counted_qty` | INTEGER | yes | units |  |  | Copied from the count. |
| `variance_qty` | INTEGER | yes | units |  |  | counted_qty - expected_qty. |
| `variance_value_cents` | INTEGER | yes | USD cents |  |  | variance_qty * unit_cost_cents. |
| `movement_count` | INTEGER | yes | rows |  |  | Ledger entries contributing. |
| `ledger_watermark_seq` | INTEGER | yes |  |  |  | Max ledger_seq visible when computed (snapshot). |
| `basis_status` | TEXT | yes |  | RELIABLE, TIMING_REVIEW |  | Whether the count basis is reliable. |
| `basis_notes` | TEXT | optional |  |  |  | JSON details for TIMING_REVIEW (violations, movements between). |
| `created_at` | TEXT | yes | ISO-8601 UTC, YYYY-MM-DDTHH:MM:SSZ |  |  | Computation time. |

### `rule_definition` — RuleDefinition

Versioned, explainable detection rules.

- Primary key: `rule_code, rule_version`

| Field | Type | Required | Units | Allowed values | Validation | Description |
|---|---|---|---|---|---|---|
| `rule_code` | TEXT | PK |  |  |  | Rule identifier. |
| `rule_version` | INTEGER | PK |  |  |  | Version; params never change within a version. |
| `alert_class` | TEXT | yes |  | SHRINKAGE, PATTERN, TRANSFER, DATA_QUALITY |  | Class of alert produced. |
| `description` | TEXT | yes |  |  |  | Plain-language logic. |
| `params_json` | TEXT | yes |  |  |  | Thresholds and windows, JSON. |
| `is_active` | INTEGER | yes |  | 0, 1 |  | 1 when used by new runs. |
| `effective_from` | TEXT | yes | ISO-8601 UTC, YYYY-MM-DDTHH:MM:SSZ |  |  | Activation time. |

### `detection_run` — DetectionRun

A detection execution (immutable request).

- Primary key: `run_id`
- Foreign keys: `(requested_by) → employee(employee_id)`

| Field | Type | Required | Units | Allowed values | Validation | Description |
|---|---|---|---|---|---|---|
| `run_id` | TEXT | PK |  |  | ^RUN-2026-\d{6}$ | Run ID. |
| `requested_by` | TEXT | yes |  |  |  | Requester. |
| `requested_at` | TEXT | yes | ISO-8601 UTC, YYYY-MM-DDTHH:MM:SSZ |  |  | Queue time. |
| `as_of` | TEXT | optional | ISO-8601 UTC, YYYY-MM-DDTHH:MM:SSZ |  |  | Evaluation horizon (default: latest occurred_at in ledger). |
| `rules_json` | TEXT | yes |  |  |  | Rule codes+versions+params used. |

### `detection_run_status` — DetectionRunStatus

Mutable progress projection of a run (1:1).

- Primary key: `run_id`
- Foreign keys: `(run_id) → detection_run(run_id)`
- Checks: `status IN ('QUEUED', 'RUNNING', 'COMPLETED', 'FAILED')`

| Field | Type | Required | Units | Allowed values | Validation | Description |
|---|---|---|---|---|---|---|
| `run_id` | TEXT | PK |  |  |  | Run. |
| `status` | TEXT | yes |  | QUEUED, RUNNING, COMPLETED, FAILED |  | State. |
| `started_at` | TEXT | optional | ISO-8601 UTC, YYYY-MM-DDTHH:MM:SSZ |  |  | Start. |
| `finished_at` | TEXT | optional | ISO-8601 UTC, YYYY-MM-DDTHH:MM:SSZ |  |  | Finish. |
| `ledger_watermark_seq` | INTEGER | optional |  |  |  | Ledger snapshot evaluated. |
| `alerts_new` | INTEGER | yes | alerts |  |  | Alerts created. |
| `alerts_redetected` | INTEGER | yes | alerts |  |  | Existing case keys seen again. |
| `duration_ms` | INTEGER | optional | ms |  |  | Wall time. |
| `error_message` | TEXT | optional |  |  |  | Failure detail. |

### `shrinkage_alert` — ShrinkageAlert *(append-only)*

Explainable finding. Immutable; its open/closed state follows its investigation.

- Primary key: `alert_id`
- Unique: `case_key`
- Foreign keys: `(count_id) → cycle_count(count_id)`; `(first_run_id) → detection_run(run_id)`; `(investigation_id) → investigation(investigation_id)`; `(recon_id) → count_reconciliation(recon_id)`; `(related_store_id) → store(store_id)`; `(rule_code, rule_version) → rule_definition(rule_code, rule_version)`; `(sku_id) → sku(sku_id)`; `(store_id) → store(store_id)`; `(transfer_id) → transfer(transfer_id)`
- Checks: `alert_class IN ('SHRINKAGE', 'PATTERN', 'TRANSFER', 'DATA_QUALITY')`; `alert_type IN ('UNEXPLAINED_SHORTAGE', 'REPEATED_NEGATIVE_ADJUSTMENTS', 'TRANSFER_DOUBLE_COUNT', 'TRANSFER_OVERDUE_DISCREPANCY', 'COUNT_TIMING_REVIEW')`; `severity IN ('LOW', 'MEDIUM', 'HIGH')`

| Field | Type | Required | Units | Allowed values | Validation | Description |
|---|---|---|---|---|---|---|
| `alert_id` | TEXT | PK |  |  | ^SHR-2026-\d{6}$ | Alert ID. |
| `case_key` | TEXT | yes (unique) |  |  |  | Stable discrepancy key; reruns never duplicate it. |
| `rule_code` | TEXT | yes |  |  |  | Rule that fired. |
| `rule_version` | INTEGER | yes |  |  |  | Rule version. |
| `alert_class` | TEXT | yes |  | SHRINKAGE, PATTERN, TRANSFER, DATA_QUALITY |  | SHRINKAGE vs data quality etc. |
| `alert_type` | TEXT | yes |  | UNEXPLAINED_SHORTAGE, REPEATED_NEGATIVE_ADJUSTMENTS, TRANSFER_DOUBLE_COUNT, TRANSFER_OVERDUE_DISCREPANCY, COUNT_TIMING_REVIEW |  | Specific finding. |
| `severity` | TEXT | yes |  | LOW, MEDIUM, HIGH |  | Triage priority. |
| `store_id` | TEXT | yes |  |  |  | Primary store (receiver for transfers). |
| `related_store_id` | TEXT | optional |  |  |  | Other end of a transfer. |
| `sku_id` | TEXT | optional |  |  |  | SKU when the alert is SKU-level. |
| `count_id` | TEXT | optional |  |  |  | Count (count-grain rules). |
| `recon_id` | TEXT | optional |  |  |  | Reconciliation version evaluated. |
| `transfer_id` | TEXT | optional |  |  |  | Transfer (transfer rule). |
| `window_start` | TEXT | optional | ISO-8601 UTC, YYYY-MM-DDTHH:MM:SSZ |  |  | Pattern window start. |
| `window_end` | TEXT | optional | ISO-8601 UTC, YYYY-MM-DDTHH:MM:SSZ |  |  | Pattern window end. |
| `threshold_value` | INTEGER | optional | units |  |  | Threshold applied. |
| `observed_value` | INTEGER | optional | units |  |  | Observed value compared to the threshold. |
| `calculation_json` | TEXT | yes |  |  |  | Full calculation shown to users. |
| `reason_text` | TEXT | yes |  |  |  | Evidence-based explanation. |
| `first_run_id` | TEXT | yes |  |  |  | Run that created it. |
| `investigation_id` | TEXT | yes |  |  |  | Case opened for it (N:1). |
| `created_at` | TEXT | yes | ISO-8601 UTC, YYYY-MM-DDTHH:MM:SSZ |  |  | Creation time. |

### `alert_evidence` — AlertEvidence *(append-only)*

Alert 1:N links to source records.

- Primary key: `evidence_id`
- Foreign keys: `(alert_id) → shrinkage_alert(alert_id)`; `(count_id) → cycle_count(count_id)`; `(import_row_id) → import_raw_row(row_id)`; `(movement_id) → inventory_movement(movement_id)`; `(recon_id) → count_reconciliation(recon_id)`; `(sku_id) → sku(sku_id)`; `(transfer_id) → transfer(transfer_id)`
- Checks: `evidence_type IN ('MOVEMENT', 'COUNT', 'RECONCILIATION', 'IMPORT_ROW', 'TRANSFER_LINE')`

| Field | Type | Required | Units | Allowed values | Validation | Description |
|---|---|---|---|---|---|---|
| `evidence_id` | INTEGER | PK |  |  |  | Surrogate key. |
| `alert_id` | TEXT | yes |  |  |  | Alert. |
| `evidence_type` | TEXT | yes |  | MOVEMENT, COUNT, RECONCILIATION, IMPORT_ROW, TRANSFER_LINE |  | Kind of source record. |
| `evidence_role` | TEXT | yes |  |  |  | How it supports the alert (CONTRIBUTING, EXPLANATION, DISPATCH, RECEIPT, WRITE_OFF, ...). |
| `movement_id` | TEXT | optional |  |  |  | Ledger entry. |
| `count_id` | TEXT | optional |  |  |  | Count. |
| `recon_id` | TEXT | optional |  |  |  | Reconciliation. |
| `import_row_id` | INTEGER | optional |  |  |  | Quarantined row. |
| `transfer_id` | TEXT | optional |  |  |  | Transfer. |
| `sku_id` | TEXT | optional |  |  |  | SKU of the evidence. |
| `qty` | INTEGER | optional | units |  |  | Units this evidence contributes. |
| `note` | TEXT | optional |  |  |  | Short explanation. |

### `detection_run_alert` — DetectionRunAlert *(append-only)*

Bridge DetectionRun N:M ShrinkageAlert (history of re-detections).

- Primary key: `run_id, alert_id`
- Foreign keys: `(alert_id) → shrinkage_alert(alert_id)`; `(run_id) → detection_run(run_id)`

| Field | Type | Required | Units | Allowed values | Validation | Description |
|---|---|---|---|---|---|---|
| `run_id` | TEXT | PK |  |  |  | Run. |
| `alert_id` | TEXT | PK |  |  |  | Alert. |
| `outcome` | TEXT | yes |  | NEW, REDETECTED |  | NEW or REDETECTED. |
| `observed_value` | INTEGER | optional | units |  |  | Value observed in this run. |

### `investigation` — Investigation

Case header (immutable). Current state is projected from investigation_event.

- Primary key: `investigation_id`
- Unique: `case_key`
- Foreign keys: `(related_store_id) → store(store_id)`; `(store_id) → store(store_id)`

| Field | Type | Required | Units | Allowed values | Validation | Description |
|---|---|---|---|---|---|---|
| `investigation_id` | TEXT | PK |  |  | ^INV-2026-\d{6}$ | Case ID. |
| `case_key` | TEXT | yes (unique) |  |  |  | Same stable key as the originating alert. |
| `origin_type` | TEXT | yes |  | COUNT, MOVEMENT_PATTERN, TRANSFER, DATA_QUALITY |  | What raised the case. |
| `store_id` | TEXT | yes |  |  |  | Primary store. |
| `related_store_id` | TEXT | optional |  |  |  | Other transfer end. |
| `opened_at` | TEXT | yes | ISO-8601 UTC, YYYY-MM-DDTHH:MM:SSZ |  |  | Open time. |

### `investigation_event` — InvestigationEvent *(append-only)*

Append-only case history (Investigation 1:N).

- Primary key: `event_id`
- Unique: `investigation_id, seq`
- Foreign keys: `(actor_id) → employee(employee_id)`; `(actor_role_code) → employee_role(role_code)`; `(assignee_id) → employee(employee_id)`; `(investigation_id) → investigation(investigation_id)`; `(run_id) → detection_run(run_id)`; `(write_off_movement_id) → inventory_movement(movement_id)`
- Checks: `event_type <> 'RESOLVED' OR (resolution_code IS NOT NULL AND length(note) >= 10)`; `event_type IN ('OPENED', 'ASSIGNED', 'REVIEW_STARTED', 'FINDING_ADDED', 'RESOLVED', 'REDETECTED')`; `to_status IN ('OPEN', 'ASSIGNED', 'IN_REVIEW', 'RESOLVED')`

| Field | Type | Required | Units | Allowed values | Validation | Description |
|---|---|---|---|---|---|---|
| `event_id` | INTEGER | PK |  |  |  | Surrogate key. |
| `investigation_id` | TEXT | yes |  |  |  | Case. |
| `seq` | INTEGER | yes |  |  | unique per case, gapless | 1-based order within the case. |
| `event_type` | TEXT | yes |  | OPENED, ASSIGNED, REVIEW_STARTED, FINDING_ADDED, RESOLVED, REDETECTED |  | What happened. |
| `from_status` | TEXT | optional |  | OPEN, ASSIGNED, IN_REVIEW, RESOLVED |  | Status before. |
| `to_status` | TEXT | yes |  | OPEN, ASSIGNED, IN_REVIEW, RESOLVED |  | Status after. |
| `actor_id` | TEXT | yes |  |  |  | Who did it. |
| `actor_role_code` | TEXT | yes |  |  |  | Role that authorised it. |
| `assignee_id` | TEXT | optional |  |  |  | Assignee (ASSIGNED). |
| `note` | TEXT | optional |  |  |  | Finding or reason text. |
| `resolution_code` | TEXT | optional |  | CONFIRMED_THEFT, COUNT_ERROR, DATA_CORRECTION, TIMING_CORRECTED, TRANSFER_RECONCILED, BENIGN_EXPLAINED, NO_ACTION |  | Decision (RESOLVED). |
| `write_off_movement_id` | TEXT | optional |  |  |  | Separate ledger write-off approved with the resolution. |
| `run_id` | TEXT | optional |  |  |  | Run (OPENED/REDETECTED). |
| `occurred_at` | TEXT | yes | ISO-8601 UTC, YYYY-MM-DDTHH:MM:SSZ |  |  | Event time. |

### `investigation_state` — InvestigationState

Mutable projection of the event log (rebuildable).

- Primary key: `investigation_id`
- Foreign keys: `(assignee_id) → employee(employee_id)`; `(investigation_id) → investigation(investigation_id)`
- Checks: `status IN ('OPEN', 'ASSIGNED', 'IN_REVIEW', 'RESOLVED')`

| Field | Type | Required | Units | Allowed values | Validation | Description |
|---|---|---|---|---|---|---|
| `investigation_id` | TEXT | PK |  |  |  | Case. |
| `status` | TEXT | yes |  | OPEN, ASSIGNED, IN_REVIEW, RESOLVED |  | Current status (projection of last event). |
| `last_seq` | INTEGER | yes |  |  |  | seq of the last applied event (optimistic concurrency). |
| `assignee_id` | TEXT | optional |  |  |  | Current assignee. |
| `resolution_code` | TEXT | optional |  |  |  | Final decision. |
| `resolved_at` | TEXT | optional | ISO-8601 UTC, YYYY-MM-DDTHH:MM:SSZ |  |  | Resolution time. |
| `updated_at` | TEXT | yes | ISO-8601 UTC, YYYY-MM-DDTHH:MM:SSZ |  |  | Last change. |

### `job` — Job

Background work queue polled by the worker.

- Primary key: `job_id`
- Checks: `job_type IN ('IMPORT', 'DETECTION')`; `status IN ('QUEUED', 'RUNNING', 'DONE', 'FAILED')`

| Field | Type | Required | Units | Allowed values | Validation | Description |
|---|---|---|---|---|---|---|
| `job_id` | INTEGER | PK |  |  |  | Job ID. |
| `job_type` | TEXT | yes |  | IMPORT, DETECTION |  | Work kind. |
| `ref_id` | TEXT | yes |  |  |  | batch_id or run_id. |
| `status` | TEXT | yes |  | QUEUED, RUNNING, DONE, FAILED |  | Queue state. |
| `attempts` | INTEGER | yes |  |  |  | Claims so far. |
| `worker_id` | TEXT | optional |  |  |  | Claiming worker. |
| `created_at` | TEXT | yes | ISO-8601 UTC, YYYY-MM-DDTHH:MM:SSZ |  |  | Enqueue time. |
| `started_at` | TEXT | optional | ISO-8601 UTC, YYYY-MM-DDTHH:MM:SSZ |  |  | Claim time. |
| `finished_at` | TEXT | optional | ISO-8601 UTC, YYYY-MM-DDTHH:MM:SSZ |  |  | Finish time. |
| `error_message` | TEXT | optional |  |  |  | Failure detail. |

### `id_sequence` — IdSequence

Counters for backend-allocated public IDs.

- Primary key: `name`

| Field | Type | Required | Units | Allowed values | Validation | Description |
|---|---|---|---|---|---|---|
| `name` | TEXT | PK |  | movement, recon, alert, investigation, batch, run, count, return |  | Sequence name. |
| `last_value` | INTEGER | yes |  |  |  | Last allocated counter. |

### `api_token` — ApiToken

Session tokens.

- Primary key: `token_sha256`
- Foreign keys: `(employee_id) → employee(employee_id)`

| Field | Type | Required | Units | Allowed values | Validation | Description |
|---|---|---|---|---|---|---|
| `token_sha256` | TEXT | PK |  |  |  | SHA-256 of the bearer token (token itself is never stored). |
| `employee_id` | TEXT | yes |  |  |  | Owner. |
| `created_at` | TEXT | yes | ISO-8601 UTC, YYYY-MM-DDTHH:MM:SSZ |  |  | Issue time. |
| `expires_at` | TEXT | yes | ISO-8601 UTC, YYYY-MM-DDTHH:MM:SSZ |  |  | Expiry. |

## Import error codes (precedence order)

| Code | Meaning |
|---|---|
| `MISSING_FIELD` | A required field is empty. |
| `INVALID_FORMAT` | A field does not match its type or ID format. |
| `INVALID_MOVEMENT_TYPE` | movement_type is not one of the allowed values. |
| `INVALID_QUANTITY` | Quantity is zero, non-integer, or has the wrong sign for the movement type. |
| `INVALID_TIMESTAMP` | received_at precedes occurred_at, or a timestamp is outside the ledger horizon. |
| `UNKNOWN_STORE` | store_id does not exist. |
| `UNKNOWN_SKU` | sku_id does not exist. |
| `UNKNOWN_SOURCE_REF` | The referenced source document, transfer, count or movement does not exist. |
| `WRONG_STORE_REFERENCE` | The submitted store differs from the store on the authoritative reference. |
| `TRANSFER_SKU_MISMATCH` | The SKU is not a line on the referenced transfer. |
| `SKU_NOT_ASSORTED` | The store does not carry the SKU at the event time (StoreSKU inactive). |
| `ADJUSTMENT_REASON_MISSING` | An adjustment was submitted without a reason code. |
| `INVALID_REASON_CODE` | The adjustment reason code is not allowed. |
| `ADJUSTMENT_NOT_APPROVED` | The adjustment has no approver authorised for this store at the event time. |
| `SELF_APPROVAL` | The approver is the same person who posted the adjustment. |
| `RETURN_DISPOSITION_INVALID` | Return disposition missing or not allowed. |
| `RETURN_SALE_MISMATCH` | The original sale does not match the return's store/SKU, or the quantity exceeds it. |
| `REVERSAL_TARGET_INVALID` | The movement to reverse is missing, already reversed, or on another store/SKU. |
| `COUNT_REF_INVALID` | The referenced count does not exist or belongs to another store/SKU. |
| `MOVEMENT_ID_CONFLICT` | The supplied movement_id is already used by another ledger entry. |
| `UNKNOWN_ACTOR` | actor_id / counted_by / approved_by does not exist. |
| `ACTOR_NOT_AUTHORIZED` | The actor had no role assignment permitting this action at this store at the event time. |
| `UNKNOWN_SHIFT` | shift_id does not exist. |
| `SHIFT_MISMATCH` | The shift belongs to another store or does not cover the event time. |
| `CONFLICT_PAYLOAD` | Same source event ID as an earlier posted row but a different payload. |

## Adjustment reasons

| Reason | Required sign | Store stock effect |
|---|---|---|
| `DAMAGE` | − | signed quantity |
| `EXPIRED` | − | signed quantity |
| `SAMPLING` | − | signed quantity |
| `OTHER` | − | signed quantity |
| `THEFT_CONFIRMED` | − | signed quantity |
| `INVESTIGATION_WRITE_OFF` | − | signed quantity |
| `TRANSFER_SHORTAGE` | − | none (transit write-off) |
| `VENDOR_CREDIT` | − | signed quantity |
| `FOUND` | + | signed quantity |
| `COUNT_CORRECTION` | either | signed quantity |

## Roles and permissions

| Role | Scope | Actions |
|---|---|---|
| `CASHIER` | STORE | VIEW_STORE, POST_SALE, POST_RETURN |
| `STOCK_CLERK` | STORE | VIEW_STORE, POST_RECEIPT, POST_TRANSFER, POST_ADJUSTMENT, SUBMIT_COUNT |
| `STORE_MANAGER` | STORE | VIEW_STORE, POST_RECEIPT, POST_TRANSFER, POST_ADJUSTMENT, POST_REVERSAL, APPROVE_ADJUSTMENT, SUBMIT_COUNT, IMPORT_BATCH, INVESTIGATE, EXPORT_DATA |
| `LOSS_PREVENTION` | REGION | VIEW_STORE, INVESTIGATE, ASSIGN_CASE, RESOLVE_CASE, EXPORT_DATA |
| `OPS_MANAGER` | ALL | VIEW_STORE, VIEW_ALL_STORES, APPROVE_ADJUSTMENT, IMPORT_BATCH, RUN_DETECTION, ASSIGN_CASE, INVESTIGATE, RESOLVE_CASE, EXPORT_DATA, POST_REVERSAL |
| `ANALYST` | ALL | VIEW_STORE, VIEW_ALL_STORES, RUN_DETECTION, EXPORT_DATA |
| `SERVICE_FEED` | ALL | VIEW_STORE, POST_SALE, POST_RETURN, POST_RECEIPT, POST_TRANSFER, POST_REVERSAL, IMPORT_BATCH |
| `SYSTEM` | ALL | VIEW_STORE, VIEW_ALL_STORES, RUN_DETECTION |
| `ADMIN` | ALL | VIEW_STORE, VIEW_ALL_STORES, POST_SALE, POST_RETURN, POST_RECEIPT, POST_TRANSFER, POST_ADJUSTMENT, POST_REVERSAL, APPROVE_ADJUSTMENT, SUBMIT_COUNT, IMPORT_BATCH, RUN_DETECTION, ASSIGN_CASE, INVESTIGATE, RESOLVE_CASE, EXPORT_DATA |

## Input file contracts

`movements_input.csv` columns: `source_system`, `source_event_id`, `movement_id`, `movement_type`, `store_id`, `sku_id`, `quantity`, `occurred_at`, `received_at`, `actor_id`, `shift_id`, `source_doc_id`, `transfer_id`, `reason_code`, `approved_by`, `reverses_movement_id`, `return_id`, `original_sale_movement_id`, `disposition`, `return_reason`, `count_ref`, `memo`

`counts_input.csv` columns: `source_system`, `count_id`, `store_id`, `sku_id`, `counted_qty`, `counted_at`, `count_cutoff_at`, `submitted_at`, `counted_by`, `shift_id`, `count_type`

Master CSVs use exactly the column names of their tables above.
