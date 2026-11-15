"""Import validation and stock posting.

Flow per batch:  receive (save raw rows, queue job)  ->  validate  ->  quarantine or post  ->  report.

Guarantees
* Idempotent: (source_system, source_event_id) is the key. Same key + same payload -> DUPLICATE
  (returns the earlier outcome). Same key + different payload after a POSTED row -> CONFLICT.
  Same key + different payload after a QUARANTINED row -> treated as a correction, linked to the
  original via ``corrects_row_id``.
* Atomic: each chunk's ledger inserts, stock-balance updates, row outcomes, idempotency keys and
  reconciliation re-versions commit in ONE write transaction (``BEGIN IMMEDIATE``). A failure rolls
  the whole chunk back; a retry re-processes it from the same raw rows.
* Quarantined rows never touch ``inventory_movement`` or ``stock_balance``.
"""
from __future__ import annotations

import csv
import hashlib
import io
import json
import logging
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Iterable

from sqlalchemy import text
from sqlalchemy.engine import Connection

from .. import catalog as C
from ..db import Database
from .common import DomainError, allocate_ids, bump_sequence_to, canonical_hash, utcnow
from .context import MasterContext, load_context
from . import reconciliation as recon

log = logging.getLogger("stashtrace.posting")

MOVEMENT_FIELDS = [
    "source_system", "source_event_id", "movement_id", "movement_type", "store_id", "sku_id", "quantity",
    "occurred_at", "received_at", "actor_id", "shift_id", "source_doc_id", "transfer_id", "reason_code",
    "approved_by", "reverses_movement_id", "return_id", "original_sale_movement_id", "disposition",
    "return_reason", "count_ref", "memo",
]
COUNT_FIELDS = [
    "source_system", "count_id", "store_id", "sku_id", "counted_qty", "counted_at", "count_cutoff_at",
    "submitted_at", "counted_by", "shift_id", "count_type",
]
ERROR_ORDER = {code: i for i, code in enumerate(C.ERROR_CODES)}


@dataclass
class ChunkStats:
    processed: int = 0
    posted: int = 0
    quarantined: int = 0
    duplicate: int = 0
    conflict: int = 0
    reversioned: int = 0
    outcomes: list[dict] = field(default_factory=list)

    def add(self, other: "ChunkStats") -> None:
        for k in ("processed", "posted", "quarantined", "duplicate", "conflict", "reversioned"):
            setattr(self, k, getattr(self, k) + getattr(other, k))


# ------------------------------------------------------------------ helpers
def _clean(v):
    if v is None:
        return None
    v = str(v).strip()
    return v or None


def normalize(raw: dict, fields: list[str], default_source: str) -> dict:
    out = {f: _clean(raw.get(f)) for f in fields}
    out["source_system"] = out.get("source_system") or default_source
    if "received_at" in fields and out.get("received_at") is None:
        out["received_at"] = utcnow()
    return out


def business_payload(row: dict) -> dict:
    """Fields that define the business event (received_at is delivery metadata, not identity)."""
    return {k: v for k, v in row.items() if k not in ("received_at", "submitted_at_delivery")}


class _Errors:
    def __init__(self):
        self.items: list[dict] = []

    def add(self, code: str, fld: str | None = None, msg: str | None = None):
        self.items.append({"code": code, "field": fld, "message": msg or C.ERROR_CODES[code]})

    def __bool__(self):
        return bool(self.items)

    def primary(self) -> str | None:
        return min(self.items, key=lambda e: ERROR_ORDER[e["code"]])["code"] if self.items else None

    def json(self) -> str | None:
        if not self.items:
            return None
        return json.dumps(sorted(self.items, key=lambda e: ERROR_ORDER[e["code"]]))


def _fmt_ok(kind: str, value: str | None) -> bool:
    return value is None or bool(C.ID_PATTERNS[kind].match(value))


def _ts_ok(value: str | None) -> bool:
    return value is None or bool(C.TS_RE.match(value))


def _int(value: str | None) -> int | None:
    if value is None:
        return None
    try:
        f = float(value)
    except ValueError:
        return None
    return int(f) if f.is_integer() else None


def _bulk_insert(conn: Connection, table: str, cols: list[str], rows: list[dict]) -> None:
    """Driver-level executemany (avoids per-row SQLAlchemy parameter processing on the hot path)."""
    if not rows:
        return
    sql = f"INSERT INTO {table} ({', '.join(cols)}) VALUES ({', '.join('?' * len(cols))})"
    conn.exec_driver_sql(sql, [tuple(r.get(c) for c in cols) for r in rows])


MOVEMENT_COLS = ["movement_id", "source_system", "source_event_id", "movement_type", "store_id", "sku_id", "quantity",
                 "stock_qty", "occurred_at", "received_at", "posted_at", "actor_id", "actor_role_code", "shift_id",
                 "source_doc_id", "transfer_id", "count_ref", "investigation_id", "reason_code", "approved_by",
                 "approver_role_code", "reverses_movement_id", "memo", "batch_id", "import_row_id"]
RESULT_COLS = ["row_id", "batch_id", "source_system", "source_event_id", "payload_sha256", "outcome", "error_code",
               "errors_json", "movement_type", "submitted_store_id", "authoritative_store_id", "sku_id", "occurred_at",
               "explain_qty", "corrects_row_id", "duplicate_of_row_id", "movement_id", "count_id", "processed_at"]
COUNT_COLS = ["count_id", "store_id", "sku_id", "counted_qty", "counted_at", "count_cutoff_at", "submitted_at",
              "counted_by", "counter_role_code", "shift_id", "count_type", "batch_id"]
RETURN_COLS = ["return_id", "store_id", "sku_id", "quantity", "disposition", "return_reason", "returned_at",
               "original_sale_movement_id", "restocked", "movement_id"]


# ---------------------------------------------------------- validation
@dataclass
class ChunkLookups:
    """DB state needed by a chunk, fetched in bulk before validating it."""
    keys: dict[tuple[str, str], dict]
    movements: dict[str, dict]           # referenced / supplied movement ids that exist in the ledger
    reversed_ids: set[str]
    return_ids: set[str]
    counts: dict[str, tuple[str, str]]   # count_id -> (store, sku)


class _Row(dict):
    __getattr__ = dict.__getitem__

    @property
    def _mapping(self):
        return self


