"""Validate a generated dataset (files) and, optionally, the database it was loaded into.

    python -m data_tools.validate --data data/full [--db var/full.db]

File checks: primary-key uniqueness, foreign keys, enums and ID formats, date ordering, lifecycle
ordering (transfer legs, returns after sales), duplicate rows, and that every deliberate violation
in the inputs is accounted for by a ground-truth label (planted anomalies must not corrupt unrelated rows).
Database checks: SQLite integrity + FK check, ledger rebuild vs cached balances, reconciliation
equation and recomputation, event-time authority of every posting, quarantine isolation, case
lifecycle consistency. Writes validation_summary.{json,md}.
"""
from __future__ import annotations

import argparse
import csv
import json
import sqlite3
from collections import Counter, defaultdict
from pathlib import Path

from backend.app import catalog as C


class Report:
    def __init__(self):
        self.checks: list[dict] = []

    def add(self, area: str, name: str, failures: int, checked: int, detail=None):
        self.checks.append({"area": area, "check": name, "checked": checked, "failures": failures,
                            "status": "PASS" if failures == 0 else "FAIL", "detail": detail})

    @property
    def ok(self) -> bool:
        return all(c["status"] == "PASS" for c in self.checks)


def rd(data: Path, name: str) -> list[dict]:
    with (data / name).open(newline="", encoding="utf-8") as fh:
        return list(csv.DictReader(fh))


