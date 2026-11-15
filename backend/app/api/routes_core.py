"""Auth, master data, movements and imports."""
from __future__ import annotations

import json

from fastapi import APIRouter, Depends, File, Form, Query, UploadFile
from sqlalchemy import text

from ..config import settings
from ..db import Database
from ..schemas import CorrectionIn, LoginIn, MovementIn, ReversalIn
from ..services import posting
from ..services.authz import Principal, issue_token, verify_password
from ..services.common import DomainError
from ..services.jobs import drain
from .deps import Page, db_dep, page_dep, paged, principal_dep, require_view, store_scope

router = APIRouter(prefix="/api/v1")


# ------------------------------------------------------------------ auth
@router.post("/auth/login")
def login(body: LoginIn, db: Database = Depends(db_dep)):
    with db.write() as conn:
        emp = conn.execute(text("SELECT employee_id, display_name, password_hash, status, employee_type FROM employee"
                                " WHERE employee_id=:e"), {"e": body.employee_id}).first()
        if emp is None or emp.status != "ACTIVE" or emp.employee_type != "HUMAN" or \
                not verify_password(body.password, emp.password_hash):
            raise DomainError("INVALID_CREDENTIALS", "Employee ID or password is incorrect.", 401)
        token, expires = issue_token(conn, emp.employee_id, settings.token_ttl_hours)
    return {"token": token, "expires_at": expires, "employee_id": emp.employee_id, "display_name": emp.display_name}


@router.get("/auth/me")
def me(p: Principal = Depends(principal_dep)):
    stores = p.stores("VIEW_STORE")
    actions = sorted({a for r in p.roles() for a in p.index.permissions.get(r, ())})
    return {"employee_id": p.employee_id, "display_name": p.display_name, "roles": p.roles(), "actions": actions,
            "stores": "ALL" if stores is None else sorted(stores)}


# ----------------------------------------------------------- master data
@router.get("/stores")
def list_stores(p: Principal = Depends(principal_dep), db: Database = Depends(db_dep)):
    params: dict = {}
    where = store_scope(p, None, "store_id", params)
    with db.read() as conn:
        items = [dict(r._mapping) for r in conn.execute(
            text(f"SELECT store_id, store_name, store_type, region_code, status FROM store WHERE {where} ORDER BY store_id"),
            params)]
    return {"items": items}


@router.get("/skus")
def list_skus(store_id: str = Query(...), q: str | None = None, page: Page = Depends(page_dep),
              p: Principal = Depends(principal_dep), db: Database = Depends(db_dep)):
    params: dict = {"q": f"%{q}%" if q else None}
    where = store_scope(p, store_id, "ss.store_id", params)
    sql = (f"SELECT k.sku_id, k.sku_name, k.category, k.unit_cost_cents, ss.active_from, ss.active_to,"
           f" b.on_hand FROM store_sku ss JOIN sku k ON k.sku_id=ss.sku_id"
           f" LEFT JOIN stock_balance b ON b.store_id=ss.store_id AND b.sku_id=ss.sku_id"
           f" WHERE {where} AND (:q IS NULL OR k.sku_id LIKE :q OR k.sku_name LIKE :q)")
    with db.read() as conn:
        return paged(conn, sql, params, page, "k.sku_id")


@router.get("/shifts")
def list_shifts(store_id: str, date: str = Query(..., pattern=r"^\d{4}-\d{2}-\d{2}$"),
                p: Principal = Depends(principal_dep), db: Database = Depends(db_dep)):
    params: dict = {"d": date}
    where = store_scope(p, store_id, "store_id", params)
    with db.read() as conn:
        items = [dict(r._mapping) for r in conn.execute(text(
            f"SELECT shift_id, shift_type, start_at, end_at FROM shift WHERE {where} AND shift_date=:d ORDER BY start_at"),
            params)]
    return {"items": items}