def _in_query(conn: Connection, sql: str, values, chunk: int = 5000):
    """Run ``sql`` (with an ``{ids}`` placeholder) for ``values`` in chunks via the DB-API driver."""
    values = sorted(values)
    for i in range(0, len(values), chunk):
        part = values[i:i + chunk]
        cur = conn.exec_driver_sql(sql.format(ids=",".join("?" * len(part))), tuple(part))
        cols = list(cur.keys())
        for r in cur:
            yield _Row(zip(cols, r))


def fetch_lookups(conn: Connection, rows: list[dict], kind: str) -> ChunkLookups:
    key_ids = {r["source_event_id"] or r.get("count_id") for r in rows if (r.get("source_event_id") or r.get("count_id"))}
    keys: dict[tuple[str, str], dict] = {}
    for r in _in_query(conn, "SELECT * FROM ingest_key WHERE source_event_id IN ({ids})", key_ids):
        keys[(r.source_system, r.source_event_id)] = dict(r._mapping)
    movements: dict[str, dict] = {}
    reversed_ids: set[str] = set()
    return_ids: set[str] = set()
    counts: dict[str, tuple[str, str]] = {}
    if kind == "MOVEMENTS":
        mids = set()
        for r in rows:
            for f in ("movement_id", "reverses_movement_id", "original_sale_movement_id"):
                if r.get(f):
                    mids.add(r[f])
        for m in _in_query(conn, "SELECT movement_id, movement_type, store_id, sku_id, quantity, stock_qty, occurred_at,"
                                 " source_system, source_event_id FROM inventory_movement WHERE movement_id IN ({ids})", mids):
            movements[m.movement_id] = dict(m._mapping)
        rev = {r["reverses_movement_id"] for r in rows if r.get("reverses_movement_id")}
        for m in _in_query(conn, "SELECT reverses_movement_id FROM inventory_movement WHERE reverses_movement_id IN ({ids})", rev):
            reversed_ids.add(m.reverses_movement_id)
        rets = {r["return_id"] for r in rows if r.get("return_id")}
        for m in _in_query(conn, "SELECT return_id FROM customer_return WHERE return_id IN ({ids})", rets):
            return_ids.add(m.return_id)
        crefs = {r["count_ref"] for r in rows if r.get("count_ref")}
        for m in _in_query(conn, "SELECT count_id, store_id, sku_id FROM cycle_count WHERE count_id IN ({ids})", crefs):
            counts[m.count_id] = (m.store_id, m.sku_id)
    else:
        cids = {r["count_id"] for r in rows if r.get("count_id")}
        for m in _in_query(conn, "SELECT count_id, store_id, sku_id FROM cycle_count WHERE count_id IN ({ids})", cids):
            counts[m.count_id] = (m.store_id, m.sku_id)
    return ChunkLookups(keys, movements, reversed_ids, return_ids, counts)


def stock_effect(mtype: str, qty: int, disposition: str | None, reason: str | None, original: dict | None) -> int:
    if mtype in ("RECEIPT", "TRANSFER_IN"):
        return qty
    if mtype in ("SALE", "TRANSFER_OUT"):
        return -qty
    if mtype == "RETURN":
        return qty if disposition in C.RESTOCKING_DISPOSITIONS else 0
    if mtype == "ADJUSTMENT":
        return 0 if reason in C.ZERO_STOCK_REASONS else qty
    if mtype == "REVERSAL":
        return -(original or {}).get("stock_qty", 0)
    return 0


