"""Cycle counts and reconciliation detail."""
from __future__ import annotations

import csv
import io
import json

from fastapi import APIRouter, Depends, Query
from fastapi.responses import StreamingResponse
from sqlalchemy import text

from ..db import Database
from ..schemas import CountIn
from ..services import posting
from ..services.authz import Principal
from ..services.common import DomainError, utcnow
from ..services.reconciliation import current_recon_sql
from .deps import Page, db_dep, page_dep, paged, principal_dep, require_view, store_scope

router = APIRouter(prefix="/api/v1")

BUCKETS = {
    "RECEIPT": "receipts_qty", "TRANSFER_IN": "transfer_in_qty", "RETURN": "restocked_returns_qty",
    "SALE": "sales_qty", "TRANSFER_OUT": "transfer_out_qty", "ADJUSTMENT": "adjustments_qty",
}


@router.post("/counts", status_code=201)
def submit_count(body: CountIn, p: Principal = Depends(principal_dep), db: Database = Depends(db_dep)):
    """Submit a physical count. Returns the import outcome and, when accepted, reconciliation v1."""
    if not p.can("SUBMIT_COUNT", body.store_id):
        raise DomainError("FORBIDDEN", f"You cannot submit counts for {body.store_id}.", 403)
    counted_by = body.counted_by or p.employee_id
    if counted_by != p.employee_id and not p.can("APPROVE_ADJUSTMENT", body.store_id):
        raise DomainError("FORBIDDEN", "Only a manager may submit a count on behalf of another counter.", 403)
    payload = body.model_dump()
    payload.update({"counted_by": counted_by, "submitted_at": utcnow(), "source_system": "STORE_OPS",
                    "count_cutoff_at": body.count_cutoff_at or body.counted_at})
    if not payload.get("count_id"):
        from ..services.common import allocate_ids
        with db.write() as conn:
            payload["count_id"] = allocate_ids(conn, "count")[0]
    out = posting.submit_single_count(db, payload, p.employee_id)
    if out["outcome"] == "POSTED":
        with db.read() as conn:
            r = conn.execute(text(f"SELECT recon_id FROM count_reconciliation r WHERE count_id=:c AND "
                                  f"{current_recon_sql('r')}"), {"c": out["count_id"]}).scalar_one()
        out["recon_id"] = r
    return out


@router.get("/counts")
def list_counts(store_id: str | None = None, sku_id: str | None = None, page: Page = Depends(page_dep),
                p: Principal = Depends(principal_dep), db: Database = Depends(db_dep)):
    params: dict = {"sku": sku_id}
    where = store_scope(p, store_id, "c.store_id", params)
    sql = (f"SELECT c.*, r.recon_id, r.version, r.expected_qty, r.variance_qty, r.basis_status FROM cycle_count c"
           f" JOIN count_reconciliation r ON r.count_id=c.count_id AND {current_recon_sql('r')}"
           f" WHERE {where} AND (:sku IS NULL OR c.sku_id=:sku)")
    with db.read() as conn:
        return paged(conn, sql, params, page, "c.counted_at DESC")


def _recon_query(p: Principal, store_id, sku_id, date_from, date_to, basis, variance, params: dict) -> str:
    where = store_scope(p, store_id, "c.store_id", params)
    params.update({"sku": sku_id, "df": date_from, "dt": date_to, "basis": basis})
    var_sql = {"short": "AND r.variance_qty < 0", "over": "AND r.variance_qty > 0", "zero": "AND r.variance_qty = 0"}.get(variance or "", "")
    return (f"SELECT r.recon_id, r.count_id, r.version, r.trigger, c.store_id, c.sku_id, k.sku_name, c.counted_at,"
            f" c.count_cutoff_at, r.expected_qty, r.counted_qty, r.variance_qty, r.variance_value_cents, r.basis_status,"
            f" r.created_at FROM count_reconciliation r JOIN cycle_count c ON c.count_id=r.count_id"
            f" JOIN sku k ON k.sku_id=c.sku_id WHERE {current_recon_sql('r')} AND {where}"
            f" AND (:sku IS NULL OR c.sku_id=:sku) AND (:df IS NULL OR c.count_cutoff_at >= :df)"
            f" AND (:dt IS NULL OR c.count_cutoff_at <= :dt) AND (:basis IS NULL OR r.basis_status=:basis) {var_sql}")


@router.get("/reconciliations")
def list_reconciliations(store_id: str | None = None, sku_id: str | None = None, date_from: str | None = None,
                         date_to: str | None = None, basis_status: str | None = None,
                         variance: str | None = Query(None, pattern="^(short|over|zero)$"),
                         page: Page = Depends(page_dep), p: Principal = Depends(principal_dep),
                         db: Database = Depends(db_dep)):
    params: dict = {}
    sql = _recon_query(p, store_id, sku_id, date_from, date_to, basis_status, variance, params)
    with db.read() as conn:
        return paged(conn, sql, params, page, "c.count_cutoff_at DESC, r.count_id DESC")


