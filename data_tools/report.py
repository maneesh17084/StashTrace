"""Assemble docs/full_run.md from the machine-written reports (no hand-copied numbers).

    python -m data_tools.report --data data/full
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path

DOCS = Path(__file__).resolve().parents[1] / "docs"


def load(p: Path):
    return json.loads(p.read_text(encoding="utf-8")) if p.exists() else None


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--data", default="data/full")
    a = ap.parse_args()
    data = Path(a.data)
    lr = load(data / "load_report.json")
    man = load(data / "manifest.json")
    val = load(DOCS / "validation_summary_full.json")
    ev = load(DOCS / "evaluation_report_full.json")
    perf = load(DOCS / "performance.json")
    rep = load(DOCS / "reproducibility_full.json")
    br = load(DOCS / "backup_restore.json")
    L = ["# Full-volume run", "",
         f"Profile `{man['profile']}`, seed `{man['seed']}`, generator {man['generator_version']}. "
         f"Command: `{man['command']}`.", ""]
    imp = lr["import"]
    L += ["## Volumes and import", "",
          f"Hardware: {lr['processor']}, {lr['cpu_count']} logical CPUs, {lr['platform']}, Python {lr['python']} "
          f"(shared machine; other workloads were running).", "",
          "| | Received | Posted (trusted) | Quarantined | Duplicate | Conflict |", "|---|---|---|---|---|---|"]
    for k in ("movements", "counts"):
        v = imp[k]
        L.append(f"| {k} | {v['received']:,} | {v['posted']:,} | {v['quarantined']:,} | {v['duplicate']:,} | {v['conflict']:,} |")
    L += ["", f"* Masters loaded in {lr['masters_seconds']} s: " + ", ".join(f"{t} {n:,}" for t, n in lr["masters"].items()) + ".",
          f"* Day-by-day replay through the import pipeline: {imp['batches']} batches in {imp['seconds']:,} s "
          f"({imp['movements']['received'] / imp['seconds']:,.0f} movement rows/s including validation, posting, "
          f"balance updates and reconciliation).",
          f"* Late arrivals created {imp['reconciliation_versions_from_late_movements']:,} additional reconciliation versions.",
          f"* Store `COUNT_CORRECTION` write-offs in the feed: {man['volumes']['count_corrections_posted_by_stores']:,}."]
    runs = lr["detection_runs"]
    L += [f"* Detection run 1: {runs[0]['alerts_new']:,} alerts in {runs[0]['duration_ms'] / 1000:.1f} s. "
          f"Rerun: {runs[1]['alerts_new']} new, {runs[1]['alerts_redetected']:,} re-detected "
          f"({runs[1]['duration_ms'] / 1000:.1f} s). Stable case keys prevent duplicate cases.",
          f"* Database rows: " + ", ".join(f"{t} {n:,}" for t, n in lr["db_rows"].items()) + ".", ""]
    if val:
        n_pass = sum(c["status"] == "PASS" for c in val["checks"])
        L += ["## Validation", "", f"**{'PASS' if val['ok'] else 'FAIL'}**: {n_pass}/{len(val['checks'])} checks on files and "
              f"database ([full table](validation_summary_full.md)). Highlights:", ""]
        for c in val["checks"]:
            if c["check"] in ("ledger rebuild == cached stock_balance", "current reconciliations recompute from the ledger",
                              "quarantined/duplicate/conflict rows never reach the ledger",
                              "every posting's actor role was effective at the event time and store",
                              "unlabelled movement rows have valid references (store/doc/transfer/shift/actor/reason)",
                              "no count is missing a late movement (re-versioning complete)", "foreign_key_check"):
                L.append(f"* {c['check']}: {c['checked']:,} checked, {c['failures']} failures")
        L.append("")
    if ev:
        L += ["## Detection quality (held-out ground truth)", "",
              "Labels come from the planted cause; the application never saw them. Grain per rule as shown.", "",
              "| Rule | Grain | TP | FP | FN | Precision | Recall | False-positive rate |", "|---|---|---|---|---|---|---|---|"]
        for grain, key in (("count", "count"), ("transfer", "transfer"), ("adjustment_pattern", "store/SKU window")):
            for rule, m in ev[grain]["rules"].items():
                L.append(f"| {rule} | {key} | {m['tp']} | {m.get('fp', m.get('fp_alerts'))} | {m['fn']} | {m['precision']} | "
                         f"{m['recall']} | {m.get('false_positive_rate')} |")
        for code, m in ev["movement"]["quarantine_rules"].items():
            L.append(f"| import: {code} | movement | {m['tp']} | {m['fp']} | {m['fn']} | {m['precision']} | {m['recall']} | – |")
        rc = ev["count"]["reconciliation_matches_oracle"]
        L += ["", f"* Reconciliation arithmetic matches the generator's independent ledger for "
                  f"{rc['checked'] - rc['mismatches']:,}/{rc['checked']:,} counts.",
              f"* Late-movement counts re-versioned: {ev['count']['late_movement_counts_with_multiple_versions']}/"
              f"{ev['count']['late_movement_counts']}; none raised an alert." if ev['count']['by_scenario_predicted'].get('LATE_MOVEMENT_BENIGN', {}).get('SHRINKAGE_ALERT', 0) == 0
              else "* Some late-movement counts raised alerts (see evaluation report).",
              f"* Baseline movement rows posted cleanly: {ev['movement']['baseline_rows_posted_ok']:,}; unexpected "
              f"non-posted rows: {ev['movement']['unexpected_non_posted_rows'] or 'none'}.", "",
              "Missed planted cases and benign cases flagged, by scenario:", "",
              "| Count scenario | Predicted |", "|---|---|"]
        for s, v in ev["count"]["by_scenario_predicted"].items():
            L.append(f"| {s} | {', '.join(f'{k} {n}' for k, n in sorted(v.items()))} |")
        L += ["", "| Transfer scenario | Predicted |", "|---|---|"]
        for s, v in ev["transfer"]["by_scenario_predicted"].items():
            L.append(f"| {s} | {', '.join(f'{k} {n}' for k, n in sorted(v.items()))} |")
        L.append("")
    if perf:
        L += ["## API latency", "", f"In-process ASGI client against the full database, {perf['results'][0]['n']} requests per endpoint.", "",
              "| Endpoint | p50 ms | p95 ms |", "|---|---|---|"]
        L += [f"| {r['endpoint']} | {r['p50_ms']} | {r['p95_ms']} |" for r in perf["results"]]
        L.append("")
    if rep:
        L += ["## Reproducibility", "", f"Generated twice with seed {rep['seed']}: **{'identical' if rep['all_identical'] else 'DIFFERENT'}** "
              f"({len(rep['files'])} files, SHA-256 compared; [details](reproducibility_full.md)). Generation time per run: "
              f"{rep['seconds']['run_a']} s / {rep['seconds']['run_b']} s.", ""]
    if br:
        L += ["## Backup and restore", "", f"Online backup {br['backup_seconds']} s, restore {br['restore_seconds']} s; restored copy "
              f"integrity `{br['integrity_check']}`, {br['foreign_key_violations']} FK violations, row counts and ledger/balance "
              f"checksums identical: **{br['identical']}**.", ""]
    (DOCS / "full_run.md").write_text("\n".join(L), encoding="utf-8")
    print("\n".join(L))
    package(data, man)


def package(data: Path, man: dict) -> None:
    """Ship the dataset with its README, data dictionary, schema and validation summary."""
    import shutil
    for f in ("data_dictionary.md", "schema.json"):
        shutil.copy(DOCS / f, data / f)
    for f in ("validation_summary_full.md", "validation_summary_full.json"):
        if (DOCS / f).exists():
            shutil.copy(DOCS / f, data / f.replace("_full", ""))
    files = "\n".join(f"| `{k}` | {v['rows'] if v['rows'] is not None else '–'} | `{v['sha256'][:16]}…` |"
                      for k, v in man["files"].items())
    readme = f"""# StashTrace synthetic dataset — profile `{man['profile']}`

