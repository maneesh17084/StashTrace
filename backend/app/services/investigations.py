"""Investigation case workflow: OPEN -> ASSIGNED -> IN_REVIEW -> RESOLVED.

Every change is an append-only ``investigation_event``; ``investigation_state`` is a projection that
also serves optimistic concurrency (``expected_seq``). Resolving never changes stock by itself. An
approved write-off is posted as a separate INVESTIGATION_WRITE_OFF ledger entry through the normal
validation path, in the same transaction as the RESOLVED event, and the event links to it.
"""
from __future__ import annotations

import json

from sqlalchemy import text
from sqlalchemy.engine import Connection

from .. import catalog as C
from ..db import Database
from .authz import Principal
from .common import DomainError, utcnow
from .posting import post_in_transaction


def _load(conn: Connection, investigation_id: str):
    inv = conn.execute(text(
        "SELECT i.*, s.status, s.last_seq, s.assignee_id FROM investigation i"
        " JOIN investigation_state s ON s.investigation_id = i.investigation_id WHERE i.investigation_id = :i"),
        {"i": investigation_id}).first()
    if inv is None:
        raise DomainError("NOT_FOUND", "Investigation not found.", 404)
    return inv


def visible(principal: Principal, inv) -> bool:
    return principal.can_view(inv.store_id) or (inv.related_store_id is not None and principal.can_view(inv.related_store_id))


def case_role(principal: Principal, action: str, inv) -> str | None:
    """Role that grants ``action`` on this case (either store of a transfer case), or None."""
    role = principal.role_for(action, inv.store_id)
    if role is None and inv.related_store_id:
        role = principal.role_for(action, inv.related_store_id)
    return role


def _require(principal: Principal, action: str, inv) -> str:
    role = case_role(principal, action, inv)
    if role is None:
        raise DomainError("FORBIDDEN", f"Your roles do not permit {action} for {inv.store_id}.", 403)
    return role


def start_review_action(principal: Principal, inv) -> str:
    """The assignee starts review with INVESTIGATE; anyone else needs ASSIGN_CASE (a supervisor)."""
    return "INVESTIGATE" if principal.employee_id == inv.assignee_id else "ASSIGN_CASE"


def can_start_review(principal: Principal, inv) -> bool:
    """Same rule apply_event enforces for REVIEW_STARTED, plus the status transition check."""
    return ("REVIEW_STARTED" in C.INVESTIGATION_TRANSITIONS[inv.status]
            and case_role(principal, start_review_action(principal, inv), inv) is not None)


def _post_write_off(conn: Connection, db: Database, inv, alert, principal: Principal, qty: int, now: str) -> str:
    if alert.sku_id is None:
        raise DomainError("WRITE_OFF_NOT_APPLICABLE", "This case is not SKU-specific; post write-offs per SKU.", 422)
    if alert.alert_type != "UNEXPLAINED_SHORTAGE":
        raise DomainError("WRITE_OFF_NOT_APPLICABLE",
                          "Write-offs from a case are only allowed for unexplained count shortages.", 422)
    if qty <= 0 or qty > (alert.observed_value or 0):
        raise DomainError("WRITE_OFF_QTY_INVALID",
                          f"Write-off must be between 1 and the remaining shortage ({alert.observed_value}).", 422)
    if principal.role_for("APPROVE_ADJUSTMENT", alert.store_id) is None:
        raise DomainError("FORBIDDEN", "Approving a write-off requires APPROVE_ADJUSTMENT for this store.", 403)
    row = {
        "source_system": "STASHTRACE", "source_event_id": f"{inv.investigation_id}-WO", "movement_type": "ADJUSTMENT",
        "store_id": alert.store_id, "sku_id": alert.sku_id, "quantity": -qty, "occurred_at": now,
        "received_at": now, "actor_id": principal.employee_id, "reason_code": "INVESTIGATION_WRITE_OFF",
        "approved_by": principal.employee_id, "count_ref": alert.count_id, "investigation_id": inv.investigation_id,
        "memo": f"Write-off approved on resolution of {inv.investigation_id}",
    }
    stats = post_in_transaction(conn, db, [row], principal.employee_id, "STASHTRACE",
                                f"write-off-{inv.investigation_id}", now)
    res = stats.outcomes[0]
    if res["outcome"] != "POSTED":
        raise DomainError("WRITE_OFF_REJECTED", "The write-off failed ledger validation.", 422,
                          {"error_code": res["error_code"], "errors": json.loads(res["errors_json"] or "[]")})
    return res["movement_id"]


