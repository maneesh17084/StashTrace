"""Reconciliation arithmetic, versioning, timing review, rule boundaries and benign explanations."""
import pytest
from sqlalchemy import text

from backend.app.services import detection
from tests.conftest import (ADMIN, CLERK1, MGR1, MGR2, OPS, S1, S2, SKU, balance, count_row, demo_movements,
                            import_rows, mv)


def run(db):
    q = detection.queue_run(db, OPS, enqueue=False)
    return detection.execute_run(db, q["run_id"])


def alerts(db, **where):
    sql = "SELECT * FROM shrinkage_alert" + (" WHERE " + " AND ".join(f"{k}=:{k}" for k in where) if where else "")
    with db.read() as conn:
        return [dict(r._mapping) for r in conn.execute(text(sql), where)]


def current(db, cid):
    with db.read() as conn:
        return dict(conn.execute(text("SELECT * FROM count_reconciliation WHERE count_id=:c ORDER BY version DESC LIMIT 1"),
                                 {"c": cid}).one()._mapping)


def test_worked_example_and_ledger_rebuild(db, demo_dir):
    import_rows(db, demo_movements(demo_dir))
    import_rows(db, [count_row("CNT-2026-000101", S1, 74, "2026-03-06T06:00:00Z")], kind="COUNTS")
    r = current(db, "CNT-2026-000101")
    assert (r["expected_qty"], r["variance_qty"]) == (79, -5)
    # Rebuild every balance independently from the accepted ledger and compare with the cache.
    with db.read() as conn:
        bad = conn.execute(text(
            "SELECT b.store_id, b.on_hand, s.opening_qty + COALESCE(SUM(m.stock_qty), 0) AS rebuilt FROM stock_balance b"
            " JOIN store_sku s USING (store_id, sku_id) LEFT JOIN inventory_movement m USING (store_id, sku_id)"
            " GROUP BY b.store_id, b.sku_id HAVING b.on_hand <> rebuilt")).all()
    assert bad == []


def test_count_never_posts_stock(db):
    before = balance(db, S1)
    import_rows(db, [count_row("CNT-2026-000102", S1, 60, "2026-03-02T06:00:00Z")], kind="COUNTS")
    assert balance(db, S1) == before
    with db.read() as conn:
        assert conn.execute(text("SELECT COUNT(*) FROM inventory_movement")).scalar_one() == 0


def test_late_movement_creates_new_version_but_post_cutoff_does_not(db):
    import_rows(db, [count_row("CNT-2026-000103", S1, 94, "2026-03-03T06:00:00Z")], kind="COUNTS")
    assert current(db, "CNT-2026-000103")["variance_qty"] == -6
    # Happened after the cutoff: no new version.
    import_rows(db, [mv("SALE", S1, 2, "2026-03-03T10:00:00Z", "EMP-000001")])
    assert current(db, "CNT-2026-000103")["version"] == 1
    # Happened before the cutoff but arrived late: version 2, variance explained.
    import_rows(db, [mv("SALE", S1, 6, "2026-03-02T15:00:00Z", "EMP-000001", received_at="2026-03-04T09:00:00Z")])
    r = current(db, "CNT-2026-000103")
    assert r["version"] == 2 and r["trigger"] == "LATE_MOVEMENT" and r["variance_qty"] == 0
    assert run(db)["alerts_new"] == 0


@pytest.mark.parametrize("shortage,expect_alert", [(4, False), (5, True), (6, True)])
def test_shortage_threshold_boundaries(db, shortage, expect_alert):
    import_rows(db, [count_row("CNT-2026-000110", S1, 100 - shortage, "2026-03-02T06:00:00Z")], kind="COUNTS")
    run(db)
    got = alerts(db, alert_type="UNEXPLAINED_SHORTAGE")
    assert bool(got) is expect_alert
    if got:
        assert got[0]["observed_value"] == shortage and got[0]["threshold_value"] == 5


