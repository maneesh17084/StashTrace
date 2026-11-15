"""Static reference catalogue: enums, roles, permissions, reason codes, rule definitions.

This module is the single source of truth for allowed values. The generator, the
validators, the API schemas and the data dictionary all import from here.
"""
from __future__ import annotations

import re

# --------------------------------------------------------------------------- IDs
ID_PATTERNS: dict[str, re.Pattern[str]] = {
    "store": re.compile(r"^STORE-\d{6}$"),
    "sku": re.compile(r"^SKU-\d{6}$"),
    "employee": re.compile(r"^EMP-\d{6}$"),
    "assignment": re.compile(r"^RA-\d{6}$"),
    "shift": re.compile(r"^SHF-2026-\d{7}$"),
    "promotion": re.compile(r"^PRM-2026-\d{6}$"),
    "doc": re.compile(r"^(PO-2026-\d{6}|POS-2026-\d{7})$"),
    "transfer": re.compile(r"^TRF-2026-\d{6}$"),
    "movement": re.compile(r"^MOV-2026-\d{8}$"),
    "return": re.compile(r"^RET-2026-\d{6}$"),
    "count": re.compile(r"^CNT-2026-\d{6}$"),
    "recon": re.compile(r"^REC-2026-\d{6}$"),
    "alert": re.compile(r"^SHR-2026-\d{6}$"),
    "investigation": re.compile(r"^INV-2026-\d{6}$"),
    "batch": re.compile(r"^IMP-2026-\d{6}$"),
    "run": re.compile(r"^RUN-2026-\d{6}$"),
}

# Sequences allocated by the backend: name -> (prefix, width)
SEQUENCES: dict[str, tuple[str, int]] = {
    "movement": ("MOV-2026-", 8),
    "recon": ("REC-2026-", 6),
    "alert": ("SHR-2026-", 6),
    "investigation": ("INV-2026-", 6),
    "batch": ("IMP-2026-", 6),
    "run": ("RUN-2026-", 6),
    "count": ("CNT-2026-", 6),
    "return": ("RET-2026-", 6),
}

TS_RE = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$")
DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")

# ------------------------------------------------------------------------ enums
STORE_TYPES = ["STORE", "WAREHOUSE"]
STORE_STATUSES = ["ACTIVE", "CLOSED"]
SKU_STATUSES = ["ACTIVE", "DISCONTINUED"]
SHRINK_RISK = ["LOW", "MEDIUM", "HIGH"]
CATEGORIES = [
    "GROCERY", "BEVERAGE", "SPIRITS", "HEALTH_BEAUTY", "COSMETICS", "ELECTRONICS",
    "ACCESSORIES", "HOME", "TOYS", "APPAREL", "FRESH", "HARDWARE",
]
EMPLOYEE_TYPES = ["HUMAN", "SERVICE"]
EMPLOYEE_STATUSES = ["ACTIVE", "TERMINATED"]
SHIFT_TYPES = ["OPEN", "CLOSE", "DAY", "SWING"]
PROMO_TYPES = ["PERCENT_OFF", "MULTI_BUY", "CLEARANCE"]
DOC_TYPES = ["PURCHASE_ORDER", "POS_BATCH"]
REPLENISHMENT_SOURCES = ["VENDOR", "DC"]

MOVEMENT_TYPES = ["RECEIPT", "SALE", "TRANSFER_OUT", "TRANSFER_IN", "RETURN", "ADJUSTMENT", "REVERSAL"]
RETURN_DISPOSITIONS = ["RESTOCK", "DAMAGED", "VENDOR_RETURN", "DISPOSE"]
RESTOCKING_DISPOSITIONS = {"RESTOCK"}
RETURN_REASONS = ["CHANGED_MIND", "DEFECTIVE", "WRONG_ITEM", "NOT_AS_DESCRIBED"]

