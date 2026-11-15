"""Hand-authored demo: two stores, one SKU, the full flow plus every negative scenario.

    python -m data_tools.demo_seed --out data/demo          # write CSVs only
    python -m data_tools.load --data data/demo --db var/demo.db --fresh

Worked example (STORE-000001 / SKU-000001, count CNT-2026-000001 at 2026-03-06T06:00:00Z):
    opening 100 + receipt 20 - sales 30 - dispatch 10 + restocked return 2 - approved damage 3 = 79
    physical count 74  ->  variance -5  ->  meets the 5-unit threshold -> UNEXPLAINED_SHORTAGE alert
The DAMAGED (non-restock) return of 1 unit has no stock effect; the damage write-off is not deducted twice.
"""
from __future__ import annotations

import argparse
import json
from datetime import date, timedelta
from pathlib import Path

from backend.app.services.posting import COUNT_FIELDS, MOVEMENT_FIELDS
from data_tools.masters import MASTER_FILES, write_csv

S1, S2, SKU = "STORE-000001", "STORE-000002", "SKU-000001"
DAYS = [date(2026, 3, 1) + timedelta(days=i) for i in range(10)]
DEMO_LOGINS = ["EMP-000004", "EMP-000005", "EMP-000006", "EMP-000007", "EMP-000010", "EMP-000011", "EMP-000012",
               "EMP-000020", "EMP-000021"]


def shift_id(store_idx: int, day_idx: int, kind: int) -> str:
    return f"SHF-2026-{(day_idx * 2 + store_idx) * 2 + kind + 1:07d}"


def sh(store: str, ts: str) -> str:
    d = DAYS.index(date.fromisoformat(ts[:10]))
    hour = int(ts[11:13])
    return shift_id(0 if store == S1 else 1, d, 0 if hour < 13 else 1)


def pos(store: str, ts: str) -> str:
    d = DAYS.index(date.fromisoformat(ts[:10]))
    return f"POS-2026-{d * 2 + (0 if store == S1 else 1) + 1:07d}"


