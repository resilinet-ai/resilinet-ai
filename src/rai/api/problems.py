"""RFC 9457 problem details for every error response (Data Model section 7)."""

from __future__ import annotations

from typing import Any

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

MEDIA_TYPE = "application/problem+json"
TYPE_BASE = "https://resilinet-ai.github.io/problems/"


class Problem(Exception):
    """Raise to answer with a problem document."""

    def __init__(
        self,
        status: int,
        code: str,
        title: str,
        detail: str | None = None,
        *,
        headers: dict[str, str] | None = None,
        errors: list[dict[str, Any]] | None = None,
    ) -> None:
        super().__init__(title)
        self.status, self.code, self.title, self.detail = status, code, title, detail
        self.headers, self.errors = headers or {}, errors


def problem_response(request: Request, problem: Problem) -> JSONResponse:
    body: dict[str, Any] = {
        "type": TYPE_BASE + problem.code.replace(".", "/"),
        "title": problem.title,
        "status": problem.status,
        "code": problem.code,
        "instance": request.url.path,
    }
    if problem.detail:
        body["detail"] = problem.detail
    if problem.errors is not None:
        body["errors"] = problem.errors
    return JSONResponse(body, problem.status, problem.headers, media_type=MEDIA_TYPE)


# Generic titles for framework errors (404 unknown path, 405, and so on).
_TITLES = {401: "Not authenticated", 404: "Not found", 405: "Method not allowed"}


def install(app: FastAPI) -> None:
    async def on_problem(request: Request, error: Exception) -> JSONResponse:
        assert isinstance(error, Problem)  # noqa: S101 - registered for Problem only
        return problem_response(request, error)

    async def on_validation(request: Request, error: Exception) -> JSONResponse:
        assert isinstance(error, RequestValidationError)  # noqa: S101
        errors = [
            {"location": ".".join(str(p) for p in e["loc"]), "message": e["msg"]}
            for e in error.errors()
        ]
        problem = Problem(422, "request.invalid", "The request is not valid", errors=errors)
        return problem_response(request, problem)

    async def on_http(request: Request, error: Exception) -> JSONResponse:
        assert isinstance(error, StarletteHTTPException)  # noqa: S101
        title = _TITLES.get(error.status_code, "Request failed")
        code = f"http.{error.status_code}"
        return problem_response(
            request, Problem(error.status_code, code, title, headers=dict(error.headers or {}))
        )

    app.add_exception_handler(Problem, on_problem)
    app.add_exception_handler(RequestValidationError, on_validation)
    app.add_exception_handler(StarletteHTTPException, on_http)
