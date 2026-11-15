"""Join ground truth to application outputs at the correct grain and report detection quality.

    python -m data_tools.evaluate --data data/full --db var/full.db

Grains: MOVEMENT (import outcome per source event), COUNT (timing review / shrinkage alert per count),
TRANSFER (one case per transfer), ADJUSTMENT_WINDOW (pattern alert overlapping a planted window).
This is the only place ground_truth.csv is read.
"""
from __future__ import annotations

import argparse
import csv
import json
import sqlite3
from collections import Counter, defaultdict
from pathlib import Path


def ratio(a, b):
    return round(a / b, 4) if b else None


def confusion(pairs):
    """pairs: iterable of (expected_bool, predicted_bool)."""
    tp = fp = fn = tn = 0
    for e, p in pairs:
        if e and p:
            tp += 1
        elif p:
            fp += 1
        elif e:
            fn += 1
        else:
            tn += 1
    return {"tp": tp, "fp": fp, "fn": fn, "tn": tn, "precision": ratio(tp, tp + fp), "recall": ratio(tp, tp + fn),
            "false_positive_rate": ratio(fp, fp + tn)}


def evaluate(data: Path, db_path: Path) -> dict:
    gt = list(csv.DictReader((data / "ground_truth.csv").open(encoding="utf-8")))
    con = sqlite3.connect(db_path)
    con.row_factory = sqlite3.Row
    q = lambda sql, *a: con.execute(sql, a).fetchall()  # noqa: E731
    by_grain = defaultdict(list)
    for r in gt:
        by_grain[r["grain"]].append(r)
    report: dict = {"data": str(data), "db": str(db_path)}

    # ------------------------------------------------------------- MOVEMENT
    results = defaultdict(list)
    for r in q("SELECT x.row_id, x.source_system, x.source_event_id, x.outcome, x.error_code, x.corrects_row_id, x.movement_id"
               " FROM import_row_result x JOIN import_batch b USING(batch_id) WHERE b.batch_kind='MOVEMENTS'"):
        results[(r["source_system"], r["source_event_id"])].append(r)
    mv_rows, per_code = [], defaultdict(lambda: {"tp": 0, "fn": 0, "fp": 0})
    planted_keys = set()
    for g in by_grain["MOVEMENT"]:
        sys_, eid = g["entity_key"].split("|", 1)
        planted_keys.add((sys_, eid))
        rs = sorted(results.get((sys_, eid), []), key=lambda r: r["row_id"])
        exp = g["expected_decision"]
        ok = False
        if exp == "QUARANTINE":
            first = rs[0] if rs else None
            ok = bool(first and first["outcome"] == "QUARANTINED" and first["error_code"] == g["expected_code"])
            if g["correction_expected"] == "POSTED":
                ok = ok and any(r["outcome"] == "POSTED" and r["corrects_row_id"] == first["row_id"] for r in rs[1:])
            per_code[g["expected_code"]]["tp" if ok else "fn"] += 1
        elif exp == "DUPLICATE":
            ok = [r["outcome"] for r in rs].count("POSTED") == 1 and any(r["outcome"] == "DUPLICATE" for r in rs)
        elif exp == "CONFLICT":
            ok = [r["outcome"] for r in rs].count("POSTED") == 1 and any(r["outcome"] == "CONFLICT" for r in rs)
        elif exp == "POSTED":
            ok = bool(rs) and rs[0]["outcome"] == "POSTED" and bool(q(
                "SELECT 1 FROM inventory_movement WHERE reverses_movement_id=?", rs[0]["movement_id"]))
        mv_rows.append((g["scenario"], ok))
    # quarantines of rows nobody planted (false positives of the import validator)
    unexpected = Counter()
    for (sys_, eid), rs in results.items():
        if (sys_, eid) in planted_keys:
            continue
        for r in rs:
            if r["outcome"] != "POSTED":
                unexpected[r["error_code"] or r["outcome"]] += 1
                if r["error_code"] in per_code:
                    per_code[r["error_code"]]["fp"] += 1
    sc = defaultdict(lambda: [0, 0])
    for s, ok in mv_rows:
        sc[s][0] += 1
        sc[s][1] += ok
    report["movement"] = {
        "by_scenario": {s: {"n": n, "correct": c, "accuracy": ratio(c, n)} for s, (n, c) in sorted(sc.items())},
        "quarantine_rules": {k: {**v, "precision": ratio(v["tp"], v["tp"] + v["fp"]),
                                 "recall": ratio(v["tp"], v["tp"] + v["fn"])} for k, v in sorted(per_code.items())},
        "unexpected_non_posted_rows": dict(unexpected),
        "baseline_rows_posted_ok": sum(1 for k, rs in results.items() if k not in planted_keys
                                       and [r["outcome"] for r in rs] == ["POSTED"]),
    }

    # ---------------------------------------------------------------- COUNT
    alerts = defaultdict(set)
    for a in q("SELECT count_id, alert_type FROM shrinkage_alert WHERE count_id IS NOT NULL"):
        alerts[a["count_id"]].add(a["alert_type"])
    posted = {r["count_id"] for r in q("SELECT count_id FROM cycle_count")}
    recon = {r["count_id"]: r for r in q(
        "SELECT r.* FROM count_reconciliation r WHERE r.version = (SELECT MAX(version) FROM count_reconciliation r2"
        " WHERE r2.count_id = r.count_id)")}
    versions = {r["count_id"]: r["n"] for r in q("SELECT count_id, COUNT(*) n FROM count_reconciliation GROUP BY count_id")}
    pairs_short, pairs_timing, pairs_quar = [], [], []
    arithmetic_mismatch = []
    scen = defaultdict(Counter)
    for g in by_grain["COUNT"]:
        cid = g["entity_key"]
        if cid not in posted:
            pred = "QUARANTINE"
        elif "COUNT_TIMING_REVIEW" in alerts[cid]:
            pred = "TIMING_REVIEW"
        elif "UNEXPLAINED_SHORTAGE" in alerts[cid]:
            pred = "SHRINKAGE_ALERT"
        else:
            pred = "NO_ALERT"
        exp = g["expected_decision"]
        scen[g["scenario"]][pred] += 1
        pairs_quar.append((exp == "QUARANTINE", pred == "QUARANTINE"))
        if exp != "QUARANTINE" and pred != "QUARANTINE":
            pairs_timing.append((exp == "TIMING_REVIEW", pred == "TIMING_REVIEW"))
            if exp != "TIMING_REVIEW":
                pairs_short.append((exp == "SHRINKAGE_ALERT", "UNEXPLAINED_SHORTAGE" in alerts[cid]))
            rc = recon[cid]
            if str(rc["expected_qty"]) != g["oracle_expected_qty"] or str(rc["counted_qty"]) != g["oracle_counted_qty"]:
                arithmetic_mismatch.append({"count_id": cid, "db_expected": rc["expected_qty"],
                                            "oracle_expected": g["oracle_expected_qty"], "scenario": g["scenario"]})
    late = [g["entity_key"] for g in by_grain["COUNT"] if g["scenario"] == "LATE_MOVEMENT_BENIGN"]
    report["count"] = {
        "rules": {"UNEXPLAINED_SHORTAGE": confusion(pairs_short), "COUNT_TIMING": confusion(pairs_timing),
                  "COUNT_QUARANTINE": confusion(pairs_quar)},
        "by_scenario_predicted": {s: dict(c) for s, c in sorted(scen.items())},
        "reconciliation_matches_oracle": {"checked": len([1 for g in by_grain["COUNT"] if g["entity_key"] in posted]),
                                          "mismatches": len(arithmetic_mismatch), "examples": arithmetic_mismatch[:10]},
        "late_movement_counts_with_multiple_versions": sum(1 for c in late if versions.get(c, 0) >= 2),
        "late_movement_counts": len(late),
    }

    # ------------------------------------------------------------- TRANSFER
    t_alerts = {r["transfer_id"]: r["alert_type"] for r in q(
        "SELECT transfer_id, alert_type FROM shrinkage_alert WHERE transfer_id IS NOT NULL")}
    tp_pairs, t_scen, type_ok = [], defaultdict(Counter), [0, 0]
    for g in by_grain["TRANSFER"]:
        pred = t_alerts.get(g["entity_key"])
        tp_pairs.append((g["expected_decision"] == "TRANSFER_ALERT", pred is not None))
        t_scen[g["scenario"]][pred or "NO_ALERT"] += 1
        if g["expected_decision"] == "TRANSFER_ALERT" and pred:
            type_ok[0] += 1
            type_ok[1] += pred == g["expected_code"]
    report["transfer"] = {"rules": {"TRANSFER_RECON": confusion(tp_pairs)},
                          "alert_type_agreement": ratio(type_ok[1], type_ok[0]),
                          "by_scenario_predicted": {s: dict(c) for s, c in sorted(t_scen.items())},
                          "alerts_on_unlabelled_transfers": len(set(t_alerts) - {g["entity_key"] for g in by_grain["TRANSFER"]})}

    # ---------------------------------------------------- ADJUSTMENT_WINDOW
    pat = q("SELECT alert_id, store_id, sku_id, window_start, window_end FROM shrinkage_alert"
            " WHERE alert_type='REPEATED_NEGATIVE_ADJUSTMENTS'")
    matched_alerts, hit = set(), 0
    for g in by_grain["ADJUSTMENT_WINDOW"]:
        found = False
        for a in pat:
            if a["store_id"] == g["store_id"] and a["sku_id"] == g["sku_id"] and \
                    a["window_start"] <= g["window_end"] and a["window_end"] >= g["window_start"]:
                matched_alerts.add(a["alert_id"])
                found = True
        hit += found
    planted_pairs = {(g["store_id"], g["sku_id"]) for g in by_grain["ADJUSTMENT_WINDOW"]}
    benign_pairs = {(r["store_id"], r["sku_id"]) for r in q(
        "SELECT DISTINCT store_id, sku_id FROM inventory_movement WHERE movement_type='ADJUSTMENT' AND stock_qty < 0"
        " AND reason_code IN ('DAMAGE','EXPIRED','SAMPLING','OTHER')")} - planted_pairs
    fp_pairs = {(a["store_id"], a["sku_id"]) for a in pat if a["alert_id"] not in matched_alerts}
    n_pos = len(by_grain["ADJUSTMENT_WINDOW"])
    report["adjustment_pattern"] = {"rules": {"REPEATED_NEG_ADJ": {
        "tp": hit, "fn": n_pos - hit, "fp_alerts": len(pat) - len(matched_alerts),
        "precision": ratio(len(matched_alerts), len(pat)), "recall": ratio(hit, n_pos),
        "false_positive_rate": ratio(len(fp_pairs & benign_pairs), len(benign_pairs)),
        "benign_store_sku_pairs_with_writeoffs": len(benign_pairs)}}}
    con.close()
    return report