# Adjustment reason -> allowed sign of quantity (+1, -1 or 0 for "either")
ADJUSTMENT_REASONS: dict[str, int] = {
    "DAMAGE": -1,
    "EXPIRED": -1,
    "SAMPLING": -1,
    "OTHER": -1,
    "THEFT_CONFIRMED": -1,
    "INVESTIGATION_WRITE_OFF": -1,
    "TRANSFER_SHORTAGE": -1,  # transit write-off: recorded loss, zero store stock effect
    "VENDOR_CREDIT": -1,
    "FOUND": 1,
    "COUNT_CORRECTION": 0,
}
# Reasons whose posting does NOT change store on-hand (loss is booked against goods in transit).
ZERO_STOCK_REASONS = {"TRANSFER_SHORTAGE"}
# Discretionary write-offs monitored by the repeated-adjustment rule.
DISCRETIONARY_REASONS = ["DAMAGE", "EXPIRED", "SAMPLING", "OTHER"]

COUNT_TYPES = ["SCHEDULED", "AD_HOC", "BLIND"]
RECON_BASIS = ["RELIABLE", "TIMING_REVIEW"]
RECON_TRIGGERS = ["COUNT_SUBMITTED", "LATE_MOVEMENT", "MANUAL_RECOMPUTE"]

BATCH_KINDS = ["MOVEMENTS", "COUNTS"]
BATCH_STATUSES = ["RECEIVED", "PROCESSING", "COMPLETED", "FAILED"]
ROW_OUTCOMES = ["POSTED", "QUARANTINED", "DUPLICATE", "CONFLICT"]
JOB_TYPES = ["IMPORT", "DETECTION"]
JOB_STATUSES = ["QUEUED", "RUNNING", "DONE", "FAILED"]
RUN_STATUSES = ["QUEUED", "RUNNING", "COMPLETED", "FAILED"]

ALERT_CLASSES = ["SHRINKAGE", "PATTERN", "TRANSFER", "DATA_QUALITY"]
ALERT_TYPES = [
    "UNEXPLAINED_SHORTAGE",
    "REPEATED_NEGATIVE_ADJUSTMENTS",
    "TRANSFER_DOUBLE_COUNT",
    "TRANSFER_OVERDUE_DISCREPANCY",
    "COUNT_TIMING_REVIEW",
]
SEVERITIES = ["LOW", "MEDIUM", "HIGH"]
ALERT_STATUSES = ["OPEN", "CLOSED"]
EVIDENCE_TYPES = ["MOVEMENT", "COUNT", "RECONCILIATION", "IMPORT_ROW", "TRANSFER_LINE"]

INVESTIGATION_STATUSES = ["OPEN", "ASSIGNED", "IN_REVIEW", "RESOLVED"]
INVESTIGATION_ORIGINS = ["COUNT", "MOVEMENT_PATTERN", "TRANSFER", "DATA_QUALITY"]
INVESTIGATION_EVENT_TYPES = ["OPENED", "ASSIGNED", "REVIEW_STARTED", "FINDING_ADDED", "RESOLVED", "REDETECTED"]
RESOLUTION_CODES = [
    "CONFIRMED_THEFT", "COUNT_ERROR", "DATA_CORRECTION", "TIMING_CORRECTED",
    "TRANSFER_RECONCILED", "BENIGN_EXPLAINED", "NO_ACTION",
]
# Allowed (from_status -> {event_type: to_status})
INVESTIGATION_TRANSITIONS: dict[str, dict[str, str]] = {
    "OPEN": {"ASSIGNED": "ASSIGNED", "FINDING_ADDED": "OPEN"},
    "ASSIGNED": {"ASSIGNED": "ASSIGNED", "REVIEW_STARTED": "IN_REVIEW", "FINDING_ADDED": "ASSIGNED"},
    "IN_REVIEW": {"FINDING_ADDED": "IN_REVIEW", "RESOLVED": "RESOLVED", "ASSIGNED": "IN_REVIEW"},
    "RESOLVED": {},
}

