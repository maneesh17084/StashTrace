"""Versioned, explainable detection rules.

Each run evaluates the ledger up to a fixed watermark and ``as_of`` horizon, then for every finding
builds a *stable case key*. A key seen before is recorded as REDETECTED (no new alert or case);
a new key creates one alert, its evidence rows, and one investigation in OPEN state.

Order of evaluation matters for explainability: data-quality findings (timing) are checked before a
count is allowed to become a shrinkage finding, and quarantined rows are subtracted as known
explanations before a shortage is called unexplained.
"""
from __future__ import annotations

import json
import logging
import time
from collections import defaultdict
from datetime import timedelta

from sqlalchemy import text
from sqlalchemy.engine import Connection

from ..db import Database
from .common import DomainError, allocate_ids, fmt_ts, parse_ts, utcnow
from .reconciliation import current_recon_sql, current_watermark

log = logging.getLogger("stashtrace.detection")

ORIGIN = {"SHRINKAGE": "COUNT", "PATTERN": "MOVEMENT_PATTERN", "TRANSFER": "TRANSFER", "DATA_QUALITY": "DATA_QUALITY"}


def active_rules(conn: Connection) -> dict[str, dict]:
    out = {}
    for r in conn.execute(text(
            "SELECT rule_code, rule_version, alert_class, params_json FROM rule_definition WHERE is_active=1"
            " ORDER BY rule_code, rule_version")):
        out[r.rule_code] = {"rule_code": r.rule_code, "rule_version": r.rule_version, "alert_class": r.alert_class,
                            "params": json.loads(r.params_json)}
    return out


def queue_run(db: Database, requested_by: str, as_of: str | None = None, enqueue: bool = True) -> dict:
    now = utcnow()
    with db.write() as conn:
        rules = active_rules(conn)
        run_id = allocate_ids(conn, "run")[0]
        conn.execute(text("INSERT INTO detection_run (run_id, requested_by, requested_at, as_of, rules_json)"
                          " VALUES (:r, :u, :t, :a, :j)"),
                     {"r": run_id, "u": requested_by, "t": now, "a": as_of, "j": json.dumps(rules, sort_keys=True)})
        conn.execute(text("INSERT INTO detection_run_status (run_id, status, alerts_new, alerts_redetected)"
                          " VALUES (:r, 'QUEUED', 0, 0)"), {"r": run_id})
        job_id = None
        if enqueue:
            job_id = conn.execute(text("INSERT INTO job (job_type, ref_id, status, attempts, created_at)"
                                       " VALUES ('DETECTION', :r, 'QUEUED', 0, :t) RETURNING job_id"),
                                  {"r": run_id, "t": now}).scalar_one()
    return {"run_id": run_id, "job_id": job_id, "status": "QUEUED"}