def validate_movement(r: dict, ctx: MasterContext, lk: ChunkLookups, pending: dict[str, dict],
                      pending_reversed: set[str], pending_returns: set[str]) -> tuple[_Errors, dict]:
    e = _Errors()
    d: dict = {"authoritative_store_id": None}
    mtype = r["movement_type"]
    store, sku = r["store_id"], r["sku_id"]

    # 1. presence & format
    for f in ("source_event_id", "movement_type", "store_id", "sku_id", "quantity", "occurred_at", "actor_id"):
        if r.get(f) is None:
            e.add("MISSING_FIELD", f)
    if r["source_system"] not in C.SOURCE_SYSTEMS:
        e.add("INVALID_FORMAT", "source_system")
    if r["source_system"] != "STASHTRACE" and r.get("shift_id") is None:
        e.add("MISSING_FIELD", "shift_id")
    for f, kind in (("store_id", "store"), ("sku_id", "sku"), ("actor_id", "employee"), ("approved_by", "employee"),
                    ("shift_id", "shift"), ("source_doc_id", "doc"), ("transfer_id", "transfer"),
                    ("movement_id", "movement"), ("reverses_movement_id", "movement"),
                    ("original_sale_movement_id", "movement"), ("return_id", "return"), ("count_ref", "count")):
        if not _fmt_ok(kind, r.get(f)):
            e.add("INVALID_FORMAT", f)
    for f in ("occurred_at", "received_at"):
        if not _ts_ok(r.get(f)):
            e.add("INVALID_FORMAT", f)
    if mtype is not None and mtype not in C.MOVEMENT_TYPES:
        e.add("INVALID_MOVEMENT_TYPE", "movement_type")
    qty = _int(r.get("quantity"))
    if r.get("quantity") is not None and (qty is None or qty == 0 or (mtype != "ADJUSTMENT" and qty < 0)):
        e.add("INVALID_QUANTITY", "quantity")
    if e:
        return e, d
    occurred = r["occurred_at"]
    if r["received_at"] < occurred:
        e.add("INVALID_TIMESTAMP", "received_at")

    # 2. master existence
    if store not in ctx.stores:
        e.add("UNKNOWN_STORE", "store_id")
    if sku not in ctx.sku_cost:
        e.add("UNKNOWN_SKU", "sku_id")
    d["authoritative_store_id"] = store if store in ctx.stores else None

    # 3. authoritative references (wrong-store check happens even if the submitted store exists)
    original = None
    if mtype in ("SALE", "RETURN", "RECEIPT"):
        want = "PURCHASE_ORDER" if mtype == "RECEIPT" else "POS_BATCH"
        doc_id = r.get("source_doc_id")
        if doc_id is None:
            e.add("MISSING_FIELD", "source_doc_id")
        elif doc_id not in ctx.docs or ctx.docs[doc_id][0] != want:
            e.add("UNKNOWN_SOURCE_REF", "source_doc_id")
        else:
            d["authoritative_store_id"] = ctx.docs[doc_id][1]
            if ctx.docs[doc_id][1] != store:
                e.add("WRONG_STORE_REFERENCE", "store_id",
                      f"Submitted {store} but {doc_id} belongs to {ctx.docs[doc_id][1]}.")
    if mtype in ("TRANSFER_OUT", "TRANSFER_IN") or (mtype == "ADJUSTMENT" and r.get("reason_code") == "TRANSFER_SHORTAGE"):
        tid = r.get("transfer_id")
        if tid is None:
            e.add("MISSING_FIELD", "transfer_id")
        elif tid not in ctx.transfers:
            e.add("UNKNOWN_SOURCE_REF", "transfer_id")
        else:
            frm, to, created, _ = ctx.transfers[tid]
            if mtype == "TRANSFER_OUT":
                d["authoritative_store_id"] = frm
                if store != frm:
                    e.add("WRONG_STORE_REFERENCE", "store_id", f"Dispatch leg of {tid} must be posted by {frm}.")
            elif mtype == "TRANSFER_IN":
                d["authoritative_store_id"] = to
                if store != to:
                    e.add("WRONG_STORE_REFERENCE", "store_id", f"Receipt leg of {tid} must be posted by {to}.")
            elif store not in (frm, to):
                e.add("WRONG_STORE_REFERENCE", "store_id", f"{store} is neither end of {tid}.")
            if (tid, sku) not in ctx.transfer_lines:
                e.add("TRANSFER_SKU_MISMATCH", "sku_id")
            if occurred < created:
                e.add("INVALID_TIMESTAMP", "occurred_at", "Transfer leg precedes the transfer order.")

    # 4. assortment at event time
    a = ctx.assortment.get((store, sku))
    if store in ctx.stores and sku in ctx.sku_cost:
        if a is None or occurred < a[0] or (a[1] is not None and occurred >= a[1]):
            e.add("SKU_NOT_ASSORTED", "sku_id")

    # 5. movement-specific rules
    if mtype == "ADJUSTMENT":
        reason = r.get("reason_code")
        if reason is None:
            e.add("ADJUSTMENT_REASON_MISSING", "reason_code")
        elif reason not in C.ADJUSTMENT_REASONS or (reason == "INVESTIGATION_WRITE_OFF" and r["source_system"] != "STASHTRACE"):
            e.add("INVALID_REASON_CODE", "reason_code")
        else:
            sign = C.ADJUSTMENT_REASONS[reason]
            if sign and (qty > 0) != (sign > 0):
                e.add("INVALID_QUANTITY", "quantity", f"{reason} requires a {'positive' if sign > 0 else 'negative'} quantity.")
        approver = r.get("approved_by")
        if approver is None:
            e.add("ADJUSTMENT_NOT_APPROVED", "approved_by")
        elif approver not in ctx.authority.employees:
            e.add("UNKNOWN_ACTOR", "approved_by")
        else:
            d["approver_role_code"] = ctx.authority.authorize(approver, "APPROVE_ADJUSTMENT", store, occurred)
            if d["approver_role_code"] is None:
                e.add("ADJUSTMENT_NOT_APPROVED", "approved_by", f"{approver} cannot approve adjustments at {store} at {occurred}.")
            if approver == r.get("actor_id") and r["source_system"] != "STASHTRACE":
                e.add("SELF_APPROVAL", "approved_by")
        if r.get("count_ref") is not None:
            c = lk.counts.get(r["count_ref"])
            if c is None or c != (store, sku):
                e.add("COUNT_REF_INVALID", "count_ref")
    if mtype == "RETURN":
        if r.get("return_id") is None:
            e.add("MISSING_FIELD", "return_id")
        elif r["return_id"] in lk.return_ids or r["return_id"] in pending_returns:
            e.add("MOVEMENT_ID_CONFLICT", "return_id", "return_id already used.")
        if r.get("disposition") not in C.RETURN_DISPOSITIONS:
            e.add("RETURN_DISPOSITION_INVALID", "disposition")
        if r.get("return_reason") is not None and r["return_reason"] not in C.RETURN_REASONS:
            e.add("INVALID_FORMAT", "return_reason")
        sale_id = r.get("original_sale_movement_id")
        if sale_id is not None:
            sale = lk.movements.get(sale_id) or pending.get(sale_id)
            if sale is None:
                e.add("UNKNOWN_SOURCE_REF", "original_sale_movement_id")
            elif (sale["movement_type"] != "SALE" or sale["store_id"] != store or sale["sku_id"] != sku
                  or sale["quantity"] < qty or sale["occurred_at"] > occurred):
                e.add("RETURN_SALE_MISMATCH", "original_sale_movement_id")
    if mtype == "REVERSAL":
        target = r.get("reverses_movement_id")
        original = (lk.movements.get(target) or pending.get(target)) if target else None
        if not r.get("memo"):
            e.add("MISSING_FIELD", "memo", "A reversal needs a memo explaining why.")
        if target is None:
            e.add("MISSING_FIELD", "reverses_movement_id")
        elif original is None:
            e.add("REVERSAL_TARGET_INVALID", "reverses_movement_id", "Movement to reverse is not in the ledger.")
        elif (original["movement_type"] == "REVERSAL" or original["store_id"] != store or original["sku_id"] != sku
              or target in lk.reversed_ids or target in pending_reversed or abs(original["quantity"]) != qty):
            e.add("REVERSAL_TARGET_INVALID", "reverses_movement_id")
    mid = r.get("movement_id")
    if mid is not None and (mid in lk.movements or mid in pending):
        e.add("MOVEMENT_ID_CONFLICT", "movement_id")

    # 6. actor authority & shift at event time
    actor = r.get("actor_id")
    if actor not in ctx.authority.employees:
        e.add("UNKNOWN_ACTOR", "actor_id")
    elif mtype in C.MOVEMENT_ACTION:
        # Investigation write-offs are authorised by case-resolution authority, not posting authority.
        action = "RESOLVE_CASE" if r.get("reason_code") == "INVESTIGATION_WRITE_OFF" else C.MOVEMENT_ACTION[mtype]
        d["actor_role_code"] = ctx.authority.authorize(actor, action, store, occurred)
        if d["actor_role_code"] is None:
            e.add("ACTOR_NOT_AUTHORIZED", "actor_id",
                  f"{actor} has no active role permitting {action} at {store} at {occurred}.")
    sid = r.get("shift_id")
    if sid is not None:
        s = ctx.shifts.get(sid)
        if s is None:
            e.add("UNKNOWN_SHIFT", "shift_id")
        elif s[0] != store or not (s[1] <= occurred < s[2]):
            e.add("SHIFT_MISMATCH", "shift_id")

    d["original"] = original
    d["qty"] = qty
    if mtype in C.MOVEMENT_TYPES and qty is not None:
        d["stock_qty"] = stock_effect(mtype, qty, r.get("disposition"), r.get("reason_code"), original)
    else:
        d["stock_qty"] = 0
    # Units the row would have removed from stock; a quarantined row with explain_qty > 0 can
    # explain a physical shortage at the authoritative store.
    if mtype == "ADJUSTMENT" and r.get("reason_code") is None and qty is not None:
        d["explain_qty"] = -qty
    else:
        d["explain_qty"] = -d["stock_qty"]
    return e, d