# ------------------------------------------------------------- roles & actions
ACTIONS = [
    "VIEW_STORE", "VIEW_ALL_STORES", "POST_SALE", "POST_RETURN", "POST_RECEIPT", "POST_TRANSFER",
    "POST_ADJUSTMENT", "POST_REVERSAL", "APPROVE_ADJUSTMENT", "SUBMIT_COUNT", "IMPORT_BATCH",
    "RUN_DETECTION", "ASSIGN_CASE", "INVESTIGATE", "RESOLVE_CASE", "EXPORT_DATA",
]

ROLES: dict[str, dict] = {
    "CASHIER": {"name": "Cashier", "scope": "STORE",
                "actions": ["VIEW_STORE", "POST_SALE", "POST_RETURN"]},
    "STOCK_CLERK": {"name": "Stock clerk", "scope": "STORE",
                    "actions": ["VIEW_STORE", "POST_RECEIPT", "POST_TRANSFER", "POST_ADJUSTMENT", "SUBMIT_COUNT"]},
    "STORE_MANAGER": {"name": "Store manager", "scope": "STORE",
                      "actions": ["VIEW_STORE", "POST_RECEIPT", "POST_TRANSFER", "POST_ADJUSTMENT", "POST_REVERSAL",
                                  "APPROVE_ADJUSTMENT", "SUBMIT_COUNT", "IMPORT_BATCH", "INVESTIGATE", "EXPORT_DATA"]},
    "LOSS_PREVENTION": {"name": "Loss prevention investigator", "scope": "REGION",
                        "actions": ["VIEW_STORE", "INVESTIGATE", "ASSIGN_CASE", "RESOLVE_CASE", "EXPORT_DATA"]},
    "OPS_MANAGER": {"name": "Operations manager", "scope": "ALL",
                    "actions": ["VIEW_STORE", "VIEW_ALL_STORES", "APPROVE_ADJUSTMENT", "IMPORT_BATCH", "RUN_DETECTION",
                                "ASSIGN_CASE", "INVESTIGATE", "RESOLVE_CASE", "EXPORT_DATA", "POST_REVERSAL"]},
    "ANALYST": {"name": "Merchandising / e-commerce analyst", "scope": "ALL",
                "actions": ["VIEW_STORE", "VIEW_ALL_STORES", "RUN_DETECTION", "EXPORT_DATA"]},
    "SERVICE_FEED": {"name": "Automated feed (service identity)", "scope": "ALL",
                     "actions": ["VIEW_STORE", "POST_SALE", "POST_RETURN", "POST_RECEIPT", "POST_TRANSFER",
                                 "POST_REVERSAL", "IMPORT_BATCH"]},
    "SYSTEM": {"name": "StashTrace detection service", "scope": "ALL",
               "actions": ["VIEW_STORE", "VIEW_ALL_STORES", "RUN_DETECTION"]},
    "ADMIN": {"name": "Administrator", "scope": "ALL", "actions": list(ACTIONS)},
}

MOVEMENT_ACTION = {
    "SALE": "POST_SALE",
    "RETURN": "POST_RETURN",
    "RECEIPT": "POST_RECEIPT",
    "TRANSFER_OUT": "POST_TRANSFER",
    "TRANSFER_IN": "POST_TRANSFER",
    "ADJUSTMENT": "POST_ADJUSTMENT",
    "REVERSAL": "POST_REVERSAL",
}