# ------------------------------------------------------------------ rules
def _timing_findings(conn: Connection, rule: dict) -> list[dict]:
    out = []
    q = text(f"""
        SELECT r.recon_id, r.count_id, r.basis_notes, r.expected_qty, r.counted_qty, r.variance_qty, r.version,
               c.store_id, c.sku_id, c.counted_at, c.count_cutoff_at, c.submitted_at
        FROM count_reconciliation r JOIN cycle_count c ON c.count_id = r.count_id
        WHERE {current_recon_sql('r')} AND r.basis_status = 'TIMING_REVIEW'
        ORDER BY r.count_id""")
    for r in conn.execute(q):
        notes = json.loads(r.basis_notes or "[]")
        lo, hi = sorted([r.counted_at, r.count_cutoff_at])
        between = conn.execute(text(
            "SELECT movement_id, movement_type, stock_qty, occurred_at FROM inventory_movement WHERE store_id=:st"
            " AND sku_id=:sk AND occurred_at > :lo AND occurred_at <= :hi AND stock_qty <> 0 ORDER BY occurred_at LIMIT 25"),
            {"st": r.store_id, "sk": r.sku_id, "lo": lo, "hi": hi}).all()
        codes = ", ".join(n["code"] for n in notes)
        reason = (f"Count {r.count_id} cannot be trusted as a shrinkage basis ({codes}): counted_at {r.counted_at}, "
                  f"cutoff {r.count_cutoff_at}, submitted {r.submitted_at}. "
                  f"{len(between)} stock movement(s) fall between the count and its cutoff. "
                  f"Shrinkage evaluation is suspended until the count basis is corrected.")
        ev = [{"evidence_type": "COUNT", "evidence_role": "SUBJECT", "count_id": r.count_id, "sku_id": r.sku_id},
              {"evidence_type": "RECONCILIATION", "evidence_role": "SUBJECT", "recon_id": r.recon_id, "count_id": r.count_id,
               "qty": r.variance_qty, "note": f"version {r.version}"}]
        ev += [{"evidence_type": "MOVEMENT", "evidence_role": "BETWEEN_COUNT_AND_CUTOFF", "movement_id": m.movement_id,
                "sku_id": r.sku_id, "qty": m.stock_qty, "note": f"{m.movement_type} at {m.occurred_at}"} for m in between]
        out.append({
            "case_key": f"COUNT_TIMING|{r.count_id}", "rule": rule, "alert_type": "COUNT_TIMING_REVIEW",
            "severity": "MEDIUM", "store_id": r.store_id, "sku_id": r.sku_id, "count_id": r.count_id,
            "recon_id": r.recon_id, "threshold_value": rule["params"]["tolerance_minutes"],
            "observed_value": len(between),
            "calculation": {"violations": notes, "counted_at": r.counted_at, "count_cutoff_at": r.count_cutoff_at,
                            "submitted_at": r.submitted_at, "movements_between": len(between)},
            "reason_text": reason, "evidence": ev,
        })
    return out


def _quarantine_explanations(conn: Connection) -> dict[tuple[str, str], list]:
    expl: dict[tuple[str, str], list] = defaultdict(list)
    q = text("""
        SELECT x.row_id, x.authoritative_store_id, x.sku_id, x.occurred_at, x.explain_qty, x.error_code
        FROM import_row_result x
        WHERE x.outcome = 'QUARANTINED' AND x.explain_qty <> 0 AND x.authoritative_store_id IS NOT NULL
          AND NOT EXISTS (SELECT 1 FROM import_row_result y WHERE y.corrects_row_id = x.row_id)
        ORDER BY x.row_id""")
    for r in conn.execute(q):
        expl[(r.authoritative_store_id, r.sku_id)].append(r)
    return expl


