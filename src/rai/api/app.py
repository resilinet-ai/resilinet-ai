"""The RAI API application (B-16: FR-01.1, NFR-SEC-1).

Run it with ``rai serve`` (binds 127.0.0.1 by default; TLS is terminated in front
of it, threat I-06). On start-up it checks that it logged in as ``rai_api`` and
refuses to serve otherwise (threat I-02).
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request, Response
from sqlalchemy import Engine, create_engine

from rai import __version__
from rai.api import (
    problems,
    routes_auth,
    routes_definitions,
    routes_keys,
    routes_members,
    routes_projects,
    routes_runs,
)
from rai.api.context import check_login_role
from rai.api.problems import Problem, problem_response
from rai.api.ratelimit import FailureLimiter, RequestLimiter
from rai.api.settings import Settings

ADDRESS_FACTOR = 10  # an address may fail this many times more than one email


def create_app(settings: Settings, *, engine: Engine | None = None) -> FastAPI:
    db = engine or create_engine(settings.database_url, pool_pre_ping=True, pool_size=5)

    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        with db.connect() as conn:
            check_login_role(conn)
        yield
        if engine is None:
            db.dispose()

    app = FastAPI(
        title="Resilinet-AI API",
        version=__version__,
        lifespan=lifespan,
        docs_url=None,
        redoc_url=None,
    )
    window = settings.login_window.total_seconds()
    app.state.settings = settings
    app.state.engine = db
    app.state.login_by_email = FailureLimiter(settings.login_attempts, window)
    app.state.login_by_address = FailureLimiter(settings.login_attempts * ADDRESS_FACTOR, window)
    app.state.key_requests = RequestLimiter(settings.key_requests_per_minute, 60)
    problems.install(app)

    @app.middleware("http")
    async def limits_and_headers(
        request: Request, call_next: Callable[[Request], Awaitable[Response]]
    ) -> Response:
        length = request.headers.get("content-length")
        if length is not None and (not length.isdigit() or int(length) > settings.max_body_bytes):
            too_big = Problem(413, "request.too_large", "The request body is too large")
            return problem_response(request, too_big)
        response = await call_next(request)
        response.headers["Cache-Control"] = "no-store"
        response.headers["X-Content-Type-Options"] = "nosniff"
        return response

    @app.get("/healthz", include_in_schema=False)
    def healthz() -> dict[str, str]:
        return {"status": "ok"}

    app.include_router(routes_auth.router)
    app.include_router(routes_members.router)
    app.include_router(routes_keys.router)
    app.include_router(routes_projects.router)
    app.include_router(routes_definitions.router)
    app.include_router(routes_runs.router)
    return app


def app_from_env() -> FastAPI:
    """Factory for ``uvicorn --factory rai.api.app:app_from_env``."""
    return create_app(Settings.from_env())
