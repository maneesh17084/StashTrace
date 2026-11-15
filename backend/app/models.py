"""Database schema (SQLAlchemy Core).

Every column carries data-dictionary metadata in ``info``:
``desc`` (meaning), ``units``, ``allowed`` (enum values or rule), ``validation``.
``data_tools/build_data_dictionary.py`` renders docs/data_dictionary.md from this file, so the
documentation cannot drift from the schema that Alembic migrates.

Conventions
* Timestamps: TEXT, ISO-8601 UTC ``YYYY-MM-DDTHH:MM:SSZ`` (sorts lexicographically).
* Dates: TEXT ``YYYY-MM-DD``.  Money: INTEGER cents.  Quantities: INTEGER units (each).
* Public IDs are immutable, zero-padded and are the primary key unless noted.
* Tables marked ``append_only`` have UPDATE/DELETE blocked by triggers (see migration 0002).
"""
from __future__ import annotations

from sqlalchemy import (
    CheckConstraint,
    Column,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    Integer,
    MetaData,
    PrimaryKeyConstraint,
    Table,
    Text,
    UniqueConstraint,
)

from . import catalog as C

NAMING = {
    "ix": "ix_%(table_name)s_%(column_0_N_name)s",
    "uq": "uq_%(table_name)s_%(column_0_N_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}
metadata = MetaData(naming_convention=NAMING)


def _enum(values: list[str]) -> str:
    return ", ".join(values)


def col(name, type_, *args, desc: str, units: str = "", allowed: str = "", validation: str = "", **kw):
    info = {"desc": desc, "units": units, "allowed": allowed, "validation": validation}
    return Column(name, type_, *args, info=info, **kw)


def in_check(column: str, values: list[str], name: str) -> CheckConstraint:
    quoted = ", ".join(f"'{v}'" for v in values)
    return CheckConstraint(f"{column} IN ({quoted})", name=name)


TS = "ISO-8601 UTC, YYYY-MM-DDTHH:MM:SSZ"

# =============================================================== master data
store = Table(
    "store", metadata,
    col("store_id", Text, primary_key=True, desc="Store or warehouse public ID.", validation="^STORE-\\d{6}$, immutable"),
    col("store_name", Text, nullable=False, desc="Display name."),
    col("store_type", Text, nullable=False, desc="Retail store or distribution warehouse.", allowed=_enum(C.STORE_TYPES)),
    col("region_code", Text, nullable=False, desc="Operating region (drives loss-prevention scope).", validation="^R\\d{2}$"),
    col("timezone", Text, nullable=False, desc="IANA timezone (informational; all stored timestamps are UTC)."),
    col("opened_on", Text, nullable=False, desc="Opening date.", units="date"),
    col("status", Text, nullable=False, desc="Lifecycle state.", allowed=_enum(C.STORE_STATUSES)),
    col("closed_on", Text, nullable=True, desc="Closure date when status=CLOSED.", units="date", validation=">= opened_on"),
    in_check("store_type", C.STORE_TYPES, "store_type"),
    in_check("status", C.STORE_STATUSES, "status"),
    info={"entity": "Store", "desc": "A selling location or distribution warehouse."},
)

sku = Table(
    "sku", metadata,
    col("sku_id", Text, primary_key=True, desc="Product public ID.", validation="^SKU-\\d{6}$, immutable"),
    col("sku_name", Text, nullable=False, desc="Product description."),
    col("category", Text, nullable=False, desc="Merchandise category.", allowed=_enum(C.CATEGORIES)),
    col("brand", Text, nullable=False, desc="Synthetic brand name."),
    col("uom", Text, nullable=False, desc="Unit of measure; all quantities are counted in this unit.", allowed="EA"),
    col("unit_cost_cents", Integer, nullable=False, desc="Standard cost per unit.", units="USD cents", validation="> 0"),
    col("unit_price_cents", Integer, nullable=False, desc="Regular selling price per unit.", units="USD cents", validation=">= unit_cost_cents"),
    col("shrink_risk", Text, nullable=False, desc="Merchandising risk class used for count targeting.", allowed=_enum(C.SHRINK_RISK)),
    col("status", Text, nullable=False, desc="Lifecycle state.", allowed=_enum(C.SKU_STATUSES)),
    col("introduced_on", Text, nullable=False, desc="Date the SKU entered the catalogue.", units="date"),
    CheckConstraint("unit_cost_cents > 0", name="cost_pos"),
    CheckConstraint("unit_price_cents >= unit_cost_cents", name="price_ge_cost"),
    in_check("category", C.CATEGORIES, "category"),
    in_check("shrink_risk", C.SHRINK_RISK, "shrink_risk"),
    in_check("status", C.SKU_STATUSES, "status"),
    info={"entity": "SKU", "desc": "A stock-keeping unit."},
)

store_sku = Table(
    "store_sku", metadata,
    col("store_id", Text, ForeignKey("store.store_id"), nullable=False, desc="Store carrying the SKU."),
    col("sku_id", Text, ForeignKey("sku.sku_id"), nullable=False, desc="SKU carried."),
    col("active_from", Text, nullable=False, desc="Assortment effective start (inclusive).", units=TS),
    col("active_to", Text, nullable=True, desc="Assortment effective end (exclusive); NULL = open-ended.", units=TS, validation="> active_from"),
    col("opening_qty", Integer, nullable=False, desc="Opening on-hand balance at opening_as_of.", units="units", validation=">= 0"),
    col("opening_as_of", Text, nullable=False, desc="Timestamp of the opening balance; movements before it are rejected.", units=TS, validation="= active_from"),
    col("replenishment_source", Text, nullable=False, desc="How the assortment is replenished.", allowed=_enum(C.REPLENISHMENT_SOURCES)),
    col("source_warehouse_id", Text, ForeignKey("store.store_id"), nullable=True, desc="Supplying warehouse when replenishment_source=DC."),
    PrimaryKeyConstraint("store_id", "sku_id"),
    CheckConstraint("opening_qty >= 0", name="opening_nonneg"),
    CheckConstraint("active_to IS NULL OR active_to > active_from", name="active_range"),
    in_check("replenishment_source", C.REPLENISHMENT_SOURCES, "repl_source"),
    info={"entity": "StoreSKU", "desc": "Bridge Store N:M SKU (assortment) with effective dates and opening balance."},
)

employee = Table(
    "employee", metadata,
    col("employee_id", Text, primary_key=True, desc="Person or service identity.", validation="^EMP-\\d{6}$"),
    col("display_name", Text, nullable=False, desc="Synthetic display name (no real personal data)."),
    col("employee_type", Text, nullable=False, desc="HUMAN or SERVICE (automated feed identity).", allowed=_enum(C.EMPLOYEE_TYPES)),
    col("home_store_id", Text, ForeignKey("store.store_id"), nullable=True, desc="Home store for store staff."),
    col("hired_on", Text, nullable=False, desc="Start date.", units="date"),
    col("terminated_on", Text, nullable=True, desc="Leaving date; NULL while employed.", units="date", validation=">= hired_on"),
    col("status", Text, nullable=False, desc="Lifecycle state.", allowed=_enum(C.EMPLOYEE_STATUSES)),
    col("password_hash", Text, nullable=True, desc="PBKDF2 hash; only set for demo log-ins. Never exported."),
    in_check("employee_type", C.EMPLOYEE_TYPES, "employee_type"),
    in_check("status", C.EMPLOYEE_STATUSES, "status"),
    info={"entity": "Employee", "desc": "Actors who post movements, count, approve or investigate."},
)

employee_role = Table(
    "employee_role", metadata,
    col("role_code", Text, primary_key=True, desc="Role identifier.", allowed=_enum(list(C.ROLES))),
    col("role_name", Text, nullable=False, desc="Display name."),
    col("default_scope", Text, nullable=False, desc="Typical assignment scope.", allowed="STORE, REGION, ALL"),
    info={"entity": "EmployeeRole", "desc": "Role catalogue. Permissions via RolePermission; people via RoleAssignment."},
)

role_permission = Table(
    "role_permission", metadata,
    col("role_code", Text, ForeignKey("employee_role.role_code"), nullable=False, desc="Role."),
    col("action", Text, nullable=False, desc="Permitted action.", allowed=_enum(C.ACTIONS)),
    PrimaryKeyConstraint("role_code", "action"),
    in_check("action", C.ACTIONS, "action"),
    info={"entity": "RolePermission", "desc": "Bridge EmployeeRole N:M action."},
)

role_assignment = Table(
    "role_assignment", metadata,
    col("assignment_id", Text, primary_key=True, desc="Assignment ID.", validation="^RA-\\d{6}$"),
    col("employee_id", Text, ForeignKey("employee.employee_id"), nullable=False, desc="Employee holding the role."),
    col("role_code", Text, ForeignKey("employee_role.role_code"), nullable=False, desc="Role granted."),
    col("store_id", Text, ForeignKey("store.store_id"), nullable=True, desc="Store scope; NULL = all stores."),
    col("effective_from", Text, nullable=False, desc="Start of authority (inclusive).", units=TS),
    col("effective_to", Text, nullable=True, desc="End of authority (exclusive); NULL = open-ended.", units=TS, validation="> effective_from"),
    CheckConstraint("effective_to IS NULL OR effective_to > effective_from", name="eff_range"),
    Index("ix_role_assignment_emp", "employee_id", "effective_from"),
    info={"entity": "RoleAssignment", "desc": "Effective-dated grant of a role to an employee for a store (or all stores)."},
)

shift = Table(
    "shift", metadata,
    col("shift_id", Text, primary_key=True, desc="Shift ID.", validation="^SHF-2026-\\d{7}$"),
    col("store_id", Text, ForeignKey("store.store_id"), nullable=False, desc="Store the shift runs at."),
    col("shift_date", Text, nullable=False, desc="Business date.", units="date"),
    col("shift_type", Text, nullable=False, desc="Shift pattern.", allowed=_enum(C.SHIFT_TYPES)),
    col("start_at", Text, nullable=False, desc="Shift start (inclusive).", units=TS),
    col("end_at", Text, nullable=False, desc="Shift end (exclusive).", units=TS, validation="> start_at"),
    CheckConstraint("end_at > start_at", name="shift_range"),
    in_check("shift_type", C.SHIFT_TYPES, "shift_type"),
    UniqueConstraint("store_id", "start_at"),
    info={"entity": "Shift", "desc": "A staffed time window at a store; every posting is tied to one."},
)

promotion = Table(
    "promotion", metadata,
    col("promotion_id", Text, primary_key=True, desc="Promotion ID.", validation="^PRM-2026-\\d{6}$"),
    col("promo_name", Text, nullable=False, desc="Display name."),
    col("promo_type", Text, nullable=False, desc="Mechanic.", allowed=_enum(C.PROMO_TYPES)),
    col("discount_pct", Integer, nullable=False, desc="Headline discount.", units="percent", validation="1..90"),
    col("start_date", Text, nullable=False, desc="First promo day (inclusive).", units="date"),
    col("end_date", Text, nullable=False, desc="Last promo day (inclusive).", units="date", validation=">= start_date"),
    CheckConstraint("end_date >= start_date", name="promo_range"),
    CheckConstraint("discount_pct BETWEEN 1 AND 90", name="discount_range"),
    in_check("promo_type", C.PROMO_TYPES, "promo_type"),
    info={"entity": "Promotion", "desc": "Explains sales uplift; never posts stock itself."},
)

promotion_scope = Table(
    "promotion_scope", metadata,
    col("promotion_id", Text, ForeignKey("promotion.promotion_id"), nullable=False, desc="Promotion."),
    col("store_id", Text, nullable=False, desc="Store in scope."),
    col("sku_id", Text, nullable=False, desc="SKU in scope."),
    PrimaryKeyConstraint("promotion_id", "store_id", "sku_id"),
    ForeignKeyConstraint(["store_id", "sku_id"], ["store_sku.store_id", "store_sku.sku_id"]),
    Index("ix_promotion_scope_pair", "store_id", "sku_id"),
    info={"entity": "PromotionScope", "desc": "Bridge Promotion N:M StoreSKU."},
)

source_document = Table(
    "source_document", metadata,
    col("doc_id", Text, primary_key=True, desc="Authoritative upstream document.", validation="PO-2026-NNNNNN | POS-2026-NNNNNNN"),
    col("doc_type", Text, nullable=False, desc="Purchase order (receipts) or POS day batch (sales/returns).", allowed=_enum(C.DOC_TYPES)),
    col("store_id", Text, ForeignKey("store.store_id"), nullable=False, desc="Store the document belongs to — the authority for wrong-store checks."),
    col("business_date", Text, nullable=False, desc="Business date.", units="date"),
    in_check("doc_type", C.DOC_TYPES, "doc_type"),
    info={"entity": "SourceDocument", "desc": "Order/POS references that movements must match."},
)

transfer = Table(
    "transfer", metadata,
    col("transfer_id", Text, primary_key=True, desc="Transfer ID.", validation="^TRF-2026-\\d{6}$"),
    col("from_store_id", Text, ForeignKey("store.store_id"), nullable=False, desc="Dispatching location."),
    col("to_store_id", Text, ForeignKey("store.store_id"), nullable=False, desc="Receiving location.", validation="<> from_store_id"),
    col("created_at", Text, nullable=False, desc="Transfer order creation time.", units=TS),
    col("expected_arrival_at", Text, nullable=False, desc="Planned arrival.", units=TS, validation=">= created_at"),
    CheckConstraint("from_store_id <> to_store_id", name="distinct_ends"),
    CheckConstraint("expected_arrival_at >= created_at", name="arrival_after_create"),
    info={"entity": "Transfer", "desc": "Inter-location transfer order; links dispatch and receipt legs. Status is derived from legs."},
)

transfer_line = Table(
    "transfer_line", metadata,
    col("transfer_id", Text, ForeignKey("transfer.transfer_id"), nullable=False, desc="Transfer."),
    col("sku_id", Text, ForeignKey("sku.sku_id"), nullable=False, desc="SKU on the transfer."),
    col("planned_qty", Integer, nullable=False, desc="Ordered quantity.", units="units", validation="> 0"),
    PrimaryKeyConstraint("transfer_id", "sku_id"),
    CheckConstraint("planned_qty > 0", name="planned_pos"),
    info={"entity": "TransferLine", "desc": "Transfer 1:N lines (one per SKU)."},
)

# ============================================================ import & audit
import_batch = Table(
    "import_batch", metadata,
    col("batch_id", Text, primary_key=True, desc="Import batch / job reference.", validation="^IMP-2026-\\d{6}$"),
    col("batch_kind", Text, nullable=False, desc="What the file contains.", allowed=_enum(C.BATCH_KINDS)),
    col("source_system", Text, nullable=False, desc="Default upstream system for rows.", allowed=_enum(C.SOURCE_SYSTEMS)),
    col("file_name", Text, nullable=False, desc="Uploaded file name."),
    col("file_sha256", Text, nullable=False, desc="SHA-256 of the uploaded bytes."),
    col("header_csv", Text, nullable=False, desc="Header line of the CSV (raw rows are stored without it)."),
    col("submitted_by", Text, ForeignKey("employee.employee_id"), nullable=False, desc="Uploader."),
    col("submitted_at", Text, nullable=False, desc="Receive time.", units=TS),
    info={"entity": "ImportBatch", "desc": "One uploaded file. Immutable; processing state lives in import_batch_status."},
)

import_batch_status = Table(
    "import_batch_status", metadata,
    col("batch_id", Text, ForeignKey("import_batch.batch_id"), primary_key=True, desc="Batch."),
    col("status", Text, nullable=False, desc="Processing state.", allowed=_enum(C.BATCH_STATUSES)),
    col("received_count", Integer, nullable=False, default=0, desc="Rows saved on receive.", units="rows"),
    col("processed_count", Integer, nullable=False, default=0, desc="Rows validated so far.", units="rows"),
    col("posted_count", Integer, nullable=False, default=0, desc="Rows posted to the ledger / counts.", units="rows"),
    col("quarantined_count", Integer, nullable=False, default=0, desc="Rows rejected with an error code.", units="rows"),
    col("duplicate_count", Integer, nullable=False, default=0, desc="Exact replays of earlier rows.", units="rows"),
    col("conflict_count", Integer, nullable=False, default=0, desc="Same event ID, different payload.", units="rows"),
    col("started_at", Text, nullable=True, desc="Worker start.", units=TS),
    col("finished_at", Text, nullable=True, desc="Worker finish.", units=TS),
    col("error_message", Text, nullable=True, desc="Fatal error if FAILED."),
    in_check("status", C.BATCH_STATUSES, "status"),
    CheckConstraint("posted_count + quarantined_count + duplicate_count + conflict_count = processed_count", name="counts_add_up"),
    info={"entity": "ImportBatchStatus", "desc": "Mutable progress projection of a batch (1:1)."},
)

import_raw_row = Table(
    "import_raw_row", metadata,
    col("row_id", Integer, primary_key=True, autoincrement=True, desc="Surrogate key."),
    col("batch_id", Text, ForeignKey("import_batch.batch_id"), nullable=False, desc="Batch."),
    col("row_number", Integer, nullable=False, desc="1-based data row number in the file.", validation="unique per batch"),
    col("raw_line", Text, nullable=False, desc="Row exactly as received (CSV line or JSON)."),
    UniqueConstraint("batch_id", "row_number"),
    info={"entity": "ImportRawRow", "desc": "Untrusted input as received (append-only). Never read by stock calculations.", "append_only": True},
)

import_row_result = Table(
    "import_row_result", metadata,
    col("row_id", Integer, ForeignKey("import_raw_row.row_id"), primary_key=True, desc="Raw row validated."),
    col("batch_id", Text, ForeignKey("import_batch.batch_id"), nullable=False, desc="Batch (denormalised for filtering)."),
    col("source_system", Text, nullable=True, desc="Upstream system of the row."),
    col("source_event_id", Text, nullable=True, desc="Upstream event / count ID (idempotency key with source_system)."),
    col("payload_sha256", Text, nullable=True, desc="SHA-256 of the canonical business payload (excludes received_at)."),
    col("outcome", Text, nullable=False, desc="Validation outcome.", allowed=_enum(C.ROW_OUTCOMES)),
    col("error_code", Text, nullable=True, desc="Primary (highest-precedence) error.", allowed="see catalog.ERROR_CODES"),
    col("errors_json", Text, nullable=True, desc="All errors found, JSON list of {code, field, message}."),
    col("movement_type", Text, nullable=True, desc="Submitted movement type (movement batches)."),
    col("submitted_store_id", Text, nullable=True, desc="Store as submitted (may be wrong)."),
    col("authoritative_store_id", Text, nullable=True, desc="Store from the authoritative reference when it could be resolved."),
    col("sku_id", Text, nullable=True, desc="SKU as submitted."),
    col("occurred_at", Text, nullable=True, desc="Event time as submitted.", units=TS),
    col("explain_qty", Integer, nullable=False, default=0, desc="Units this row would have removed from stock had it posted (quarantined rows explain count shortages).", units="units"),
    col("corrects_row_id", Integer, ForeignKey("import_raw_row.row_id"), nullable=True, desc="Earlier quarantined row this submission corrects."),
    col("duplicate_of_row_id", Integer, ForeignKey("import_raw_row.row_id"), nullable=True, desc="Earlier row this replays/conflicts with."),
    col("movement_id", Text, ForeignKey("inventory_movement.movement_id"), nullable=True, desc="Ledger entry created (POSTED) or matched (DUPLICATE)."),
    col("count_id", Text, ForeignKey("cycle_count.count_id"), nullable=True, desc="Count created or matched (count batches)."),
    col("processed_at", Text, nullable=False, desc="Validation time.", units=TS),
    in_check("outcome", C.ROW_OUTCOMES, "outcome"),
    Index("ix_import_row_result_batch_outcome", "batch_id", "outcome"),
    Index("ix_import_row_result_explain", "authoritative_store_id", "sku_id", "outcome"),
    Index("ix_import_row_result_corrects", "corrects_row_id"),
    Index("ix_import_row_result_submitted_store", "submitted_store_id", "outcome"),
    Index("ix_import_row_result_exceptions", "outcome", "error_code", "submitted_store_id", "occurred_at"),
    info={"entity": "ImportRowResult", "desc": "Validation outcome per raw row (append-only, 1:1 with ImportRawRow).", "append_only": True},
)

ingest_key = Table(
    "ingest_key", metadata,
    col("source_system", Text, nullable=False, desc="Upstream system."),
    col("source_event_id", Text, nullable=False, desc="Upstream event ID."),
    col("latest_row_id", Integer, ForeignKey("import_raw_row.row_id"), nullable=False, desc="Row that currently defines this key's outcome."),
    col("payload_sha256", Text, nullable=False, desc="Payload hash of latest_row_id."),
    col("outcome", Text, nullable=False, desc="POSTED or QUARANTINED.", allowed="POSTED, QUARANTINED"),
    col("movement_id", Text, nullable=True, desc="Ledger entry when POSTED (movement keys)."),
    col("count_id", Text, nullable=True, desc="Count when POSTED (count keys)."),
    PrimaryKeyConstraint("source_system", "source_event_id"),
    Index("ix_ingest_key_event", "source_event_id"),
    info={"entity": "IngestKey", "desc": "Idempotency index (derived, rebuildable from import_row_result)."},
)

# ============================================================ stock activity
inventory_movement = Table(
    "inventory_movement", metadata,
    col("ledger_seq", Integer, primary_key=True, autoincrement=True, desc="Monotonic posting sequence; used as the ledger watermark."),
    col("movement_id", Text, nullable=False, unique=True, desc="Public movement ID.", validation="^MOV-2026-\\d{8}$, immutable"),
    col("source_system", Text, nullable=False, desc="Upstream system.", allowed=_enum(C.SOURCE_SYSTEMS)),
    col("source_event_id", Text, nullable=False, desc="Upstream event ID; unique with source_system (idempotency)."),
    col("movement_type", Text, nullable=False, desc="Kind of movement.", allowed=_enum(C.MOVEMENT_TYPES)),
    col("store_id", Text, nullable=False, desc="Store whose on-hand is affected."),
    col("sku_id", Text, nullable=False, desc="SKU."),
    col("quantity", Integer, nullable=False, desc="Units in the event. Positive, except ADJUSTMENT which is signed.", units="units", validation="<> 0"),
    col("stock_qty", Integer, nullable=False, desc="Signed effect on store on-hand (derived from type/disposition/reason).", units="units"),
    col("occurred_at", Text, nullable=False, desc="When it physically happened.", units=TS),
    col("received_at", Text, nullable=False, desc="When the upstream system emitted it / we received it.", units=TS, validation=">= occurred_at"),
    col("posted_at", Text, nullable=False, desc="When StashTrace posted it to the ledger.", units=TS),
    col("actor_id", Text, ForeignKey("employee.employee_id"), nullable=False, desc="Who performed it (service identity for feeds)."),
    col("actor_role_code", Text, ForeignKey("employee_role.role_code"), nullable=False, desc="Role that authorised the actor at occurred_at."),
    col("shift_id", Text, ForeignKey("shift.shift_id"), nullable=True, desc="Shift covering occurred_at at this store."),
    col("source_doc_id", Text, ForeignKey("source_document.doc_id"), nullable=True, desc="PO (receipts) or POS batch (sales/returns)."),
    col("transfer_id", Text, ForeignKey("transfer.transfer_id"), nullable=True, desc="Transfer for TRANSFER_* legs and transit write-offs."),
    col("count_ref", Text, ForeignKey("cycle_count.count_id"), nullable=True, desc="Count that a COUNT_CORRECTION adjustment follows."),
    col("investigation_id", Text, ForeignKey("investigation.investigation_id"), nullable=True, desc="Case that approved an INVESTIGATION_WRITE_OFF."),
    col("reason_code", Text, nullable=True, desc="Adjustment reason (required for ADJUSTMENT).", allowed=_enum(list(C.ADJUSTMENT_REASONS))),
    col("approved_by", Text, ForeignKey("employee.employee_id"), nullable=True, desc="Approver for adjustments (must differ from actor)."),
    col("approver_role_code", Text, ForeignKey("employee_role.role_code"), nullable=True, desc="Role that authorised the approval."),
    col("reverses_movement_id", Text, ForeignKey("inventory_movement.movement_id"), nullable=True, unique=True, desc="Original entry a REVERSAL negates (at most one reversal each)."),
    col("memo", Text, nullable=True, desc="Free-text justification; required for REVERSAL.", validation="<= 500 chars"),
    col("batch_id", Text, ForeignKey("import_batch.batch_id"), nullable=False, desc="Import batch (API postings use a one-row batch)."),
    col("import_row_id", Integer, ForeignKey("import_raw_row.row_id"), nullable=False, desc="Raw row that produced the posting."),
    ForeignKeyConstraint(["store_id", "sku_id"], ["store_sku.store_id", "store_sku.sku_id"]),
    UniqueConstraint("source_system", "source_event_id"),
    in_check("movement_type", C.MOVEMENT_TYPES, "movement_type"),
    CheckConstraint("quantity <> 0", name="qty_nonzero"),
    CheckConstraint("movement_type = 'ADJUSTMENT' OR quantity > 0", name="qty_positive_unless_adj"),
    CheckConstraint("received_at >= occurred_at", name="received_after_occurred"),
    CheckConstraint("movement_type <> 'ADJUSTMENT' OR (reason_code IS NOT NULL AND approved_by IS NOT NULL)", name="adj_reason_approval"),
    CheckConstraint("movement_type <> 'REVERSAL' OR reverses_movement_id IS NOT NULL", name="reversal_target"),
    CheckConstraint("movement_type NOT IN ('TRANSFER_IN','TRANSFER_OUT') OR transfer_id IS NOT NULL", name="transfer_ref"),
    Index("ix_mov_pair_time", "store_id", "sku_id", "occurred_at"),
    Index("ix_mov_type_store_time", "movement_type", "store_id", "occurred_at"),
    Index("ix_mov_transfer", "transfer_id", "sku_id"),
    info={"entity": "InventoryMovement", "desc": "The trusted, append-only inventory ledger. Corrections are linked REVERSAL + replacement entries.", "append_only": True},
)

customer_return = Table(
    "customer_return", metadata,
    col("return_id", Text, primary_key=True, desc="Return ID.", validation="^RET-2026-\\d{6}$"),
    col("store_id", Text, ForeignKey("store.store_id"), nullable=False, desc="Store receiving the return."),
    col("sku_id", Text, ForeignKey("sku.sku_id"), nullable=False, desc="SKU returned."),
    col("quantity", Integer, nullable=False, desc="Units returned.", units="units", validation="> 0"),
    col("disposition", Text, nullable=False, desc="What happened to the goods. Only RESTOCK adds sellable stock.", allowed=_enum(C.RETURN_DISPOSITIONS)),
    col("return_reason", Text, nullable=True, desc="Customer reason.", allowed=_enum(C.RETURN_REASONS)),
    col("returned_at", Text, nullable=False, desc="Return time.", units=TS),
    col("original_sale_movement_id", Text, ForeignKey("inventory_movement.movement_id"), nullable=True, desc="Optional link to the original SALE (receipted return)."),
    col("restocked", Integer, nullable=False, desc="1 when disposition permits restocking.", allowed="0, 1"),
    col("movement_id", Text, ForeignKey("inventory_movement.movement_id"), nullable=False, unique=True, desc="RETURN ledger entry (1:1)."),
    CheckConstraint("quantity > 0", name="ret_qty_pos"),
    in_check("disposition", C.RETURN_DISPOSITIONS, "disposition"),
    info={"entity": "Return", "desc": "Customer return; posted with a RETURN movement whose stock effect depends on disposition.", "append_only": True},
)

stock_balance = Table(
    "stock_balance", metadata,
    col("store_id", Text, nullable=False, desc="Store."),
    col("sku_id", Text, nullable=False, desc="SKU."),
    col("on_hand", Integer, nullable=False, desc="opening_qty + SUM(stock_qty) over the ledger. Cached; rebuildable.", units="units"),
    col("last_ledger_seq", Integer, nullable=False, desc="Highest ledger_seq included."),
    col("updated_at", Text, nullable=False, desc="Last update.", units=TS),
    PrimaryKeyConstraint("store_id", "sku_id"),
    ForeignKeyConstraint(["store_id", "sku_id"], ["store_sku.store_id", "store_sku.sku_id"]),
    info={"entity": "StockBalance", "desc": "Cached on-hand per StoreSKU, updated in the same transaction as each posting."},
)

# ====================================================== counts & reconciliation
cycle_count = Table(
    "cycle_count", metadata,
    col("count_id", Text, primary_key=True, desc="Count ID.", validation="^CNT-2026-\\d{6}$"),
    col("store_id", Text, nullable=False, desc="Store counted."),
    col("sku_id", Text, nullable=False, desc="SKU counted."),
    col("counted_qty", Integer, nullable=False, desc="Physical quantity found.", units="units", validation=">= 0"),
    col("counted_at", Text, nullable=False, desc="When the physical count was performed.", units=TS),
    col("count_cutoff_at", Text, nullable=False, desc="Ledger cutoff the count represents (movements with occurred_at <= cutoff are included).", units=TS),
    col("submitted_at", Text, nullable=False, desc="When the count was submitted.", units=TS),
    col("counted_by", Text, ForeignKey("employee.employee_id"), nullable=False, desc="Counter."),
    col("counter_role_code", Text, ForeignKey("employee_role.role_code"), nullable=False, desc="Role authorising the count."),
    col("shift_id", Text, ForeignKey("shift.shift_id"), nullable=True, desc="Shift covering counted_at."),
    col("count_type", Text, nullable=False, desc="Count programme.", allowed=_enum(C.COUNT_TYPES)),
    col("batch_id", Text, ForeignKey("import_batch.batch_id"), nullable=True, desc="Import batch (NULL for API submissions)."),
    ForeignKeyConstraint(["store_id", "sku_id"], ["store_sku.store_id", "store_sku.sku_id"]),
    CheckConstraint("counted_qty >= 0", name="counted_nonneg"),
    in_check("count_type", C.COUNT_TYPES, "count_type"),
    Index("ix_cycle_count_pair", "store_id", "sku_id", "count_cutoff_at"),
    info={"entity": "CycleCount", "desc": "Physical count snapshot. Never posts stock.", "append_only": True},
)

count_reconciliation = Table(
    "count_reconciliation", metadata,
    col("recon_id", Text, primary_key=True, desc="Reconciliation version ID.", validation="^REC-2026-\\d{6}$"),
    col("count_id", Text, ForeignKey("cycle_count.count_id"), nullable=False, desc="Count reconciled."),
    col("version", Integer, nullable=False, desc="1, 2, ... per count; highest is current.", validation="unique per count"),
    col("supersedes_recon_id", Text, ForeignKey("count_reconciliation.recon_id"), nullable=True, desc="Previous version."),
    col("trigger", Text, nullable=False, desc="Why this version was computed.", allowed=_enum(C.RECON_TRIGGERS)),
    col("opening_qty", Integer, nullable=False, desc="StoreSKU opening balance.", units="units"),
    col("receipts_qty", Integer, nullable=False, desc="+ RECEIPT stock (net of reversals).", units="units"),
    col("transfer_in_qty", Integer, nullable=False, desc="+ TRANSFER_IN stock.", units="units"),
    col("restocked_returns_qty", Integer, nullable=False, desc="+ RETURN stock (restockable dispositions only).", units="units"),
    col("sales_qty", Integer, nullable=False, desc="- SALE units (positive number).", units="units"),
    col("transfer_out_qty", Integer, nullable=False, desc="- TRANSFER_OUT units (positive number).", units="units"),
    col("adjustments_qty", Integer, nullable=False, desc="+ signed approved ADJUSTMENT stock.", units="units"),
    col("expected_qty", Integer, nullable=False, desc="Derived expected on-hand at cutoff.", units="units"),
    col("counted_qty", Integer, nullable=False, desc="Copied from the count.", units="units"),
    col("variance_qty", Integer, nullable=False, desc="counted_qty - expected_qty.", units="units"),
    col("variance_value_cents", Integer, nullable=False, desc="variance_qty * unit_cost_cents.", units="USD cents"),
    col("movement_count", Integer, nullable=False, desc="Ledger entries contributing.", units="rows"),
    col("ledger_watermark_seq", Integer, nullable=False, desc="Max ledger_seq visible when computed (snapshot)."),
    col("basis_status", Text, nullable=False, desc="Whether the count basis is reliable.", allowed=_enum(C.RECON_BASIS)),
    col("basis_notes", Text, nullable=True, desc="JSON details for TIMING_REVIEW (violations, movements between)."),
    col("created_at", Text, nullable=False, desc="Computation time.", units=TS),
    UniqueConstraint("count_id", "version"),
    in_check("trigger", C.RECON_TRIGGERS, "trigger"),
    in_check("basis_status", C.RECON_BASIS, "basis_status"),
    CheckConstraint(
        "expected_qty = opening_qty + receipts_qty + transfer_in_qty + restocked_returns_qty"
        " - sales_qty - transfer_out_qty + adjustments_qty", name="stock_equation"),
    CheckConstraint("variance_qty = counted_qty - expected_qty", name="variance_equation"),
    info={"entity": "CountReconciliation", "desc": "Versioned expected-vs-counted calculation for a count (CycleCount 1:N).", "append_only": True},
)

# =============================================================== detection
rule_definition = Table(
    "rule_definition", metadata,
    col("rule_code", Text, nullable=False, desc="Rule identifier."),
    col("rule_version", Integer, nullable=False, desc="Version; params never change within a version."),
    col("alert_class", Text, nullable=False, desc="Class of alert produced.", allowed=_enum(C.ALERT_CLASSES)),
    col("description", Text, nullable=False, desc="Plain-language logic."),
    col("params_json", Text, nullable=False, desc="Thresholds and windows, JSON."),
    col("is_active", Integer, nullable=False, desc="1 when used by new runs.", allowed="0, 1"),
    col("effective_from", Text, nullable=False, desc="Activation time.", units=TS),
    PrimaryKeyConstraint("rule_code", "rule_version"),
    info={"entity": "RuleDefinition", "desc": "Versioned, explainable detection rules."},
)

detection_run = Table(
    "detection_run", metadata,
    col("run_id", Text, primary_key=True, desc="Run ID.", validation="^RUN-2026-\\d{6}$"),
    col("requested_by", Text, ForeignKey("employee.employee_id"), nullable=False, desc="Requester."),
    col("requested_at", Text, nullable=False, desc="Queue time.", units=TS),
    col("as_of", Text, nullable=True, desc="Evaluation horizon (default: latest occurred_at in ledger).", units=TS),
    col("rules_json", Text, nullable=False, desc="Rule codes+versions+params used."),
    info={"entity": "DetectionRun", "desc": "A detection execution (immutable request)."},
)

detection_run_status = Table(
    "detection_run_status", metadata,
    col("run_id", Text, ForeignKey("detection_run.run_id"), primary_key=True, desc="Run."),
    col("status", Text, nullable=False, desc="State.", allowed=_enum(C.RUN_STATUSES)),
    col("started_at", Text, nullable=True, desc="Start.", units=TS),
    col("finished_at", Text, nullable=True, desc="Finish.", units=TS),
    col("ledger_watermark_seq", Integer, nullable=True, desc="Ledger snapshot evaluated."),
    col("alerts_new", Integer, nullable=False, default=0, desc="Alerts created.", units="alerts"),
    col("alerts_redetected", Integer, nullable=False, default=0, desc="Existing case keys seen again.", units="alerts"),
    col("duration_ms", Integer, nullable=True, desc="Wall time.", units="ms"),
    col("error_message", Text, nullable=True, desc="Failure detail."),
    in_check("status", C.RUN_STATUSES, "status"),
    info={"entity": "DetectionRunStatus", "desc": "Mutable progress projection of a run (1:1)."},
)

shrinkage_alert = Table(
    "shrinkage_alert", metadata,
    col("alert_id", Text, primary_key=True, desc="Alert ID.", validation="^SHR-2026-\\d{6}$"),
    col("case_key", Text, nullable=False, unique=True, desc="Stable discrepancy key; reruns never duplicate it."),
    col("rule_code", Text, nullable=False, desc="Rule that fired."),
    col("rule_version", Integer, nullable=False, desc="Rule version."),
    col("alert_class", Text, nullable=False, desc="SHRINKAGE vs data quality etc.", allowed=_enum(C.ALERT_CLASSES)),
    col("alert_type", Text, nullable=False, desc="Specific finding.", allowed=_enum(C.ALERT_TYPES)),
    col("severity", Text, nullable=False, desc="Triage priority.", allowed=_enum(C.SEVERITIES)),
    col("store_id", Text, ForeignKey("store.store_id"), nullable=False, desc="Primary store (receiver for transfers)."),
    col("related_store_id", Text, ForeignKey("store.store_id"), nullable=True, desc="Other end of a transfer."),
    col("sku_id", Text, ForeignKey("sku.sku_id"), nullable=True, desc="SKU when the alert is SKU-level."),
    col("count_id", Text, ForeignKey("cycle_count.count_id"), nullable=True, desc="Count (count-grain rules)."),
    col("recon_id", Text, ForeignKey("count_reconciliation.recon_id"), nullable=True, desc="Reconciliation version evaluated."),
    col("transfer_id", Text, ForeignKey("transfer.transfer_id"), nullable=True, desc="Transfer (transfer rule)."),
    col("window_start", Text, nullable=True, desc="Pattern window start.", units=TS),
    col("window_end", Text, nullable=True, desc="Pattern window end.", units=TS),
    col("threshold_value", Integer, nullable=True, desc="Threshold applied.", units="units"),
    col("observed_value", Integer, nullable=True, desc="Observed value compared to the threshold.", units="units"),
    col("calculation_json", Text, nullable=False, desc="Full calculation shown to users."),
    col("reason_text", Text, nullable=False, desc="Evidence-based explanation."),
    col("first_run_id", Text, ForeignKey("detection_run.run_id"), nullable=False, desc="Run that created it."),
    col("investigation_id", Text, ForeignKey("investigation.investigation_id"), nullable=False, desc="Case opened for it (N:1)."),
    col("created_at", Text, nullable=False, desc="Creation time.", units=TS),
    ForeignKeyConstraint(["rule_code", "rule_version"], ["rule_definition.rule_code", "rule_definition.rule_version"]),
    in_check("alert_class", C.ALERT_CLASSES, "alert_class"),
    in_check("alert_type", C.ALERT_TYPES, "alert_type"),
    in_check("severity", C.SEVERITIES, "severity"),
    Index("ix_alert_store_class", "store_id", "alert_class"),
    Index("ix_alert_related_store", "related_store_id"),
    info={"entity": "ShrinkageAlert", "desc": "Explainable finding. Immutable; its open/closed state follows its investigation.", "append_only": True},
)

alert_evidence = Table(
    "alert_evidence", metadata,
    col("evidence_id", Integer, primary_key=True, autoincrement=True, desc="Surrogate key."),
    col("alert_id", Text, ForeignKey("shrinkage_alert.alert_id"), nullable=False, desc="Alert."),
    col("evidence_type", Text, nullable=False, desc="Kind of source record.", allowed=_enum(C.EVIDENCE_TYPES)),
    col("evidence_role", Text, nullable=False, desc="How it supports the alert (CONTRIBUTING, EXPLANATION, DISPATCH, RECEIPT, WRITE_OFF, ...)."),
    col("movement_id", Text, ForeignKey("inventory_movement.movement_id"), nullable=True, desc="Ledger entry."),
    col("count_id", Text, ForeignKey("cycle_count.count_id"), nullable=True, desc="Count."),
    col("recon_id", Text, ForeignKey("count_reconciliation.recon_id"), nullable=True, desc="Reconciliation."),
    col("import_row_id", Integer, ForeignKey("import_raw_row.row_id"), nullable=True, desc="Quarantined row."),
    col("transfer_id", Text, ForeignKey("transfer.transfer_id"), nullable=True, desc="Transfer."),
    col("sku_id", Text, ForeignKey("sku.sku_id"), nullable=True, desc="SKU of the evidence."),
    col("qty", Integer, nullable=True, desc="Units this evidence contributes.", units="units"),
    col("note", Text, nullable=True, desc="Short explanation."),
    in_check("evidence_type", C.EVIDENCE_TYPES, "evidence_type"),
    Index("ix_alert_evidence_alert", "alert_id"),
    info={"entity": "AlertEvidence", "desc": "Alert 1:N links to source records.", "append_only": True},
)

detection_run_alert = Table(
    "detection_run_alert", metadata,
    col("run_id", Text, ForeignKey("detection_run.run_id"), nullable=False, desc="Run."),
    col("alert_id", Text, ForeignKey("shrinkage_alert.alert_id"), nullable=False, desc="Alert."),
    col("outcome", Text, nullable=False, desc="NEW or REDETECTED.", allowed="NEW, REDETECTED"),
    col("observed_value", Integer, nullable=True, desc="Value observed in this run.", units="units"),
    PrimaryKeyConstraint("run_id", "alert_id"),
    info={"entity": "DetectionRunAlert", "desc": "Bridge DetectionRun N:M ShrinkageAlert (history of re-detections).", "append_only": True},
)

investigation = Table(
    "investigation", metadata,
    col("investigation_id", Text, primary_key=True, desc="Case ID.", validation="^INV-2026-\\d{6}$"),
    col("case_key", Text, nullable=False, unique=True, desc="Same stable key as the originating alert."),
    col("origin_type", Text, nullable=False, desc="What raised the case.", allowed=_enum(C.INVESTIGATION_ORIGINS)),
    col("store_id", Text, ForeignKey("store.store_id"), nullable=False, desc="Primary store."),
    col("related_store_id", Text, ForeignKey("store.store_id"), nullable=True, desc="Other transfer end."),
    col("opened_at", Text, nullable=False, desc="Open time.", units=TS),
    info={"entity": "Investigation", "desc": "Case header (immutable). Current state is projected from investigation_event."},
)

investigation_event = Table(
    "investigation_event", metadata,
    col("event_id", Integer, primary_key=True, autoincrement=True, desc="Surrogate key."),
    col("investigation_id", Text, ForeignKey("investigation.investigation_id"), nullable=False, desc="Case."),
    col("seq", Integer, nullable=False, desc="1-based order within the case.", validation="unique per case, gapless"),
    col("event_type", Text, nullable=False, desc="What happened.", allowed=_enum(C.INVESTIGATION_EVENT_TYPES)),
    col("from_status", Text, nullable=True, desc="Status before.", allowed=_enum(C.INVESTIGATION_STATUSES)),
    col("to_status", Text, nullable=False, desc="Status after.", allowed=_enum(C.INVESTIGATION_STATUSES)),
    col("actor_id", Text, ForeignKey("employee.employee_id"), nullable=False, desc="Who did it."),
    col("actor_role_code", Text, ForeignKey("employee_role.role_code"), nullable=False, desc="Role that authorised it."),
    col("assignee_id", Text, ForeignKey("employee.employee_id"), nullable=True, desc="Assignee (ASSIGNED)."),
    col("note", Text, nullable=True, desc="Finding or reason text."),
    col("resolution_code", Text, nullable=True, desc="Decision (RESOLVED).", allowed=_enum(C.RESOLUTION_CODES)),
    col("write_off_movement_id", Text, ForeignKey("inventory_movement.movement_id"), nullable=True, desc="Separate ledger write-off approved with the resolution."),
    col("run_id", Text, ForeignKey("detection_run.run_id"), nullable=True, desc="Run (OPENED/REDETECTED)."),
    col("occurred_at", Text, nullable=False, desc="Event time.", units=TS),
    UniqueConstraint("investigation_id", "seq"),
    in_check("event_type", C.INVESTIGATION_EVENT_TYPES, "event_type"),
    in_check("to_status", C.INVESTIGATION_STATUSES, "to_status"),
    CheckConstraint("event_type <> 'RESOLVED' OR (resolution_code IS NOT NULL AND length(note) >= 10)", name="resolution_reason"),
    info={"entity": "InvestigationEvent", "desc": "Append-only case history (Investigation 1:N).", "append_only": True},
)

investigation_state = Table(
    "investigation_state", metadata,
    col("investigation_id", Text, ForeignKey("investigation.investigation_id"), primary_key=True, desc="Case."),
    col("status", Text, nullable=False, desc="Current status (projection of last event).", allowed=_enum(C.INVESTIGATION_STATUSES)),
    col("last_seq", Integer, nullable=False, desc="seq of the last applied event (optimistic concurrency)."),
    col("assignee_id", Text, ForeignKey("employee.employee_id"), nullable=True, desc="Current assignee."),
    col("resolution_code", Text, nullable=True, desc="Final decision."),
    col("resolved_at", Text, nullable=True, desc="Resolution time.", units=TS),
    col("updated_at", Text, nullable=False, desc="Last change.", units=TS),
    in_check("status", C.INVESTIGATION_STATUSES, "status"),
    Index("ix_investigation_state_status", "status"),
    info={"entity": "InvestigationState", "desc": "Mutable projection of the event log (rebuildable)."},
)

# ============================================================ infrastructure
job = Table(
    "job", metadata,
    col("job_id", Integer, primary_key=True, autoincrement=True, desc="Job ID."),
    col("job_type", Text, nullable=False, desc="Work kind.", allowed=_enum(C.JOB_TYPES)),
    col("ref_id", Text, nullable=False, desc="batch_id or run_id."),
    col("status", Text, nullable=False, desc="Queue state.", allowed=_enum(C.JOB_STATUSES)),
    col("attempts", Integer, nullable=False, default=0, desc="Claims so far."),
    col("worker_id", Text, nullable=True, desc="Claiming worker."),
    col("created_at", Text, nullable=False, desc="Enqueue time.", units=TS),
    col("started_at", Text, nullable=True, desc="Claim time.", units=TS),
    col("finished_at", Text, nullable=True, desc="Finish time.", units=TS),
    col("error_message", Text, nullable=True, desc="Failure detail."),
    in_check("job_type", C.JOB_TYPES, "job_type"),
    in_check("status", C.JOB_STATUSES, "status"),
    Index("ix_job_status", "status", "job_id"),
    info={"entity": "Job", "desc": "Background work queue polled by the worker."},
)

id_sequence = Table(
    "id_sequence", metadata,
    col("name", Text, primary_key=True, desc="Sequence name.", allowed=", ".join(C.SEQUENCES)),
    col("last_value", Integer, nullable=False, desc="Last allocated counter."),
    info={"entity": "IdSequence", "desc": "Counters for backend-allocated public IDs."},
)

api_token = Table(
    "api_token", metadata,
    col("token_sha256", Text, primary_key=True, desc="SHA-256 of the bearer token (token itself is never stored)."),
    col("employee_id", Text, ForeignKey("employee.employee_id"), nullable=False, desc="Owner."),
    col("created_at", Text, nullable=False, desc="Issue time.", units=TS),
    col("expires_at", Text, nullable=False, desc="Expiry.", units=TS),
    info={"entity": "ApiToken", "desc": "Session tokens."},
)

APPEND_ONLY_TABLES = [t.name for t in metadata.tables.values() if t.info.get("append_only")]