def _shortage_findings(conn: Connection, rule: dict) -> list[dict]:
    p = rule["params"]
    threshold = int(p["threshold_units"])
    expl = _quarantine_explanations(conn)
    out = []
    q = text(f"""
        SELECT r.*, c.store_id, c.sku_id, c.count_cutoff_at, c.counted_at, k.unit_cost_cents
        FROM count_reconciliation r JOIN cycle_count c ON c.count_id = r.count_id JOIN sku k ON k.sku_id = c.sku_id
        WHERE {current_recon_sql('r')} AND r.basis_status = 'RELIABLE' AND r.variance_qty < 0
        ORDER BY r.count_id""")
    for r in conn.execute(q).all():
        shortage = -r.variance_qty
        rows = [e for e in expl.get((r.store_id, r.sku_id), []) if e.occurred_at <= r.count_cutoff_at]
        explained = sum(e.explain_qty for e in rows)
        remaining = shortage - explained
        if remaining < threshold:
            continue
        value = remaining * r.unit_cost_cents
        severity = "HIGH" if value >= p["high_severity_value_cents"] else ("MEDIUM" if remaining >= p["medium_severity_units"] else "LOW")
        calc = {
            "formula": "expected = opening + receipts + transfer_in + restocked_returns - sales - transfer_out + adjustments",
            "opening_qty": r.opening_qty, "receipts_qty": r.receipts_qty, "transfer_in_qty": r.transfer_in_qty,
            "restocked_returns_qty": r.restocked_returns_qty, "sales_qty": r.sales_qty,
            "transfer_out_qty": r.transfer_out_qty, "adjustments_qty": r.adjustments_qty,
            "expected_qty": r.expected_qty, "counted_qty": r.counted_qty, "variance_qty": r.variance_qty,
            "shortage_qty": shortage, "explained_by_quarantined_rows_qty": explained,
            "remaining_shortage_qty": remaining, "threshold_units": threshold, "unit_cost_cents": r.unit_cost_cents,
            "remaining_value_cents": value, "recon_version": r.version, "ledger_watermark_seq": r.ledger_watermark_seq,
        }
        reason = (f"Count {r.count_id} found {r.counted_qty} units against an expected {r.expected_qty} "
                  f"(variance {r.variance_qty}). {explained} unit(s) are explained by quarantined rows; "
                  f"{remaining} unit(s) (${value / 100:,.2f}) remain unexplained, meeting the "
                  f"{threshold}-unit threshold.")
        ev = [{"evidence_type": "RECONCILIATION", "evidence_role": "SUBJECT", "recon_id": r.recon_id,
               "count_id": r.count_id, "sku_id": r.sku_id, "qty": r.variance_qty, "note": f"version {r.version}"},
              {"evidence_type": "COUNT", "evidence_role": "SUBJECT", "count_id": r.count_id, "sku_id": r.sku_id,
               "qty": r.counted_qty}]
        ev += [{"evidence_type": "IMPORT_ROW", "evidence_role": "EXPLANATION", "import_row_id": e.row_id,
                "sku_id": r.sku_id, "qty": e.explain_qty, "note": f"quarantined: {e.error_code}"} for e in rows]
        recent = conn.execute(text(
            "SELECT movement_id, movement_type, stock_qty, occurred_at FROM inventory_movement WHERE store_id=:st"
            " AND sku_id=:sk AND occurred_at <= :c AND ledger_seq <= :wm AND stock_qty <> 0"
            " ORDER BY occurred_at DESC LIMIT 15"),
            {"st": r.store_id, "sk": r.sku_id, "c": r.count_cutoff_at, "wm": r.ledger_watermark_seq}).all()
        ev += [{"evidence_type": "MOVEMENT", "evidence_role": "CONTRIBUTING", "movement_id": m.movement_id,
                "sku_id": r.sku_id, "qty": m.stock_qty, "note": f"{m.movement_type} at {m.occurred_at}"} for m in recent]
        out.append({
            "case_key": f"SHORTAGE|{r.count_id}", "rule": rule, "alert_type": "UNEXPLAINED_SHORTAGE",
            "severity": severity, "store_id": r.store_id, "sku_id": r.sku_id, "count_id": r.count_id,
            "recon_id": r.recon_id, "threshold_value": threshold, "observed_value": remaining,
            "calculation": calc, "reason_text": reason, "evidence": ev,
        })
    return out


