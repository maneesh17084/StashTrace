"""Alerts, investigations, detection runs and the dashboard."""
from __future__ import annotations

import json

from fastapi import APIRouter, Depends
from sqlalchemy import text

from ..config import settings
from ..db import Database
from ..schemas import DetectionRunIn, InvestigationEventIn
from ..services import detection, investigations
from ..services.authz import Principal
from ..services.common import DomainError
from ..services.jobs import drain
from ..services.reconciliation import current_recon_sql
from .deps import Page, db_dep, page_dep, paged, principal_dep, store_scope

router = APIRouter(prefix="/api/v1")

ALERT_LIST_SQL = (
    "SELECT a.alert_id, a.case_key, a.rule_code, a.rule_version, a.alert_class, a.alert_type, a.severity, a.store_id,"
    " a.related_store_id, a.sku_id, a.count_id, a.transfer_id, a.threshold_value, a.observed_value, a.reason_text,"
    " a.created_at, a.investigation_id, s.status AS case_status, s.assignee_id,"
    " CASE WHEN s.status='RESOLVED' THEN 'CLOSED' ELSE 'OPEN' END AS status"
    " FROM shrinkage_alert a JOIN investigation_state s ON s.investigation_id=a.investigation_id"
)


@router.get("/alerts")
def list_alerts(store_id: str | None = None, alert_class: str | None = None, alert_type: str | None = None,
                status: str | None = None, severity: str | None = None, page: Page = Depends(page_dep),
                p: Principal = Depends(principal_dep), db: Database = Depends(db_dep)):
    params: dict = {"ac": alert_class, "at": alert_type, "st": status, "sev": severity}
    where = store_scope(p, store_id, "a.store_id", params, related_column="a.related_store_id")
    sql = (f"{ALERT_LIST_SQL} WHERE {where} AND (:ac IS NULL OR a.alert_class=:ac) AND (:at IS NULL OR a.alert_type=:at)"
           f" AND (:sev IS NULL OR a.severity=:sev)"
           f" AND (:st IS NULL OR (CASE WHEN s.status='RESOLVED' THEN 'CLOSED' ELSE 'OPEN' END)=:st)")
    with db.read() as conn:
        out = paged(conn, sql, params, page,
                    "CASE a.severity WHEN 'HIGH' THEN 0 WHEN 'MEDIUM' THEN 1 ELSE 2 END, a.alert_id DESC")
    return out


def _alert_or_404(conn, p: Principal, alert_id: str):
    a = conn.execute(text(f"{ALERT_LIST_SQL} WHERE a.alert_id=:a"), {"a": alert_id}).first()
    if a is None or not (p.can_view(a.store_id) or (a.related_store_id and p.can_view(a.related_store_id))):
        raise DomainError("NOT_FOUND", "Not found.", 404)
    return a


@router.get("/alerts/{alert_id}")
def get_alert(alert_id: str, p: Principal = Depends(principal_dep), db: Database = Depends(db_dep)):
    with db.read() as conn:
        a = _alert_or_404(conn, p, alert_id)
        out = dict(a._mapping)
        full = conn.execute(text("SELECT calculation_json, window_start, window_end, recon_id, first_run_id"
                                 " FROM shrinkage_alert WHERE alert_id=:a"), {"a": alert_id}).one()
        out.update(dict(full._mapping))
        out["calculation"] = json.loads(out.pop("calculation_json"))
        out["rule"] = dict(conn.execute(text(
            "SELECT rule_code, rule_version, alert_class, description, params_json, effective_from FROM rule_definition"
            " WHERE rule_code=:c AND rule_version=:v"), {"c": a.rule_code, "v": a.rule_version}).one()._mapping)
        out["rule"]["params"] = json.loads(out["rule"].pop("params_json"))
        out["evidence"] = []
        for e in conn.execute(text(
                "SELECT e.*, m.movement_type, m.occurred_at, m.received_at, m.actor_id, m.approved_by, m.reason_code,"
                " m.store_id AS movement_store_id, x.error_code, x.batch_id AS import_batch_id, r.raw_line"
                " FROM alert_evidence e LEFT JOIN inventory_movement m ON m.movement_id=e.movement_id"
                " LEFT JOIN import_row_result x ON x.row_id=e.import_row_id"
                " LEFT JOIN import_raw_row r ON r.row_id=e.import_row_id WHERE e.alert_id=:a ORDER BY e.evidence_id"),
                {"a": alert_id}):
            out["evidence"].append(dict(e._mapping))
        out["detections"] = [dict(r._mapping) for r in conn.execute(text(
            "SELECT d.run_id, d.outcome, d.observed_value, s.finished_at FROM detection_run_alert d"
            " JOIN detection_run_status s ON s.run_id=d.run_id WHERE d.alert_id=:a ORDER BY d.run_id"), {"a": alert_id})]
    return out