def to_markdown(rep: dict) -> str:
    L = ["# Detection evaluation", "", f"Dataset `{rep['data']}`, database `{rep['db']}`.", "",
         "## Per-rule quality", "", "| Rule | Grain | TP | FP | FN | Precision | Recall | FPR |", "|---|---|---|---|---|---|---|---|"]
    for grain, key in (("count", "count"), ("transfer", "transfer"), ("adjustment_pattern", "adjustment window")):
        for rule, m in rep[grain]["rules"].items():
            fp = m.get("fp", m.get("fp_alerts"))
            L.append(f"| {rule} | {key} | {m['tp']} | {fp} | {m['fn']} | {m['precision']} | {m['recall']} | {m.get('false_positive_rate')} |")
    for code, m in rep["movement"]["quarantine_rules"].items():
        L.append(f"| quarantine {code} | movement | {m['tp']} | {m['fp']} | {m['fn']} | {m['precision']} | {m['recall']} | - |")
    L += ["", "## Movement scenarios (expected import outcome reached)", "", "| Scenario | n | correct |", "|---|---|---|"]
    for s, v in rep["movement"]["by_scenario"].items():
        L.append(f"| {s} | {v['n']} | {v['correct']} |")
    L += ["", f"Baseline rows posted cleanly: {rep['movement']['baseline_rows_posted_ok']:,}. "
              f"Unexpected non-posted rows: {rep['movement']['unexpected_non_posted_rows'] or 'none'}.", "",
          "## Count scenarios -> predicted decision", "", "| Scenario | predicted |", "|---|---|"]
    for s, v in rep["count"]["by_scenario_predicted"].items():
        L.append(f"| {s} | {', '.join(f'{k}: {n}' for k, n in sorted(v.items()))} |")
    rc = rep["count"]["reconciliation_matches_oracle"]
    L += ["", f"Reconciliation arithmetic vs generator oracle: {rc['checked'] - rc['mismatches']:,}/{rc['checked']:,} counts match.",
          f"Late-movement counts re-versioned: {rep['count']['late_movement_counts_with_multiple_versions']}/{rep['count']['late_movement_counts']}.",
          "", "## Transfer scenarios -> predicted", "", "| Scenario | predicted |", "|---|---|"]
    for s, v in rep["transfer"]["by_scenario_predicted"].items():
        L.append(f"| {s} | {', '.join(f'{k}: {n}' for k, n in sorted(v.items()))} |")
    L.append(f"\nAlert-type agreement on true transfer positives: {rep['transfer']['alert_type_agreement']}.")
    return "\n".join(L) + "\n"


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--data", required=True)
    ap.add_argument("--db", required=True)
    ap.add_argument("--out", default=None, help="directory for evaluation_report.{json,md} (default: --data)")
    a = ap.parse_args()
    rep = evaluate(Path(a.data), Path(a.db))
    out = Path(a.out or a.data)
    (out / "evaluation_report.json").write_text(json.dumps(rep, indent=2), encoding="utf-8")
    (out / "evaluation_report.md").write_text(to_markdown(rep), encoding="utf-8")
    print(to_markdown(rep))


if __name__ == "__main__":
    main()
