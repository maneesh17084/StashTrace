"""The first working flow: two stores, one SKU, through the HTTP API.

import movements -> submit a physical count -> explain the variance -> resolve the alert.
"""
from sqlalchemy import text

from backend.app.services import jobs
from tests.conftest import (ADMIN, CLERK1, LP, OPS, S1, SKU, balance, csv_bytes, demo_movements, login, shift)
from backend.app.services.posting import MOVEMENT_FIELDS


def test_full_flow_via_api(client, db, demo_dir):
    admin = login(client, ADMIN)
    # 1. Import: rows are saved and a job id is returned; the worker posts them.
    rows = demo_movements(demo_dir)
    r = client.post("/api/v1/imports", headers=admin, data={"batch_kind": "MOVEMENTS", "source_system": "STORE_OPS"},
                    files={"file": ("demo.csv", csv_bytes(MOVEMENT_FIELDS, rows), "text/csv")})
    assert r.status_code == 202, r.text
    batch = r.json()
    assert batch["job_id"] and batch["received_count"] == len(rows)
    assert jobs.drain(db) == 1
    b = client.get(f"/api/v1/imports/{batch['batch_id']}", headers=admin).json()
    assert b["status"] == "COMPLETED"
    assert (b["posted_count"], b["quarantined_count"], b["duplicate_count"], b["conflict_count"]) == (16, 4, 1, 1)
    assert b["errors_by_code"] == {"WRONG_STORE_REFERENCE": 1, "ADJUSTMENT_REASON_MISSING": 1,
                                   "ACTOR_NOT_AUTHORIZED": 1, "SELF_APPROVAL": 1, "CONFLICT_PAYLOAD": 1}
    assert balance(db, S1) == 73

    # 2. Count entry by the store clerk.
    clerk = login(client, CLERK1)
    r = client.post("/api/v1/counts", headers=clerk, json={
        "store_id": S1, "sku_id": SKU, "counted_qty": 74, "counted_at": "2026-03-06T06:00:00Z",
        "shift_id": shift(S1, "2026-03-06T06:00:00Z"), "count_type": "SCHEDULED"})
    assert r.status_code == 201, r.text
    out = r.json()
    assert out["outcome"] == "POSTED"
    assert balance(db, S1) == 73  # a count never posts stock

    # 3. Explain the variance: 100 + 20 - 30 - 10 + 2 - 3 = 79, count 74 -> -5.
    rec = client.get(f"/api/v1/reconciliations/{out['recon_id']}", headers=clerk).json()
    assert (rec["opening_qty"], rec["receipts_qty"], rec["sales_qty"], rec["transfer_out_qty"],
            rec["restocked_returns_qty"], rec["adjustments_qty"]) == (100, 20, 30, 10, 2, -3)
    assert rec["expected_qty"] == 79 and rec["variance_qty"] == -5 and rec["basis_status"] == "RELIABLE"
    assert rec["movements"]["total"] == 8  # incl. the non-restocked return (stock effect 0)
    assert sum(m["stock_qty"] for m in rec["movements"]["items"]) == 79 - 100

    # 4. Detection (ops manager) raises an evidence-based shrinkage alert.
    ops = login(client, OPS)
    r = client.post("/api/v1/detection-runs", headers=ops, json={})
    assert r.status_code == 202
    jobs.drain(db)
    alerts = client.get("/api/v1/alerts", headers=ops, params={"alert_class": "SHRINKAGE"}).json()["items"]
    shortage = [a for a in alerts if a["count_id"] == out["count_id"]]
    assert len(shortage) == 1
    a = client.get(f"/api/v1/alerts/{shortage[0]['alert_id']}", headers=ops).json()
    assert a["calculation"]["remaining_shortage_qty"] == 5 and a["threshold_value"] == 5
    assert a["rule"]["rule_code"] == "UNEXPLAINED_SHORTAGE" and a["rule"]["rule_version"] == 1
    assert any(e["evidence_type"] == "RECONCILIATION" for e in a["evidence"])

    # 5. Investigate and resolve. LP assigns themselves, reviews, adds a finding.
    lp = login(client, LP)
    inv = a["investigation_id"]
    ev = lambda who, body: client.post(f"/api/v1/investigations/{inv}/events", headers=who, json=body)  # noqa: E731
    assert ev(lp, {"event_type": "ASSIGNED", "assignee_id": LP}).status_code == 201
    assert ev(lp, {"event_type": "REVIEW_STARTED"}).status_code == 201
    assert ev(lp, {"event_type": "FINDING_ADDED", "note": "CCTV shows the display case left unlocked."}).status_code == 201
    # LP may resolve but cannot approve a write-off.
    r = ev(lp, {"event_type": "RESOLVED", "resolution_code": "CONFIRMED_THEFT", "note": "Theft confirmed on CCTV.",
                "write_off_qty": 5})
    assert r.status_code == 403
    r = ev(ops, {"event_type": "RESOLVED", "resolution_code": "CONFIRMED_THEFT",
                 "note": "Theft confirmed on CCTV; writing off 5 units.", "write_off_qty": 5})
    assert r.status_code == 201, r.text
    wo = r.json()["write_off_movement_id"]
    assert wo
    # The write-off is a separate ledger event; resolving alone changed nothing else.
    m = client.get(f"/api/v1/movements/{wo}", headers=ops).json()
    assert m["reason_code"] == "INVESTIGATION_WRITE_OFF" and m["quantity"] == -5 and m["investigation_id"] == inv
    assert balance(db, S1) == 68
    case = client.get(f"/api/v1/investigations/{inv}", headers=ops).json()
    assert case["status"] == "RESOLVED"
    assert [e["event_type"] for e in case["events"]] == ["OPENED", "ASSIGNED", "REVIEW_STARTED", "FINDING_ADDED", "RESOLVED"]
    last = case["events"][-1]
    assert last["actor_id"] == OPS and last["resolution_code"] == "CONFIRMED_THEFT" and last["occurred_at"]

    # 6. Correct the quarantined adjustment (missing reason) through a linked new submission.
    q = client.get(f"/api/v1/imports/{batch['batch_id']}/rows", headers=admin,
                   params={"error_code": "ADJUSTMENT_REASON_MISSING"}).json()["items"][0]
    r = client.post(f"/api/v1/imports/rows/{q['row_id']}/correction", headers=admin, json={"reason_code": "DAMAGE"})
    assert r.status_code == 201 and r.json()["outcome"] == "POSTED"
    assert r.json()["corrects_row_id"] == q["row_id"]
    with db.read() as conn:
        orig = conn.execute(text("SELECT outcome FROM import_row_result WHERE row_id=:r"), {"r": q["row_id"]}).scalar_one()
    assert orig == "QUARANTINED"  # history is kept
    assert balance(db, S1) == 67


def test_health_and_static(client):
    assert client.get("/api/v1/health").json()["status"] == "ok"
    assert client.get("/").status_code == 200
    assert client.get("/static/js/api.js").status_code == 200
    assert client.get("/api/v1/alerts").status_code == 401
