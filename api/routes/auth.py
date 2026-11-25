"""Sign-up, sign-in, refresh, sign-out and /v1/me."""

from __future__ import annotations

import re
import uuid
from datetime import timedelta
import psycopg
from fastapi import APIRouter, Request, Response
from pydantic import BaseModel, Field, field_validator

from api import auth
from api.context import StateDep, UserCtx, audit, client_ip
from api.core import serialize as ser
from api.core.db import set_user, set_workspace
from api.core.errors import Problem, conflict, invalid, unauthenticated

router = APIRouter(tags=["auth"])

EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
LOCK_AFTER = 10
LOCK_FOR = timedelta(minutes=15)


def _email(v: str) -> str:
    v = v.strip()
    if not EMAIL_RE.match(v) or len(v) > 254:
        raise ValueError("not a valid email address")
    return v


class SignUp(BaseModel):
    email: str
    password: str = Field(min_length=12, max_length=128)
    display_name: str | None = Field(default=None, max_length=120)
    workspace_name: str = Field(min_length=1, max_length=120)

    _e = field_validator("email")(lambda cls, v: _email(v))


class SignIn(BaseModel):
    email: str = Field(max_length=254)
    password: str = Field(max_length=128)


class RefreshBody(BaseModel):
    refresh_token: str = Field(min_length=1, max_length=200)


class SignOutBody(RefreshBody):
    everywhere: bool = False


class MePatch(BaseModel):
    display_name: str | None = Field(default=None, max_length=120)
    current_password: str | None = Field(default=None, max_length=128)
    new_password: str | None = Field(default=None, min_length=12, max_length=128)


def _tokens(st, user_id: uuid.UUID, session_id: uuid.UUID, refresh: str, refresh_expires) -> dict:
    now = auth.utcnow()
    access, access_exp = auth.issue_access(st.settings.jwt_signing_key, user_id, session_id, now)
    return {
        "token_type": "Bearer",
        "access_token": access,
        "access_token_expires_at": ser.ts(access_exp),
        "refresh_token": refresh,
        "refresh_token_expires_at": ser.ts(refresh_expires),
    }


def _new_session(conn: psycopg.Connection, user_id, family_id, user_agent: str | None) -> tuple[uuid.UUID, str, object]:
    sid = uuid.uuid4()
    token, digest = auth.new_refresh_token()
    expires = (auth.utcnow() + auth.REFRESH_TTL).replace(microsecond=0)
    conn.execute(
        "INSERT INTO biz.sessions (id, user_id, family_id, refresh_token_hash, expires_at, user_agent)"
        " VALUES (%s, %s, %s, %s, %s, %s)",
        (sid, user_id, family_id, digest, expires, (user_agent or "")[:400] or None),
    )
    return sid, token, expires


def _require_key(st) -> None:
    if not st.settings.jwt_signing_key:
        raise Problem(503, "internal", "the API has no JWT signing key configured")


@router.post("/v1/auth/sign-up", status_code=201)
def sign_up(body: SignUp, request: Request, st: StateDep) -> dict:
    _require_key(st)
    st.limiter.hit(f"signup:{client_ip(request)}", 10, 3600)
    with st.db.tx() as conn:
        if conn.execute("SELECT 1 FROM biz.auth_user_by_email(%s)", (body.email,)).fetchone():
            raise conflict("an account with this email already exists", code="email_taken")
    password_hash = auth.hash_password(body.password)
    uid, ws, family = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    try:
        with st.db.tx() as conn:
            set_user(conn, uid)
            urow = conn.execute(
                "INSERT INTO biz.users (id, email, password_hash, display_name, last_sign_in_at)"
                " VALUES (%s, %s, %s, %s, now()) RETURNING *",
                (uid, body.email, password_hash, body.display_name),
            ).fetchone()
            sid, token, expires = _new_session(conn, uid, family, request.headers.get("user-agent"))
            set_workspace(conn, ws)
            conn.execute("INSERT INTO biz.workspaces (id, name) VALUES (%s, %s)", (ws, body.workspace_name.strip()))
            conn.execute("INSERT INTO biz.memberships (workspace_id, user_id, role) VALUES (%s, %s, 'owner')", (ws, uid))
            audit(conn, ws, uid, "workspace.created", "workspace", str(ws))
    except psycopg.errors.UniqueViolation:
        raise conflict("an account with this email already exists", code="email_taken") from None
    return {**_tokens(st, uid, sid, token, expires), "user": ser.user(urow), "workspace_id": str(ws)}


