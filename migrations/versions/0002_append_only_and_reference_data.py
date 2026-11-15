"""append-only triggers and reference data

Reference data is frozen here (copied from backend/app/catalog.py at revision time) so that
re-running migrations always produces the same database, even if the catalogue later changes.
A changed rule threshold must ship as a NEW rule_version in a new migration.

Revision ID: 0002
Revises: 0001
"""
from __future__ import annotations

import json

from alembic import op

revision = "0002"
down_revision = "0001"
branch_labels = None
depends_on = None

APPEND_ONLY = ["alert_evidence", "count_reconciliation", "customer_return", "cycle_count", "detection_run_alert", "import_raw_row", "import_row_result", "inventory_movement", "investigation_event", "shrinkage_alert"]

ROLES = json.loads("{\"CASHIER\": {\"name\": \"Cashier\", \"scope\": \"STORE\", \"actions\": [\"VIEW_STORE\", \"POST_SALE\", \"POST_RETURN\"]}, \"STOCK_CLERK\": {\"name\": \"Stock clerk\", \"scope\": \"STORE\", \"actions\": [\"VIEW_STORE\", \"POST_RECEIPT\", \"POST_TRANSFER\", \"POST_ADJUSTMENT\", \"SUBMIT_COUNT\"]}, \"STORE_MANAGER\": {\"name\": \"Store manager\", \"scope\": \"STORE\", \"actions\": [\"VIEW_STORE\", \"POST_RECEIPT\", \"POST_TRANSFER\", \"POST_ADJUSTMENT\", \"POST_REVERSAL\", \"APPROVE_ADJUSTMENT\", \"SUBMIT_COUNT\", \"IMPORT_BATCH\", \"INVESTIGATE\", \"EXPORT_DATA\"]}, \"LOSS_PREVENTION\": {\"name\": \"Loss prevention investigator\", \"scope\": \"REGION\", \"actions\": [\"VIEW_STORE\", \"INVESTIGATE\", \"ASSIGN_CASE\", \"RESOLVE_CASE\", \"EXPORT_DATA\"]}, \"OPS_MANAGER\": {\"name\": \"Operations manager\", \"scope\": \"ALL\", \"actions\": [\"VIEW_STORE\", \"VIEW_ALL_STORES\", \"APPROVE_ADJUSTMENT\", \"IMPORT_BATCH\", \"RUN_DETECTION\", \"ASSIGN_CASE\", \"INVESTIGATE\", \"RESOLVE_CASE\", \"EXPORT_DATA\", \"POST_REVERSAL\"]}, \"ANALYST\": {\"name\": \"Merchandising / e-commerce analyst\", \"scope\": \"ALL\", \"actions\": [\"VIEW_STORE\", \"VIEW_ALL_STORES\", \"RUN_DETECTION\", \"EXPORT_DATA\"]}, \"SERVICE_FEED\": {\"name\": \"Automated feed (service identity)\", \"scope\": \"ALL\", \"actions\": [\"VIEW_STORE\", \"POST_SALE\", \"POST_RETURN\", \"POST_RECEIPT\", \"POST_TRANSFER\", \"POST_REVERSAL\", \"IMPORT_BATCH\"]}, \"SYSTEM\": {\"name\": \"StashTrace detection service\", \"scope\": \"ALL\", \"actions\": [\"VIEW_STORE\", \"VIEW_ALL_STORES\", \"RUN_DETECTION\"]}, \"ADMIN\": {\"name\": \"Administrator\", \"scope\": \"ALL\", \"actions\": [\"VIEW_STORE\", \"VIEW_ALL_STORES\", \"POST_SALE\", \"POST_RETURN\", \"POST_RECEIPT\", \"POST_TRANSFER\", \"POST_ADJUSTMENT\", \"POST_REVERSAL\", \"APPROVE_ADJUSTMENT\", \"SUBMIT_COUNT\", \"IMPORT_BATCH\", \"RUN_DETECTION\", \"ASSIGN_CASE\", \"INVESTIGATE\", \"RESOLVE_CASE\", \"EXPORT_DATA\"]}}")

