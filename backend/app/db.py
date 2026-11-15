"""SQLite engine setup.

SQLite specifics that replace what PostgreSQL would give us:
* WAL journal mode so API reads proceed while the worker writes.
* ``PRAGMA foreign_keys=ON`` on every connection (SQLite defaults it off).
* Write transactions start with ``BEGIN IMMEDIATE`` (via ``write_engine``). SQLite allows one
  writer at a time, so this takes the write lock up front: postings for the same store/SKU are
  serialised, and a reader never has to upgrade to a writer mid-transaction (no SQLITE_BUSY deadlock).
"""
from __future__ import annotations

from contextlib import contextmanager
from typing import Iterator

from sqlalchemy import create_engine, event
from sqlalchemy.engine import Connection, Engine

from .config import settings

_engines: dict[str, tuple[Engine, Engine]] = {}


def _configure(engine: Engine) -> None:
    @event.listens_for(engine, "connect")
    def _on_connect(dbapi_conn, _record):  # pragma: no cover - driver hook
        dbapi_conn.isolation_level = None  # let us emit BEGIN ourselves
        cur = dbapi_conn.cursor()
        cur.execute("PRAGMA foreign_keys=ON")
        cur.execute("PRAGMA journal_mode=WAL")
        cur.execute("PRAGMA synchronous=NORMAL")
        cur.execute("PRAGMA busy_timeout=60000")
        cur.execute("PRAGMA temp_store=MEMORY")
        cur.execute(f"PRAGMA cache_size=-{settings.sqlite_cache_mb * 1024}")
        cur.execute(f"PRAGMA mmap_size={settings.sqlite_mmap_mb * 1024 * 1024}")
        cur.close()

    @event.listens_for(engine, "begin")
    def _on_begin(conn: Connection):  # pragma: no cover - driver hook
        mode = conn.get_execution_options().get("sqlite_begin", "DEFERRED")
        conn.exec_driver_sql(f"BEGIN {mode}")


def get_engines(url: str | None = None) -> tuple[Engine, Engine]:
    """Return (read_engine, write_engine) for a database URL, cached per URL."""
    url = url or settings.db_url
    if url not in _engines:
        engine = create_engine(url, future=True, connect_args={"check_same_thread": False, "timeout": 60})
        _configure(engine)
        write = engine.execution_options(sqlite_begin="IMMEDIATE")
        _engines[url] = (engine, write)
    return _engines[url]


def dispose_all() -> None:
    for read, _ in _engines.values():
        read.dispose()
    _engines.clear()


class Database:
    """Small facade so services and tests can target a specific database file."""

    def __init__(self, url: str | None = None):
        self.url = url or settings.db_url
        self.read_engine, self.write_engine = get_engines(self.url)

    @contextmanager
    def read(self) -> Iterator[Connection]:
        with self.read_engine.connect() as conn:
            with conn.begin():
                yield conn

    @contextmanager
    def write(self) -> Iterator[Connection]:
        """A serialised write transaction. Commits on success, rolls back on any exception."""
        with self.write_engine.connect() as conn:
            with conn.begin():
                yield conn


_default: Database | None = None


def get_db() -> Database:
    global _default
    if _default is None:
        _default = Database()
    return _default


def set_default_db(db: Database) -> None:
    global _default
    _default = db
