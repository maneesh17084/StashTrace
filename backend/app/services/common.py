"""Shared helpers: clock, ID allocation, hashing, errors."""
from __future__ import annotations

import hashlib
import json
from datetime import datetime, timedelta, timezone

from sqlalchemy import text
from sqlalchemy.engine import Connection

from .. import catalog as C


class DomainError(Exception):
    """Business-rule violation surfaced to API callers (HTTP 4xx)."""

    def __init__(self, code: str, message: str, status: int = 422, details: dict | None = None):
        super().__init__(message)
        self.code = code
        self.message = message
        self.status = status
        self.details = details or {}


def utcnow() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_ts(value: str) -> datetime:
    return datetime.strptime(value, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)


def fmt_ts(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def add_minutes(ts: str, minutes: float) -> str:
    return fmt_ts(parse_ts(ts) + timedelta(minutes=minutes))


def canonical_hash(payload: dict) -> str:
    body = json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(body.encode("utf-8")).hexdigest()


def allocate_ids(conn: Connection, name: str, n: int = 1) -> list[str]:
    """Allocate ``n`` consecutive public IDs inside the caller's (write) transaction."""
    if n <= 0:
        return []
    prefix, width = C.SEQUENCES[name]
    last = conn.exec_driver_sql("UPDATE id_sequence SET last_value = last_value + ? WHERE name = ? RETURNING last_value",
                                (n, name)).scalar_one() - n
    return [f"{prefix}{i:0{width}d}" for i in range(last + 1, last + n + 1)]


def bump_sequence_to(conn: Connection, name: str, public_id: str) -> None:
    """Keep the sequence ahead of externally supplied IDs (e.g. generator-assigned movement IDs)."""
    prefix, _ = C.SEQUENCES[name]
    value = int(public_id[len(prefix):])
    conn.exec_driver_sql("UPDATE id_sequence SET last_value=? WHERE name=? AND last_value < ?", (value, name, value))


def rows(result) -> list[dict]:
    return [dict(r._mapping) for r in result]


def row(result) -> dict | None:
    r = result.first()
    return dict(r._mapping) if r is not None else None