def validate_files(data: Path, rep: Report) -> None:
    f = {n: rd(data, n) for n in ("stores.csv", "skus.csv", "store_skus.csv", "employees.csv", "role_assignments.csv",
                                  "shifts.csv", "promotions.csv", "promotion_scopes.csv", "source_documents.csv",
                                  "transfers.csv", "transfer_lines.csv", "counts_input.csv")}
    mv = rd(data, "movements_input.csv")
    gt = rd(data, "ground_truth.csv")
    A = "files"

    def uniq(name, keyf, rows):
        c = Counter(keyf(r) for r in rows)
        dup = [k for k, n in c.items() if n > 1]
        rep.add(A, f"primary key unique: {name}", len(dup), len(rows), dup[:5] or None)

    uniq("store.store_id", lambda r: r["store_id"], f["stores.csv"])
    uniq("sku.sku_id", lambda r: r["sku_id"], f["skus.csv"])
    uniq("store_sku(store_id, sku_id)", lambda r: (r["store_id"], r["sku_id"]), f["store_skus.csv"])
    uniq("employee.employee_id", lambda r: r["employee_id"], f["employees.csv"])
    uniq("role_assignment.assignment_id", lambda r: r["assignment_id"], f["role_assignments.csv"])
    uniq("shift.shift_id", lambda r: r["shift_id"], f["shifts.csv"])
    uniq("promotion.promotion_id", lambda r: r["promotion_id"], f["promotions.csv"])
    uniq("promotion_scope", lambda r: (r["promotion_id"], r["store_id"], r["sku_id"]), f["promotion_scopes.csv"])
    uniq("source_document.doc_id", lambda r: r["doc_id"], f["source_documents.csv"])
    uniq("transfer.transfer_id", lambda r: r["transfer_id"], f["transfers.csv"])
    uniq("transfer_line(transfer_id, sku_id)", lambda r: (r["transfer_id"], r["sku_id"]), f["transfer_lines.csv"])
    uniq("count input count_id", lambda r: r["count_id"], f["counts_input.csv"])
    uniq("ground truth gt_id", lambda r: r["gt_id"], gt)

    stores = {r["store_id"]: r for r in f["stores.csv"]}
    skus = {r["sku_id"] for r in f["skus.csv"]}
    pairs = {(r["store_id"], r["sku_id"]): r for r in f["store_skus.csv"]}
    emps = {r["employee_id"]: r for r in f["employees.csv"]}
    shifts = {r["shift_id"]: r for r in f["shifts.csv"]}
    docs = {r["doc_id"]: r for r in f["source_documents.csv"]}
    trf = {r["transfer_id"]: r for r in f["transfers.csv"]}
    tl = {(r["transfer_id"], r["sku_id"]) for r in f["transfer_lines.csv"]}
    promos = {r["promotion_id"] for r in f["promotions.csv"]}

    def fk(name, rows, pred):
        bad = [r for r in rows if not pred(r)]
        rep.add(A, f"foreign keys: {name}", len(bad), len(rows), [dict(list(b.items())[:4]) for b in bad[:3]] or None)

    fk("store_sku -> store, sku, source warehouse", f["store_skus.csv"],
       lambda r: r["store_id"] in stores and r["sku_id"] in skus and (not r["source_warehouse_id"] or
                                                                      stores.get(r["source_warehouse_id"], {}).get("store_type") == "WAREHOUSE"))
    fk("employee -> home store", f["employees.csv"], lambda r: not r["home_store_id"] or r["home_store_id"] in stores)
    fk("role_assignment -> employee, role, store", f["role_assignments.csv"],
       lambda r: r["employee_id"] in emps and r["role_code"] in C.ROLES and (not r["store_id"] or r["store_id"] in stores))
    fk("shift -> store", f["shifts.csv"], lambda r: r["store_id"] in stores)
    fk("promotion_scope -> promotion, store_sku", f["promotion_scopes.csv"],
       lambda r: r["promotion_id"] in promos and (r["store_id"], r["sku_id"]) in pairs)
    fk("source_document -> store", f["source_documents.csv"], lambda r: r["store_id"] in stores)
    fk("transfer -> stores (distinct ends)", f["transfers.csv"],
       lambda r: r["from_store_id"] in stores and r["to_store_id"] in stores and r["from_store_id"] != r["to_store_id"])
    fk("transfer_line -> transfer, assortment at both ends", f["transfer_lines.csv"],
       lambda r: r["transfer_id"] in trf and (trf[r["transfer_id"]]["from_store_id"], r["sku_id"]) in pairs
       and (trf[r["transfer_id"]]["to_store_id"], r["sku_id"]) in pairs)

    def enum(name, rows, col, allowed, optional=False):
        bad = [r[col] for r in rows if not (r[col] in allowed or (optional and r[col] == ""))]
        rep.add(A, f"allowed values: {name}", len(bad), len(rows), sorted(set(bad))[:5] or None)

    enum("store.store_type", f["stores.csv"], "store_type", C.STORE_TYPES)
    enum("sku.category", f["skus.csv"], "category", C.CATEGORIES)
    enum("sku.shrink_risk", f["skus.csv"], "shrink_risk", C.SHRINK_RISK)
    enum("shift.shift_type", f["shifts.csv"], "shift_type", C.SHIFT_TYPES)
    enum("promotion.promo_type", f["promotions.csv"], "promo_type", C.PROMO_TYPES)
    enum("source_document.doc_type", f["source_documents.csv"], "doc_type", C.DOC_TYPES)
    enum("movement.movement_type", mv, "movement_type", C.MOVEMENT_TYPES)
    enum("movement.disposition", mv, "disposition", C.RETURN_DISPOSITIONS, optional=True)
    enum("count.count_type", f["counts_input.csv"], "count_type", C.COUNT_TYPES)
    bad_ids = sum(1 for r in mv if not C.ID_PATTERNS["movement"].match(r["movement_id"]))
    bad_ids += sum(1 for r in f["counts_input.csv"] if not C.ID_PATTERNS["count"].match(r["count_id"]))
    bad_ids += sum(1 for r in f["stores.csv"] if not C.ID_PATTERNS["store"].match(r["store_id"]))
    bad_ids += sum(1 for r in f["skus.csv"] if not C.ID_PATTERNS["sku"].match(r["sku_id"]))
    rep.add(A, "ID formats (movement, count, store, sku)", bad_ids, len(mv) + len(f["counts_input.csv"]))

    def order(name, rows, a, b, strict=False, allow=lambda r: False):
        bad = [r for r in rows if r[b] and r[a] and not (r[b] > r[a] if strict else r[b] >= r[a]) and not allow(r)]
        rep.add(A, f"date ordering: {name}", len(bad), len(rows))

    order("store_sku active_to > active_from", f["store_skus.csv"], "active_from", "active_to", True)
    order("role_assignment effective_to > effective_from", f["role_assignments.csv"], "effective_from", "effective_to", True)
    order("shift end_at > start_at", f["shifts.csv"], "start_at", "end_at", True)
    order("promotion end_date >= start_date", f["promotions.csv"], "start_date", "end_date")
    order("transfer expected_arrival >= created", f["transfers.csv"], "created_at", "expected_arrival_at")
    order("movement received_at >= occurred_at", mv, "occurred_at", "received_at")
    timing_keys = {g["entity_key"] for g in gt if g["grain"] == "COUNT" and g["timing_subtype"] == "SUBMIT_EARLY"}
    order("count submitted_at >= counted_at (except labelled SUBMIT_EARLY)", f["counts_input.csv"], "counted_at",
          "submitted_at", allow=lambda r: r["count_id"] in timing_keys)
    order("movement sorted by received_at (feed order)", [{"a": a["received_at"], "b": b["received_at"]} for a, b in zip(mv, mv[1:])], "a", "b")

    # every deliberate violation must be labelled; unlabelled rows must be fully valid references
    labelled = {g["entity_key"] for g in gt if g["grain"] == "MOVEMENT"}
    keyed = Counter((r["source_system"], r["source_event_id"]) for r in mv)
    unlabelled_repeats = [k for k, n in keyed.items() if n > 1 and f"{k[0]}|{k[1]}" not in labelled]
    rep.add(A, "repeated event keys are all labelled (replay/conflict/correction)", len(unlabelled_repeats), len(keyed),
            unlabelled_repeats[:5] or None)
    exact = Counter(hash(tuple(v for k, v in r.items() if k != "received_at")) for r in mv)
    dup_rows = sum(n - 1 for n in exact.values() if n > 1)
    replay_labels = sum(1 for g in gt if g["scenario"] == "FEED_REPLAY")
    rep.add(A, "exact duplicate rows == labelled feed replays", abs(dup_rows - replay_labels), len(mv),
            {"duplicate_rows": dup_rows, "labelled": replay_labels})
    bad_ref = []
    for r in mv:
        key = f"{r['source_system']}|{r['source_event_id']}"
        if key in labelled:
            continue
        st = r["store_id"]
        ok = st in stores and r["sku_id"] in skus and (st, r["sku_id"]) in pairs and r["actor_id"] in emps \
            and r["shift_id"] in shifts and shifts[r["shift_id"]]["store_id"] == st
        if r["source_doc_id"]:
            ok = ok and docs.get(r["source_doc_id"], {}).get("store_id") == st
        if r["transfer_id"]:
            t = trf.get(r["transfer_id"])
            ok = ok and t is not None and (r["transfer_id"], r["sku_id"]) in tl and st in (t["from_store_id"], t["to_store_id"])
        if r["movement_type"] == "ADJUSTMENT":
            ok = ok and r["reason_code"] in C.ADJUSTMENT_REASONS and r["approved_by"] in emps and r["approved_by"] != r["actor_id"]
        if not ok:
            bad_ref.append(key)
    rep.add(A, "unlabelled movement rows have valid references (store/doc/transfer/shift/actor/reason)", len(bad_ref),
            len(mv), bad_ref[:5] or None)
    # lifecycle: receipt legs after dispatch legs; returns after their sale
    first_out, bad_leg = {}, 0
    for r in sorted(mv, key=lambda r: r["occurred_at"]):
        k = (r["transfer_id"], r["sku_id"])
        if r["movement_type"] == "TRANSFER_OUT":
            first_out.setdefault(k, r["occurred_at"])
        elif r["movement_type"] == "TRANSFER_IN" and (k not in first_out or first_out[k] > r["occurred_at"]):
            bad_leg += 1
    rep.add(A, "lifecycle: transfer receipt follows dispatch", bad_leg, len(first_out))
    sale_at = {r["movement_id"]: r["occurred_at"] for r in mv if r["movement_type"] == "SALE"}
    rets = [r for r in mv if r["original_sale_movement_id"]]
    rep.add(A, "lifecycle: return follows its original sale", sum(1 for r in rets if sale_at.get(r["original_sale_movement_id"], "9") > r["occurred_at"]), len(rets))
    term = {e["employee_id"]: e["terminated_on"] for e in f["employees.csv"] if e["terminated_on"]}
    bad_term = [a for a in f["role_assignments.csv"] if a["employee_id"] in term and (not a["effective_to"] or a["effective_to"][:10] > term[a["employee_id"]])]
    rep.add(A, "lifecycle: leavers' role assignments end by termination", len(bad_term), len(term))
    # ground truth is separate and references real inputs only
    in_cols = set(mv[0]) | set(f["counts_input.csv"][0])
    leak = sorted(in_cols & {"scenario", "is_anomaly", "expected_decision", "expected_code", "hidden_loss_units"})
    rep.add(A, "ground truth not leaked into input columns", len(leak), len(in_cols), leak or None)
    keys = {f"{r['source_system']}|{r['source_event_id']}" for r in mv}
    cids = {r["count_id"] for r in f["counts_input.csv"]}
    orphan = [g["gt_id"] for g in gt if (g["grain"] == "MOVEMENT" and g["entity_key"] not in keys)
              or (g["grain"] == "COUNT" and g["entity_key"] not in cids)
              or (g["grain"] == "TRANSFER" and g["entity_key"] not in trf)]
    rep.add(A, "ground truth rows reference existing inputs", len(orphan), len(gt), orphan[:5] or None)
    manifest = json.loads((data / "manifest.json").read_text(encoding="utf-8"))
    vol = manifest["parameters"]
    rep.add(A, "volume: movement inputs == profile target", abs(len(mv) - vol["movements"]), len(mv))
    rep.add(A, "volume: count inputs == profile target", abs(len(f["counts_input.csv"]) - vol["counts"]), len(f["counts_input.csv"]))