@router.post("/v1/auth/sign-in")
def sign_in(body: SignIn, request: Request, st: StateDep) -> dict:
    _require_key(st)
    email = body.email.strip()
    st.limiter.hit(f"signin-ip:{client_ip(request)}", 20, 60)
    st.limiter.hit(f"signin-acct:{email.lower()}", 10, 60)
    with st.db.tx() as conn:
        found = conn.execute("SELECT * FROM biz.auth_user_by_email(%s)", (email,)).fetchone()
    now = auth.utcnow()
    if found is None:
        auth.verify_password(None, body.password)  # same work as a real check
        raise unauthenticated("wrong email or password", code="invalid_credentials")
    if found["locked_until"] and found["locked_until"] > now:
        raise unauthenticated("too many failed sign-ins; try again in 15 minutes", code="account_locked")
    ok = auth.verify_password(found["password_hash"], body.password) and not found["disabled"]
    uid = found["user_id"]
    with st.db.tx() as conn:
        set_user(conn, uid)
        if not ok:
            conn.execute(
                "UPDATE biz.users SET"
                " failed_sign_in_count = CASE WHEN failed_sign_in_count + 1 >= %(n)s THEN 0 ELSE failed_sign_in_count + 1 END,"
                " locked_until = CASE WHEN failed_sign_in_count + 1 >= %(n)s THEN now() + %(lock)s ELSE locked_until END,"
                " updated_at = now() WHERE id = %(uid)s",
                {"n": LOCK_AFTER, "lock": LOCK_FOR, "uid": uid},
            )
        else:
            new_hash = auth.hash_password(body.password) if auth.needs_rehash(found["password_hash"]) else None
            urow = conn.execute(
                "UPDATE biz.users SET failed_sign_in_count = 0, locked_until = NULL, last_sign_in_at = now(),"
                " password_hash = coalesce(%s, password_hash), updated_at = now() WHERE id = %s RETURNING *",
                (new_hash, uid),
            ).fetchone()
            conn.execute("DELETE FROM biz.sessions WHERE user_id = %s AND expires_at < now()", (uid,))
            sid, token, expires = _new_session(conn, uid, uuid.uuid4(), request.headers.get("user-agent"))
            ws = conn.execute(
                "SELECT workspace_id FROM biz.memberships WHERE user_id = %s ORDER BY created_at DESC, workspace_id LIMIT 1",
                (uid,),
            ).fetchone()
    if not ok:
        raise unauthenticated("wrong email or password", code="invalid_credentials")
    return {
        **_tokens(st, uid, sid, token, expires),
        "user": ser.user(urow),
        "workspace_id": str(ws["workspace_id"]) if ws else None,
    }


@router.post("/v1/auth/refresh")
def refresh(body: RefreshBody, request: Request, st: StateDep) -> dict:
    _require_key(st)
    st.limiter.hit(f"refresh:{client_ip(request)}", 60, 60)
    digest = auth.crypto.sha256(body.refresh_token)
    outcome = "ok"
    with st.db.tx() as conn:
        row = conn.execute("SELECT * FROM biz.auth_session_by_refresh_hash(%s)", (digest,)).fetchone()
        if row is None:
            raise unauthenticated("unknown refresh token")
        uid = row["user_id"]
        set_user(conn, uid)
        if row["revoked_at"] is not None:
            raise unauthenticated("this session was signed out")
        if row["rotated_at"] is None and row["expires_at"] <= auth.utcnow():
            raise unauthenticated("the refresh token expired; sign in again", code="token_expired")
        rotated = None
        if row["rotated_at"] is None:
            rotated = conn.execute(
                "UPDATE biz.sessions SET rotated_at = now(), last_used_at = now()"
                " WHERE id = %s AND rotated_at IS NULL AND revoked_at IS NULL RETURNING id",
                (row["session_id"],),
            ).fetchone()
        if rotated is None:
            # Already rotated: someone replayed an old token. Revoke the family, commit, then refuse.
            conn.execute(
                "UPDATE biz.sessions SET revoked_at = now(), revoked_reason = 'reuse_detected'"
                " WHERE family_id = %s AND revoked_at IS NULL",
                (row["family_id"],),
            )
            outcome = "reused"
        else:
            disabled = conn.execute("SELECT disabled_at FROM biz.users WHERE id = %s", (uid,)).fetchone()
            if disabled is None or disabled["disabled_at"] is not None:
                raise unauthenticated("this account is disabled")
            sid, token, expires = _new_session(conn, uid, row["family_id"], request.headers.get("user-agent"))
            # Keep the family's rotated rows for a week, for reuse detection; then drop them.
            conn.execute(
                "DELETE FROM biz.sessions WHERE family_id = %s AND rotated_at < now() - interval '7 days'",
                (row["family_id"],),
            )
    if outcome == "reused":
        raise unauthenticated("this refresh token was already used; every device in this sign-in was signed out",
                              code="refresh_reused")  # fmt: skip
    return _tokens(st, uid, sid, token, expires)


