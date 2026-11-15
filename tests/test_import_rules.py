"""Import validation, idempotency and integrity: no quarantined row may affect stock."""
import pytest
from sqlalchemy import text

from backend.app.services import posting
from tests.conftest import (ADMIN, CLERK1, MGR1, MGR2, S1, S2, SKU, balance, import_rows, mv, pos)

T = "2026-03-02T10:00:00Z"


def outcome(res, i=0):
    r = res["rows"][i]
    return r["outcome"], r["error_code"]


def test_wrong_store_is_quarantined_even_if_store_exists(db):
    before = balance(db, S2)
    res = import_rows(db, [mv("SALE", S2, 2, T, "EMP-000001", source_doc_id=pos(S1, T))])
    assert outcome(res) == ("QUARANTINED", "WRONG_STORE_REFERENCE")
    assert res["rows"][0]["authoritative_store_id"] == S1
    assert balance(db, S2) == before and balance(db, S1) == 100


def test_transfer_leg_wrong_store(db):
    res = import_rows(db, [mv("TRANSFER_OUT", S2, 3, "2026-03-03T15:00:00Z", "EMP-000021", transfer_id="TRF-2026-000001")])
    assert outcome(res) == ("QUARANTINED", "WRONG_STORE_REFERENCE")


def test_adjustment_without_reason_then_corrected(db):
    row = mv("ADJUSTMENT", S1, -2, T, CLERK1, approved_by=MGR1)
    res = import_rows(db, [row])
    assert outcome(res) == ("QUARANTINED", "ADJUSTMENT_REASON_MISSING")
    assert balance(db, S1) == 100
    fixed = dict(row, reason_code="DAMAGE", received_at="2026-03-03T09:00:00Z")
    res2 = import_rows(db, [fixed])
    assert outcome(res2) == ("POSTED", None)
    assert res2["rows"][0]["corrects_row_id"] == res["rows"][0]["row_id"]
    assert balance(db, S1) == 98
    # An invalid reason in a correction is rejected too.
    bad = mv("ADJUSTMENT", S1, -1, T, CLERK1, approved_by=MGR1, reason_code="LOST_IT")
    assert outcome(import_rows(db, [bad])) == ("QUARANTINED", "INVALID_REASON_CODE")


def test_idempotent_replay_and_conflict(db):
    rows = [mv("SALE", S1, 2, T, "EMP-000001"), mv("RECEIPT", S1, 5, "2026-03-02T08:00:00Z", CLERK1)]
    first = import_rows(db, rows)
    assert first["posted"] == 2
    after = balance(db, S1)
    replay = import_rows(db, rows)                  # identical retry of the whole batch
    assert replay["duplicate"] == 2 and replay["posted"] == 0
    assert [r["movement_id"] for r in replay["rows"]] == [r["movement_id"] for r in first["rows"]]
    late_retry = import_rows(db, [dict(rows[0], received_at="2026-03-05T00:00:00Z")])  # delivery time differs only
    assert outcome(late_retry) == ("DUPLICATE", None)
    conflict = import_rows(db, [dict(rows[0], quantity=3)])
    assert outcome(conflict) == ("CONFLICT", "CONFLICT_PAYLOAD")
    assert balance(db, S1) == after
    with db.read() as conn:
        assert conn.execute(text("SELECT COUNT(*) FROM inventory_movement")).scalar_one() == 2


def test_role_assignment_outside_effective_dates(db):
    ok = import_rows(db, [mv("RECEIPT", S1, 5, "2026-03-02T09:00:00Z", "EMP-000013")])   # before the end date
    assert outcome(ok) == ("POSTED", None)
    late = import_rows(db, [mv("RECEIPT", S1, 5, "2026-03-04T09:00:00Z", "EMP-000013")])  # after it
    assert outcome(late) == ("QUARANTINED", "ACTOR_NOT_AUTHORIZED")
    wrong_role = import_rows(db, [mv("RECEIPT", S1, 5, T, "EMP-000012")])  # cashier cannot receive stock
    assert outcome(wrong_role) == ("QUARANTINED", "ACTOR_NOT_AUTHORIZED")
    other_store = import_rows(db, [mv("RECEIPT", S1, 5, T, "EMP-000021")])  # store-2 clerk at store 1
    assert outcome(other_store) == ("QUARANTINED", "ACTOR_NOT_AUTHORIZED")


@pytest.mark.parametrize("override,code", [
    ({"store_id": "STORE-000999"}, "UNKNOWN_STORE"),
    ({"sku_id": "SKU-999999"}, "UNKNOWN_SKU"),
    ({"store_id": "STORE-1"}, "INVALID_FORMAT"),
    ({"movement_type": "SHRINK"}, "INVALID_MOVEMENT_TYPE"),
    ({"quantity": "0"}, "INVALID_QUANTITY"),
    ({"quantity": "1.5"}, "INVALID_QUANTITY"),
    ({"received_at": "2026-03-01T00:00:00Z"}, "INVALID_TIMESTAMP"),
    ({"occurred_at": "2026-02-27T10:00:00Z", "received_at": "2026-02-27T10:00:00Z", "shift_id": None}, "MISSING_FIELD"),
    ({"source_doc_id": "POS-2026-9999999"}, "UNKNOWN_SOURCE_REF"),
    ({"shift_id": "SHF-2026-0000002"}, "SHIFT_MISMATCH"),
    ({"actor_id": "EMP-999999"}, "UNKNOWN_ACTOR"),
])
def test_rejections_do_not_touch_stock(db, override, code):
    row = mv("SALE", S1, 2, T, "EMP-000001")
    row.update(override)
    res = import_rows(db, [row])
    assert outcome(res) == ("QUARANTINED", code)
    assert balance(db, S1) == 100
    with db.read() as conn:
        assert conn.execute(text("SELECT COUNT(*) FROM inventory_movement")).scalar_one() == 0