def validate_db(db: Path, data: Path | None, rep: Report) -> None:
    con = sqlite3.connect(db)
    A = "database"
    one = lambda sql: con.execute(sql).fetchone()[0]  # noqa: E731
    ic = one("PRAGMA integrity_check")
    rep.add(A, "SQLite integrity_check", 0 if ic == "ok" else 1, 1, ic)
    fkc = con.execute("PRAGMA foreign_key_check").fetchall()
    rep.add(A, "foreign_key_check", len(fkc), one("SELECT COUNT(*) FROM inventory_movement"), fkc[:5] or None)
    bad = con.execute("""SELECT b.store_id, b.sku_id, b.on_hand, s.opening_qty + COALESCE(m.total, 0)
        FROM stock_balance b JOIN store_sku s USING (store_id, sku_id)
        LEFT JOIN (SELECT store_id, sku_id, SUM(stock_qty) total FROM inventory_movement GROUP BY store_id, sku_id) m
        USING (store_id, sku_id) WHERE b.on_hand <> s.opening_qty + COALESCE(m.total, 0)""").fetchall()
    rep.add(A, "ledger rebuild == cached stock_balance", len(bad), one("SELECT COUNT(*) FROM stock_balance"), bad[:5] or None)
    rep.add(A, "stock equation holds on every reconciliation", one(
        "SELECT COUNT(*) FROM count_reconciliation WHERE expected_qty <> opening_qty + receipts_qty + transfer_in_qty"
        " + restocked_returns_qty - sales_qty - transfer_out_qty + adjustments_qty OR variance_qty <> counted_qty - expected_qty"),
        one("SELECT COUNT(*) FROM count_reconciliation"))
    # recompute every current reconciliation independently in SQL
    mism = one("""
        WITH cur AS (SELECT r.* FROM count_reconciliation r WHERE r.version = (SELECT MAX(version) FROM count_reconciliation x WHERE x.count_id=r.count_id)),
        calc AS (SELECT cur.recon_id, s.opening_qty + COALESCE((SELECT SUM(m.stock_qty) FROM inventory_movement m
                 WHERE m.store_id=c.store_id AND m.sku_id=c.sku_id AND m.occurred_at <= c.count_cutoff_at
                 AND m.ledger_seq <= cur.ledger_watermark_seq), 0) AS expected
                 FROM cur JOIN cycle_count c ON c.count_id=cur.count_id JOIN store_sku s ON s.store_id=c.store_id AND s.sku_id=c.sku_id)
        SELECT COUNT(*) FROM calc JOIN cur USING (recon_id) WHERE calc.expected <> cur.expected_qty""")
    rep.add(A, "current reconciliations recompute from the ledger", mism, one("SELECT COUNT(DISTINCT count_id) FROM count_reconciliation"))
    late = one("""SELECT COUNT(*) FROM cycle_count c WHERE EXISTS (SELECT 1 FROM inventory_movement m WHERE m.store_id=c.store_id
        AND m.sku_id=c.sku_id AND m.occurred_at <= c.count_cutoff_at AND m.ledger_seq > (SELECT MAX(ledger_watermark_seq)
        FROM count_reconciliation r WHERE r.count_id=c.count_id) AND m.stock_qty <> 0)""")
    rep.add(A, "no count is missing a late movement (re-versioning complete)", late, one("SELECT COUNT(*) FROM cycle_count"))
    rep.add(A, "quarantined/duplicate/conflict rows never reach the ledger", one(
        "SELECT COUNT(*) FROM inventory_movement m JOIN import_row_result x ON x.row_id=m.import_row_id WHERE x.outcome <> 'POSTED'"),
        one("SELECT COUNT(*) FROM import_row_result WHERE outcome <> 'POSTED'"))
    rep.add(A, "every ledger row traces to a POSTED import row", one(
        "SELECT COUNT(*) FROM inventory_movement m LEFT JOIN import_row_result x ON x.row_id=m.import_row_id"
        " WHERE x.row_id IS NULL OR x.movement_id <> m.movement_id"), one("SELECT COUNT(*) FROM inventory_movement"))
    rep.add(A, "batch counters add up", one(
        "SELECT COUNT(*) FROM import_batch_status s WHERE s.processed_count <> (SELECT COUNT(*) FROM import_row_result x"
        " WHERE x.batch_id=s.batch_id) OR (s.status='COMPLETED' AND s.processed_count <> s.received_count)"),
        one("SELECT COUNT(*) FROM import_batch_status"))
    rep.add(A, "no duplicate ledger events (source key) or double reversals", one(
        "SELECT (SELECT COUNT(*) FROM (SELECT 1 FROM inventory_movement GROUP BY source_system, source_event_id HAVING COUNT(*)>1))"
        " + (SELECT COUNT(*) FROM (SELECT 1 FROM inventory_movement WHERE reverses_movement_id IS NOT NULL GROUP BY reverses_movement_id HAVING COUNT(*)>1))"),
        one("SELECT COUNT(*) FROM inventory_movement"))
    rep.add(A, "transfer legs posted only by the referenced end", one(
        "SELECT COUNT(*) FROM inventory_movement m JOIN transfer t ON t.transfer_id=m.transfer_id WHERE"
        " (m.movement_type='TRANSFER_OUT' AND m.store_id<>t.from_store_id) OR (m.movement_type='TRANSFER_IN' AND m.store_id<>t.to_store_id)"),
        one("SELECT COUNT(*) FROM inventory_movement WHERE transfer_id IS NOT NULL"))
    rep.add(A, "adjustments carry reason and an approver other than the actor", one(
        "SELECT COUNT(*) FROM inventory_movement WHERE movement_type='ADJUSTMENT' AND (reason_code IS NULL OR approved_by IS NULL"
        " OR (approved_by = actor_id AND source_system <> 'STASHTRACE'))"),
        one("SELECT COUNT(*) FROM inventory_movement WHERE movement_type='ADJUSTMENT'"))
    # event-time authority: the recorded role was effective for that actor/store at occurred_at
    rep.add(A, "every posting's actor role was effective at the event time and store", one("""
        SELECT COUNT(*) FROM inventory_movement m WHERE NOT EXISTS (SELECT 1 FROM role_assignment a
          WHERE a.employee_id=m.actor_id AND a.role_code=m.actor_role_code AND (a.store_id IS NULL OR a.store_id=m.store_id)
          AND a.effective_from <= m.occurred_at AND (a.effective_to IS NULL OR m.occurred_at < a.effective_to))"""),
        one("SELECT COUNT(*) FROM inventory_movement"))
    rep.add(A, "every posting with a shift is inside that shift at that store", one(
        "SELECT COUNT(*) FROM inventory_movement m JOIN shift s ON s.shift_id=m.shift_id WHERE s.store_id<>m.store_id"
        " OR m.occurred_at < s.start_at OR m.occurred_at >= s.end_at"), one("SELECT COUNT(*) FROM inventory_movement WHERE shift_id IS NOT NULL"))
    rep.add(A, "case state == last event; event seq gapless", one("""
        SELECT COUNT(*) FROM investigation_state s WHERE s.status <> (SELECT to_status FROM investigation_event e
          WHERE e.investigation_id=s.investigation_id ORDER BY seq DESC LIMIT 1)
          OR s.last_seq <> (SELECT COUNT(*) FROM investigation_event e WHERE e.investigation_id=s.investigation_id)
          OR s.last_seq <> (SELECT MAX(seq) FROM investigation_event e WHERE e.investigation_id=s.investigation_id)"""),
        one("SELECT COUNT(*) FROM investigation"))
    rep.add(A, "every alert has rule version, run, case and evidence", one("""
        SELECT COUNT(*) FROM shrinkage_alert a WHERE NOT EXISTS (SELECT 1 FROM alert_evidence e WHERE e.alert_id=a.alert_id)
          OR NOT EXISTS (SELECT 1 FROM rule_definition r WHERE r.rule_code=a.rule_code AND r.rule_version=a.rule_version)"""),
        one("SELECT COUNT(*) FROM shrinkage_alert"))
    rep.add(A, "no shrinkage alert on a count under timing review", one(
        "SELECT COUNT(*) FROM shrinkage_alert a JOIN count_reconciliation r ON r.recon_id=a.recon_id"
        " WHERE a.alert_type='UNEXPLAINED_SHORTAGE' AND r.basis_status='TIMING_REVIEW'"),
        one("SELECT COUNT(*) FROM shrinkage_alert WHERE alert_type='UNEXPLAINED_SHORTAGE'"))
    if data is not None:
        posted = one("SELECT COUNT(*) FROM import_row_result x JOIN import_batch b USING(batch_id) WHERE b.batch_kind='MOVEMENTS'"
                     " AND b.source_system <> 'STASHTRACE'")
        n_in = sum(1 for _ in open(data / "movements_input.csv", encoding="utf-8")) - 1
        rep.add(A, "every movement input row has exactly one outcome", abs(posted - n_in), n_in)
    con.close()


def to_md(rep: Report, title: str) -> str:
    lines = [f"# {title}", "", f"Overall: **{'PASS' if rep.ok else 'FAIL'}** "
             f"({sum(c['status'] == 'PASS' for c in rep.checks)}/{len(rep.checks)} checks)", "",
             "| Area | Check | Checked | Failures | Status |", "|---|---|---|---|---|"]
    for c in rep.checks:
        lines.append(f"| {c['area']} | {c['check']} | {c['checked']:,} | {c['failures']:,} | {c['status']} |")
    return "\n".join(lines) + "\n"


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--data", required=True)
    ap.add_argument("--db")
    ap.add_argument("--out", default=None)
    a = ap.parse_args()
    data = Path(a.data)
    rep = Report()
    validate_files(data, rep)
    if a.db:
        validate_db(Path(a.db), data, rep)
    out = Path(a.out or a.data)
    (out / "validation_summary.json").write_text(json.dumps({"ok": rep.ok, "checks": rep.checks}, indent=2), encoding="utf-8")
    md = to_md(rep, f"Validation summary — {data.name}")
    (out / "validation_summary.md").write_text(md, encoding="utf-8")
    print(md)
    raise SystemExit(0 if rep.ok else 1)


if __name__ == "__main__":
    main()