@router.post("/v1/auth/sign-out", status_code=204)
def sign_out(body: SignOutBody, st: StateDep) -> Response:
    digest = auth.crypto.sha256(body.refresh_token)
    with st.db.tx() as conn:
        row = conn.execute("SELECT * FROM biz.auth_session_by_refresh_hash(%s)", (digest,)).fetchone()
        if row is not None:
            set_user(conn, row["user_id"])
            if body.everywhere:
                conn.execute(
                    "UPDATE biz.sessions SET revoked_at = now(), revoked_reason = 'sign_out'"
                    " WHERE user_id = %s AND revoked_at IS NULL",
                    (row["user_id"],),
                )
            else:
                conn.execute(
                    "UPDATE biz.sessions SET revoked_at = now(), revoked_reason = 'sign_out'"
                    " WHERE family_id = %s AND revoked_at IS NULL",
                    (row["family_id"],),
                )
    return Response(status_code=204)


def _me(conn, uid) -> dict:
    urow = conn.execute("SELECT * FROM biz.users WHERE id = %s", (uid,)).fetchone()
    rows = conn.execute(
        "SELECT w.*, m.role FROM biz.memberships m JOIN biz.workspaces w ON w.id = m.workspace_id"
        " WHERE m.user_id = %s ORDER BY w.created_at, w.id",
        (uid,),
    ).fetchall()
    return {"user": ser.user(urow), "memberships": [{"workspace": ser.workspace(r), "role": r["role"]} for r in rows]}


@router.get("/v1/me")
def get_me(ctx: UserCtx) -> dict:
    with ctx.user_tx() as conn:
        return _me(conn, ctx.user_id)


@router.patch("/v1/me")
def update_me(body: MePatch, ctx: UserCtx) -> dict:
    if body.new_password is not None and not body.current_password:
        raise invalid([{"field": "/current_password", "message": "required with new_password"}])
    new_hash = None
    if body.new_password is not None:
        with ctx.user_tx() as conn:
            cur = conn.execute("SELECT password_hash FROM biz.users WHERE id = %s", (ctx.user_id,)).fetchone()
        if not auth.verify_password(cur["password_hash"], body.current_password or ""):
            raise invalid([{"field": "/current_password", "message": "wrong password"}])
        new_hash = auth.hash_password(body.new_password)
    with ctx.user_tx() as conn:
        fields = body.model_fields_set
        if "display_name" in fields:
            conn.execute(
                "UPDATE biz.users SET display_name = %s, updated_at = now() WHERE id = %s",
                ((body.display_name or "").strip() or None, ctx.user_id),
            )
        if new_hash:
            conn.execute("UPDATE biz.users SET password_hash = %s, updated_at = now() WHERE id = %s", (new_hash, ctx.user_id))
            # Revoke every other sign-in (other families); keep this one.
            conn.execute(
                "UPDATE biz.sessions SET revoked_at = now(), revoked_reason = 'password_changed'"
                " WHERE user_id = %s AND revoked_at IS NULL"
                " AND family_id <> (SELECT family_id FROM biz.sessions WHERE id = %s)",
                (ctx.user_id, ctx.principal.session_id),
            )
        urow = conn.execute("SELECT * FROM biz.users WHERE id = %s", (ctx.user_id,)).fetchone()
    return ser.user(urow)