# ---------------------------------------------------------------- error codes
# Ordered by precedence: the first one found becomes the row's primary error_code.
ERROR_CODES: dict[str, str] = {
    "MISSING_FIELD": "A required field is empty.",
    "INVALID_FORMAT": "A field does not match its type or ID format.",
    "INVALID_MOVEMENT_TYPE": "movement_type is not one of the allowed values.",
    "INVALID_QUANTITY": "Quantity is zero, non-integer, or has the wrong sign for the movement type.",
    "INVALID_TIMESTAMP": "received_at precedes occurred_at, or a timestamp is outside the ledger horizon.",
    "UNKNOWN_STORE": "store_id does not exist.",
    "UNKNOWN_SKU": "sku_id does not exist.",
    "UNKNOWN_SOURCE_REF": "The referenced source document, transfer, count or movement does not exist.",
    "WRONG_STORE_REFERENCE": "The submitted store differs from the store on the authoritative reference.",
    "TRANSFER_SKU_MISMATCH": "The SKU is not a line on the referenced transfer.",
    "SKU_NOT_ASSORTED": "The store does not carry the SKU at the event time (StoreSKU inactive).",
    "ADJUSTMENT_REASON_MISSING": "An adjustment was submitted without a reason code.",
    "INVALID_REASON_CODE": "The adjustment reason code is not allowed.",
    "ADJUSTMENT_NOT_APPROVED": "The adjustment has no approver authorised for this store at the event time.",
    "SELF_APPROVAL": "The approver is the same person who posted the adjustment.",
    "RETURN_DISPOSITION_INVALID": "Return disposition missing or not allowed.",
    "RETURN_SALE_MISMATCH": "The original sale does not match the return's store/SKU, or the quantity exceeds it.",
    "REVERSAL_TARGET_INVALID": "The movement to reverse is missing, already reversed, or on another store/SKU.",
    "COUNT_REF_INVALID": "The referenced count does not exist or belongs to another store/SKU.",
    "MOVEMENT_ID_CONFLICT": "The supplied movement_id is already used by another ledger entry.",
    "UNKNOWN_ACTOR": "actor_id / counted_by / approved_by does not exist.",
    "ACTOR_NOT_AUTHORIZED": "The actor had no role assignment permitting this action at this store at the event time.",
    "UNKNOWN_SHIFT": "shift_id does not exist.",
    "SHIFT_MISMATCH": "The shift belongs to another store or does not cover the event time.",
    "CONFLICT_PAYLOAD": "Same source event ID as an earlier posted row but a different payload.",
}

# ------------------------------------------------------------- detection rules
RULES: list[dict] = [
    {
        "rule_code": "COUNT_TIMING",
        "rule_version": 1,
        "alert_class": "DATA_QUALITY",
        "alert_type": "COUNT_TIMING_REVIEW",
        "description": "Count basis is unreliable: the declared cutoff is later than the physical count "
                       "(beyond tolerance) or the count was submitted before it was performed. "
                       "Routes the count to timing review; no shrinkage alert is raised for it.",
        "params": {"tolerance_minutes": 15},
    },
    {
        "rule_code": "UNEXPLAINED_SHORTAGE",
        "rule_version": 1,
        "alert_class": "SHRINKAGE",
        "alert_type": "UNEXPLAINED_SHORTAGE",
        "description": "Current reconciliation of a reliable count shows a shortage that remains after "
                       "subtracting quarantined (unposted) outbound rows for the same store/SKU before the cutoff. "
                       "Raised when the remaining shortage >= threshold_units.",
        "params": {"threshold_units": 5, "high_severity_value_cents": 20000, "medium_severity_units": 10},
    },
    {
        "rule_code": "REPEATED_NEG_ADJ",
        "rule_version": 1,
        "alert_class": "PATTERN",
        "alert_type": "REPEATED_NEGATIVE_ADJUSTMENTS",
        "description": "At one store/SKU, at least min_count discretionary negative adjustments "
                       "(DAMAGE, EXPIRED, SAMPLING, OTHER) totalling >= min_units within window_days.",
        "params": {"window_days": 14, "min_count": 3, "min_units": 6, "reasons": DISCRETIONARY_REASONS},
    },
    {
        "rule_code": "TRANSFER_RECON",
        "rule_version": 1,
        "alert_class": "TRANSFER",
        "alert_type": "TRANSFER_OVERDUE_DISCREPANCY",
        "description": "Per transfer/SKU: shortfall = dispatched - received. A transit write-off "
                       "(TRANSFER_SHORTAGE) booked at both ends, or write-offs exceeding the shortfall, is a "
                       "double count. A shortfall not written off once expected_arrival + grace_days has passed is "
                       "an overdue discrepancy. One case per transfer, linking both ends.",
        "params": {"grace_days": 7},
    },
]
RULES_BY_CODE = {r["rule_code"]: r for r in RULES}

SOURCE_SYSTEMS = ["POS", "WMS", "STORE_OPS", "STASHTRACE"]
