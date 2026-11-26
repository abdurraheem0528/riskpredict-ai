"""Workspaces, settings and members. These use the workspace id in the path."""

from __future__ import annotations

import uuid
from uuid import UUID

from fastapi import APIRouter, Response
from pydantic import BaseModel, Field, field_validator

from api.context import ROLE_RANK, PathWsCtx, UserCtx, audit
from api.core import serialize as ser
from api.core.db import set_workspace
from api.core.errors import conflict, forbidden, not_found
from api.routes.connections import CONNECTION_FIELDS, disconnect_external

router = APIRouter(tags=["workspaces"])

Role = str


class WorkspaceInput(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=120)
    base_currency: str | None = Field(default=None, pattern=r"^[A-Z]{3}$")
    alert_min_score: float | None = Field(default=None, ge=0, le=100)
    share_watch_terms: bool | None = None

    @field_validator("base_currency")
    @classmethod
    def _iso(cls, v: str | None) -> str | None:
        from jobs.codes import is_currency

        if v is not None and not is_currency(v):
            raise ValueError("not an ISO 4217 currency code")
        return v


class NewWorkspace(WorkspaceInput):
    name: str = Field(min_length=1, max_length=120)


class MemberAdd(BaseModel):
    email: str = Field(max_length=254)
    role: str = Field(pattern="^(owner|admin|editor|viewer)$")


class MemberRole(BaseModel):
    role: str = Field(pattern="^(owner|admin|editor|viewer)$")


@router.get("/v1/workspaces")
def list_workspaces(ctx: UserCtx) -> dict:
    with ctx.user_tx() as conn:
        rows = conn.execute(
            "SELECT w.* FROM biz.memberships m JOIN biz.workspaces w ON w.id = m.workspace_id"
            " WHERE m.user_id = %s ORDER BY w.created_at, w.id",
            (ctx.user_id,),
        ).fetchall()
    return {"items": [ser.workspace(r) for r in rows]}


@router.post("/v1/workspaces", status_code=201)
def create_workspace(body: NewWorkspace, ctx: UserCtx) -> dict:
    ws = uuid.uuid4()
    with ctx.user_tx() as conn:
        set_workspace(conn, ws)
        row = conn.execute(
            "INSERT INTO biz.workspaces (id, name, base_currency, alert_min_score, share_watch_terms)"
            " VALUES (%s, %s, %s, coalesce(%s, 60), coalesce(%s, false)) RETURNING *",
            (ws, body.name.strip(), body.base_currency, body.alert_min_score, body.share_watch_terms),
        ).fetchone()
        conn.execute("INSERT INTO biz.memberships (workspace_id, user_id, role) VALUES (%s, %s, 'owner')", (ws, ctx.user_id))
        audit(conn, ws, ctx.user_id, "workspace.created", "workspace", str(ws))
    return ser.workspace(row)


@router.get("/v1/workspaces/{workspace_id}")
def get_workspace(ctx: PathWsCtx) -> dict:
    with ctx.tx() as conn:
        return ser.workspace(conn.execute("SELECT * FROM biz.workspaces WHERE id = %s", (ctx.workspace_id,)).fetchone())


@router.patch("/v1/workspaces/{workspace_id}")
def update_workspace(body: WorkspaceInput, ctx: PathWsCtx) -> dict:
    fields = body.model_fields_set
    with ctx.tx("admin") as conn:
        row = conn.execute(
            "UPDATE biz.workspaces SET"
            " name = CASE WHEN %(set_name)s THEN %(name)s ELSE name END,"
            " base_currency = CASE WHEN %(set_ccy)s THEN %(ccy)s ELSE base_currency END,"
            " alert_min_score = CASE WHEN %(set_min)s THEN %(min)s ELSE alert_min_score END,"
            " share_watch_terms = CASE WHEN %(set_share)s THEN %(share)s ELSE share_watch_terms END,"
            " updated_at = now() WHERE id = %(id)s RETURNING *",
            {
                "set_name": "name" in fields and body.name is not None, "name": (body.name or "").strip(),
                "set_ccy": "base_currency" in fields, "ccy": body.base_currency,
                "set_min": "alert_min_score" in fields and body.alert_min_score is not None, "min": body.alert_min_score,
                "set_share": "share_watch_terms" in fields and body.share_watch_terms is not None,
                "share": body.share_watch_terms, "id": ctx.workspace_id,
            },
        ).fetchone()  # fmt: skip
        audit(conn, ctx.workspace_id, ctx.user_id, "workspace.updated", "workspace", str(ctx.workspace_id),
              {"fields": sorted(fields)})  # fmt: skip
    return ser.workspace(row)


