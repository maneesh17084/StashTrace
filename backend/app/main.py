"""FastAPI application: /api/v1 plus the static frontend on the same origin.

Run:  uvicorn backend.app.main:app --reload
"""
from __future__ import annotations

import logging
import time
import uuid

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from sqlalchemy import text

from .api import routes_cases, routes_core, routes_counts
from .config import settings
from .db import get_db
from .services.common import DomainError

logging.basicConfig(level=settings.log_level, format="%(asctime)s %(levelname)s %(name)s %(message)s")
log = logging.getLogger("stashtrace.api")

app = FastAPI(title="StashTrace API", version="1.0.0", docs_url="/api/docs", openapi_url="/api/openapi.json")


@app.middleware("http")
async def request_context(request: Request, call_next):
    rid = request.headers.get("X-Request-ID") or uuid.uuid4().hex[:16]
    t0 = time.perf_counter()
    response = await call_next(request)
    ms = (time.perf_counter() - t0) * 1000
    response.headers["X-Request-ID"] = rid
    response.headers["X-Response-Time-ms"] = f"{ms:.1f}"
    response.headers["X-Content-Type-Options"] = "nosniff"
    if request.url.path.startswith("/api/"):
        log.info("rid=%s %s %s %s %.1fms", rid, request.method, request.url.path, response.status_code, ms)
    return response


@app.exception_handler(DomainError)
async def domain_error(request: Request, exc: DomainError):
    return JSONResponse(status_code=exc.status,
                        content={"error": {"code": exc.code, "message": exc.message, "details": exc.details}})


@app.exception_handler(RequestValidationError)
async def validation_error(request: Request, exc: RequestValidationError):
    details = [{"field": ".".join(str(x) for x in e["loc"][1:]), "message": e["msg"]} for e in exc.errors()]
    return JSONResponse(status_code=422, content={"error": {"code": "INVALID_REQUEST",
                                                            "message": "Request failed validation.",
                                                            "details": {"fields": details}}})


@app.get("/api/v1/health")
def health():
    with get_db().read() as conn:
        rev = conn.execute(text("SELECT version_num FROM alembic_version")).scalar()
        pending = conn.execute(text("SELECT COUNT(*) FROM job WHERE status IN ('QUEUED','RUNNING')")).scalar()
        failed = conn.execute(text("SELECT COUNT(*) FROM job WHERE status='FAILED'")).scalar()
    return {"status": "ok", "schema_revision": rev, "jobs_pending": pending, "jobs_failed": failed}


app.include_router(routes_core.router)
app.include_router(routes_counts.router)
app.include_router(routes_cases.router)

_front = settings.frontend_dir
if _front.exists():
    app.mount("/static", StaticFiles(directory=_front), name="static")

    @app.get("/", include_in_schema=False)
    def index():
        return FileResponse(_front / "index.html")