def validate_count(r: dict, ctx: MasterContext) -> tuple[_Errors, dict]:
    e = _Errors()
    d: dict = {}
    for f in ("count_id", "store_id", "sku_id", "counted_qty", "counted_at", "count_cutoff_at", "submitted_at",
              "counted_by", "shift_id"):
        if r.get(f) is None:
            e.add("MISSING_FIELD", f)
    for f, kind in (("count_id", "count"), ("store_id", "store"), ("sku_id", "sku"), ("counted_by", "employee"),
                    ("shift_id", "shift")):
        if not _fmt_ok(kind, r.get(f)):
            e.add("INVALID_FORMAT", f)
    for f in ("counted_at", "count_cutoff_at", "submitted_at"):
        if not _ts_ok(r.get(f)):
            e.add("INVALID_FORMAT", f)
    if (r.get("count_type") or "SCHEDULED") not in C.COUNT_TYPES:
        e.add("INVALID_FORMAT", "count_type")
    qty = _int(r.get("counted_qty"))
    if r.get("counted_qty") is not None and (qty is None or qty < 0):
        e.add("INVALID_QUANTITY", "counted_qty")
    if e:
        return e, d
    store, sku = r["store_id"], r["sku_id"]
    if store not in ctx.stores:
        e.add("UNKNOWN_STORE", "store_id")
    if sku not in ctx.sku_cost:
        e.add("UNKNOWN_SKU", "sku_id")
    if store in ctx.stores and sku in ctx.sku_cost:
        a = ctx.assortment.get((store, sku))
        if a is None or r["count_cutoff_at"] < a[0] or (a[1] is not None and r["counted_at"] >= a[1]):
            e.add("SKU_NOT_ASSORTED", "sku_id")
    if r["counted_by"] not in ctx.authority.employees:
        e.add("UNKNOWN_ACTOR", "counted_by")
    else:
        d["counter_role_code"] = ctx.authority.authorize(r["counted_by"], "SUBMIT_COUNT", store, r["counted_at"])
        if d["counter_role_code"] is None:
            e.add("ACTOR_NOT_AUTHORIZED", "counted_by")
    s = ctx.shifts.get(r["shift_id"])
    if s is None:
        e.add("UNKNOWN_SHIFT", "shift_id")
    elif s[0] != store or not (s[1] <= r["counted_at"] < s[2]):
        e.add("SHIFT_MISMATCH", "shift_id")
    d["qty"] = qty
    return e, d


# ------------------------------------------------------------- processing
def _idempotency(key, h, lk_keys, local_keys):
    """Return (outcome_or_None, prior_key_row, corrects_row_id)."""
    prior = local_keys.get(key) or lk_keys.get(key)
    if prior is None:
        return None, None, None
    if prior["payload_sha256"] == h:
        return "DUPLICATE", prior, None
    if prior["outcome"] == "POSTED":
        return "CONFLICT", prior, None
    return None, prior, prior["latest_row_id"]  # correction of a quarantined row