RULES = json.loads("[{\"rule_code\": \"COUNT_TIMING\", \"rule_version\": 1, \"alert_class\": \"DATA_QUALITY\", \"description\": \"Count basis is unreliable: the declared cutoff is later than the physical count (beyond tolerance) or the count was submitted before it was performed. Routes the count to timing review; no shrinkage alert is raised for it.\", \"params\": {\"tolerance_minutes\": 15}}, {\"rule_code\": \"UNEXPLAINED_SHORTAGE\", \"rule_version\": 1, \"alert_class\": \"SHRINKAGE\", \"description\": \"Current reconciliation of a reliable count shows a shortage that remains after subtracting quarantined (unposted) outbound rows for the same store/SKU before the cutoff. Raised when the remaining shortage >= threshold_units.\", \"params\": {\"threshold_units\": 5, \"high_severity_value_cents\": 20000, \"medium_severity_units\": 10}}, {\"rule_code\": \"REPEATED_NEG_ADJ\", \"rule_version\": 1, \"alert_class\": \"PATTERN\", \"description\": \"At one store/SKU, at least min_count discretionary negative adjustments (DAMAGE, EXPIRED, SAMPLING, OTHER) totalling >= min_units within window_days.\", \"params\": {\"window_days\": 14, \"min_count\": 3, \"min_units\": 6, \"reasons\": [\"DAMAGE\", \"EXPIRED\", \"SAMPLING\", \"OTHER\"]}}, {\"rule_code\": \"TRANSFER_RECON\", \"rule_version\": 1, \"alert_class\": \"TRANSFER\", \"description\": \"Per transfer/SKU: shortfall = dispatched - received. A transit write-off (TRANSFER_SHORTAGE) booked at both ends, or write-offs exceeding the shortfall, is a double count. A shortfall not written off once expected_arrival + grace_days has passed is an overdue discrepancy. One case per transfer, linking both ends.\", \"params\": {\"grace_days\": 7}}]")

SEQUENCES = ["alert", "batch", "count", "investigation", "movement", "recon", "return", "run"]

EFFECTIVE_FROM = "2026-01-01T00:00:00Z"


def upgrade() -> None:
    for table in APPEND_ONLY:
        for op_name in ("UPDATE", "DELETE"):
            op.execute(
                f"CREATE TRIGGER trg_{table}_no_{op_name.lower()} BEFORE {op_name} ON {table} "
                f"BEGIN SELECT RAISE(ABORT, '{table} is append-only'); END;"
            )
    conn = op.get_bind()
    for code, role in ROLES.items():
        conn.exec_driver_sql(
            "INSERT INTO employee_role (role_code, role_name, default_scope) VALUES (?, ?, ?)",
            (code, role["name"], role["scope"]),
        )
        for action in role["actions"]:
            conn.exec_driver_sql("INSERT INTO role_permission (role_code, action) VALUES (?, ?)", (code, action))
    for rule in RULES:
        conn.exec_driver_sql(
            "INSERT INTO rule_definition (rule_code, rule_version, alert_class, description, params_json, is_active, effective_from)"
            " VALUES (?, ?, ?, ?, ?, 1, ?)",
            (rule["rule_code"], rule["rule_version"], rule["alert_class"], rule["description"],
             json.dumps(rule["params"], sort_keys=True), EFFECTIVE_FROM),
        )
    for name in SEQUENCES:
        conn.exec_driver_sql("INSERT INTO id_sequence (name, last_value) VALUES (?, 0)", (name,))


def downgrade() -> None:
    for table in APPEND_ONLY:
        for op_name in ("update", "delete"):
            op.execute(f"DROP TRIGGER IF EXISTS trg_{table}_no_{op_name}")
    for table in ("id_sequence", "rule_definition", "role_permission", "employee_role"):
        op.execute(f"DELETE FROM {table}")