def _adjustment_findings(conn: Connection, rule: dict, as_of: str, wm: int) -> list[dict]:
    p = rule["params"]
    reasons = list(p["reasons"])
    placeholders = ",".join(f":r{i}" for i in range(len(reasons)))
    params = {f"r{i}": v for i, v in enumerate(reasons)}
    params.update({"as_of": as_of, "wm": wm})
    q = text(f"""
        SELECT m.movement_id, m.store_id, m.sku_id, m.quantity, m.occurred_at, m.reason_code, m.actor_id, m.approved_by
        FROM inventory_movement m
        WHERE m.movement_type = 'ADJUSTMENT' AND m.stock_qty < 0 AND m.reason_code IN ({placeholders})
          AND m.occurred_at <= :as_of AND m.ledger_seq <= :wm
          AND NOT EXISTS (SELECT 1 FROM inventory_movement v WHERE v.reverses_movement_id = m.movement_id)
        ORDER BY m.store_id, m.sku_id, m.occurred_at, m.ledger_seq""")
    by_pair: dict[tuple[str, str], list] = defaultdict(list)
    for r in conn.execute(q, params):
        by_pair[(r.store_id, r.sku_id)].append(r)
    window = timedelta(days=int(p["window_days"]))
    out = []
    for (store, sku), adjs in by_pair.items():
        i = 0
        while i < len(adjs):
            end = fmt_ts(parse_ts(adjs[i].occurred_at) + window)
            j = i
            while j + 1 < len(adjs) and adjs[j + 1].occurred_at <= end:
                j += 1
            cluster = adjs[i:j + 1]
            units = sum(-a.quantity for a in cluster)
            if len(cluster) >= p["min_count"] and units >= p["min_units"]:
                actors = sorted({a.actor_id for a in cluster})
                approvers = sorted({a.approved_by for a in cluster})
                reason = (f"{len(cluster)} negative adjustments totalling {units} units at {store}/{sku} between "
                          f"{cluster[0].occurred_at} and {cluster[-1].occurred_at} (rule: >= {p['min_count']} "
                          f"adjustments and >= {p['min_units']} units within {p['window_days']} days). "
                          f"Posted by {', '.join(actors)}; approved by {', '.join(approvers)}.")
                out.append({
                    "case_key": f"ADJ_PATTERN|{store}|{sku}|{cluster[0].movement_id}", "rule": rule,
                    "alert_type": "REPEATED_NEGATIVE_ADJUSTMENTS",
                    "severity": "HIGH" if len(actors) == 1 and len(cluster) >= p["min_count"] + 1 else "MEDIUM",
                    "store_id": store, "sku_id": sku, "window_start": cluster[0].occurred_at,
                    "window_end": cluster[-1].occurred_at, "threshold_value": int(p["min_units"]),
                    "observed_value": units,
                    "calculation": {"window_days": p["window_days"], "min_count": p["min_count"],
                                    "min_units": p["min_units"], "adjustment_count": len(cluster), "units": units,
                                    "reasons": sorted({a.reason_code for a in cluster}), "actors": actors,
                                    "approvers": approvers},
                    "reason_text": reason,
                    "evidence": [{"evidence_type": "MOVEMENT", "evidence_role": "CONTRIBUTING",
                                  "movement_id": a.movement_id, "sku_id": sku, "qty": a.quantity,
                                  "note": f"{a.reason_code} by {a.actor_id}, approved by {a.approved_by} at {a.occurred_at}"}
                                 for a in cluster],
                })
                i = j + 1
            else:
                i += 1
    return out