def process_movement_chunk(conn: Connection, ctx: MasterContext, batch_id: str | None,
                           items: list[tuple[int, dict]], now: str | None = None,
                           uploader_scope: set[str] | None = None) -> ChunkStats:
    """Validate and post one chunk inside the caller's write transaction.

    ``items`` are (raw_row_id, normalized_row) in submission order.
    """
    now = now or utcnow()
    stats = ChunkStats()
    rows = [r for _, r in items]
    lk = fetch_lookups(conn, rows, "MOVEMENTS")
    seq_before = recon.current_watermark(conn)
    local_keys: dict[tuple[str, str], dict] = {}
    pending: dict[str, dict] = {}
    pending_reversed: set[str] = set()
    pending_returns: set[str] = set()
    movements, returns, results = [], [], []
    deltas: dict[tuple[str, str], int] = defaultdict(int)
    need_ids = 0

    for row_id, r in items:
        key = (r["source_system"], r["source_event_id"])
        h = canonical_hash(business_payload(r))
        res = {
            "row_id": row_id, "batch_id": batch_id, "source_system": r["source_system"],
            "source_event_id": r["source_event_id"], "payload_sha256": h, "movement_type": r.get("movement_type"),
            "submitted_store_id": r.get("store_id"), "authoritative_store_id": None, "sku_id": r.get("sku_id"),
            "occurred_at": r.get("occurred_at"), "explain_qty": 0, "corrects_row_id": None,
            "duplicate_of_row_id": None, "movement_id": None, "count_id": None, "processed_at": now,
            "error_code": None, "errors_json": None,
        }
        outcome, prior, corrects = (None, None, None)
        if r["source_event_id"] is not None:
            outcome, prior, corrects = _idempotency(key, h, lk.keys, local_keys)
        if outcome == "DUPLICATE":
            res.update(outcome="DUPLICATE", duplicate_of_row_id=prior["latest_row_id"], movement_id=prior.get("movement_id"))
            if prior.get("_mv") is not None:  # replay of a row posted earlier in this same chunk
                res["_mv"] = prior["_mv"]
            stats.duplicate += 1
        elif outcome == "CONFLICT":
            res.update(outcome="CONFLICT", duplicate_of_row_id=prior["latest_row_id"], error_code="CONFLICT_PAYLOAD",
                       errors_json=json.dumps([{"code": "CONFLICT_PAYLOAD", "field": "source_event_id",
                                                "message": C.ERROR_CODES["CONFLICT_PAYLOAD"]}]))
            stats.conflict += 1
        else:
            res["corrects_row_id"] = corrects
            errors, d = validate_movement(r, ctx, lk, pending, pending_reversed, pending_returns)
            if uploader_scope is not None and r.get("store_id") not in uploader_scope:
                errors.add("ACTOR_NOT_AUTHORIZED", "store_id", "The uploader is not authorised to import for this store.")
            res["authoritative_store_id"] = d.get("authoritative_store_id")
            if errors:
                res.update(outcome="QUARANTINED", error_code=errors.primary(), errors_json=errors.json(),
                           explain_qty=d.get("explain_qty", 0) or 0)
                stats.quarantined += 1
                if r["source_event_id"] is not None:
                    local_keys[key] = {"latest_row_id": row_id, "payload_sha256": h, "outcome": "QUARANTINED",
                                       "movement_id": None, "count_id": None, "_new": True}
            else:
                mv = {
                    "movement_id": r.get("movement_id"), "source_system": r["source_system"],
                    "source_event_id": r["source_event_id"], "movement_type": r["movement_type"],
                    "store_id": r["store_id"], "sku_id": r["sku_id"], "quantity": d["qty"], "stock_qty": d["stock_qty"],
                    "occurred_at": r["occurred_at"], "received_at": r["received_at"], "posted_at": now,
                    "actor_id": r["actor_id"], "actor_role_code": d["actor_role_code"], "shift_id": r.get("shift_id"),
                    "source_doc_id": r.get("source_doc_id"), "transfer_id": r.get("transfer_id"),
                    "count_ref": r.get("count_ref"), "investigation_id": r.get("investigation_id"),
                    "reason_code": r.get("reason_code"), "approved_by": r.get("approved_by"),
                    "approver_role_code": d.get("approver_role_code"),
                    "reverses_movement_id": r.get("reverses_movement_id"), "memo": r.get("memo"), "batch_id": batch_id,
                    "import_row_id": row_id,
                }
                if mv["movement_id"] is None:
                    need_ids += 1
                movements.append(mv)
                if r["movement_type"] == "RETURN":
                    returns.append({
                        "return_id": r["return_id"], "store_id": r["store_id"], "sku_id": r["sku_id"],
                        "quantity": d["qty"], "disposition": r["disposition"], "return_reason": r.get("return_reason"),
                        "returned_at": r["occurred_at"], "original_sale_movement_id": r.get("original_sale_movement_id"),
                        "restocked": 1 if r["disposition"] in C.RESTOCKING_DISPOSITIONS else 0, "_mv": mv,
                    })
                    pending_returns.add(r["return_id"])
                if r["movement_type"] == "REVERSAL":
                    pending_reversed.add(r["reverses_movement_id"])
                deltas[(r["store_id"], r["sku_id"])] += d["stock_qty"]
                res.update(outcome="POSTED", _mv=mv)
                stats.posted += 1
                local_keys[key] = {"latest_row_id": row_id, "payload_sha256": h, "outcome": "POSTED",
                                   "_mv": mv, "count_id": None, "_new": True}
                if mv["movement_id"] is not None:
                    pending[mv["movement_id"]] = mv
        results.append(res)
        stats.processed += 1

    # Allocate backend IDs for postings that came without one.
    new_ids = iter(allocate_ids(conn, "movement", need_ids))
    max_supplied = None
    for mv in movements:
        if mv["movement_id"] is None:
            mv["movement_id"] = next(new_ids)
        elif max_supplied is None or mv["movement_id"] > max_supplied:
            max_supplied = mv["movement_id"]
    if max_supplied:
        bump_sequence_to(conn, "movement", max_supplied)

    _bulk_insert(conn, "inventory_movement", MOVEMENT_COLS, movements)
    if returns:
        for rt in returns:
            rt["movement_id"] = rt.pop("_mv")["movement_id"]
        _bulk_insert(conn, "customer_return", RETURN_COLS, returns)
    for res in results:
        mv = res.pop("_mv", None)
        if mv is not None:
            res["movement_id"] = mv["movement_id"]
    _insert_results(conn, results)
    _upsert_keys(conn, local_keys)
    _apply_deltas(conn, deltas, now)
    if movements:
        stats.reversioned = recon.reversion_for_late_movements(conn, seq_before)
    stats.outcomes = results
    return stats


def _insert_results(conn: Connection, results: list[dict]) -> None:
    _bulk_insert(conn, "import_row_result", RESULT_COLS, results)


def _upsert_keys(conn: Connection, local_keys: dict) -> None:
    payload = []
    for (system, event_id), k in local_keys.items():
        if not k.get("_new"):
            continue
        mv = k.get("_mv")
        payload.append({"s": system, "e": event_id, "r": k["latest_row_id"], "h": k["payload_sha256"],
                        "o": k["outcome"], "m": mv["movement_id"] if mv else k.get("movement_id"), "c": k.get("count_id")})
    if payload:
        conn.exec_driver_sql(
            "INSERT INTO ingest_key (source_system, source_event_id, latest_row_id, payload_sha256, outcome, movement_id,"
            " count_id) VALUES (?, ?, ?, ?, ?, ?, ?) ON CONFLICT(source_system, source_event_id) DO UPDATE SET"
            " latest_row_id=excluded.latest_row_id, payload_sha256=excluded.payload_sha256, outcome=excluded.outcome,"
            " movement_id=excluded.movement_id, count_id=excluded.count_id",
            [(p["s"], p["e"], p["r"], p["h"], p["o"], p["m"], p["c"]) for p in payload])


def _apply_deltas(conn: Connection, deltas: dict, now: str) -> None:
    if not deltas:
        return
    seq = recon.current_watermark(conn)
    conn.execute(text(
        "INSERT INTO stock_balance (store_id, sku_id, on_hand, last_ledger_seq, updated_at)"
        " SELECT store_id, sku_id, opening_qty + :d, :seq, :now FROM store_sku WHERE store_id=:st AND sku_id=:sk"
        " ON CONFLICT(store_id, sku_id) DO UPDATE SET on_hand = on_hand + :d, last_ledger_seq = :seq, updated_at = :now"
    ), [{"st": st, "sk": sk, "d": dlt, "seq": seq, "now": now} for (st, sk), dlt in deltas.items()])