@router.get("/employees")
def list_employees(store_id: str, action: str = "INVESTIGATE", p: Principal = Depends(principal_dep),
                   db: Database = Depends(db_dep)):
    """People who currently hold ``action`` for a store (e.g. assignable investigators)."""
    require_view(p, store_id)
    with db.read() as conn:
        emps = conn.execute(text("SELECT employee_id, display_name FROM employee WHERE status='ACTIVE'"
                                 " AND employee_type='HUMAN' ORDER BY employee_id")).all()
    items = [{"employee_id": e.employee_id, "display_name": e.display_name,
              "role": p.index.authorize(e.employee_id, action, store_id, p.now)} for e in emps]
    return {"items": [i for i in items if i["role"]]}


# -------------------------------------------------------------- movements
@router.get("/movements")
def list_movements(store_id: str, sku_id: str | None = None, date_from: str | None = None, date_to: str | None = None,
                   movement_type: str | None = None, page: Page = Depends(page_dep),
                   p: Principal = Depends(principal_dep), db: Database = Depends(db_dep)):
    params: dict = {"sku": sku_id, "df": date_from, "dt": date_to, "mt": movement_type}
    where = store_scope(p, store_id, "m.store_id", params)
    sql = (f"SELECT m.movement_id, m.movement_type, m.store_id, m.sku_id, m.quantity, m.stock_qty, m.occurred_at,"
           f" m.received_at, m.posted_at, m.actor_id, m.actor_role_code, m.shift_id, m.source_doc_id, m.transfer_id,"
           f" m.reason_code, m.approved_by, m.reverses_movement_id, m.memo, m.batch_id FROM inventory_movement m"
           f" WHERE {where} AND (:sku IS NULL OR m.sku_id=:sku) AND (:df IS NULL OR m.occurred_at >= :df)"
           f" AND (:dt IS NULL OR m.occurred_at <= :dt) AND (:mt IS NULL OR m.movement_type=:mt)")
    with db.read() as conn:
        return paged(conn, sql, params, page, "m.occurred_at DESC, m.ledger_seq DESC")


@router.get("/movements/{movement_id}")
def get_movement(movement_id: str, p: Principal = Depends(principal_dep), db: Database = Depends(db_dep)):
    with db.read() as conn:
        m = conn.execute(text("SELECT * FROM inventory_movement WHERE movement_id=:m"), {"m": movement_id}).first()
        if m is None:
            raise DomainError("NOT_FOUND", "Not found.", 404)
        require_view(p, m.store_id)
        out = dict(m._mapping)
        rev = conn.execute(text("SELECT movement_id FROM inventory_movement WHERE reverses_movement_id=:m"),
                           {"m": movement_id}).first()
        out["reversed_by"] = rev.movement_id if rev else None
        ret = conn.execute(text("SELECT * FROM customer_return WHERE movement_id=:m"), {"m": movement_id}).first()
        out["return"] = dict(ret._mapping) if ret else None
    return out


@router.post("/movements", status_code=201)
def post_movement(body: MovementIn, p: Principal = Depends(principal_dep), db: Database = Depends(db_dep)):
    """Post one movement. Idempotent on (source_system, source_event_id)."""
    if not p.can_view(body.store_id):
        raise DomainError("FORBIDDEN", f"You are not authorised for {body.store_id}.", 403)
    out = posting.post_single_movement(db, body.model_dump(), p.employee_id)
    return out


@router.post("/movements/{movement_id}/reversal", status_code=201)
def reverse(movement_id: str, body: ReversalIn, p: Principal = Depends(principal_dep), db: Database = Depends(db_dep)):
    with db.read() as conn:
        m = conn.execute(text("SELECT store_id FROM inventory_movement WHERE movement_id=:m"), {"m": movement_id}).first()
    if m is None:
        raise DomainError("NOT_FOUND", "Not found.", 404)
    require_view(p, m.store_id)
    if not p.can("POST_REVERSAL", m.store_id):
        raise DomainError("FORBIDDEN", "Reversals require POST_REVERSAL for this store.", 403)
    repl = body.replacement.model_dump() if body.replacement else None
    return posting.reverse_movement(db, movement_id, body.reason, p.employee_id, repl)