def test_not_assorted_before_opening(db):
    row = mv("SALE", S1, 1, "2026-02-27T10:00:00Z", "EMP-000001", shift_id="SHF-2026-0000001",
             source_doc_id="POS-2026-0000001")
    assert outcome(import_rows(db, [row]))[1] in ("SKU_NOT_ASSORTED", "SHIFT_MISMATCH")
    errs = import_rows(db, [dict(row, source_event_id="X-NA")])["rows"][0]["errors_json"]
    assert "SKU_NOT_ASSORTED" in errs


def test_self_approval_and_unapproved(db):
    assert outcome(import_rows(db, [mv("ADJUSTMENT", S1, -1, T, MGR1, reason_code="DAMAGE", approved_by=MGR1)])) == \
        ("QUARANTINED", "SELF_APPROVAL")
    assert outcome(import_rows(db, [mv("ADJUSTMENT", S1, -1, T, CLERK1, reason_code="DAMAGE")])) == \
        ("QUARANTINED", "ADJUSTMENT_NOT_APPROVED")
    assert outcome(import_rows(db, [mv("ADJUSTMENT", S1, -1, T, CLERK1, reason_code="DAMAGE", approved_by=MGR2)])) == \
        ("QUARANTINED", "ADJUSTMENT_NOT_APPROVED")  # other store's manager
    assert outcome(import_rows(db, [mv("ADJUSTMENT", S1, 2, T, CLERK1, reason_code="DAMAGE", approved_by=MGR1)])) == \
        ("QUARANTINED", "INVALID_QUANTITY")  # damage must be negative


def test_returns_restock_only_when_disposition_permits(db):
    sale = import_rows(db, [mv("SALE", S1, 3, T, "EMP-000001", movement_id="MOV-2026-00000500")])
    assert outcome(sale)[0] == "POSTED"
    r1 = import_rows(db, [mv("RETURN", S1, 1, "2026-03-03T11:00:00Z", "EMP-000012", return_id="RET-2026-000010",
                             disposition="RESTOCK", original_sale_movement_id="MOV-2026-00000500")])
    r2 = import_rows(db, [mv("RETURN", S1, 1, "2026-03-03T12:00:00Z", "EMP-000012", return_id="RET-2026-000011",
                             disposition="DAMAGED")])
    assert outcome(r1)[0] == outcome(r2)[0] == "POSTED"
    assert balance(db, S1) == 100 - 3 + 1
    too_many = import_rows(db, [mv("RETURN", S1, 5, "2026-03-03T12:30:00Z", "EMP-000012", return_id="RET-2026-000012",
                                   disposition="RESTOCK", original_sale_movement_id="MOV-2026-00000500")])
    assert outcome(too_many) == ("QUARANTINED", "RETURN_SALE_MISMATCH")
    no_disp = import_rows(db, [mv("RETURN", S1, 1, "2026-03-03T12:40:00Z", "EMP-000012", return_id="RET-2026-000013")])
    assert outcome(no_disp) == ("QUARANTINED", "RETURN_DISPOSITION_INVALID")


def test_reversal_and_replacement_are_linked_not_edited(db):
    res = import_rows(db, [mv("SALE", S1, 5, T, "EMP-000001")])
    mid = res["rows"][0]["movement_id"]
    out = posting.reverse_movement(db, mid, "Cashier scanned 5 instead of 2", MGR1,
                                   mv("SALE", S1, 2, T, "EMP-000001"))
    assert balance(db, S1) == 98
    with db.read() as conn:
        orig = conn.execute(text("SELECT quantity FROM inventory_movement WHERE movement_id=:m"), {"m": mid}).scalar_one()
        assert orig == 5  # original entry untouched
        with pytest.raises(Exception):
            conn.execute(text("UPDATE inventory_movement SET quantity=2 WHERE movement_id=:m"), {"m": mid})
    with pytest.raises(Exception):
        posting.reverse_movement(db, mid, "second reversal attempt", MGR1, None)
    assert out["reversal_movement_id"] and out["replacement_movement_id"]


def test_store_scoped_uploader_cannot_import_other_store(db):
    res = import_rows(db, [mv("SALE", S1, 1, T, "EMP-000001")], submitter=MGR2)
    assert outcome(res) == ("QUARANTINED", "ACTOR_NOT_AUTHORIZED")


def test_header_only_upload_is_a_clear_422(client, db):
    from tests.conftest import login
    body = b"source_system,source_event_id,movement_type,store_id,sku_id,quantity,occurred_at,actor_id,shift_id\n"
    r = client.post("/api/v1/imports", headers=login(client, ADMIN), data={"batch_kind": "MOVEMENTS"},
                    files={"file": ("empty.csv", body, "text/csv")})
    assert r.status_code == 422 and r.json()["error"]["code"] == "EMPTY_FILE"


def test_api_payload_validation(client, db):
    from tests.conftest import login
    h = login(client, ADMIN)
    bad = {"source_event_id": "X1", "movement_type": "SALE", "store_id": S1, "sku_id": SKU, "quantity": 0,
           "occurred_at": T, "actor_id": "EMP-000001", "shift_id": "SHF-2026-0000003"}
    r = client.post("/api/v1/movements", headers=h, json=bad)
    assert r.status_code == 422 and r.json()["error"]["code"] == "INVALID_REQUEST"
    r = client.post("/api/v1/movements", headers=h, json={**bad, "quantity": 1, "store_id": "S1"})
    assert r.status_code == 422
    r = client.post("/api/v1/movements", headers=h, json={**bad, "quantity": 1, "extra": "x"})
    assert r.status_code == 422
