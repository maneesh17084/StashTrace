"""Request dependencies: database handle, authenticated principal, pagination, store scoping."""
from __future__ import annotations

from dataclasses import dataclass

from fastapi import Depends, Header, Query
from sqlalchemy import text

from ..db import Database, get_db
from ..services.authz import Principal, employee_for_token
from ..services.common import DomainError
from ..services.context import load_context


def db_dep() -> Database:
    return get_db()


def principal_dep(authorization: str | None = Header(default=None), db: Database = Depends(db_dep)) -> Principal:
    if not authorization or not authorization.lower().startswith("bearer "):
        raise DomainError("UNAUTHENTICATED", "Sign in first.", 401)
    token = authorization.split(" ", 1)[1].strip()
    with db.read() as conn:
        emp_id = employee_for_token(conn, token)
        if emp_id is None:
            raise DomainError("UNAUTHENTICATED", "Session expired or invalid.", 401)
        emp = conn.execute(text("SELECT employee_id, display_name, employee_type, status FROM employee"
                                " WHERE employee_id=:e"), {"e": emp_id}).one()
        if emp.status != "ACTIVE":
            raise DomainError("UNAUTHENTICATED", "Account is not active.", 401)
        ctx = load_context(conn, db.url, max_age_s=5.0)
    return Principal(emp.employee_id, emp.display_name, emp.employee_type, ctx.authority)


@dataclass
class Page:
    page: int
    page_size: int

    @property
    def offset(self) -> int:
        return (self.page - 1) * self.page_size


def page_dep(page: int = Query(1, ge=1, le=100000), page_size: int = Query(50, ge=1, le=200)) -> Page:
    return Page(page, page_size)


def store_scope(principal: Principal, store_id: str | None, column: str, params: dict,
                related_column: str | None = None) -> str:
    """SQL predicate restricting rows to stores the caller may view.

    An explicit ``store_id`` outside the caller's scope is a 403, so IDs cannot be probed.
    """
    visible = principal.stores("VIEW_STORE")
    if store_id:
        if visible is not None and store_id not in visible:
            raise DomainError("FORBIDDEN", f"You are not authorised for {store_id}.", 403)
        params["scope_store"] = store_id
        if related_column:
            return f"({column} = :scope_store OR {related_column} = :scope_store)"
        return f"{column} = :scope_store"
    if visible is None:
        return "1=1"
    if not visible:
        return "1=0"
    names = []
    for i, s in enumerate(sorted(visible)):
        params[f"vs{i}"] = s
        names.append(f":vs{i}")
    inlist = ",".join(names)
    if related_column:
        return f"({column} IN ({inlist}) OR {related_column} IN ({inlist}))"
    return f"{column} IN ({inlist})"


def require_view(principal: Principal, *store_ids: str | None) -> None:
    if not any(s and principal.can_view(s) for s in store_ids):
        raise DomainError("NOT_FOUND", "Not found.", 404)


def paged(conn, base_sql: str, params: dict, page: Page, order: str) -> dict:
    total = conn.execute(text(f"SELECT COUNT(*) FROM ({base_sql})"), params).scalar_one()
    items = [dict(r._mapping) for r in conn.execute(
        text(f"{base_sql} ORDER BY {order} LIMIT :_lim OFFSET :_off"),
        {**params, "_lim": page.page_size, "_off": page.offset})]
    return {"items": items, "page": page.page, "page_size": page.page_size, "total": total}
