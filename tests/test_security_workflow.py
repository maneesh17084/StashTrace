"""Access control, case lifecycle rules, append-only history, concurrency and mid-transaction failure."""
import threading

import pytest
from sqlalchemy import text

from backend.app.services import detection, posting
from backend.app.services import reconciliation as recon_mod
from tests.conftest import (ADMIN, ANALYST, CLERK1, LP, MGR1, MGR2, OPS, S1, S2, SKU, balance, count_row,
                            demo_movements, import_rows, login, mv, shift)


@pytest.fixture
def seeded(db, demo_dir):
    import_rows(db, demo_movements(demo_dir))
    import_rows(db, [count_row("CNT-2026-000201", S1, 74, "2026-03-06T06:00:00Z")], kind="COUNTS")
    q = detection.queue_run(db, OPS, enqueue=False)
    detection.execute_run(db, q["run_id"])
    with db.read() as conn:
        a = conn.execute(text("SELECT alert_id, investigation_id, recon_id FROM shrinkage_alert"
                              " WHERE alert_type='UNEXPLAINED_SHORTAGE'")).one()
    return dict(a._mapping)


def test_operator_cannot_read_other_store_by_id(client, seeded):
    h2 = login(client, MGR2)  # STORE-000002 manager
    assert client.get(f"/api/v1/reconciliations/{seeded['recon_id']}", headers=h2).status_code == 404
    assert client.get(f"/api/v1/alerts/{seeded['alert_id']}", headers=h2).status_code == 404
    assert client.get(f"/api/v1/investigations/{seeded['investigation_id']}", headers=h2).status_code == 404
    assert client.get("/api/v1/reconciliations", headers=h2, params={"store_id": S1}).status_code == 403
    assert client.get("/api/v1/movements", headers=h2, params={"store_id": S1}).status_code == 403
    assert client.get("/api/v1/reconciliations/export.csv", headers=h2, params={"store_id": S1}).status_code == 403
    own = client.get("/api/v1/alerts", headers=h2).json()["items"]
    assert all(S2 in (a["store_id"], a["related_store_id"]) for a in own)
    dash = client.get("/api/v1/dashboard/summary", headers=h2).json()
    assert [s["store_id"] for s in dash["stores"]] == [S2]
    # Own store works.
    assert client.get("/api/v1/reconciliations/export.csv", headers=h2, params={"store_id": S2}).status_code == 200


def test_dashboard_all_stores_for_ops(client, seeded):
    d = client.get("/api/v1/dashboard/summary", headers=login(client, OPS)).json()
    s1 = next(s for s in d["stores"] if s["store_id"] == S1)
    assert s1["counts"] == 1 and s1["timing_reviews"] == 0 and s1["open_shrinkage"] == 1
    assert s1["unexplained_units"] == 5 and s1["quarantined_rows"] >= 1
    assert d["totals"]["open_cases"] >= 2


def test_operator_cannot_change_other_store(client, seeded):
    h2 = login(client, MGR2)
    r = client.post("/api/v1/counts", headers=h2, json={"store_id": S1, "sku_id": SKU, "counted_qty": 1,
                                                       "counted_at": "2026-03-06T06:00:00Z",
                                                       "shift_id": shift(S1, "2026-03-06T06:00:00Z")})
    assert r.status_code == 403
    r = client.post(f"/api/v1/investigations/{seeded['investigation_id']}/events", headers=h2,
                    json={"event_type": "ASSIGNED", "assignee_id": MGR2})
    assert r.status_code == 404


def test_unauthorized_roles_cannot_assign_resolve_or_approve(client, seeded):
    inv = seeded["investigation_id"]
    post = lambda who, body: client.post(f"/api/v1/investigations/{inv}/events", headers=login(client, who), json=body)  # noqa: E731
    assert post(CLERK1, {"event_type": "ASSIGNED", "assignee_id": LP}).status_code == 403
    assert post(ANALYST, {"event_type": "ASSIGNED", "assignee_id": LP}).status_code == 403
    assert post(OPS, {"event_type": "ASSIGNED", "assignee_id": CLERK1}).json()["error"]["code"] == "INVALID_ASSIGNEE"
    assert post(OPS, {"event_type": "RESOLVED", "resolution_code": "NO_ACTION", "note": "nothing to see here"}).status_code == 409
    assert post(OPS, {"event_type": "ASSIGNED", "assignee_id": LP}).status_code == 201
    assert post(LP, {"event_type": "REVIEW_STARTED"}).status_code == 201
    assert post(MGR1, {"event_type": "RESOLVED", "resolution_code": "NO_ACTION", "note": "store manager tries"}).status_code == 403
    assert post(LP, {"event_type": "RESOLVED", "resolution_code": "NO_ACTION", "note": "short"}).status_code == 422
    assert post(LP, {"event_type": "RESOLVED", "resolution_code": "BOGUS", "note": "long enough reason"}).status_code == 422
    stale = post(LP, {"event_type": "FINDING_ADDED", "note": "late finding", "expected_seq": 1})
    assert stale.status_code == 409 and stale.json()["error"]["code"] == "STALE_CASE"
    ok = post(LP, {"event_type": "RESOLVED", "resolution_code": "COUNT_ERROR", "note": "Recount found the 5 units."})
    assert ok.status_code == 201 and ok.json()["write_off_movement_id"] is None
    assert post(OPS, {"event_type": "FINDING_ADDED", "note": "after resolution"}).status_code == 409
    # Detection cannot be triggered by a store-scoped user.
    assert client.post("/api/v1/detection-runs", headers=login(client, MGR1), json={}).status_code == 403


