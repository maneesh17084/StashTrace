"""Alembic environment. Uses STASHTRACE_DB_URL (or the -x db_url=... override)."""
from __future__ import annotations

from alembic import context
from sqlalchemy import create_engine, event

from backend.app.config import settings
from backend.app.models import metadata

config = context.config
target_metadata = metadata


def _url() -> str:
    x = context.get_x_argument(as_dictionary=True)
    return x.get("db_url") or settings.db_url


def run_migrations_offline() -> None:
    context.configure(url=_url(), target_metadata=target_metadata, literal_binds=True, render_as_batch=True)
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    engine = create_engine(_url())

    @event.listens_for(engine, "connect")
    def _fk(dbapi_conn, _rec):
        dbapi_conn.execute("PRAGMA journal_mode=WAL")

    with engine.connect() as connection:
        context.configure(connection=connection, target_metadata=target_metadata, render_as_batch=True)
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
