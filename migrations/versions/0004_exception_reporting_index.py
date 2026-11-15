"""covering index for exception reporting

Dashboard and import screens aggregate quarantined rows by error code and store; at 1M rows a full
scan of import_row_result took ~0.5 s. This covering index serves both aggregates.

Revision ID: 0004
Revises: 0003
"""
from __future__ import annotations

from alembic import op

revision = "0004"
down_revision = "0003"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_index("ix_import_row_result_exceptions", "import_row_result",
                    ["outcome", "error_code", "submitted_store_id", "occurred_at"])


def downgrade() -> None:
    op.drop_index("ix_import_row_result_exceptions", table_name="import_row_result")
