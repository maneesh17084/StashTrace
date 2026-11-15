"""Authorization: effective-dated role assignments evaluated at a point in time.

The same index answers two questions:
* event-time authority during import ("was this actor allowed to post a SALE at STORE-000007 at
  2026-03-04T10:15:00Z?"), and
* request-time authority for API callers ("may this user resolve cases for STORE-000007 now?").
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import os
import secrets
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

from sqlalchemy import text
from sqlalchemy.engine import Connection

from .common import utcnow


@dataclass
class Grant:
    role_code: str
    store_id: str | None  # None = all stores
    effective_from: str
    effective_to: str | None


class AuthorityIndex:
    def __init__(self, conn: Connection):
        self.permissions: dict[str, set[str]] = defaultdict(set)
        for r in conn.execute(text("SELECT role_code, action FROM role_permission")):
            self.permissions[r.role_code].add(r.action)
        self.grants: dict[str, list[Grant]] = defaultdict(list)
        for r in conn.execute(
            text("SELECT employee_id, role_code, store_id, effective_from, effective_to FROM role_assignment "
                 "ORDER BY employee_id, effective_from, assignment_id")
        ):
            self.grants[r.employee_id].append(Grant(r.role_code, r.store_id, r.effective_from, r.effective_to))
        self.employees: dict[str, str] = {
            r.employee_id: r.employee_type for r in conn.execute(text("SELECT employee_id, employee_type FROM employee"))
        }

    def authorize(self, employee_id: str, action: str, store_id: str | None, at: str) -> str | None:
        """Return the role_code that grants ``action`` at ``store_id`` at time ``at``, else None."""
        for g in self.grants.get(employee_id, ()):
            if g.effective_from > at or (g.effective_to is not None and at >= g.effective_to):
                continue
            if g.store_id is not None and store_id is not None and g.store_id != store_id:
                continue
            if g.store_id is not None and store_id is None:
                continue
            if action in self.permissions.get(g.role_code, ()):
                return g.role_code
        return None

    def stores_for(self, employee_id: str, action: str, at: str) -> set[str] | None:
        """Stores where the action is allowed at ``at``. ``None`` means every store."""
        stores: set[str] = set()
        for g in self.grants.get(employee_id, ()):
            if g.effective_from > at or (g.effective_to is not None and at >= g.effective_to):
                continue
            if action not in self.permissions.get(g.role_code, ()):
                continue
            if g.store_id is None:
                return None
            stores.add(g.store_id)
        return stores


@dataclass
class Principal:
    employee_id: str
    display_name: str
    employee_type: str
    index: AuthorityIndex
    now: str = field(default_factory=utcnow)

    def role_for(self, action: str, store_id: str | None) -> str | None:
        return self.index.authorize(self.employee_id, action, store_id, self.now)

    def can(self, action: str, store_id: str | None = None) -> bool:
        return self.role_for(action, store_id) is not None

    def stores(self, action: str = "VIEW_STORE") -> set[str] | None:
        return self.index.stores_for(self.employee_id, action, self.now)

    def can_view(self, store_id: str | None) -> bool:
        visible = self.stores("VIEW_STORE")
        return visible is None or (store_id is not None and store_id in visible)

    def roles(self) -> list[str]:
        out = []
        for g in self.index.grants.get(self.employee_id, ()):
            if g.effective_from <= self.now and (g.effective_to is None or self.now < g.effective_to):
                out.append(g.role_code)
        return sorted(set(out))


# ----------------------------------------------------------------- passwords
_ITER = 200_000


def hash_password(password: str, salt: bytes | None = None) -> str:
    salt = salt or os.urandom(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode(), salt, _ITER)
    return f"pbkdf2_sha256${_ITER}${base64.b64encode(salt).decode()}${base64.b64encode(digest).decode()}"


def verify_password(password: str, stored: str | None) -> bool:
    if not stored:
        return False
    try:
        _, iters, salt_b64, digest_b64 = stored.split("$")
        digest = hashlib.pbkdf2_hmac("sha256", password.encode(), base64.b64decode(salt_b64), int(iters))
        return hmac.compare_digest(digest, base64.b64decode(digest_b64))
    except (ValueError, TypeError):
        return False


def issue_token(conn: Connection, employee_id: str, ttl_hours: int) -> tuple[str, str]:
    token = secrets.token_urlsafe(32)
    now = datetime.now(timezone.utc)
    expires = (now + timedelta(hours=ttl_hours)).strftime("%Y-%m-%dT%H:%M:%SZ")
    conn.execute(
        text("INSERT INTO api_token (token_sha256, employee_id, created_at, expires_at) VALUES (:h, :e, :c, :x)"),
        {"h": hashlib.sha256(token.encode()).hexdigest(), "e": employee_id, "c": utcnow(), "x": expires},
    )
    return token, expires


def employee_for_token(conn: Connection, token: str) -> str | None:
    r = conn.execute(
        text("SELECT employee_id, expires_at FROM api_token WHERE token_sha256=:h"),
        {"h": hashlib.sha256(token.encode()).hexdigest()},
    ).first()
    if r is None or r.expires_at <= utcnow():
        return None
    return r.employee_id
