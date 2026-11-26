"""FastAPI app for docs/contracts/openapi.yaml.

    uvicorn api.main:app --host 0.0.0.0 --port 8000 --no-access-log

Uvicorn's own access log is off because URLs can carry one-time secrets (the
OAuth code and state); the middleware below logs method, route template, status
and time only (contracts decision 12).

Serverless (Vercel, ``app.py`` in the deploy bundle): each request may run on a
fresh, short-lived instance and the lifespan hook may not run. So nothing here
depends on it: the database pool opens on first use, and the schema check runs
once per instance on the first connection (``check_schema``), not at startup.
State that must be shared (rate limits, live-update events) is in the database.
"""

from __future__ import annotations

import logging
import threading
import time
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

from api import __version__
from api.context import State
from api.core import db as dbmod
from api.core.config import Settings
from api.core.db import Database
from api.core.errors import Problem
from api.ratelimit import RateLimiter
from api.routes import auth, connections, data, geo, live, scenarios, sources, workspaces

log = logging.getLogger("riskpredict.api")
access = logging.getLogger("riskpredict.access")
PROBLEM = "application/problem+json"


def _problem_response(p: Problem, request: Request) -> JSONResponse:
    return JSONResponse(p.body(), status_code=p.status, media_type=PROBLEM, headers=p.headers)


def _pointer(loc: tuple) -> str:
    parts = [str(x) for x in loc[1:]] if loc and loc[0] == "body" else [str(x) for x in loc]
    return "/" + "/".join(parts) if parts else ""


def schema_hook(st: State):
    """Lazy, idempotent schema check: on this instance's first database connection, answer
    503 (and check again next time) while the migration this build expects is missing."""
    lock = threading.Lock()

    def hook(conn) -> None:
        with lock:
            if st.db.on_first_use is None:
                return
            st.schema_version = dbmod.schema_version(conn)
            if not dbmod.has_migration(conn, st.settings.expected_schema_version):
                log.error("migration %s is not applied; run python -m jobs.migrate", st.settings.expected_schema_version)
                raise Problem(503, "internal", "the database schema is older than this API; run the migrations")
            st.db.on_first_use = None

    return hook


def create_app(settings: Settings | None = None, *, check_db: bool = True) -> FastAPI:
    settings = settings or Settings.from_env()
    database = Database(settings.database_url, max_size=settings.db_max_connections,
                        max_idle=settings.db_max_idle_seconds)  # fmt: skip
    st = State(settings, database, RateLimiter(database, settings.jwt_signing_key))
    if check_db:
        database.on_first_use = schema_hook(st)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        # Optional: may not run on a serverless platform; nothing depends on it.
        log.info("api started; integrations: %s", settings.describe())
        yield
        database.close()

    app = FastAPI(
        title="RiskPredict AI API",
        version=__version__,
        lifespan=lifespan,
        docs_url=None,
        redoc_url=None,
        openapi_url=None,  # the contract is docs/contracts/openapi.yaml
    )
    app.state.rp = st

    if settings.web_origin:
        app.add_middleware(
            CORSMiddleware,
            allow_origins=[settings.web_origin],
            allow_credentials=False,  # no cookies: tokens travel in headers
            allow_methods=["GET", "POST", "PATCH", "DELETE", "OPTIONS"],
            allow_headers=["Authorization", "Content-Type", "X-Workspace-Id"],
            expose_headers=["Retry-After"],
            max_age=600,
        )

    @app.middleware("http")
    async def access_log(request: Request, call_next):
        start = time.perf_counter()
        response = await call_next(request)
        route = request.scope.get("route")
        access.info("%s %s %s %.0fms", request.method, getattr(route, "path", "-"), response.status_code,
                    (time.perf_counter() - start) * 1000)  # fmt: skip
        return response

    @app.exception_handler(Problem)
    async def on_problem(request: Request, exc: Problem):
        return _problem_response(exc, request)

    @app.exception_handler(RequestValidationError)
    async def on_validation(request: Request, exc: RequestValidationError):
        errors = []
        for e in exc.errors()[:50]:
            loc = tuple(e.get("loc") or ())
            if loc and loc[0] in ("header",) and len(loc) > 1 and str(loc[1]).lower() == "x-workspace-id":
                return _problem_response(Problem(400, "workspace_required", "send a valid X-Workspace-Id header"), request)
            if loc and loc[0] in ("path", "query", "header"):
                field = f"{loc[0]}:{loc[1]}" if len(loc) > 1 else str(loc[0])
            else:
                field = _pointer(loc)
            errors.append({"field": field, "message": str(e.get("msg", "invalid"))[:200]})
        if any(e["field"].startswith("path:") for e in errors):
            return _problem_response(Problem(404, "not_found", "not found"), request)
        return _problem_response(Problem(422, "validation_failed", "validation failed", errors=errors), request)

    @app.exception_handler(StarletteHTTPException)
    async def on_http(request: Request, exc: StarletteHTTPException):
        code = {404: "not_found", 405: "bad_request", 401: "unauthenticated", 403: "forbidden"}.get(exc.status_code, "bad_request")
        return _problem_response(Problem(exc.status_code, code, None), request)

    @app.exception_handler(Exception)
    async def on_error(request: Request, exc: Exception):
        log.error("unhandled %s on %s", type(exc).__name__, getattr(request.scope.get("route"), "path", "-"))
        return _problem_response(Problem(500, "internal", "something went wrong"), request)

    for r in (live, auth, workspaces, connections, sources, data, geo, scenarios):
        app.include_router(r.router)
    return app


def _app() -> FastAPI:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    logging.getLogger("httpx").setLevel(logging.WARNING)  # its INFO lines include URLs
    return create_app()


def __getattr__(name: str):
    # `uvicorn api.main:app` builds the app on first access, not at import time.
    if name == "app":
        globals()["app"] = _app()
        return globals()["app"]
    raise AttributeError(name)