@router.delete("/v1/workspaces/{workspace_id}", status_code=204)
def delete_workspace(ctx: PathWsCtx) -> Response:
    with ctx.tx("owner") as conn:
        conns = conn.execute(f"SELECT {CONNECTION_FIELDS} FROM biz.connections").fetchall()
    for c in conns:
        disconnect_external(ctx.settings, c)
    with ctx.tx("owner") as conn:
        conn.execute("DELETE FROM biz.workspaces WHERE id = %s", (ctx.workspace_id,))
    return Response(status_code=204)


# --- members ------------------------------------------------------------------------


@router.get("/v1/workspaces/{workspace_id}/members")
def list_members(ctx: PathWsCtx) -> dict:
    with ctx.tx() as conn:
        rows = conn.execute(ser.MEMBER_SELECT + " WHERE m.workspace_id = %s ORDER BY m.created_at, u.id",
                            (ctx.workspace_id,)).fetchall()  # fmt: skip
    return {"items": [ser.member(r) for r in rows]}


def _owner_count(conn, ws: UUID) -> int:
    return conn.execute("SELECT count(*) AS n FROM biz.memberships WHERE workspace_id = %s AND role = 'owner'", (ws,)).fetchone()["n"]


@router.post("/v1/workspaces/{workspace_id}/members", status_code=201)
def add_member(body: MemberAdd, ctx: PathWsCtx) -> dict:
    with ctx.tx("admin") as conn:
        if body.role == "owner" and ctx.role != "owner":
            raise forbidden("only an owner can add an owner")
        found = conn.execute("SELECT user_id FROM biz.auth_user_by_email(%s)", (body.email.strip(),)).fetchone()
        if found is None:
            raise not_found("no account with this email; they need to sign up first")
        uid = found["user_id"]
        done = conn.execute(
            "INSERT INTO biz.memberships (workspace_id, user_id, role) VALUES (%s, %s, %s)"
            " ON CONFLICT DO NOTHING RETURNING user_id",
            (ctx.workspace_id, uid, body.role),
        ).fetchone()
        if done is None:
            raise conflict("already a member")
        audit(conn, ctx.workspace_id, ctx.user_id, "member.added", "user", str(uid), {"role": body.role})
        row = conn.execute(ser.MEMBER_SELECT + " WHERE m.workspace_id = %s AND m.user_id = %s",
                           (ctx.workspace_id, uid)).fetchone()  # fmt: skip
    return ser.member(row)


@router.patch("/v1/workspaces/{workspace_id}/members/{user_id}")
def update_member(user_id: UUID, body: MemberRole, ctx: PathWsCtx) -> dict:
    with ctx.tx("admin") as conn:
        cur = conn.execute(
            "SELECT role FROM biz.memberships WHERE workspace_id = %s AND user_id = %s FOR UPDATE",
            (ctx.workspace_id, user_id),
        ).fetchone()
        if cur is None:
            raise not_found("not a member")
        if "owner" in (cur["role"], body.role) and ctx.role != "owner":
            raise forbidden("only an owner can make or unmake owners")
        if cur["role"] == "owner" and body.role != "owner" and _owner_count(conn, ctx.workspace_id) <= 1:
            raise conflict("the workspace needs at least one owner", code="last_owner")
        conn.execute("UPDATE biz.memberships SET role = %s WHERE workspace_id = %s AND user_id = %s",
                     (body.role, ctx.workspace_id, user_id))  # fmt: skip
        audit(conn, ctx.workspace_id, ctx.user_id, "member.role_changed", "user", str(user_id),
              {"from": cur["role"], "to": body.role})  # fmt: skip
        row = conn.execute(ser.MEMBER_SELECT + " WHERE m.workspace_id = %s AND m.user_id = %s",
                           (ctx.workspace_id, user_id)).fetchone()  # fmt: skip
    return ser.member(row)


@router.delete("/v1/workspaces/{workspace_id}/members/{user_id}", status_code=204)
def remove_member(user_id: UUID, ctx: PathWsCtx) -> Response:
    with ctx.tx() as conn:
        self_leave = user_id == ctx.user_id
        if not self_leave and ROLE_RANK[ctx.role or "viewer"] < ROLE_RANK["admin"]:
            raise forbidden("this needs the admin role")
        cur = conn.execute(
            "SELECT role FROM biz.memberships WHERE workspace_id = %s AND user_id = %s FOR UPDATE",
            (ctx.workspace_id, user_id),
        ).fetchone()
        if cur is None:
            raise not_found("not a member")
        if cur["role"] == "owner" and not self_leave and ctx.role != "owner":
            raise forbidden("only an owner can remove an owner")
        if cur["role"] == "owner" and _owner_count(conn, ctx.workspace_id) <= 1:
            raise conflict("the last owner cannot leave; delete the workspace or add another owner", code="last_owner")
        audit(conn, ctx.workspace_id, ctx.user_id, "member.removed", "user", str(user_id))
        conn.execute("DELETE FROM biz.memberships WHERE workspace_id = %s AND user_id = %s", (ctx.workspace_id, user_id))
    return Response(status_code=204)