Regenerate exactly: `{man['command']}` (seed `{man['seed']}`, generator {man['generator_version']}).
Provenance: {man['provenance']['method']}. External data: {man['provenance']['external_data']}.
{man['provenance']['license']}

Period {man['period']['start']} + {man['period']['days']} days. Inputs: {man['volumes']['movement_inputs']:,} movement rows,
{man['volumes']['count_inputs']:,} count rows.

| File | Rows | SHA-256 |
|---|---|---|
{files}

* Masters: `stores`, `skus`, `store_skus` (assortment + opening balances), `employees`, `employee_roles`,
  `role_permissions`, `role_assignments`, `shifts`, `promotions`, `promotion_scopes`, `source_documents`,
  `transfers`, `transfer_lines`. Same columns as the database tables (see `data_dictionary.md`).
* Inputs: `movements_input.csv` (sorted by `received_at`, the feed order) and `counts_input.csv` (sorted by
  `submitted_at`). Load them through the application: `python -m data_tools.load --data <this folder> --db <file>`.
* `sql/seed_masters.sql` inserts the masters into a migrated database; `sql/stage_inputs.sqlite3.sql` stages the raw
  inputs for ad-hoc analysis with the sqlite3 CLI.
* **Evaluation only**: `ground_truth.csv` / `ground_truth.json`. Never load them into the application.
* `validation_summary.md`: checks run on these files and on the loaded database.
"""
    (data / "README.md").write_text(readme, encoding="utf-8")


if __name__ == "__main__":
    main()