@router.get("/investigations")
def list_investigations(store_id: str | None = None, status: str | None = None, assignee_id: str | None = None,
                        page: Page = Depends(page_dep), p: Principal = Depends(principal_dep),
                        db: Database = Depends(db_dep)):
    params: dict = {"st": status, "as": assignee_id}
    where = store_scope(p, store_id, "i.store_id", params, related_column="i.related_store_id")
    sql = (f"SELECT i.investigation_id, i.case_key, i.origin_type, i.store_id, i.related_store_id, i.opened_at,"
           f" s.status, s.assignee_id, s.resolution_code, s.resolved_at, s.last_seq,"
           f" (SELECT a.alert_id FROM shrinkage_alert a WHERE a.investigation_id=i.investigation_id LIMIT 1) AS alert_id,"
           f" (SELECT a.alert_type FROM shrinkage_alert a WHERE a.investigation_id=i.investigation_id LIMIT 1) AS alert_type"
           f" FROM investigation i JOIN investigation_state s ON s.investigation_id=i.investigation_id"
           f" WHERE {where} AND (:st IS NULL OR s.status=:st) AND (:as IS NULL OR s.assignee_id=:as)")
    with db.read() as conn:
        return paged(conn, sql, params, page, "i.investigation_id DESC")


@router.get("/investigations/{investigation_id}")
def get_investigation(investigation_id: str, p: Principal = Depends(principal_dep), db: Database = Depends(db_dep)):
    with db.read() as conn:
        inv = conn.execute(text(
            "SELECT i.*, s.status, s.last_seq, s.assignee_id, s.resolution_code, s.resolved_at FROM investigation i"
            " JOIN investigation_state s ON s.investigation_id=i.investigation_id WHERE i.investigation_id=:i"),
            {"i": investigation_id}).first()
        if inv is None or not investigations.visible(p, inv):
            raise DomainError("NOT_FOUND", "Not found.", 404)
        out = dict(inv._mapping)
        out["events"] = [dict(e._mapping) for e in conn.execute(text(
            "SELECT e.*, emp.display_name AS actor_name FROM investigation_event e JOIN employee emp"
            " ON emp.employee_id=e.actor_id WHERE e.investigation_id=:i ORDER BY e.seq"), {"i": investigation_id})]
        out["alerts"] = [dict(a._mapping) for a in conn.execute(text(
            "SELECT alert_id, alert_type, alert_class, severity, reason_text, sku_id, observed_value FROM shrinkage_alert"
            " WHERE investigation_id=:i"), {"i": investigation_id})]
    from ..catalog import INVESTIGATION_TRANSITIONS
    out["allowed_events"] = sorted(INVESTIGATION_TRANSITIONS[out["status"]])
    out["can"] = {
        "assign": p.can("ASSIGN_CASE", inv.store_id) or bool(inv.related_store_id and p.can("ASSIGN_CASE", inv.related_store_id)),
        "investigate": p.can("INVESTIGATE", inv.store_id),
        # Same function apply_event uses to authorise REVIEW_STARTED, so the button never lies.
        "start_review": investigations.can_start_review(p, inv),
        "resolve": p.can("RESOLVE_CASE", inv.store_id) or bool(inv.related_store_id and p.can("RESOLVE_CASE", inv.related_store_id)),
        "approve_write_off": p.can("APPROVE_ADJUSTMENT", inv.store_id),
    }
    return out