def process_count_chunk(conn: Connection, ctx: MasterContext, batch_id: str | None,
                        items: list[tuple[int, dict]], now: str | None = None,
                        uploader_scope: set[str] | None = None) -> ChunkStats:
    now = now or utcnow()
    stats = ChunkStats()
    rows = [r for _, r in items]
    for r in rows:
        r["source_event_id"] = r.get("count_id")
    lk = fetch_lookups(conn, rows, "COUNTS")
    local_keys: dict = {}
    results = []
    wm = recon.current_watermark(conn)
    tol = recon.timing_tolerance(conn)
    supplied_max = None
    for row_id, r in items:
        key = (r["source_system"], r["count_id"])
        payload = {k: r.get(k) for k in COUNT_FIELDS}
        h = canonical_hash(payload)
        res = {
            "row_id": row_id, "batch_id": batch_id, "source_system": r["source_system"],
            "source_event_id": r["count_id"], "payload_sha256": h, "movement_type": None,
            "submitted_store_id": r.get("store_id"), "authoritative_store_id": r.get("store_id"),
            "sku_id": r.get("sku_id"), "occurred_at": r.get("counted_at"), "explain_qty": 0,
            "corrects_row_id": None, "duplicate_of_row_id": None, "movement_id": None, "count_id": None,
            "processed_at": now, "error_code": None, "errors_json": None,
        }
        outcome, prior, corrects = (None, None, None)
        if r.get("count_id"):
            outcome, prior, corrects = _idempotency(key, h, lk.keys, local_keys)
        if outcome == "DUPLICATE":
            res.update(outcome="DUPLICATE", duplicate_of_row_id=prior["latest_row_id"], count_id=prior.get("count_id"))
            stats.duplicate += 1
        elif outcome == "CONFLICT":
            res.update(outcome="CONFLICT", duplicate_of_row_id=prior["latest_row_id"], error_code="CONFLICT_PAYLOAD",
                       errors_json=json.dumps([{"code": "CONFLICT_PAYLOAD", "field": "count_id",
                                                "message": C.ERROR_CODES["CONFLICT_PAYLOAD"]}]))
            stats.conflict += 1
        else:
            res["corrects_row_id"] = corrects
            errors, d = validate_count(r, ctx)
            if uploader_scope is not None and r.get("store_id") not in uploader_scope:
                errors.add("ACTOR_NOT_AUTHORIZED", "store_id", "The uploader is not authorised to import for this store.")
            if not errors and r["count_id"] in lk.counts:
                errors.add("MOVEMENT_ID_CONFLICT", "count_id", "count_id already used.")
            if errors:
                res.update(outcome="QUARANTINED", error_code=errors.primary(), errors_json=errors.json())
                stats.quarantined += 1
                local_keys[key] = {"latest_row_id": row_id, "payload_sha256": h, "outcome": "QUARANTINED",
                                   "count_id": None, "_new": True}
            else:
                count = {
                    "count_id": r["count_id"], "store_id": r["store_id"], "sku_id": r["sku_id"],
                    "counted_qty": d["qty"], "counted_at": r["counted_at"], "count_cutoff_at": r["count_cutoff_at"],
                    "submitted_at": r["submitted_at"], "counted_by": r["counted_by"],
                    "counter_role_code": d["counter_role_code"], "shift_id": r["shift_id"],
                    "count_type": r.get("count_type") or "SCHEDULED", "batch_id": batch_id,
                }
                _bulk_insert(conn, "cycle_count", COUNT_COLS, [count])
                recon.create_version(conn, count, "COUNT_SUBMITTED", wm, tol)
                lk.counts[r["count_id"]] = (r["store_id"], r["sku_id"])
                if supplied_max is None or r["count_id"] > supplied_max:
                    supplied_max = r["count_id"]
                res.update(outcome="POSTED", count_id=r["count_id"])
                stats.posted += 1
                local_keys[key] = {"latest_row_id": row_id, "payload_sha256": h, "outcome": "POSTED",
                                   "count_id": r["count_id"], "_new": True}
        results.append(res)
        stats.processed += 1
    if supplied_max:
        bump_sequence_to(conn, "count", supplied_max)
    _insert_results(conn, results)
    _upsert_keys(conn, local_keys)
    stats.outcomes = results
    return stats


# ---------------------------------------------------------------- batches
def receive_batch(db: Database, kind: str, file_name: str, data: bytes, submitted_by: str,
                  source_system: str = "STORE_OPS", enqueue: bool = True) -> dict:
    """Save the raw rows and (optionally) queue processing. Returns batch info incl. job id."""
    if kind not in C.BATCH_KINDS:
        raise DomainError("INVALID_BATCH_KIND", f"batch_kind must be one of {C.BATCH_KINDS}")
    if source_system not in C.SOURCE_SYSTEMS:
        raise DomainError("INVALID_SOURCE_SYSTEM", f"source_system must be one of {C.SOURCE_SYSTEMS}")
    try:
        content = data.decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        raise DomainError("INVALID_ENCODING", "File must be UTF-8 CSV.") from exc
    reader = csv.reader(io.StringIO(content))
    try:
        header = next(reader)
    except StopIteration as exc:
        raise DomainError("EMPTY_FILE", "File has no header row.") from exc
    expected = MOVEMENT_FIELDS if kind == "MOVEMENTS" else COUNT_FIELDS
    missing = [f for f in ("store_id", "sku_id") if f not in header]
    unknown = [h for h in header if h not in expected]
    if missing or unknown:
        raise DomainError("INVALID_HEADER", "CSV header does not match the import contract.",
                          details={"missing": missing, "unknown": unknown, "expected": expected})
    lines = []
    buf = io.StringIO()
    writer = csv.writer(buf, lineterminator="")
    for rec in reader:
        if not any(x.strip() for x in rec):
            continue
        buf.seek(0)
        buf.truncate()
        writer.writerow(rec)
        lines.append(buf.getvalue())
    buf.seek(0)
    buf.truncate()
    writer.writerow(header)
    header_line = buf.getvalue()
    if not lines:
        raise DomainError("EMPTY_FILE", "The file has a header row but no data rows.")
    now = utcnow()
    with db.write() as conn:
        batch_id = allocate_ids(conn, "batch")[0]
        conn.execute(text(
            "INSERT INTO import_batch (batch_id, batch_kind, source_system, file_name, file_sha256, header_csv,"
            " submitted_by, submitted_at) VALUES (:b, :k, :s, :f, :h, :hdr, :u, :t)"),
            {"b": batch_id, "k": kind, "s": source_system, "f": file_name, "h": hashlib.sha256(data).hexdigest(),
             "hdr": header_line, "u": submitted_by, "t": now})
        conn.execute(text(
            "INSERT INTO import_batch_status (batch_id, status, received_count, processed_count, posted_count,"
            " quarantined_count, duplicate_count, conflict_count) VALUES (:b, 'RECEIVED', :n, 0, 0, 0, 0, 0)"),
            {"b": batch_id, "n": len(lines)})
        conn.exec_driver_sql("INSERT INTO import_raw_row (batch_id, row_number, raw_line) VALUES (?, ?, ?)",
                             [(batch_id, i + 1, line) for i, line in enumerate(lines)])
        job_id = None
        if enqueue:
            job_id = conn.execute(text(
                "INSERT INTO job (job_type, ref_id, status, attempts, created_at) VALUES ('IMPORT', :b, 'QUEUED', 0, :t)"
                " RETURNING job_id"), {"b": batch_id, "t": now}).scalar_one()
    return {"batch_id": batch_id, "job_id": job_id, "received_count": len(lines), "status": "RECEIVED"}