def build() -> dict[str, list[dict]]:
    out: dict[str, list[dict]] = {}
    out["stores.csv"] = [
        {"store_id": S1, "store_name": "Demo Riverside", "store_type": "STORE", "region_code": "R01",
         "timezone": "UTC", "opened_on": "2020-05-01", "status": "ACTIVE"},
        {"store_id": S2, "store_name": "Demo Hillcrest", "store_type": "STORE", "region_code": "R01",
         "timezone": "UTC", "opened_on": "2021-09-15", "status": "ACTIVE"},
    ]
    out["skus.csv"] = [{"sku_id": SKU, "sku_name": "Wireless earbuds, black", "category": "ELECTRONICS",
                        "brand": "Nordvale", "uom": "EA", "unit_cost_cents": 2450, "unit_price_cents": 4999,
                        "shrink_risk": "HIGH", "status": "ACTIVE", "introduced_on": "2025-11-01"}]
    out["store_skus.csv"] = [
        {"store_id": S1, "sku_id": SKU, "active_from": "2026-03-01T00:00:00Z", "opening_qty": 100,
         "opening_as_of": "2026-03-01T00:00:00Z", "replenishment_source": "VENDOR"},
        {"store_id": S2, "sku_id": SKU, "active_from": "2026-03-01T00:00:00Z", "opening_qty": 40,
         "opening_as_of": "2026-03-01T00:00:00Z", "replenishment_source": "VENDOR"},
    ]
    emps = [
        ("EMP-000001", "POS feed", "SERVICE", None, "SERVICE_FEED", None),
        ("EMP-000002", "WMS feed", "SERVICE", None, "SERVICE_FEED", None),
        ("EMP-000003", "StashTrace detection", "SERVICE", None, "SYSTEM", None),
        ("EMP-000004", "Ada Admin", "HUMAN", None, "ADMIN", None),
        ("EMP-000005", "Omar Ops", "HUMAN", None, "OPS_MANAGER", None),
        ("EMP-000006", "Ana Analyst", "HUMAN", None, "ANALYST", None),
        ("EMP-000007", "Lee Prevention", "HUMAN", None, "LOSS_PREVENTION", "R01"),
        ("EMP-000010", "Morgan Manager", "HUMAN", S1, "STORE_MANAGER", S1),
        ("EMP-000011", "Casey Clerk", "HUMAN", S1, "STOCK_CLERK", S1),
        ("EMP-000012", "Riley Cashier", "HUMAN", S1, "CASHIER", S1),
        ("EMP-000013", "Former Clerk", "HUMAN", S1, "STOCK_CLERK", S1),
        ("EMP-000020", "Jordan Manager", "HUMAN", S2, "STORE_MANAGER", S2),
        ("EMP-000021", "Taylor Clerk", "HUMAN", S2, "STOCK_CLERK", S2),
    ]
    out["employees.csv"], out["role_assignments.csv"] = [], []
    ra = 0
    for eid, name, etype, home, role, scope in emps:
        term = "2026-03-03" if eid == "EMP-000013" else None
        out["employees.csv"].append({"employee_id": eid, "display_name": name, "employee_type": etype,
                                     "home_store_id": home, "hired_on": "2025-01-06", "terminated_on": term,
                                     "status": "TERMINATED" if term else "ACTIVE"})
        stores = [S1, S2] if scope == "R01" else ([scope] if scope else [None])
        for st in stores:
            ra += 1
            out["role_assignments.csv"].append({
                "assignment_id": f"RA-{ra:06d}", "employee_id": eid, "role_code": role, "store_id": st,
                "effective_from": "2025-01-06T00:00:00Z", "effective_to": "2026-03-03T00:00:00Z" if term else None})
    out["shifts.csv"], out["source_documents.csv"] = [], []
    for d_idx, d in enumerate(DAYS):
        for s_idx, st in enumerate([S1, S2]):
            for kind, (a, b, name) in enumerate([("05:00", "13:00", "OPEN"), ("13:00", "21:00", "CLOSE")]):
                out["shifts.csv"].append({"shift_id": shift_id(s_idx, d_idx, kind), "store_id": st,
                                          "shift_date": d.isoformat(), "shift_type": name,
                                          "start_at": f"{d}T{a}:00Z", "end_at": f"{d}T{b}:00Z"})
            out["source_documents.csv"].append({"doc_id": f"POS-2026-{d_idx * 2 + s_idx + 1:07d}",
                                                "doc_type": "POS_BATCH", "store_id": st, "business_date": d.isoformat()})
    out["source_documents.csv"].append({"doc_id": "PO-2026-000001", "doc_type": "PURCHASE_ORDER", "store_id": S1,
                                        "business_date": "2026-03-02"})
    out["promotions.csv"] = [{"promotion_id": "PRM-2026-000001", "promo_name": "Spring audio event",
                              "promo_type": "PERCENT_OFF", "discount_pct": 15, "start_date": "2026-03-02",
                              "end_date": "2026-03-05"}]
    out["promotion_scopes.csv"] = [{"promotion_id": "PRM-2026-000001", "store_id": S1, "sku_id": SKU}]
    out["transfers.csv"] = [
        {"transfer_id": "TRF-2026-000001", "from_store_id": S1, "to_store_id": S2,
         "created_at": "2026-03-03T12:00:00Z", "expected_arrival_at": "2026-03-04T12:00:00Z"},
        {"transfer_id": "TRF-2026-000002", "from_store_id": S1, "to_store_id": S2,
         "created_at": "2026-03-06T12:00:00Z", "expected_arrival_at": "2026-03-08T12:00:00Z"},
    ]
    out["transfer_lines.csv"] = [{"transfer_id": "TRF-2026-000001", "sku_id": SKU, "planned_qty": 10},
                                 {"transfer_id": "TRF-2026-000002", "sku_id": SKU, "planned_qty": 6}]

    mv: list[dict] = []

    def add(n, sys, mtype, store, qty, ts, actor, received=None, **kw):
        row = {"source_system": sys, "source_event_id": f"DEMO-{n:04d}", "movement_id": f"MOV-2026-{n:08d}",
               "movement_type": mtype, "store_id": store, "sku_id": SKU, "quantity": qty, "occurred_at": ts,
               "received_at": received or ts, "actor_id": actor, "shift_id": sh(kw.pop("shift_store", store), ts)}
        row.update(kw)
        mv.append(row)
        return row

    # --- the worked example at STORE-000001 -------------------------------------------------
    add(1, "WMS", "RECEIPT", S1, 20, "2026-03-02T08:00:00Z", "EMP-000011", source_doc_id="PO-2026-000001")
    add(2, "POS", "SALE", S1, 12, "2026-03-02T10:15:00Z", "EMP-000001", source_doc_id=pos(S1, "2026-03-02"))
    add(3, "POS", "SALE", S1, 10, "2026-03-03T14:30:00Z", "EMP-000001", source_doc_id=pos(S1, "2026-03-03"))
    add(4, "WMS", "TRANSFER_OUT", S1, 10, "2026-03-03T15:00:00Z", "EMP-000011", transfer_id="TRF-2026-000001")
    add(5, "POS", "RETURN", S1, 2, "2026-03-04T11:00:00Z", "EMP-000012", source_doc_id=pos(S1, "2026-03-04"),
        return_id="RET-2026-000001", original_sale_movement_id="MOV-2026-00000002", disposition="RESTOCK",
        return_reason="CHANGED_MIND")
    add(6, "POS", "RETURN", S1, 1, "2026-03-04T12:00:00Z", "EMP-000012", source_doc_id=pos(S1, "2026-03-04"),
        return_id="RET-2026-000002", disposition="DAMAGED", return_reason="DEFECTIVE")
    add(7, "POS", "SALE", S1, 8, "2026-03-04T16:00:00Z", "EMP-000001", source_doc_id=pos(S1, "2026-03-04"))
    add(8, "STORE_OPS", "ADJUSTMENT", S1, -3, "2026-03-05T09:00:00Z", "EMP-000011", reason_code="DAMAGE",
        approved_by="EMP-000010")
    # --- STORE-000002 -----------------------------------------------------------------------
    add(9, "WMS", "TRANSFER_IN", S2, 10, "2026-03-04T09:00:00Z", "EMP-000021", transfer_id="TRF-2026-000001")
    add(10, "POS", "SALE", S2, 5, "2026-03-05T15:00:00Z", "EMP-000001", source_doc_id=pos(S2, "2026-03-05"))
    add(11, "POS", "SALE", S2, 2, "2026-03-06T10:00:00Z", "EMP-000001", source_doc_id=pos(S2, "2026-03-06"))
    add(12, "POS", "SALE", S2, 3, "2026-03-07T11:00:00Z", "EMP-000001", source_doc_id=pos(S2, "2026-03-07"))
    # --- transfer counted as shrinkage at both ends (after the worked-example cutoff) -------
    add(13, "WMS", "TRANSFER_OUT", S1, 6, "2026-03-06T15:00:00Z", "EMP-000011", transfer_id="TRF-2026-000002")
    add(14, "WMS", "TRANSFER_IN", S2, 4, "2026-03-07T09:00:00Z", "EMP-000021", transfer_id="TRF-2026-000002")
    add(15, "STORE_OPS", "ADJUSTMENT", S1, -2, "2026-03-07T12:00:00Z", "EMP-000011", reason_code="TRANSFER_SHORTAGE",
        approved_by="EMP-000010", transfer_id="TRF-2026-000002")
    add(16, "STORE_OPS", "ADJUSTMENT", S2, -2, "2026-03-07T12:30:00Z", "EMP-000021", reason_code="TRANSFER_SHORTAGE",
        approved_by="EMP-000020", transfer_id="TRF-2026-000002")
    # --- negative scenarios (all after the cutoff so the worked example stays exact) ---------
    add(17, "POS", "SALE", S2, 2, "2026-03-06T12:00:00Z", "EMP-000001", source_doc_id=pos(S1, "2026-03-06"),
        shift_store=S2)                                   # wrong store: POS batch belongs to STORE-000001
    add(18, "STORE_OPS", "ADJUSTMENT", S1, -1, "2026-03-06T13:30:00Z", "EMP-000011",
        approved_by="EMP-000010")                         # adjustment lacks a reason
    add(19, "WMS", "RECEIPT", S1, 5, "2026-03-06T10:00:00Z", "EMP-000013",
        source_doc_id="PO-2026-000001")                   # actor's role assignment ended 2026-03-03
    dup = dict(mv[1])
    dup["received_at"] = "2026-03-02T18:00:00Z"
    mv.append(dup)                                        # feed retry of DEMO-0002 -> DUPLICATE
    conflict = dict(mv[2])
    conflict["quantity"] = 11
    conflict["received_at"] = "2026-03-03T19:00:00Z"
    mv.append(conflict)                                   # same event id, different qty -> CONFLICT
    add(20, "STORE_OPS", "ADJUSTMENT", S1, -1, "2026-03-06T14:00:00Z", "EMP-000010", reason_code="DAMAGE",
        approved_by="EMP-000010")                         # manager approving their own adjustment
    mv.sort(key=lambda r: (r["received_at"], r["source_event_id"]))
    out["movements_input.csv"] = mv

    out["counts_input.csv"] = [
        {"source_system": "STORE_OPS", "count_id": "CNT-2026-000001", "store_id": S1, "sku_id": SKU,
         "counted_qty": 74, "counted_at": "2026-03-06T06:00:00Z", "count_cutoff_at": "2026-03-06T06:00:00Z",
         "submitted_at": "2026-03-06T06:20:00Z", "counted_by": "EMP-000011", "shift_id": sh(S1, "2026-03-06T06:00:00Z"),
         "count_type": "SCHEDULED"},
        {"source_system": "STORE_OPS", "count_id": "CNT-2026-000002", "store_id": S2, "sku_id": SKU,
         "counted_qty": 45, "counted_at": "2026-03-06T06:10:00Z", "count_cutoff_at": "2026-03-06T06:10:00Z",
         "submitted_at": "2026-03-06T06:30:00Z", "counted_by": "EMP-000021", "shift_id": sh(S2, "2026-03-06T06:10:00Z"),
         "count_type": "SCHEDULED"},
        {"source_system": "STORE_OPS", "count_id": "CNT-2026-000003", "store_id": S2, "sku_id": SKU,
         "counted_qty": 43, "counted_at": "2026-03-07T06:00:00Z", "count_cutoff_at": "2026-03-07T20:00:00Z",
         "submitted_at": "2026-03-07T06:15:00Z", "counted_by": "EMP-000021", "shift_id": sh(S2, "2026-03-07T06:00:00Z"),
         "count_type": "AD_HOC"},                         # cutoff contradicts the count time -> timing review
    ]
    return out


