"""Master-data lookup context used by import validation.

Loading ~100k master rows takes a fraction of a second, but a feed replay may process hundreds of
small batches, so the context is cached per database and invalidated by a cheap fingerprint of the
master tables.
"""
from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field

from sqlalchemy import text
from sqlalchemy.engine import Connection

from .authz import AuthorityIndex

_FINGERPRINT_SQL = """
SELECT
 (SELECT count(*) FROM store) || ':' ||
 (SELECT count(*) FROM sku) || ':' ||
 (SELECT count(*) || '/' || coalesce(sum(length(coalesce(active_to,''))),0) FROM store_sku) || ':' ||
 (SELECT count(*) FROM employee) || ':' ||
 (SELECT count(*) || '/' || coalesce(sum(length(coalesce(effective_to,''))),0) FROM role_assignment) || ':' ||
 (SELECT count(*) FROM shift) || ':' ||
 (SELECT count(*) FROM source_document) || ':' ||
 (SELECT count(*) FROM transfer) || ':' ||
 (SELECT count(*) FROM transfer_line) || ':' ||
 (SELECT count(*) FROM role_permission)
"""


@dataclass
class MasterContext:
    fingerprint: str
    stores: dict[str, str]                       # store_id -> store_type
    sku_cost: dict[str, int]                     # sku_id -> unit_cost_cents
    assortment: dict[tuple[str, str], tuple[str, str | None]]  # (store, sku) -> (active_from, active_to)
    opening: dict[tuple[str, str], int]
    docs: dict[str, tuple[str, str]]             # doc_id -> (doc_type, store_id)
    transfers: dict[str, tuple[str, str, str, str]]  # id -> (from, to, created_at, expected_arrival_at)
    transfer_lines: set[tuple[str, str]]
    shifts: dict[str, tuple[str, str, str]]      # id -> (store_id, start_at, end_at)
    authority: AuthorityIndex
    checked_at: float = field(default_factory=time.monotonic)


_cache: dict[str, MasterContext] = {}
_lock = threading.Lock()


def load_context(conn: Connection, cache_key: str, max_age_s: float = 0.0) -> MasterContext:
    """Return cached master data, re-validated against the fingerprint.

    ``max_age_s`` lets read-only callers (API authorisation) skip the fingerprint query for a few
    seconds; import validation always re-checks (default 0).
    """
    with _lock:
        cached = _cache.get(cache_key)
    if cached is not None and max_age_s and time.monotonic() - cached.checked_at < max_age_s:
        return cached
    fp = conn.execute(text(_FINGERPRINT_SQL)).scalar_one()
    with _lock:
        cached = _cache.get(cache_key)
        if cached is not None and cached.fingerprint == fp:
            cached.checked_at = time.monotonic()
            return cached
    ctx = MasterContext(
        fingerprint=fp,
        stores={r[0]: r[1] for r in conn.execute(text("SELECT store_id, store_type FROM store"))},
        sku_cost={r[0]: r[1] for r in conn.execute(text("SELECT sku_id, unit_cost_cents FROM sku"))},
        assortment={},
        opening={},
        docs={r[0]: (r[1], r[2]) for r in conn.execute(text("SELECT doc_id, doc_type, store_id FROM source_document"))},
        transfers={
            r[0]: (r[1], r[2], r[3], r[4])
            for r in conn.execute(text("SELECT transfer_id, from_store_id, to_store_id, created_at, expected_arrival_at FROM transfer"))
        },
        transfer_lines={(r[0], r[1]) for r in conn.execute(text("SELECT transfer_id, sku_id FROM transfer_line"))},
        shifts={r[0]: (r[1], r[2], r[3]) for r in conn.execute(text("SELECT shift_id, store_id, start_at, end_at FROM shift"))},
        authority=AuthorityIndex(conn),
    )
    for r in conn.execute(text("SELECT store_id, sku_id, active_from, active_to, opening_qty FROM store_sku")):
        ctx.assortment[(r[0], r[1])] = (r[2], r[3])
        ctx.opening[(r[0], r[1])] = r[4]
    with _lock:
        _cache[cache_key] = ctx
    return ctx


def invalidate(cache_key: str | None = None) -> None:
    with _lock:
        if cache_key is None:
            _cache.clear()
        else:
            _cache.pop(cache_key, None)