def test_start_review_permission_matches_enforcement(client, seeded):
    inv = seeded["investigation_id"]
    url = f"/api/v1/investigations/{inv}"

    def flag(who):
        return client.get(url, headers=login(client, who)).json()["can"]["start_review"]

    # OPEN case: nobody can start review yet, however senior.
    assert [flag(e) for e in (OPS, LP, ANALYST, MGR1)] == [False, False, False, False]
    assert client.post(f"{url}/events", headers=login(client, OPS),
                       json={"event_type": "ASSIGNED", "assignee_id": LP}).status_code == 201
    # ASSIGNED to LP: the flag must agree with what the server does for every user.
    for who, expected in ((ANALYST, False), (MGR1, False), (LP, True), (OPS, True)):
        assert flag(who) is expected, who
    for who in (ANALYST, MGR1):  # flag False -> the action is refused, case unchanged
        r = client.post(f"{url}/events", headers=login(client, who), json={"event_type": "REVIEW_STARTED"})
        assert r.status_code == 403, (who, r.text)
    r = client.post(f"{url}/events", headers=login(client, LP), json={"event_type": "REVIEW_STARTED"})
    assert r.status_code == 201
    assert flag(LP) is False  # IN_REVIEW: no longer available


def test_history_is_append_only(db, seeded):
    with db.write() as conn:
        with pytest.raises(Exception, match="append-only"):
            conn.execute(text("UPDATE investigation_event SET note='x'"))
    with db.write() as conn:
        with pytest.raises(Exception, match="append-only"):
            conn.execute(text("DELETE FROM count_reconciliation"))
    with db.write() as conn:
        with pytest.raises(Exception, match="append-only"):
            conn.execute(text("DELETE FROM import_row_result"))


def test_concurrent_postings_same_store_sku(db):
    errors = []

    def worker(i):
        try:
            import_rows(db, [mv("SALE", S1, 1, f"2026-03-02T1{i % 10}:0{i // 10}:00Z", "EMP-000001")])
        except Exception as exc:  # pragma: no cover - surfaced below
            errors.append(exc)

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(20)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert errors == []
    assert balance(db, S1) == 80
    with db.read() as conn:
        assert conn.execute(text("SELECT COUNT(*) FROM inventory_movement")).scalar_one() == 20


def test_failure_mid_transaction_leaves_no_partial_effects_and_retry_posts_once(db, monkeypatch):
    rows = [mv("SALE", S1, 2, "2026-03-02T10:00:00Z", "EMP-000001"), mv("SALE", S1, 3, "2026-03-02T11:00:00Z", "EMP-000001")]
    from tests.conftest import csv_bytes
    b = posting.receive_batch(db, "MOVEMENTS", "x.csv", csv_bytes(posting.MOVEMENT_FIELDS, rows), ADMIN, enqueue=False)

    def boom(*a, **k):
        raise RuntimeError("disk on fire")

    monkeypatch.setattr(recon_mod, "reversion_for_late_movements", boom)
    with pytest.raises(RuntimeError):
        posting.process_batch(db, b["batch_id"])
    with db.read() as conn:
        assert conn.execute(text("SELECT COUNT(*) FROM inventory_movement")).scalar_one() == 0
        assert conn.execute(text("SELECT COUNT(*) FROM import_row_result")).scalar_one() == 0
        assert conn.execute(text("SELECT COUNT(*) FROM ingest_key")).scalar_one() == 0
    assert balance(db, S1) == 100
    monkeypatch.undo()
    res = posting.process_batch(db, b["batch_id"])
    assert res["posted"] == 2
    assert posting.process_batch(db, b["batch_id"])["processed"] == 0  # nothing left to do
    assert balance(db, S1) == 95


def test_detection_rerun_is_idempotent(db, seeded):
    with db.read() as conn:
        n = conn.execute(text("SELECT COUNT(*) FROM investigation")).scalar_one()
    q = detection.queue_run(db, OPS, enqueue=False)
    out = detection.execute_run(db, q["run_id"])
    assert out["alerts_new"] == 0 and out["alerts_redetected"] == n
    with db.read() as conn:
        assert conn.execute(text("SELECT COUNT(*) FROM investigation")).scalar_one() == n