EXPECTED = {
    "CNT-2026-000001": {"expected_qty": 79, "variance_qty": -5, "basis_status": "RELIABLE", "alert": "UNEXPLAINED_SHORTAGE"},
    "CNT-2026-000002": {"expected_qty": 45, "variance_qty": 0, "basis_status": "RELIABLE", "alert": None},
    "CNT-2026-000003": {"basis_status": "TIMING_REVIEW", "alert": "COUNT_TIMING_REVIEW"},
    "quarantine": {"DEMO-0017": "WRONG_STORE_REFERENCE", "DEMO-0018": "ADJUSTMENT_REASON_MISSING",
                   "DEMO-0019": "ACTOR_NOT_AUTHORIZED", "DEMO-0020": "SELF_APPROVAL"},
    "duplicate": ["DEMO-0002"], "conflict": ["DEMO-0003"],
    "transfer_alert": {"TRF-2026-000002": "TRANSFER_DOUBLE_COUNT"},
    "balance_after_all": {S1: 100 + 20 - 30 - 10 + 2 - 3 - 6, S2: 40 + 10 - 5 - 2 - 3 + 4},
}


def write(out_dir: Path) -> None:
    data = build()
    cols = {f: c for f, _, c in MASTER_FILES}
    cols["movements_input.csv"] = MOVEMENT_FIELDS
    cols["counts_input.csv"] = COUNT_FIELDS
    for fname, rows in data.items():
        write_csv(out_dir / fname, cols[fname], rows)
    (out_dir / "manifest.json").write_text(json.dumps({"profile": "demo", "demo_logins": DEMO_LOGINS,
                                                       "expected": EXPECTED}, indent=2), encoding="utf-8")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default="data/demo")
    write(Path(ap.parse_args().out))


if __name__ == "__main__":
    main()