def _transfer_findings(conn: Connection, rule: dict, as_of: str, wm: int) -> list[dict]:
    grace = timedelta(days=int(rule["params"]["grace_days"]))
    q = text("""
        SELECT t.transfer_id, t.from_store_id, t.to_store_id, t.expected_arrival_at, m.sku_id, m.movement_id,
               m.movement_type, m.store_id, m.quantity, m.reason_code, m.occurred_at
        FROM inventory_movement m JOIN transfer t ON t.transfer_id = m.transfer_id
        WHERE m.transfer_id IS NOT NULL AND m.occurred_at <= :as_of AND m.ledger_seq <= :wm
          AND NOT EXISTS (SELECT 1 FROM inventory_movement v WHERE v.reverses_movement_id = m.movement_id)
        ORDER BY t.transfer_id, m.sku_id, m.occurred_at""")
    lines: dict[str, dict] = {}
    for r in conn.execute(q, {"as_of": as_of, "wm": wm}):
        t = lines.setdefault(r.transfer_id, {"from": r.from_store_id, "to": r.to_store_id,
                                             "expected": r.expected_arrival_at, "skus": defaultdict(list)})
        t["skus"][r.sku_id].append(r)
    out = []
    for tid, t in lines.items():
        overdue_after = fmt_ts(parse_ts(t["expected"]) + grace)
        flagged, evidence, calc_lines = [], [], []
        any_double = False
        observed = 0
        for sku, ms in t["skus"].items():
            shipped = sum(m.quantity for m in ms if m.movement_type == "TRANSFER_OUT")
            received = sum(m.quantity for m in ms if m.movement_type == "TRANSFER_IN")
            wo_from = sum(-m.quantity for m in ms if m.reason_code == "TRANSFER_SHORTAGE" and m.store_id == t["from"])
            wo_to = sum(-m.quantity for m in ms if m.reason_code == "TRANSFER_SHORTAGE" and m.store_id == t["to"])
            if shipped == 0:
                continue  # not dispatched yet: nothing in transit
            shortfall = shipped - received
            written_off = wo_from + wo_to
            open_remainder = shortfall - written_off
            double = (wo_from > 0 and wo_to > 0) or written_off > max(shortfall, 0)
            overdue = open_remainder != 0 and as_of > overdue_after
            calc_lines.append({"sku_id": sku, "dispatched": shipped, "received": received, "shortfall": shortfall,
                               "written_off_at_sender": wo_from, "written_off_at_receiver": wo_to,
                               "open_in_transit": open_remainder, "double_count": double, "overdue": overdue})
            if double or overdue:
                flagged.append(sku)
                any_double = any_double or double
                observed += (written_off - max(shortfall, 0)) if double else abs(open_remainder)
                for m in ms:
                    role = {"TRANSFER_OUT": "DISPATCH", "TRANSFER_IN": "RECEIPT"}.get(m.movement_type, "WRITE_OFF")
                    evidence.append({"evidence_type": "MOVEMENT", "evidence_role": role, "movement_id": m.movement_id,
                                     "transfer_id": tid, "sku_id": sku, "qty": m.quantity,
                                     "note": f"{m.movement_type} {m.reason_code or ''} at {m.store_id} {m.occurred_at}".strip()})
                evidence.append({"evidence_type": "TRANSFER_LINE", "evidence_role": "SUBJECT", "transfer_id": tid,
                                 "sku_id": sku, "qty": open_remainder})
        if not flagged:
            continue
        atype = "TRANSFER_DOUBLE_COUNT" if any_double else "TRANSFER_OVERDUE_DISCREPANCY"
        if any_double:
            reason = (f"Transfer {tid} ({t['from']} -> {t['to']}): the in-transit loss on {', '.join(flagged)} is "
                      f"booked more than once (write-offs at both ends or exceeding the dispatch/receipt shortfall). "
                      f"{observed} unit(s) of loss are double-counted.")
        else:
            reason = (f"Transfer {tid} ({t['from']} -> {t['to']}): {observed} unit(s) on {', '.join(flagged)} remain "
                      f"unreconciled after expected arrival {t['expected']} + {rule['params']['grace_days']} days grace.")
        out.append({
            "case_key": f"TRANSFER|{tid}", "rule": rule, "alert_type": atype,
            "severity": "HIGH" if any_double else "MEDIUM", "store_id": t["to"], "related_store_id": t["from"],
            "sku_id": flagged[0] if len(flagged) == 1 else None, "transfer_id": tid,
            "threshold_value": 0, "observed_value": observed,
            "calculation": {"grace_days": rule["params"]["grace_days"], "expected_arrival_at": t["expected"],
                            "overdue_after": overdue_after, "as_of": as_of, "lines": calc_lines},
            "reason_text": reason, "evidence": evidence,
        })
    return out


# -------------------------------------------------------------- persistence
def _system_actor(conn: Connection, fallback: str) -> tuple[str, str]:
    r = conn.execute(text("SELECT employee_id FROM role_assignment WHERE role_code='SYSTEM' ORDER BY assignment_id LIMIT 1")).first()
    if r:
        return r.employee_id, "SYSTEM"
    return fallback, "ADMIN"


