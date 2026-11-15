"""Reproducibility check: generate the same profile twice with the same seed and compare.

    python -m data_tools.repro --profile full --seed 20261022

Compares row counts and SHA-256 checksums of every output file (files are written in a deterministic
order). Also generates once with a different seed to show the comparison is sensitive. Writes
reproducibility_report.{json,md} (default: docs/).
"""
from __future__ import annotations

import argparse
import json
import shutil
import tempfile
import time
from pathlib import Path

from data_tools.generate import generate


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--profile", default="starter")
    ap.add_argument("--seed", type=int, default=20261022)
    ap.add_argument("--out", default="docs")
    a = ap.parse_args()
    tmp = Path(tempfile.mkdtemp(prefix="stashtrace-repro-"))
    try:
        runs = {}
        for name, seed in (("run_a", a.seed), ("run_b", a.seed), ("other_seed", a.seed + 1)):
            t = time.perf_counter()
            m = generate(a.profile, seed, tmp / name, quiet=True)
            runs[name] = {"seed": seed, "seconds": round(time.perf_counter() - t, 1), "files": m["files"]}
        files = sorted(runs["run_a"]["files"])
        rows = []
        for f in files:
            fa, fb, fo = (runs[k]["files"][f] for k in ("run_a", "run_b", "other_seed"))
            rows.append({"file": f, "rows": fa["rows"], "sha256_run_a": fa["sha256"], "sha256_run_b": fb["sha256"],
                         "identical": fa["sha256"] == fb["sha256"] and fa["rows"] == fb["rows"],
                         "differs_with_other_seed": fa["sha256"] != fo["sha256"]})
        report = {"profile": a.profile, "seed": a.seed, "all_identical": all(r["identical"] for r in rows),
                  "seconds": {k: v["seconds"] for k, v in runs.items()}, "files": rows}
        out = Path(a.out)
        out.mkdir(parents=True, exist_ok=True)
        (out / f"reproducibility_{a.profile}.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
        md = [f"# Reproducibility — profile `{a.profile}`, seed {a.seed}", "",
              f"Generated twice with the same seed: **{'IDENTICAL' if report['all_identical'] else 'DIFFERENT'}**. "
              f"Run times: {report['seconds']}. A run with seed {a.seed + 1} is included to show checksums are sensitive.", "",
              "| File | Rows | SHA-256 (run A) | Same in run B | Differs with other seed |", "|---|---|---|---|---|"]
        for r in rows:
            md.append(f"| {r['file']} | {r['rows'] if r['rows'] is not None else '–'} | `{r['sha256_run_a'][:16]}…` | "
                      f"{'yes' if r['identical'] else 'NO'} | {'yes' if r['differs_with_other_seed'] else 'no'} |")
        (out / f"reproducibility_{a.profile}.md").write_text("\n".join(md) + "\n", encoding="utf-8")
        print("\n".join(md))
        raise SystemExit(0 if report["all_identical"] else 1)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    main()