# ---------------------------------------------------------------- imports
@router.post("/imports", status_code=202)
async def upload_import(file: UploadFile = File(...), batch_kind: str = Form("MOVEMENTS"),
                        source_system: str = Form("STORE_OPS"), p: Principal = Depends(principal_dep),
                        db: Database = Depends(db_dep)):
    if p.stores("IMPORT_BATCH") == set() and p.stores("SUBMIT_COUNT") == set():
        raise DomainError("FORBIDDEN", "Your roles do not permit imports.", 403)
    data = await file.read()
    if len(data) > settings.max_upload_mb * 1024 * 1024:
        raise DomainError("FILE_TOO_LARGE", f"Maximum upload is {settings.max_upload_mb} MB.", 413)
    out = posting.receive_batch(db, batch_kind, file.filename or "upload.csv", data, p.employee_id, source_system)
    if settings.run_jobs_inline:
        drain(db)
    return out


def _batch_visible(conn, p: Principal, batch) -> None:
    if batch.submitted_by == p.employee_id or p.stores("VIEW_STORE") is None:
        return
    # Store-scoped users may see a batch only if every row they could see belongs to their stores.
    visible = p.stores("VIEW_STORE") or set()
    other = conn.execute(text("SELECT 1 FROM import_row_result WHERE batch_id=:b AND submitted_store_id IS NOT NULL"
                              " AND submitted_store_id NOT IN (SELECT value FROM json_each(:v)) LIMIT 1"),
                         {"b": batch.batch_id, "v": json.dumps(sorted(visible))}).first()
    if other is not None:
        raise DomainError("NOT_FOUND", "Not found.", 404)


@router.get("/imports")
def list_imports(page: Page = Depends(page_dep), p: Principal = Depends(principal_dep), db: Database = Depends(db_dep)):
    params: dict = {"me": p.employee_id, "all": 1 if p.stores("VIEW_STORE") is None else 0}
    sql = ("SELECT b.batch_id, b.batch_kind, b.source_system, b.file_name, b.submitted_by, b.submitted_at, s.status,"
           " s.received_count, s.processed_count, s.posted_count, s.quarantined_count, s.duplicate_count,"
           " s.conflict_count, s.started_at, s.finished_at, s.error_message FROM import_batch b"
           " JOIN import_batch_status s ON s.batch_id=b.batch_id WHERE (:all = 1 OR b.submitted_by = :me)")
    with db.read() as conn:
        return paged(conn, sql, params, page, "b.batch_id DESC")


@router.get("/imports/{batch_id}")
def get_import(batch_id: str, p: Principal = Depends(principal_dep), db: Database = Depends(db_dep)):
    with db.read() as conn:
        b = conn.execute(text("SELECT b.*, s.* FROM import_batch b JOIN import_batch_status s ON s.batch_id=b.batch_id"
                              " WHERE b.batch_id=:b"), {"b": batch_id}).first()
        if b is None:
            raise DomainError("NOT_FOUND", "Not found.", 404)
        _batch_visible(conn, p, b)
        out = dict(b._mapping)
        out.pop("header_csv", None)
        out["errors_by_code"] = {r.error_code: r.n for r in conn.execute(text(
            "SELECT error_code, COUNT(*) n FROM import_row_result WHERE batch_id=:b AND error_code IS NOT NULL"
            " GROUP BY error_code ORDER BY n DESC"), {"b": batch_id})}
        job = conn.execute(text("SELECT job_id, status, attempts, error_message FROM job WHERE job_type='IMPORT'"
                                " AND ref_id=:b ORDER BY job_id DESC LIMIT 1"), {"b": batch_id}).first()
        out["job"] = dict(job._mapping) if job else None
    return out