def test_quarantined_rows_explain_shortage_before_alerting(db):
    # 3 damaged units were physically removed but the adjustment lacked a reason (quarantined).
    import_rows(db, [mv("ADJUSTMENT", S1, -3, "2026-03-02T09:00:00Z", CLERK1, approved_by=MGR1)])
    import_rows(db, [count_row("CNT-2026-000120", S1, 93, "2026-03-03T06:00:00Z")], kind="COUNTS")  # -7
    run(db)
    assert alerts(db) == []  # 7 - 3 explained = 4 < 5
    import_rows(db, [count_row("CNT-2026-000121", S2, 32, "2026-03-03T06:10:00Z", counter="EMP-000021")], kind="COUNTS")
    run(db)
    got = alerts(db, alert_type="UNEXPLAINED_SHORTAGE")
    assert len(got) == 1 and got[0]["store_id"] == S2 and got[0]["observed_value"] == 8


def test_legitimate_damage_and_returns_are_not_flagged(db):
    import_rows(db, [
        mv("SALE", S1, 4, "2026-03-02T10:00:00Z", "EMP-000001"),
        mv("ADJUSTMENT", S1, -3, "2026-03-02T11:00:00Z", CLERK1, reason_code="DAMAGE", approved_by=MGR1),
        mv("RETURN", S1, 2, "2026-03-02T12:00:00Z", "EMP-000012", return_id="RET-2026-000020", disposition="RESTOCK"),
        mv("RETURN", S1, 1, "2026-03-02T12:30:00Z", "EMP-000012", return_id="RET-2026-000021", disposition="DISPOSE"),
    ])
    import_rows(db, [count_row("CNT-2026-000130", S1, 95, "2026-03-03T06:00:00Z")], kind="COUNTS")
    assert current(db, "CNT-2026-000130")["variance_qty"] == 0  # 100 - 4 - 3 + 2 (+0 disposed) = 95
    run(db)
    assert alerts(db) == []


def test_timing_conflict_routes_to_review_not_shrinkage(db):
    import_rows(db, [mv("SALE", S1, 9, "2026-03-03T10:00:00Z", "EMP-000001")])
    # Counted at 06:00 (100 on the shelf) but claims a 20:00 cutoff, after a 9-unit sale and after submission.
    import_rows(db, [count_row("CNT-2026-000140", S1, 100, "2026-03-03T06:00:00Z", cutoff="2026-03-03T20:00:00Z")],
                kind="COUNTS")
    # Submitted before it was counted, with a real 12-unit gap.
    import_rows(db, [count_row("CNT-2026-000141", S2, 28, "2026-03-03T06:00:00Z", counter="EMP-000021",
                               submitted="2026-03-03T05:50:00Z")], kind="COUNTS")
    r = current(db, "CNT-2026-000140")
    assert r["basis_status"] == "TIMING_REVIEW"
    run(db)
    types = sorted(a["alert_type"] for a in alerts(db))
    assert types == ["COUNT_TIMING_REVIEW", "COUNT_TIMING_REVIEW"]  # no shrinkage alert for either count
    assert {a["alert_class"] for a in alerts(db)} == {"DATA_QUALITY"}


def test_transfer_in_transit_is_not_a_loss_at_either_end(db):
    import_rows(db, [mv("TRANSFER_OUT", S1, 10, "2026-03-03T15:00:00Z", CLERK1, transfer_id="TRF-2026-000001")])
    import_rows(db, [count_row("CNT-2026-000150", S1, 90, "2026-03-04T06:00:00Z"),
                     count_row("CNT-2026-000151", S2, 40, "2026-03-04T06:10:00Z", counter="EMP-000021")], kind="COUNTS")
    assert current(db, "CNT-2026-000150")["variance_qty"] == 0
    assert current(db, "CNT-2026-000151")["variance_qty"] == 0
    assert run(db)["alerts_new"] == 0  # in transit, not overdue as of the latest ledger time