def _persist(conn: Connection, run_id: str, findings: list[dict], requested_by: str) -> tuple[int, int]:
    existing = {r.case_key: r.alert_id for r in conn.execute(text("SELECT case_key, alert_id FROM shrinkage_alert"))}
    new = [f for f in findings if f["case_key"] not in existing]
    seen = [f for f in findings if f["case_key"] in existing]
    now = utcnow()
    actor, actor_role = _system_actor(conn, requested_by)
    alert_ids = allocate_ids(conn, "alert", len(new))
    inv_ids = allocate_ids(conn, "investigation", len(new))
    inv_rows, state_rows, event_rows, alert_rows, ev_rows, run_rows = [], [], [], [], [], []
    for f, aid, iid in zip(new, alert_ids, inv_ids):
        rule = f["rule"]
        inv_rows.append({"i": iid, "k": f["case_key"], "o": ORIGIN[rule["alert_class"]], "s": f["store_id"],
                         "rs": f.get("related_store_id"), "t": now})
        state_rows.append({"i": iid, "t": now})
        event_rows.append({"i": iid, "a": actor, "ar": actor_role, "t": now, "r": run_id,
                           "n": f"Opened from {aid}: {f['alert_type']}"})
        alert_rows.append({
            "alert_id": aid, "case_key": f["case_key"], "rule_code": rule["rule_code"],
            "rule_version": rule["rule_version"], "alert_class": rule["alert_class"], "alert_type": f["alert_type"],
            "severity": f["severity"], "store_id": f["store_id"], "related_store_id": f.get("related_store_id"),
            "sku_id": f.get("sku_id"), "count_id": f.get("count_id"), "recon_id": f.get("recon_id"),
            "transfer_id": f.get("transfer_id"), "window_start": f.get("window_start"),
            "window_end": f.get("window_end"), "threshold_value": f.get("threshold_value"),
            "observed_value": f.get("observed_value"), "calculation_json": json.dumps(f["calculation"], sort_keys=True),
            "reason_text": f["reason_text"], "first_run_id": run_id, "investigation_id": iid, "created_at": now,
        })
        for e in f["evidence"]:
            ev_rows.append({"alert_id": aid, "evidence_type": e["evidence_type"], "evidence_role": e["evidence_role"],
                            "movement_id": e.get("movement_id"), "count_id": e.get("count_id"),
                            "recon_id": e.get("recon_id"), "import_row_id": e.get("import_row_id"),
                            "transfer_id": e.get("transfer_id"), "sku_id": e.get("sku_id"), "qty": e.get("qty"),
                            "note": e.get("note")})
        run_rows.append({"r": run_id, "a": aid, "o": "NEW", "v": f.get("observed_value")})
    for f in seen:
        run_rows.append({"r": run_id, "a": existing[f["case_key"]], "o": "REDETECTED", "v": f.get("observed_value")})
    if inv_rows:
        conn.execute(text("INSERT INTO investigation (investigation_id, case_key, origin_type, store_id, related_store_id,"
                          " opened_at) VALUES (:i, :k, :o, :s, :rs, :t)"), inv_rows)
        conn.execute(text("INSERT INTO investigation_state (investigation_id, status, last_seq, updated_at)"
                          " VALUES (:i, 'OPEN', 1, :t)"), state_rows)
        conn.execute(text("INSERT INTO investigation_event (investigation_id, seq, event_type, from_status, to_status,"
                          " actor_id, actor_role_code, note, run_id, occurred_at) VALUES (:i, 1, 'OPENED', NULL, 'OPEN',"
                          " :a, :ar, :n, :r, :t)"), event_rows)
        conn.execute(text(
            "INSERT INTO shrinkage_alert (alert_id, case_key, rule_code, rule_version, alert_class, alert_type, severity,"
            " store_id, related_store_id, sku_id, count_id, recon_id, transfer_id, window_start, window_end,"
            " threshold_value, observed_value, calculation_json, reason_text, first_run_id, investigation_id, created_at)"
            " VALUES (:alert_id, :case_key, :rule_code, :rule_version, :alert_class, :alert_type, :severity, :store_id,"
            " :related_store_id, :sku_id, :count_id, :recon_id, :transfer_id, :window_start, :window_end,"
            " :threshold_value, :observed_value, :calculation_json, :reason_text, :first_run_id, :investigation_id,"
            " :created_at)"), alert_rows)
    if ev_rows:
        conn.execute(text(
            "INSERT INTO alert_evidence (alert_id, evidence_type, evidence_role, movement_id, count_id, recon_id,"
            " import_row_id, transfer_id, sku_id, qty, note) VALUES (:alert_id, :evidence_type, :evidence_role,"
            " :movement_id, :count_id, :recon_id, :import_row_id, :transfer_id, :sku_id, :qty, :note)"), ev_rows)
    if run_rows:
        conn.execute(text("INSERT INTO detection_run_alert (run_id, alert_id, outcome, observed_value)"
                          " VALUES (:r, :a, :o, :v)"), run_rows)
    return len(new), len(seen)