def _parse_rows(header: list[str], raw: Iterable, fields: list[str], default_source: str) -> list[tuple[int, dict]]:
    out = []
    for row_id, line in raw:
        values = next(csv.reader([line]))
        rec = dict(zip(header, values))
        out.append((row_id, normalize(rec, fields, default_source)))
    return out


def process_batch(db: Database, batch_id: str, chunk_size: int | None = None) -> dict:
    """Validate and post every unprocessed row of a batch. Safe to re-run (resumes after last chunk)."""
    from ..config import settings
    chunk_size = chunk_size or settings.import_chunk_size
    with db.read() as conn:
        b = conn.execute(text("SELECT * FROM import_batch WHERE batch_id=:b"), {"b": batch_id}).one()
    header = next(csv.reader([b.header_csv]))
    fields = MOVEMENT_FIELDS if b.batch_kind == "MOVEMENTS" else COUNT_FIELDS
    with db.write() as conn:
        conn.execute(text("UPDATE import_batch_status SET status='PROCESSING', started_at=COALESCE(started_at, :t)"
                          " WHERE batch_id=:b"), {"b": batch_id, "t": utcnow()})
    total = ChunkStats()
    last_row = 0
    while True:
        with db.write() as conn:
            ctx = load_context(conn, db.url)
            raw = conn.execute(text(
                "SELECT r.row_id, r.raw_line FROM import_raw_row r LEFT JOIN import_row_result x ON x.row_id=r.row_id"
                " WHERE r.batch_id=:b AND x.row_id IS NULL AND r.row_id > :last ORDER BY r.row_id LIMIT :n"),
                {"b": batch_id, "last": last_row, "n": chunk_size}).all()
            if not raw:
                break
            last_row = raw[-1][0]
            items = _parse_rows(header, raw, fields, b.source_system)
            # Store-scoped uploaders can only import rows for their own stores.
            scope = ctx.authority.stores_for(b.submitted_by, "IMPORT_BATCH", b.submitted_at)
            if scope is not None:
                scope = scope | (ctx.authority.stores_for(b.submitted_by, "SUBMIT_COUNT", b.submitted_at) or set())
            if b.batch_kind == "MOVEMENTS":
                stats = process_movement_chunk(conn, ctx, batch_id, items, uploader_scope=scope)
            else:
                stats = process_count_chunk(conn, ctx, batch_id, items, uploader_scope=scope)
            conn.execute(text(
                "UPDATE import_batch_status SET processed_count=processed_count+:p, posted_count=posted_count+:po,"
                " quarantined_count=quarantined_count+:q, duplicate_count=duplicate_count+:d,"
                " conflict_count=conflict_count+:c WHERE batch_id=:b"),
                {"p": stats.processed, "po": stats.posted, "q": stats.quarantined, "d": stats.duplicate,
                 "c": stats.conflict, "b": batch_id})
            total.add(stats)
    with db.write() as conn:
        conn.execute(text("UPDATE import_batch_status SET status='COMPLETED', finished_at=:t WHERE batch_id=:b"),
                     {"b": batch_id, "t": utcnow()})
    log.info("batch %s processed=%s posted=%s quarantined=%s duplicate=%s conflict=%s reversioned=%s", batch_id,
             total.processed, total.posted, total.quarantined, total.duplicate, total.conflict, total.reversioned)
    return {"batch_id": batch_id, "processed": total.processed, "posted": total.posted,
            "quarantined": total.quarantined, "duplicate": total.duplicate, "conflict": total.conflict,
            "reconciliations_reversioned": total.reversioned}


def mark_batch_failed(db: Database, batch_id: str, message: str) -> None:
    with db.write() as conn:
        conn.execute(text("UPDATE import_batch_status SET status='FAILED', error_message=:m, finished_at=:t"
                          " WHERE batch_id=:b"), {"b": batch_id, "m": message[:2000], "t": utcnow()})