def test_transfer_double_count_one_case_both_ends(db):
    import_rows(db, [
        mv("TRANSFER_OUT", S1, 6, "2026-03-06T15:00:00Z", CLERK1, transfer_id="TRF-2026-000002"),
        mv("TRANSFER_IN", S2, 4, "2026-03-07T09:00:00Z", "EMP-000021", transfer_id="TRF-2026-000002"),
        mv("ADJUSTMENT", S1, -2, "2026-03-07T12:00:00Z", CLERK1, reason_code="TRANSFER_SHORTAGE", approved_by=MGR1,
           transfer_id="TRF-2026-000002"),
        mv("ADJUSTMENT", S2, -2, "2026-03-07T12:30:00Z", "EMP-000021", reason_code="TRANSFER_SHORTAGE",
           approved_by=MGR2, transfer_id="TRF-2026-000002"),
    ])
    # Transit write-offs have no store stock effect: both stores' on-hand are unaffected by them.
    assert balance(db, S1) == 94 and balance(db, S2) == 44
    run(db)
    got = alerts(db, transfer_id="TRF-2026-000002")
    assert len(got) == 1 and got[0]["alert_type"] == "TRANSFER_DOUBLE_COUNT"
    assert {got[0]["store_id"], got[0]["related_store_id"]} == {S1, S2}
    with db.read() as conn:
        roles = sorted(r[0] for r in conn.execute(text("SELECT evidence_role FROM alert_evidence WHERE alert_id=:a"),
                                                  {"a": got[0]["alert_id"]}))
    assert roles.count("WRITE_OFF") == 2 and "DISPATCH" in roles and "RECEIPT" in roles
    assert run(db)["alerts_new"] == 0  # stable case key: rerun does not open a second case


def test_transfer_written_off_once_is_benign_but_unresolved_is_overdue(db):
    import_rows(db, [
        mv("TRANSFER_OUT", S1, 10, "2026-03-03T15:00:00Z", CLERK1, transfer_id="TRF-2026-000001"),
        mv("TRANSFER_IN", S2, 8, "2026-03-04T09:00:00Z", "EMP-000021", transfer_id="TRF-2026-000001"),
    ])
    q = detection.queue_run(db, OPS, as_of="2026-03-06T00:00:00Z", enqueue=False)
    assert detection.execute_run(db, q["run_id"])["alerts_new"] == 0  # within grace
    q = detection.queue_run(db, OPS, as_of="2026-03-20T00:00:00Z", enqueue=False)
    detection.execute_run(db, q["run_id"])
    assert [a["alert_type"] for a in alerts(db)] == ["TRANSFER_OVERDUE_DISCREPANCY"]


def test_repeated_negative_adjustments_pattern(db):
    adj = lambda d, q: mv("ADJUSTMENT", S1, -q, f"2026-03-0{d}T12:00:00Z", CLERK1, reason_code="DAMAGE", approved_by=MGR1)  # noqa: E731
    import_rows(db, [adj(2, 2), adj(4, 2)])
    run(db)
    assert alerts(db) == []  # two adjustments: below min_count
    import_rows(db, [adj(6, 3)])
    run(db)
    got = alerts(db, alert_type="REPEATED_NEGATIVE_ADJUSTMENTS")
    assert len(got) == 1 and got[0]["observed_value"] == 7
    with db.read() as conn:
        n = conn.execute(text("SELECT COUNT(*) FROM alert_evidence WHERE alert_id=:a"), {"a": got[0]["alert_id"]}).scalar_one()
    assert n == 3


def test_every_alert_traces_to_rule_run_and_sources(db, demo_dir):
    import_rows(db, demo_movements(demo_dir))
    import_rows(db, [count_row("CNT-2026-000160", S1, 74, "2026-03-06T06:00:00Z")], kind="COUNTS")
    run(db)
    with db.read() as conn:
        orphans = conn.execute(text(
            "SELECT a.alert_id FROM shrinkage_alert a LEFT JOIN rule_definition r ON r.rule_code=a.rule_code"
            " AND r.rule_version=a.rule_version LEFT JOIN detection_run d ON d.run_id=a.first_run_id"
            " WHERE r.rule_code IS NULL OR d.run_id IS NULL OR NOT EXISTS"
            " (SELECT 1 FROM alert_evidence e WHERE e.alert_id=a.alert_id)")).all()
        assert orphans == []
        assert conn.execute(text("PRAGMA foreign_key_check")).all() == []