@router.post("/investigations/{investigation_id}/events", status_code=201)
def add_event(investigation_id: str, body: InvestigationEventIn, p: Principal = Depends(principal_dep),
              db: Database = Depends(db_dep)):
    return investigations.apply_event(db, investigation_id, p, body.model_dump())


@router.post("/detection-runs", status_code=202)
def start_run(body: DetectionRunIn, p: Principal = Depends(principal_dep), db: Database = Depends(db_dep)):
    if not p.can("RUN_DETECTION"):
        raise DomainError("FORBIDDEN", "Detection runs require RUN_DETECTION across all stores.", 403)
    out = detection.queue_run(db, p.employee_id, body.as_of)
    if settings.run_jobs_inline:
        drain(db)
    return out


@router.get("/detection-runs")
def list_runs(page: Page = Depends(page_dep), p: Principal = Depends(principal_dep), db: Database = Depends(db_dep)):
    sql = ("SELECT r.run_id, r.requested_by, r.requested_at, r.as_of, s.status, s.started_at, s.finished_at,"
           " s.alerts_new, s.alerts_redetected, s.duration_ms, s.ledger_watermark_seq, s.error_message"
           " FROM detection_run r JOIN detection_run_status s ON s.run_id=r.run_id")
    with db.read() as conn:
        return paged(conn, sql, {}, page, "r.run_id DESC")


@router.get("/detection-runs/{run_id}")
def get_run(run_id: str, p: Principal = Depends(principal_dep), db: Database = Depends(db_dep)):
    with db.read() as conn:
        r = conn.execute(text("SELECT r.*, s.* FROM detection_run r JOIN detection_run_status s ON s.run_id=r.run_id"
                              " WHERE r.run_id=:r"), {"r": run_id}).first()
        if r is None:
            raise DomainError("NOT_FOUND", "Not found.", 404)
        out = dict(r._mapping)
        out["rules"] = json.loads(out.pop("rules_json"))
    return out


@router.get("/rules")
def list_rules(p: Principal = Depends(principal_dep), db: Database = Depends(db_dep)):
    with db.read() as conn:
        items = [dict(r._mapping) for r in conn.execute(text("SELECT * FROM rule_definition ORDER BY rule_code, rule_version"))]
    for i in items:
        i["params"] = json.loads(i.pop("params_json"))
    return {"items": items}