def apply_event(db: Database, investigation_id: str, principal: Principal, body: dict) -> dict:
    etype = body.get("event_type")
    if etype not in ("ASSIGNED", "REVIEW_STARTED", "FINDING_ADDED", "RESOLVED"):
        raise DomainError("INVALID_EVENT", "event_type must be ASSIGNED, REVIEW_STARTED, FINDING_ADDED or RESOLVED.")
    now = utcnow()
    with db.write() as conn:
        inv = _load(conn, investigation_id)
        if not visible(principal, inv):
            raise DomainError("NOT_FOUND", "Investigation not found.", 404)
        if body.get("expected_seq") is not None and int(body["expected_seq"]) != inv.last_seq:
            raise DomainError("STALE_CASE", f"Case changed since you loaded it (now at event {inv.last_seq}).", 409)
        allowed = C.INVESTIGATION_TRANSITIONS[inv.status]
        if etype not in allowed:
            raise DomainError("INVALID_TRANSITION", f"{etype} is not allowed while the case is {inv.status}.", 409,
                              {"allowed": sorted(allowed)})
        to_status = allowed[etype]
        note = (body.get("note") or "").strip() or None
        assignee = None
        resolution = None
        write_off_id = None
        if etype == "ASSIGNED":
            role = _require(principal, "ASSIGN_CASE", inv)
            assignee = body.get("assignee_id")
            if not assignee:
                raise DomainError("MISSING_FIELD", "assignee_id is required.")
            idx = principal.index
            if idx.authorize(assignee, "INVESTIGATE", inv.store_id, now) is None:
                raise DomainError("INVALID_ASSIGNEE", f"{assignee} is not authorised to investigate {inv.store_id}.")
        elif etype == "REVIEW_STARTED":
            role = _require(principal, start_review_action(principal, inv), inv)
            assignee = inv.assignee_id
        elif etype == "FINDING_ADDED":
            role = _require(principal, "INVESTIGATE", inv)
            if not note:
                raise DomainError("MISSING_FIELD", "A finding needs a note.")
            assignee = inv.assignee_id
        else:  # RESOLVED
            role = _require(principal, "RESOLVE_CASE", inv)
            resolution = body.get("resolution_code")
            if resolution not in C.RESOLUTION_CODES:
                raise DomainError("INVALID_RESOLUTION", f"resolution_code must be one of {C.RESOLUTION_CODES}.")
            if not note or len(note) < 10:
                raise DomainError("MISSING_FIELD", "A resolution reason of at least 10 characters is required.")
            assignee = inv.assignee_id
            if body.get("write_off_qty"):
                alert = conn.execute(text("SELECT * FROM shrinkage_alert WHERE investigation_id=:i ORDER BY alert_id LIMIT 1"),
                                     {"i": investigation_id}).one()
                write_off_id = _post_write_off(conn, db, inv, alert, principal, int(body["write_off_qty"]), now)
        seq = inv.last_seq + 1
        conn.execute(text(
            "INSERT INTO investigation_event (investigation_id, seq, event_type, from_status, to_status, actor_id,"
            " actor_role_code, assignee_id, note, resolution_code, write_off_movement_id, occurred_at)"
            " VALUES (:i, :seq, :e, :f, :t, :a, :ar, :as, :n, :rc, :wo, :now)"),
            {"i": investigation_id, "seq": seq, "e": etype, "f": inv.status, "t": to_status,
             "a": principal.employee_id, "ar": role, "as": assignee, "n": note, "rc": resolution, "wo": write_off_id,
             "now": now})
        conn.execute(text(
            "UPDATE investigation_state SET status=:s, last_seq=:seq, assignee_id=:as, resolution_code=COALESCE(:rc,"
            " resolution_code), resolved_at=CASE WHEN :s='RESOLVED' THEN :now ELSE resolved_at END, updated_at=:now"
            " WHERE investigation_id=:i AND last_seq=:prev"),
            {"s": to_status, "seq": seq, "as": assignee, "rc": resolution, "now": now, "i": investigation_id,
             "prev": inv.last_seq})
    return {"investigation_id": investigation_id, "seq": seq, "event_type": etype, "from_status": inv.status,
            "to_status": to_status, "write_off_movement_id": write_off_id}
