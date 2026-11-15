"""Cycle-count reconciliation.

    Expected stock = Opening balance
                   + Receipts + Transfer receipts + Restocked returns
                   - Sales - Transfer dispatches
                   + Signed approved adjustments
    Variance       = Physical count - Expected stock

Only ledger entries with ``occurred_at <= count_cutoff_at`` and ``ledger_seq <= watermark`` are
included. A REVERSAL is attributed to the bucket of the movement it reverses. Counts never post
stock. Each computation is stored as a new immutable version; the highest version is current.
"""
from __future__ import annotations

import json

from sqlalchemy import text
from sqlalchemy.engine import Connection

from .common import add_minutes, allocate_ids, utcnow

_BUCKET_SQL = text(
    """
    SELECT
      COALESCE(SUM(CASE WHEN b='RECEIPT' THEN s END), 0)        AS receipts,
      COALESCE(SUM(CASE WHEN b='TRANSFER_IN' THEN s END), 0)    AS transfer_in,
      COALESCE(SUM(CASE WHEN b='RETURN' THEN s END), 0)         AS restocked_returns,
      COALESCE(-SUM(CASE WHEN b='SALE' THEN s END), 0)          AS sales,
      COALESCE(-SUM(CASE WHEN b='TRANSFER_OUT' THEN s END), 0)  AS transfer_out,
      COALESCE(SUM(CASE WHEN b='ADJUSTMENT' THEN s END), 0)     AS adjustments,
      COUNT(*)                                                  AS n
    FROM (
      SELECT m.stock_qty AS s, COALESCE(o.movement_type, m.movement_type) AS b
      FROM inventory_movement m
      LEFT JOIN inventory_movement o ON o.movement_id = m.reverses_movement_id
      WHERE m.store_id = :store AND m.sku_id = :sku
        AND m.occurred_at <= :cutoff AND m.ledger_seq <= :wm
    )
    """
)

_BETWEEN_SQL = text(
    """
    SELECT COUNT(*) FROM inventory_movement
    WHERE store_id=:store AND sku_id=:sku AND occurred_at > :lo AND occurred_at <= :hi AND ledger_seq <= :wm
      AND stock_qty <> 0
    """
)


_PAIR_SQL = text("SELECT s.opening_qty, k.unit_cost_cents FROM store_sku s JOIN sku k ON k.sku_id=s.sku_id "
                 "WHERE s.store_id=:st AND s.sku_id=:sk")
_PREV_SQL = text("SELECT * FROM count_reconciliation WHERE count_id=:c ORDER BY version DESC LIMIT 1")
_INSERT_SQL = text(
    "INSERT INTO count_reconciliation (recon_id, count_id, version, supersedes_recon_id, trigger, opening_qty,"
    " receipts_qty, transfer_in_qty, restocked_returns_qty, sales_qty, transfer_out_qty, adjustments_qty,"
    " expected_qty, counted_qty, variance_qty, variance_value_cents, movement_count, ledger_watermark_seq,"
    " basis_status, basis_notes, created_at) VALUES (:recon_id, :count_id, :version, :supersedes_recon_id,"
    " :trigger, :opening_qty, :receipts_qty, :transfer_in_qty, :restocked_returns_qty, :sales_qty,"
    " :transfer_out_qty, :adjustments_qty, :expected_qty, :counted_qty, :variance_qty, :variance_value_cents,"
    " :movement_count, :ledger_watermark_seq, :basis_status, :basis_notes, :created_at)")


def current_watermark(conn: Connection) -> int:
    return conn.exec_driver_sql("SELECT COALESCE(MAX(ledger_seq), 0) FROM inventory_movement").scalar_one()


def timing_tolerance(conn: Connection) -> int:
    p = conn.execute(
        text("SELECT params_json FROM rule_definition WHERE rule_code='COUNT_TIMING' AND is_active=1 "
             "ORDER BY rule_version DESC LIMIT 1")
    ).scalar_one()
    return int(json.loads(p)["tolerance_minutes"])