@router.get("/reconciliations/export.csv")
def export_reconciliations(store_id: str | None = None, sku_id: str | None = None, date_from: str | None = None,
                           date_to: str | None = None, p: Principal = Depends(principal_dep),
                           db: Database = Depends(db_dep)):
    if store_id:
        if not p.can("EXPORT_DATA", store_id):
            raise DomainError("FORBIDDEN", "Export requires EXPORT_DATA for this store.", 403)
    elif p.stores("EXPORT_DATA") is not None:
        raise DomainError("FORBIDDEN", "Choose a store to export.", 403)
    params: dict = {}
    sql = _recon_query(p, store_id, sku_id, date_from, date_to, None, None, params)
    with db.read() as conn:
        rows = conn.execute(text(sql + " ORDER BY r.count_id"), params).all()
    buf = io.StringIO()
    w = csv.writer(buf)
    if rows:
        w.writerow(rows[0]._mapping.keys())
        for r in rows:
            w.writerow(list(r))
    return StreamingResponse(iter([buf.getvalue()]), media_type="text/csv",
                             headers={"Content-Disposition": "attachment; filename=reconciliations.csv"})


@router.get("/reconciliations/{recon_id}")
def get_reconciliation(recon_id: str, movements_page: int = Query(1, ge=1), movements_page_size: int = Query(100, ge=1, le=500),
                       p: Principal = Depends(principal_dep), db: Database = Depends(db_dep)):
    with db.read() as conn:
        r = conn.execute(text(
            "SELECT r.*, c.store_id, c.sku_id, c.counted_at, c.count_cutoff_at, c.submitted_at, c.counted_by,"
            " c.counter_role_code, c.shift_id, c.count_type, c.batch_id, k.sku_name, k.unit_cost_cents, s.store_name,"
            " ss.opening_as_of FROM count_reconciliation r JOIN cycle_count c ON c.count_id=r.count_id"
            " JOIN sku k ON k.sku_id=c.sku_id JOIN store s ON s.store_id=c.store_id"
            " JOIN store_sku ss ON ss.store_id=c.store_id AND ss.sku_id=c.sku_id WHERE r.recon_id=:r"),
            {"r": recon_id}).first()
        if r is None:
            raise DomainError("NOT_FOUND", "Not found.", 404)
        require_view(p, r.store_id)
        out = dict(r._mapping)
        out["basis_notes"] = json.loads(out["basis_notes"] or "[]")
        out["versions"] = [dict(v._mapping) for v in conn.execute(text(
            "SELECT recon_id, version, trigger, expected_qty, variance_qty, basis_status, ledger_watermark_seq, created_at"
            " FROM count_reconciliation WHERE count_id=:c ORDER BY version"), {"c": r.count_id})]
        out["is_current"] = out["versions"][-1]["recon_id"] == recon_id
        mparams = {"st": r.store_id, "sk": r.sku_id, "c": r.count_cutoff_at, "wm": r.ledger_watermark_seq}
        base = ("SELECT m.movement_id, m.movement_type, COALESCE(o.movement_type, m.movement_type) AS bucket,"
                " m.quantity, m.stock_qty, m.occurred_at, m.received_at, m.posted_at, m.actor_id, m.actor_role_code,"
                " m.shift_id, m.source_doc_id, m.transfer_id, m.reason_code, m.approved_by, m.reverses_movement_id,"
                " m.ledger_seq FROM inventory_movement m LEFT JOIN inventory_movement o"
                " ON o.movement_id = m.reverses_movement_id WHERE m.store_id=:st AND m.sku_id=:sk"
                " AND m.occurred_at <= :c AND m.ledger_seq <= :wm")
        out["bucket_check"] = {b: {"rows": 0} for b in BUCKETS}
        for row in conn.execute(text(f"SELECT bucket, COUNT(*) n, SUM(stock_qty) s FROM ({base}) GROUP BY bucket"), mparams):
            out["bucket_check"][row.bucket] = {"rows": row.n, "stock_qty": row.s}
        total = sum(v["rows"] for v in out["bucket_check"].values())
        off = (movements_page - 1) * movements_page_size
        out["movements"] = {
            "items": [dict(m._mapping) for m in conn.execute(
                text(base + " ORDER BY m.occurred_at, m.ledger_seq LIMIT :l OFFSET :o"),
                {**mparams, "l": movements_page_size, "o": off})],
            "page": movements_page, "page_size": movements_page_size, "total": total,
        }
        # Movements that arrived late (posted after this version was computed) but belong before the cutoff.
        out["late_movements_after_watermark"] = conn.execute(text(
            "SELECT COUNT(*) FROM inventory_movement WHERE store_id=:st AND sku_id=:sk AND occurred_at <= :c"
            " AND ledger_seq > :wm"), mparams).scalar_one()
        out["promotions"] = [dict(x._mapping) for x in conn.execute(text(
            "SELECT p.promotion_id, p.promo_name, p.start_date, p.end_date, p.discount_pct FROM promotion p"
            " JOIN promotion_scope ps ON ps.promotion_id=p.promotion_id WHERE ps.store_id=:st AND ps.sku_id=:sk"
            " AND p.start_date <= substr(:c,1,10) ORDER BY p.start_date DESC LIMIT 5"), mparams)]
        out["alerts"] = [dict(a._mapping) for a in conn.execute(text(
            "SELECT alert_id, alert_type, severity, case_key FROM shrinkage_alert WHERE count_id=:c"),
            {"c": r.count_id})]
    return out
