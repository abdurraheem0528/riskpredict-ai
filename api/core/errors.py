"""Problem details (RFC 9457) as exceptions, usable from the worker too.

``detail`` is always safe to show: never tokens, SQL or row contents
(contracts decision 12).
"""

from __future__ import annotations

from typing import Any

TITLES = {
    400: "Bad request",
    401: "Unauthorized",
    403: "Forbidden",
    404: "Not found",
    409: "Conflict",
    422: "Unprocessable content",
    429: "Too many requests",
    500: "Internal error",
    503: "Service unavailable",
}


class Problem(Exception):
    def __init__(
        self,
        status: int,
        code: str,
        detail: str | None = None,
        *,
        errors: list[dict[str, str]] | None = None,
        headers: dict[str, str] | None = None,
        title: str | None = None,
    ):
        super().__init__(detail or code)
        self.status = status
        self.code = code
        self.detail = detail
        self.errors = errors
        self.headers = headers or {}
        self.title = title or TITLES.get(status, "Error")

    def body(self, instance: str | None = None) -> dict[str, Any]:
        out: dict[str, Any] = {"type": "about:blank", "title": self.title, "status": self.status, "code": self.code}
        if self.detail:
            out["detail"] = self.detail
        if instance:
            out["instance"] = instance
        if self.errors is not None:
            out["errors"] = self.errors
        return out


def bad_request(detail: str | None = None, code: str = "bad_request") -> Problem:
    return Problem(400, code, detail)


def unauthenticated(detail: str = "sign in again", code: str = "unauthenticated") -> Problem:
    return Problem(401, code, detail)


def forbidden(detail: str = "your role does not allow this") -> Problem:
    return Problem(403, "forbidden", detail)


def not_found(detail: str = "not found") -> Problem:
    return Problem(404, "not_found", detail)


def conflict(detail: str, code: str = "conflict") -> Problem:
    return Problem(409, code, detail)


def invalid(errors: list[dict[str, str]], detail: str = "validation failed") -> Problem:
    return Problem(422, "validation_failed", detail, errors=errors)


class UpstreamUnavailable(Problem):
    """Google, Composio or Groq could not be reached, refused, or is not configured."""

    def __init__(self, service: str, detail: str | None = None, code: str = "upstream_unavailable"):
        super().__init__(503, code, detail or f"{service} is unavailable right now")
        self.service = service


class UpstreamNotFound(Problem):
    def __init__(self, detail: str = "not found at the provider"):
        super().__init__(404, "not_found", detail)


class UpstreamAuthError(Problem):
    """The provider rejected our credentials (revoked grant, expired account)."""

    def __init__(self, service: str, detail: str | None = None):
        super().__init__(409, "connection_not_active", detail or f"{service} needs to be reconnected")
        self.service = service