def compute(conn: Connection, count: dict, watermark: int, tolerance_min: int) -> dict:
    """Compute (but do not store) a reconciliation for a count row."""
    store, sku, cutoff = count["store_id"], count["sku_id"], count["count_cutoff_at"]
    ss = conn.execute(_PAIR_SQL, {"st": store, "sk": sku}).one()
    b = conn.execute(_BUCKET_SQL, {"store": store, "sku": sku, "cutoff": cutoff, "wm": watermark}).one()
    expected = (ss.opening_qty + b.receipts + b.transfer_in + b.restocked_returns
                - b.sales - b.transfer_out + b.adjustments)
    variance = count["counted_qty"] - expected

    # Count basis: does the count's timing contradict the ledger sequence?
    violations = []
    counted_at, submitted_at = count["counted_at"], count["submitted_at"]
    if submitted_at < counted_at:
        violations.append({"code": "SUBMITTED_BEFORE_COUNTED", "counted_at": counted_at, "submitted_at": submitted_at})
    if cutoff > add_minutes(submitted_at, tolerance_min):
        violations.append({"code": "CUTOFF_AFTER_SUBMISSION", "count_cutoff_at": cutoff, "submitted_at": submitted_at,
                           "tolerance_minutes": tolerance_min})
    lo, hi = sorted([counted_at, cutoff])
    if hi > add_minutes(lo, tolerance_min):
        between = conn.execute(_BETWEEN_SQL, {"store": store, "sku": sku, "lo": lo, "hi": hi, "wm": watermark}).scalar_one()
        if between > 0:
            code = "CUTOFF_AFTER_COUNT" if cutoff > counted_at else "COUNT_AFTER_CUTOFF"
            violations.append({"code": code, "counted_at": counted_at, "count_cutoff_at": cutoff,
                               "stock_movements_between": between, "tolerance_minutes": tolerance_min})
    return {
        "opening_qty": ss.opening_qty,
        "receipts_qty": b.receipts,
        "transfer_in_qty": b.transfer_in,
        "restocked_returns_qty": b.restocked_returns,
        "sales_qty": b.sales,
        "transfer_out_qty": b.transfer_out,
        "adjustments_qty": b.adjustments,
        "expected_qty": expected,
        "counted_qty": count["counted_qty"],
        "variance_qty": variance,
        "variance_value_cents": variance * ss.unit_cost_cents,
        "movement_count": b.n,
        "ledger_watermark_seq": watermark,
        "basis_status": "TIMING_REVIEW" if violations else "RELIABLE",
        "basis_notes": json.dumps(violations) if violations else None,
    }


_COMPARE = ("expected_qty", "variance_qty", "basis_status", "movement_count")


def create_version(conn: Connection, count: dict, trigger: str, watermark: int | None = None,
                   tolerance_min: int | None = None, only_if_changed: bool = False) -> dict | None:
    watermark = current_watermark(conn) if watermark is None else watermark
    tolerance_min = timing_tolerance(conn) if tolerance_min is None else tolerance_min
    prev = conn.execute(_PREV_SQL, {"c": count["count_id"]}).first()
    result = compute(conn, count, watermark, tolerance_min)
    if prev is not None and only_if_changed:
        if all(getattr(prev, k) == result[k] for k in _COMPARE if k != "movement_count") and \
           prev.basis_notes == result["basis_notes"]:
            return None
    recon_id = allocate_ids(conn, "recon")[0]
    rec = {
        "recon_id": recon_id,
        "count_id": count["count_id"],
        "version": 1 if prev is None else prev.version + 1,
        "supersedes_recon_id": None if prev is None else prev.recon_id,
        "trigger": trigger,
        "created_at": utcnow(),
        **result,
    }
    conn.execute(_INSERT_SQL, rec)
    return rec


def reversion_for_late_movements(conn: Connection, seq_before: int) -> int:
    """Create new versions for counts whose cutoff covers movements posted after ``seq_before``.

    A movement that happened after a count's cutoff never touches that count.
    """
    affected = conn.execute(
        text(
            """
            SELECT DISTINCT c.count_id, c.store_id, c.sku_id, c.counted_qty, c.counted_at, c.count_cutoff_at,
                   c.submitted_at
            FROM inventory_movement m
            JOIN cycle_count c ON c.store_id = m.store_id AND c.sku_id = m.sku_id
                               AND m.occurred_at <= c.count_cutoff_at
            WHERE m.ledger_seq > :seq
            UNION
            SELECT DISTINCT c.count_id, c.store_id, c.sku_id, c.counted_qty, c.counted_at, c.count_cutoff_at,
                   c.submitted_at
            FROM inventory_movement m
            JOIN cycle_count c ON c.store_id = m.store_id AND c.sku_id = m.sku_id
                               AND m.occurred_at > MIN(c.counted_at, c.count_cutoff_at)
                               AND m.occurred_at <= MAX(c.counted_at, c.count_cutoff_at)
            WHERE m.ledger_seq > :seq
            ORDER BY 1
            """
        ),
        {"seq": seq_before},
    ).all()
    if not affected:
        return 0
    wm = current_watermark(conn)
    tol = timing_tolerance(conn)
    created = 0
    for r in affected:
        if create_version(conn, dict(r._mapping), "LATE_MOVEMENT", wm, tol, only_if_changed=True):
            created += 1
    return created


def current_recon_sql(alias: str = "r") -> str:
    """SQL predicate selecting only the current (highest) version."""
    return (f"{alias}.version = (SELECT MAX(r2.version) FROM count_reconciliation r2 "
            f"WHERE r2.count_id = {alias}.count_id)")