@router.get("/dashboard/summary")
def dashboard(date_from: str | None = None, date_to: str | None = None, p: Principal = Depends(principal_dep),
              db: Database = Depends(db_dep)):
    """Per-store comparison of unexplained loss, recorded loss, open cases and exception volumes."""
    params: dict = {"df": date_from, "dt": date_to}
    w_store = store_scope(p, None, "s.store_id", params)
    with db.read() as conn:
        stores = {r.store_id: dict(r._mapping) for r in conn.execute(text(
            f"SELECT s.store_id, s.store_name, s.store_type, s.region_code FROM store s WHERE {w_store}"), params)}
        for st in stores.values():
            st.update({"unexplained_units": 0, "unexplained_value_cents": 0, "recorded_loss_units": 0,
                       "recorded_loss_value_cents": 0, "counts": 0, "open_cases": 0, "open_shrinkage": 0,
                       "open_data_quality": 0, "open_pattern": 0, "open_transfer": 0, "quarantined_rows": 0,
                       "timing_reviews": 0})
        q_alert = text(
            "SELECT a.store_id, a.alert_class, COUNT(*) n,"
            " SUM(CASE WHEN a.alert_type='UNEXPLAINED_SHORTAGE' THEN a.observed_value ELSE 0 END) units,"
            " SUM(CASE WHEN a.alert_type='UNEXPLAINED_SHORTAGE' THEN json_extract(a.calculation_json,"
            " '$.remaining_value_cents') ELSE 0 END) value FROM shrinkage_alert a JOIN investigation_state s"
            " ON s.investigation_id=a.investigation_id LEFT JOIN cycle_count c ON c.count_id=a.count_id"
            " WHERE s.status <> 'RESOLVED' AND (:df IS NULL OR c.count_cutoff_at IS NULL OR c.count_cutoff_at >= :df)"
            " AND (:dt IS NULL OR c.count_cutoff_at IS NULL OR c.count_cutoff_at <= :dt) GROUP BY a.store_id, a.alert_class")
        for r in conn.execute(q_alert, params):
            if r.store_id not in stores:
                continue
            st = stores[r.store_id]
            st["open_cases"] += r.n
            st[f"open_{r.alert_class.lower()}"] = r.n
            st["unexplained_units"] += r.units or 0
            st["unexplained_value_cents"] += int(r.value or 0)
        q_loss = text(
            "SELECT m.store_id, -SUM(m.quantity) units, -SUM(m.quantity * k.unit_cost_cents) value"
            " FROM inventory_movement m JOIN sku k ON k.sku_id=m.sku_id WHERE m.movement_type='ADJUSTMENT'"
            " AND m.quantity < 0 AND (:df IS NULL OR m.occurred_at >= :df) AND (:dt IS NULL OR m.occurred_at <= :dt)"
            " GROUP BY m.store_id")
        for r in conn.execute(q_loss, params):
            if r.store_id in stores:
                stores[r.store_id]["recorded_loss_units"] = r.units
                stores[r.store_id]["recorded_loss_value_cents"] = r.value
        for r in conn.execute(text(
                f"SELECT c.store_id, COUNT(*) n, SUM(CASE WHEN r.basis_status='TIMING_REVIEW' THEN 1 ELSE 0 END) timing_n"
                f" FROM cycle_count c JOIN count_reconciliation r ON r.count_id=c.count_id AND {current_recon_sql('r')}"
                f" WHERE (:df IS NULL OR c.count_cutoff_at >= :df) AND (:dt IS NULL OR c.count_cutoff_at <= :dt)"
                f" GROUP BY c.store_id"), params):
            if r.store_id in stores:
                stores[r.store_id]["counts"] = r.n
                stores[r.store_id]["timing_reviews"] = r.timing_n
        for r in conn.execute(text(
                "SELECT submitted_store_id, COUNT(*) n FROM import_row_result WHERE outcome='QUARANTINED'"
                " AND (:df IS NULL OR occurred_at >= :df) AND (:dt IS NULL OR occurred_at <= :dt)"
                " GROUP BY submitted_store_id"), params):
            if r.submitted_store_id in stores:
                stores[r.submitted_store_id]["quarantined_rows"] = r.n
        exceptions = [dict(r._mapping) for r in conn.execute(text(
            "SELECT error_code, COUNT(*) n FROM import_row_result WHERE outcome='QUARANTINED'"
            " AND (:df IS NULL OR occurred_at >= :df) AND (:dt IS NULL OR occurred_at <= :dt)"
            " AND (submitted_store_id IN (SELECT value FROM json_each(:vis)) OR :all = 1)"
            " GROUP BY error_code ORDER BY n DESC"),
            {**params, "vis": json.dumps(sorted(stores)), "all": 1 if p.stores() is None else 0})]
    rows = sorted(stores.values(), key=lambda s: (-s["unexplained_value_cents"], s["store_id"]))
    totals = {k: sum(s[k] for s in rows) for k in (
        "unexplained_units", "unexplained_value_cents", "recorded_loss_units", "recorded_loss_value_cents", "counts",
        "open_cases", "open_shrinkage", "open_data_quality", "open_pattern", "open_transfer", "quarantined_rows",
        "timing_reviews")}
    return {"stores": rows, "totals": totals, "exceptions_by_code": exceptions, "date_from": date_from, "date_to": date_to}
