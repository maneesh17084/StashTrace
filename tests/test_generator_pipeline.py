"""Generator determinism, ground-truth separation and a tiny end-to-end evaluation."""
import json
import shutil

from data_tools import evaluate, load as loader, validate
from data_tools.generate import generate


def test_same_seed_same_bytes_different_seed_differs(tmp_path):
    a = generate("tiny", 20261022, tmp_path / "a", quiet=True)["files"]
    b = generate("tiny", 20261022, tmp_path / "b", quiet=True)["files"]
    c = generate("tiny", 7, tmp_path / "c", quiet=True)["files"]
    assert {k: v["sha256"] for k, v in a.items()} == {k: v["sha256"] for k, v in b.items()}
    assert a["movements_input.csv"]["sha256"] != c["movements_input.csv"]["sha256"]
    assert a["movements_input.csv"]["rows"] == 8000 and a["counts_input.csv"]["rows"] == 300


def test_tiny_pipeline_without_ground_truth_then_evaluate(tmp_path, monkeypatch):
    data = tmp_path / "tiny"
    generate("tiny", 20261022, data, quiet=True)
    # Prove the loader never needs labels: hide them during the load.
    hidden = tmp_path / "hidden"
    hidden.mkdir()
    for f in ("ground_truth.csv", "ground_truth.json"):
        shutil.move(str(data / f), str(hidden / f))
    db = tmp_path / "tiny.db"
    monkeypatch.setattr("sys.argv", ["load", "--data", str(data), "--db", str(db), "--fresh", "--password", "x"])
    loader.main()
    for f in ("ground_truth.csv", "ground_truth.json"):
        shutil.move(str(hidden / f), str(data / f))
    rep = validate.Report()
    validate.validate_files(data, rep)
    validate.validate_db(db, data, rep)
    failed = [c for c in rep.checks if c["status"] != "PASS"]
    assert failed == []
    ev = evaluate.evaluate(data, db)
    assert ev["count"]["reconciliation_matches_oracle"]["mismatches"] == 0
    assert all(v["correct"] == v["n"] for v in ev["movement"]["by_scenario"].values())
    assert ev["count"]["rules"]["COUNT_TIMING"]["recall"] == 1.0
    assert ev["count"]["rules"]["UNEXPLAINED_SHORTAGE"]["recall"] >= 0.9
    assert ev["transfer"]["rules"]["TRANSFER_RECON"]["recall"] == 1.0
    report = json.loads((data / "load_report.json").read_text())
    assert report["detection_runs"][1]["alerts_new"] == 0  # rerun opens no duplicate cases