def execute_run(db: Database, run_id: str) -> dict:
    t0 = time.perf_counter()
    with db.write() as conn:
        run = conn.execute(text("SELECT * FROM detection_run WHERE run_id=:r"), {"r": run_id}).first()
        if run is None:
            raise DomainError("NOT_FOUND", f"Run {run_id} not found", 404)
        st = conn.execute(text("SELECT status FROM detection_run_status WHERE run_id=:r"), {"r": run_id}).scalar_one()
        if st == "COMPLETED":
            return {"run_id": run_id, "status": "COMPLETED", "skipped": True}
        conn.execute(text("UPDATE detection_run_status SET status='RUNNING', started_at=:t WHERE run_id=:r"),
                     {"r": run_id, "t": utcnow()})
    try:
        with db.write() as conn:
            rules = json.loads(run.rules_json)
            wm = current_watermark(conn)
            as_of = run.as_of or conn.execute(text("SELECT COALESCE(MAX(occurred_at), :n) FROM inventory_movement"),
                                              {"n": utcnow()}).scalar_one()
            findings: list[dict] = []
            if "COUNT_TIMING" in rules:
                findings += _timing_findings(conn, rules["COUNT_TIMING"])
            if "UNEXPLAINED_SHORTAGE" in rules:
                findings += _shortage_findings(conn, rules["UNEXPLAINED_SHORTAGE"])
            if "REPEATED_NEG_ADJ" in rules:
                findings += _adjustment_findings(conn, rules["REPEATED_NEG_ADJ"], as_of, wm)
            if "TRANSFER_RECON" in rules:
                findings += _transfer_findings(conn, rules["TRANSFER_RECON"], as_of, wm)
            n_new, n_seen = _persist(conn, run_id, findings, run.requested_by)
            ms = int((time.perf_counter() - t0) * 1000)
            conn.execute(text(
                "UPDATE detection_run_status SET status='COMPLETED', finished_at=:t, ledger_watermark_seq=:wm,"
                " alerts_new=:n, alerts_redetected=:s, duration_ms=:ms WHERE run_id=:r"),
                {"t": utcnow(), "wm": wm, "n": n_new, "s": n_seen, "ms": ms, "r": run_id})
    except Exception as exc:
        with db.write() as conn:
            conn.execute(text("UPDATE detection_run_status SET status='FAILED', finished_at=:t, error_message=:m"
                              " WHERE run_id=:r"), {"t": utcnow(), "m": str(exc)[:2000], "r": run_id})
        raise
    log.info("run %s: %s new, %s redetected in %sms", run_id, n_new, n_seen, ms)
    return {"run_id": run_id, "status": "COMPLETED", "alerts_new": n_new, "alerts_redetected": n_seen,
            "duration_ms": ms, "as_of": as_of, "ledger_watermark_seq": wm}