# ------------------------------------------------------- single postings
def post_in_transaction(conn: Connection, db: Database, rows: list[dict], submitted_by: str, source_system: str,
                        file_name: str, now: str | None = None) -> ChunkStats:
    """Post rows atomically inside the caller's write transaction via a one-off batch.

    Used where several ledger entries must commit together with another change (reversal +
    replacement, case resolution + write-off). Rows still go through full validation.
    """
    now = now or utcnow()
    buf = io.StringIO()
    w = csv.writer(buf, lineterminator="")
    w.writerow(MOVEMENT_FIELDS)
    header = buf.getvalue()
    lines = []
    for r in rows:
        buf.seek(0)
        buf.truncate()
        w.writerow(["" if r.get(f) is None else r.get(f) for f in MOVEMENT_FIELDS])
        lines.append(buf.getvalue())
    batch_id = allocate_ids(conn, "batch")[0]
    conn.execute(text(
        "INSERT INTO import_batch (batch_id, batch_kind, source_system, file_name, file_sha256, header_csv, submitted_by,"
        " submitted_at) VALUES (:b, 'MOVEMENTS', :s, :f, :h, :hdr, :u, :t)"),
        {"b": batch_id, "s": source_system, "f": file_name, "h": hashlib.sha256(chr(10).join(lines).encode()).hexdigest(),
         "hdr": header, "u": submitted_by, "t": now})
    items = []
    for i, (r, line) in enumerate(zip(rows, lines)):
        row_id = conn.execute(text("INSERT INTO import_raw_row (batch_id, row_number, raw_line) VALUES (:b, :n, :l)"
                                   " RETURNING row_id"), {"b": batch_id, "n": i + 1, "l": line}).scalar_one()
        norm = normalize({k: (None if v is None else str(v)) for k, v in r.items()}, MOVEMENT_FIELDS, source_system)
        if r.get("investigation_id"):
            norm["investigation_id"] = r["investigation_id"]
        items.append((row_id, norm))
    stats = process_movement_chunk(conn, load_context(conn, db.url), batch_id, items, now)
    conn.execute(text(
        "INSERT INTO import_batch_status (batch_id, status, received_count, processed_count, posted_count,"
        " quarantined_count, duplicate_count, conflict_count, started_at, finished_at)"
        " VALUES (:b, 'COMPLETED', :n, :n, :p, :q, :d, :c, :t, :t)"),
        {"b": batch_id, "n": len(rows), "p": stats.posted, "q": stats.quarantined, "d": stats.duplicate,
         "c": stats.conflict, "t": now})
    stats.batch_id = batch_id  # type: ignore[attr-defined]
    return stats


def reverse_movement(db: Database, movement_id: str, memo: str, actor_id: str, replacement: dict | None) -> dict:
    """Correct a posted entry with a linked REVERSAL (+ optional replacement) in one transaction."""
    now = utcnow()
    with db.write() as conn:
        orig = conn.execute(text("SELECT * FROM inventory_movement WHERE movement_id=:m"), {"m": movement_id}).first()
        if orig is None:
            raise DomainError("NOT_FOUND", "Movement not found.", 404)
        rows = [{
            "source_system": "STASHTRACE", "source_event_id": f"{movement_id}-REV", "movement_type": "REVERSAL",
            "store_id": orig.store_id, "sku_id": orig.sku_id, "quantity": abs(orig.quantity), "occurred_at": orig.occurred_at,
            "received_at": now, "actor_id": actor_id, "reverses_movement_id": movement_id, "memo": memo,
            "transfer_id": orig.transfer_id,
        }]
        if replacement:
            rows.append(dict(replacement))
        stats = post_in_transaction(conn, db, rows, actor_id, "STASHTRACE", f"reversal-{movement_id}", now)
        bad = [o for o in stats.outcomes if o["outcome"] != "POSTED"]
        if bad:
            raise DomainError("REVERSAL_REJECTED", "Reversal/replacement failed validation; nothing was posted.", 422,
                              {"rows": [{"error_code": b["error_code"], "errors": json.loads(b["errors_json"] or "[]")}
                                        for b in bad]})
    return {"reversal_movement_id": stats.outcomes[0]["movement_id"],
            "replacement_movement_id": stats.outcomes[1]["movement_id"] if replacement else None,
            "batch_id": stats.batch_id}  # type: ignore[attr-defined]

def post_single_movement(db: Database, payload: dict, submitted_by: str) -> dict:
    """API posting of one movement through the same validation path, as a one-row batch."""
    line_fields = [f for f in MOVEMENT_FIELDS]
    buf = io.StringIO()
    w = csv.writer(buf, lineterminator="\n")
    w.writerow(line_fields)
    w.writerow(["" if payload.get(f) is None else payload.get(f) for f in line_fields])
    batch = receive_batch(db, "MOVEMENTS", "api-movement.csv", buf.getvalue().encode(), submitted_by,
                          payload.get("source_system") or "STORE_OPS", enqueue=False)
    process_batch(db, batch["batch_id"])
    return row_outcome(db, batch["batch_id"])


def submit_single_count(db: Database, payload: dict, submitted_by: str) -> dict:
    buf = io.StringIO()
    w = csv.writer(buf, lineterminator="\n")
    w.writerow(COUNT_FIELDS)
    w.writerow(["" if payload.get(f) is None else payload.get(f) for f in COUNT_FIELDS])
    batch = receive_batch(db, "COUNTS", "api-count.csv", buf.getvalue().encode(), submitted_by,
                          payload.get("source_system") or "STORE_OPS", enqueue=False)
    process_batch(db, batch["batch_id"])
    return row_outcome(db, batch["batch_id"])


def row_outcome(db: Database, batch_id: str) -> dict:
    with db.read() as conn:
        r = conn.execute(text(
            "SELECT x.*, r.row_number FROM import_row_result x JOIN import_raw_row r ON r.row_id=x.row_id"
            " WHERE x.batch_id=:b ORDER BY x.row_id LIMIT 1"), {"b": batch_id}).one()
    out = dict(r._mapping)
    out["errors"] = json.loads(out.pop("errors_json") or "[]")
    return out


def correct_row(db: Database, row_id: int, corrected: dict, submitted_by: str) -> dict:
    """Submit a corrected version of a quarantined row as a new one-row batch linked to the original."""
    with db.read() as conn:
        orig = conn.execute(text(
            "SELECT x.*, b.batch_kind, b.header_csv, r.raw_line FROM import_row_result x"
            " JOIN import_batch b ON b.batch_id=x.batch_id JOIN import_raw_row r ON r.row_id=x.row_id"
            " WHERE x.row_id=:r"), {"r": row_id}).first()
    if orig is None:
        raise DomainError("NOT_FOUND", "Row not found.", 404)
    if orig.outcome != "QUARANTINED":
        raise DomainError("NOT_QUARANTINED", "Only quarantined rows can be corrected.", 409)
    header = next(csv.reader([orig.header_csv]))
    base = dict(zip(header, next(csv.reader([orig.raw_line]))))
    base.update({k: v for k, v in corrected.items() if v is not None})
    base["source_system"] = orig.source_system
    if orig.batch_kind == "MOVEMENTS":
        base["source_event_id"] = orig.source_event_id
        out = post_single_movement(db, base, submitted_by)
    else:
        base["count_id"] = orig.source_event_id
        out = submit_single_count(db, base, submitted_by)
    return out
