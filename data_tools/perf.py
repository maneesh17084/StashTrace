"""Measure API latency (p50/p95) against a loaded database, in-process.

    python -m data_tools.perf --db var/full.db --data data/full --password <demo password>

Uses FastAPI's TestClient (ASGI in-process; no network hop), which isolates application and SQLite cost.
"""
from __future__ import annotations

import argparse
import json
import os
import platform
import statistics
import time
from pathlib import Path


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", required=True)
    ap.add_argument("--data", default=None, help="dataset dir (reads demo_logins from manifest.json)")
    ap.add_argument("--password", default=os.environ.get("STASHTRACE_DEMO_PASSWORD", "stashtrace-demo"))
    ap.add_argument("--n", type=int, default=30)
    ap.add_argument("--out", default="docs")
    a = ap.parse_args()
    from fastapi.testclient import TestClient

    from backend.app import db as dbmod
    dbmod.set_default_db(dbmod.Database(f"sqlite:///{Path(a.db).resolve().as_posix()}"))
    from backend.app.main import app
    c = TestClient(app)
    logins = (json.loads((Path(a.data) / "manifest.json").read_text())["demo_logins"] if a.data
              else ["EMP-000004", "EMP-000005"])
    ops = logins[1]
    mgr = logins[4] if len(logins) > 4 else logins[0]

    def tok(e):
        r = c.post("/api/v1/auth/login", json={"employee_id": e, "password": a.password})
        r.raise_for_status()
        return {"Authorization": f"Bearer {r.json()['token']}"}

    h_ops, h_mgr = tok(ops), tok(mgr)
    store = c.get("/api/v1/stores", headers=h_mgr).json()["items"][0]["store_id"]
    recs = c.get("/api/v1/reconciliations", headers=h_ops, params={"page_size": 50}).json()["items"]
    alerts = c.get("/api/v1/alerts", headers=h_ops, params={"page_size": 50}).json()["items"]
    targets = [
        ("GET /dashboard/summary (all stores)", lambda i: c.get("/api/v1/dashboard/summary", headers=h_ops)),
        ("GET /dashboard/summary (one store)", lambda i: c.get("/api/v1/dashboard/summary", headers=h_mgr)),
        ("GET /alerts (open queue, page 1)", lambda i: c.get("/api/v1/alerts", headers=h_ops, params={"status": "OPEN"})),
        ("GET /alerts/{id}", lambda i: c.get(f"/api/v1/alerts/{alerts[i % len(alerts)]['alert_id']}", headers=h_ops)),
        ("GET /reconciliations (store filter)",
         lambda i: c.get("/api/v1/reconciliations", headers=h_mgr, params={"store_id": store})),
        ("GET /reconciliations/{id}",
         lambda i: c.get(f"/api/v1/reconciliations/{recs[i % len(recs)]['recon_id']}", headers=h_ops)),
        ("GET /movements (store + sku)", lambda i: c.get("/api/v1/movements", headers=h_ops, params={
            "store_id": recs[i % len(recs)]["store_id"], "sku_id": recs[i % len(recs)]["sku_id"]})),
        ("GET /imports", lambda i: c.get("/api/v1/imports", headers=h_ops)),
        ("GET /investigations (open)", lambda i: c.get("/api/v1/investigations", headers=h_ops, params={"status": "OPEN"})),
    ]
    rows = []
    for name, fn in targets:
        fn(0)  # warm-up
        times = []
        for i in range(a.n):
            t = time.perf_counter()
            r = fn(i)
            times.append((time.perf_counter() - t) * 1000)
            assert r.status_code == 200, (name, r.status_code, r.text[:200])
        times.sort()
        rows.append({"endpoint": name, "n": a.n, "p50_ms": round(statistics.median(times), 1),
                     "p95_ms": round(times[int(0.95 * (len(times) - 1))], 1), "max_ms": round(times[-1], 1)})
    hw = {"platform": platform.platform(), "processor": platform.processor(), "cpu_count": os.cpu_count(),
          "python": platform.python_version(), "db_bytes": Path(a.db).stat().st_size}
    out = Path(a.out)
    out.mkdir(exist_ok=True)
    (out / "performance.json").write_text(json.dumps({"hardware": hw, "results": rows}, indent=2), encoding="utf-8")
    md = ["# API latency", "",
          f"Database `{a.db}` ({hw['db_bytes'] / 1e9:.2f} GB). Hardware: {hw['processor']}, {hw['cpu_count']} logical "
          f"CPUs, {hw['platform']}, Python {hw['python']}. In-process ASGI client, warm cache, {a.n} sequential "
          f"requests per endpoint.", "", "| Endpoint | p50 ms | p95 ms | max ms |", "|---|---|---|---|"]
    md += [f"| {r['endpoint']} | {r['p50_ms']} | {r['p95_ms']} | {r['max_ms']} |" for r in rows]
    (out / "performance.md").write_text("\n".join(md) + "\n", encoding="utf-8")
    print("\n".join(md))


if __name__ == "__main__":
    main()
