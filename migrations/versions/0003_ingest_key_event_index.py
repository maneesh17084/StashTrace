"""index ingest_key by source_event_id

Import lookups batch-fetch idempotency keys by source_event_id; without this index each chunk scanned
the whole table, making large imports quadratic.

Revision ID: 0003
Revises: 0002
"""
from __future__ import annotations

from alembic import op

revision = "0003"
down_revision = "0002"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_index("ix_ingest_key_event", "ingest_key", ["source_event_id"])


def downgrade() -> None:
    op.drop_index("ix_ingest_key_event", table_name="ingest_key")
