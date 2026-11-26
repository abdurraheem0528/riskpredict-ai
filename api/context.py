"""Request context: who is calling, which workspace, and the transaction that
carries the row-level-security settings.

Order inside every workspace transaction (contracts decisions 3 and 5):

1. ``set_config('app.user_id', <token sub>, true)``: the user's own sessions and
   memberships become visible.
2. Check that the token's session is not revoked, and read the caller's role in
   the workspace. Not a member → ``404 not_found`` (ids don't leak); role too
   low → ``403 forbidden``.
3. Only then ``set_config('app.workspace_id', <id>, true)``.

The workspace comes from ``X-Workspace-Id``, except for ``/v1/workspaces/{id}…``,
which uses the path. Both settings are transaction-local, so nothing survives
on a pooled connection.
"""

from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
from typing import Annotated, Iterator
from uuid import UUID

import psycopg
from fastapi import Depends, Header, Path, Request

from api import auth
from api.core.config import Settings
from api.core.db import Database, set_user, set_workspace
from api.core.errors import Problem, bad_request, forbidden, not_found, unauthenticated
from api.ratelimit import RateLimiter

ROLE_RANK = {"viewer": 0, "editor": 1, "admin": 2, "owner": 3}


@dataclass
class State:
    settings: Settings
    db: Database
    limiter: RateLimiter
    schema_version: str | None = None


def get_state(request: Request) -> State:
    return request.app.state.rp


StateDep = Annotated[State, Depends(get_state)]


@dataclass(frozen=True)
class Principal:
    user_id: UUID
    session_id: UUID


def get_principal(request: Request, st: StateDep) -> Principal:
    header = request.headers.get("authorization", "")
    scheme, _, token = header.partition(" ")
    if scheme.lower() != "bearer" or not token.strip():
        raise unauthenticated("missing bearer token")
    if not st.settings.jwt_signing_key:
        raise Problem(503, "internal", "the API has no JWT signing key configured")
    claims = auth.verify_access(st.settings.jwt_signing_key, token.strip())
    return Principal(claims.user_id, claims.session_id)


PrincipalDep = Annotated[Principal, Depends(get_principal)]

_SESSION_OK = """
SELECT EXISTS (SELECT 1 FROM biz.sessions s
               WHERE s.id = %(sid)s AND s.user_id = %(uid)s AND s.revoked_at IS NULL AND s.expires_at > now())
       AND EXISTS (SELECT 1 FROM biz.users u WHERE u.id = %(uid)s AND u.disabled_at IS NULL) AS ok,
       (SELECT m.role FROM biz.memberships m WHERE m.workspace_id = %(ws)s AND m.user_id = %(uid)s) AS role
"""


@dataclass
class Ctx:
    state: State
    principal: Principal
    workspace_id: UUID | None = None
    role: str | None = None

    @property
    def user_id(self) -> UUID:
        return self.principal.user_id

    @property
    def settings(self) -> Settings:
        return self.state.settings

    @contextmanager
    def user_tx(self) -> Iterator[psycopg.Connection]:
        """A transaction as the user, with no workspace selected."""
        with self.state.db.tx() as conn:
            set_user(conn, self.user_id)
            row = conn.execute(_SESSION_OK, {"sid": self.principal.session_id, "uid": self.user_id, "ws": None}).fetchone()
            if not row["ok"]:
                raise unauthenticated("this session was signed out")
            yield conn

    @contextmanager
    def tx(self, min_role: str = "viewer") -> Iterator[psycopg.Connection]:
        """A transaction in the workspace, after the membership check."""
        if self.workspace_id is None:
            raise Problem(400, "workspace_required", "send the X-Workspace-Id header")
        with self.state.db.tx() as conn:
            set_user(conn, self.user_id)
            row = conn.execute(
                _SESSION_OK, {"sid": self.principal.session_id, "uid": self.user_id, "ws": self.workspace_id}
            ).fetchone()
            if not row["ok"]:
                raise unauthenticated("this session was signed out")
            if row["role"] is None:
                raise not_found("workspace not found")
            self.role = row["role"]
            if ROLE_RANK[self.role] < ROLE_RANK[min_role]:
                raise forbidden(f"this needs the {min_role} role")
            set_workspace(conn, self.workspace_id)
            yield conn


def _parse_uuid(value: str, what: str) -> UUID:
    try:
        return UUID(value)
    except (ValueError, AttributeError):
        raise bad_request(f"{what} is not a valid id") from None


def user_ctx(st: StateDep, p: PrincipalDep) -> Ctx:
    return Ctx(st, p)


def header_ctx(st: StateDep, p: PrincipalDep, x_workspace_id: Annotated[str | None, Header()] = None) -> Ctx:
    if not x_workspace_id:
        raise Problem(400, "workspace_required", "send the X-Workspace-Id header")
    return Ctx(st, p, _parse_uuid(x_workspace_id, "X-Workspace-Id"))


def path_ctx(st: StateDep, p: PrincipalDep, workspace_id: Annotated[UUID, Path()]) -> Ctx:
    return Ctx(st, p, workspace_id)


UserCtx = Annotated[Ctx, Depends(user_ctx)]
WsCtx = Annotated[Ctx, Depends(header_ctx)]
PathWsCtx = Annotated[Ctx, Depends(path_ctx)]


def client_ip(request: Request) -> str:
    """The caller's address for rate limits. On Vercel the edge sets ``x-real-ip`` itself
    (``CLIENT_IP_HEADER``); a client cannot choose it. Never stored in clear."""
    header = request.app.state.rp.settings.client_ip_header
    if header:
        value = request.headers.get(header, "").split(",")[0].strip()
        if value:
            return value[:100]
    return request.client.host if request.client else "unknown"


def audit(conn: psycopg.Connection, workspace_id: UUID, user_id: UUID | None, action: str,
          target_type: str | None = None, target_id: str | None = None, details: dict | None = None,
          actor: str = "user") -> None:  # fmt: skip
    """Append to biz.audit_log. ``details`` holds ids and counts only."""
    from psycopg.types.json import Jsonb

    conn.execute(
        "INSERT INTO biz.audit_log (workspace_id, actor_type, actor_user_id, action, target_type, target_id, details)"
        " VALUES (%s, %s, %s, %s, %s, %s, %s)",
        (workspace_id, actor, user_id, action, target_type, target_id, Jsonb(details or {})),
    )