@router.get("/imports/{batch_id}/rows")
def import_rows(batch_id: str, outcome: str | None = "QUARANTINED", error_code: str | None = None,
                page: Page = Depends(page_dep), p: Principal = Depends(principal_dep), db: Database = Depends(db_dep)):
    with db.read() as conn:
        b = conn.execute(text("SELECT * FROM import_batch WHERE batch_id=:b"), {"b": batch_id}).first()
        if b is None:
            raise DomainError("NOT_FOUND", "Not found.", 404)
        _batch_visible(conn, p, b)
        params = {"b": batch_id, "o": outcome, "e": error_code}
        sql = ("SELECT x.row_id, r.row_number, x.outcome, x.error_code, x.errors_json, x.source_event_id,"
               " x.movement_type, x.submitted_store_id, x.authoritative_store_id, x.sku_id, x.occurred_at,"
               " x.movement_id, x.count_id, x.corrects_row_id, x.duplicate_of_row_id, r.raw_line,"
               " (SELECT c.row_id FROM import_row_result c WHERE c.corrects_row_id = x.row_id LIMIT 1) AS corrected_by_row_id"
               " FROM import_row_result x JOIN import_raw_row r ON r.row_id=x.row_id"
               " WHERE x.batch_id=:b AND (:o IS NULL OR x.outcome=:o) AND (:e IS NULL OR x.error_code=:e)")
        out = paged(conn, sql, params, page, "x.row_id")
        out["header"] = b.header_csv
    for it in out["items"]:
        it["errors"] = json.loads(it.pop("errors_json") or "[]")
    return out


@router.post("/imports/rows/{row_id}/correction", status_code=201)
def correct(row_id: int, body: CorrectionIn, p: Principal = Depends(principal_dep), db: Database = Depends(db_dep)):
    with db.read() as conn:
        x = conn.execute(text("SELECT authoritative_store_id, submitted_store_id FROM import_row_result WHERE row_id=:r"),
                         {"r": row_id}).first()
    if x is None:
        raise DomainError("NOT_FOUND", "Not found.", 404)
    require_view(p, x.authoritative_store_id, x.submitted_store_id)
    if not (p.can("IMPORT_BATCH", x.authoritative_store_id) or p.can("IMPORT_BATCH", x.submitted_store_id)):
        raise DomainError("FORBIDDEN", "Corrections require IMPORT_BATCH for this store.", 403)
    return posting.correct_row(db, row_id, body.model_dump(exclude_none=True), p.employee_id)


@router.post("/imports/{batch_id}/retry", status_code=202)
def retry_import(batch_id: str, p: Principal = Depends(principal_dep), db: Database = Depends(db_dep)):
    """Re-queue a failed batch. Already-processed rows are skipped, so a retry never double-posts."""
    if not p.can("IMPORT_BATCH"):
        raise DomainError("FORBIDDEN", "Retry requires IMPORT_BATCH.", 403)
    from ..services.common import utcnow
    with db.write() as conn:
        st = conn.execute(text("SELECT status FROM import_batch_status WHERE batch_id=:b"), {"b": batch_id}).first()
        if st is None:
            raise DomainError("NOT_FOUND", "Not found.", 404)
        if st.status not in ("FAILED", "RECEIVED"):
            raise DomainError("NOT_RETRYABLE", f"Batch is {st.status}.", 409)
        conn.execute(text("UPDATE import_batch_status SET status='RECEIVED', error_message=NULL WHERE batch_id=:b"),
                     {"b": batch_id})
        job_id = conn.execute(text("INSERT INTO job (job_type, ref_id, status, attempts, created_at)"
                                   " VALUES ('IMPORT', :b, 'QUEUED', 0, :t) RETURNING job_id"),
                              {"b": batch_id, "t": utcnow()}).scalar_one()
    if settings.run_jobs_inline:
        drain(db)
    return {"batch_id": batch_id, "job_id": job_id}
