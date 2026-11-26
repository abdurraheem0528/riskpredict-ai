"""Connections: Google Sheets directly (OAuth + Picker), other tools via Composio."""

from __future__ import annotations

import logging
import secrets
import uuid
from datetime import timedelta
from typing import Annotated
from urllib.parse import urlencode
from uuid import UUID

from fastapi import APIRouter, Query, Response
from fastapi.responses import RedirectResponse
from pydantic import BaseModel, Field

from api import auth, events, work
from api.context import StateDep, UserCtx, WsCtx, audit
from api.core import composio, crypto, google
from api.core import serialize as ser
from api.core.errors import Problem, UpstreamUnavailable, bad_request, conflict, not_found
from api.core.providers import CONNECTION_COLUMNS, ConnectionRef, google_access_token, google_refresh_token

log = logging.getLogger("riskpredict.connections")
router = APIRouter(tags=["connections"])

CONNECTION_FIELDS = CONNECTION_COLUMNS + ", watch_channel_id, watch_resource_id"


def disconnect_external(settings, row: dict) -> None:
    """Revoke the Google grant and stop its Drive channel, or delete the Composio
    account. Best effort: a provider that is down must not block deleting our rows."""
    ref = ConnectionRef.from_row(row)
    try:
        if ref.provider == "google_sheets" and ref.refresh_token_ciphertext is not None:
            refresh_token = google_refresh_token(settings, ref)
            if row.get("watch_channel_id"):
                try:
                    token = google_access_token(settings, ref)
                    google.stop_channel(token if isinstance(token, str) else token.access_token,
                                        row["watch_channel_id"], row.get("watch_resource_id"))  # fmt: skip
                except Problem:
                    pass
            google.revoke(refresh_token)
            google.access_tokens.drop(str(ref.id))
        elif ref.provider.startswith("composio:") and ref.composio_connected_account_id:
            composio.delete_account(settings, ref.composio_connected_account_id)
    except Problem as exc:
        log.info("disconnect: provider cleanup skipped (%s)", exc.code)


# --- providers ---------------------------------------------------------------------


@router.get("/v1/connections/providers")
def list_providers(ctx: UserCtx) -> dict:
    s = ctx.settings
    items = [{"id": "google_sheets", "name": "Google Sheets", "kind": "direct", "available": s.google_enabled}]
    if s.composio_enabled:
        for app, name in sorted(composio.APP_NAMES.items()):
            items.append({"id": f"composio:{app}", "name": name, "kind": "composio",
                          "available": app in s.composio_auth_configs})  # fmt: skip
    return {"items": items}


@router.get("/v1/connections")
def list_connections(ctx: WsCtx) -> dict:
    with ctx.tx() as conn:
        rows = conn.execute(ser.CONNECTION_SELECT + " ORDER BY c.created_at, c.id").fetchall()
    return {"items": [ser.connection(r) for r in rows]}


# --- Google -------------------------------------------------------------------------


class GoogleStart(BaseModel):
    return_path: str = Field(default="/connect", pattern=r"^/[A-Za-z0-9/_-]*$", max_length=200)


def _state(ws: UUID, conn_id: UUID, return_path: str) -> str:
    return crypto.b64url(ws.bytes + conn_id.bytes + secrets.token_bytes(16) + return_path.encode())


def _parse_state(state: str) -> tuple[UUID, UUID, str]:
    try:
        raw = crypto.b64url_decode(state)
        if len(raw) < 48:
            raise ValueError
        path = raw[48:].decode()
        if not path.startswith("/"):
            raise ValueError
        return UUID(bytes=raw[:16]), UUID(bytes=raw[16:32]), path
    except (ValueError, UnicodeDecodeError):
        raise bad_request("invalid OAuth state") from None


@router.post("/v1/connections/google/start", status_code=201)
def start_google(ctx: WsCtx, body: GoogleStart | None = None) -> dict:
    s = ctx.settings
    if not s.google_enabled or not s.public_api_url:
        raise UpstreamUnavailable("Google", "Google Sheets is not configured on this server")
    return_path = (body or GoogleStart()).return_path
    conn_id = uuid.uuid4()
    state = _state(ctx.workspace_id, conn_id, return_path)
    with ctx.tx("admin") as conn:
        conn.execute(
            "INSERT INTO biz.connections (id, workspace_id, provider, display_name, status, oauth_state_hash, created_by)"
            " VALUES (%s, %s, 'google_sheets', 'Google Sheets', 'pending', %s, %s)",
            (conn_id, ctx.workspace_id, crypto.sha256(state), ctx.user_id),
        )
        audit(conn, ctx.workspace_id, ctx.user_id, "connection.created", "connection", str(conn_id), {"provider": "google_sheets"})
    verifier = crypto.derive(s.jwt_signing_key, "pkce", state)
    return {"connection_id": str(conn_id), "authorization_url": google.authorization_url(s, state, verifier)}


@router.get("/v1/connections/google/callback")
def google_callback(
    st: StateDep,
    state: Annotated[str, Query(max_length=1000)],
    code: Annotated[str | None, Query(max_length=2000)] = None,
    error: Annotated[str | None, Query(max_length=200)] = None,
) -> Response:
    s = st.settings
    ws, conn_id, return_path = _parse_state(state)

    def back(status: str, problem: str | None = None) -> RedirectResponse:
        q = {"connection_id": str(conn_id), "status": status}
        if problem:
            q["code"] = problem
        return RedirectResponse(f"{s.web_origin}{return_path}?{urlencode(q)}", status_code=302)

    with st.db.workspace_tx(ws) as conn:
        row = conn.execute(
            "SELECT id, oauth_state_hash FROM biz.connections WHERE id = %s AND provider = 'google_sheets'"
            " AND status = 'pending'",
            (conn_id,),
        ).fetchone()
        if row is None or not crypto.equal(row["oauth_state_hash"], crypto.sha256(state)):
            raise bad_request("unknown or already used OAuth state")
        if error or not code:
            conn.execute(
                "UPDATE biz.connections SET status = 'error', status_reason = %s, oauth_state_hash = NULL,"
                " updated_at = now() WHERE id = %s",
                ("Google consent was not given", conn_id),
            )
            events.publish(conn, ws, "connection.updated", {"connection_id": str(conn_id), "status": "error",
                                                            "status_reason": "Google consent was not given"})  # fmt: skip
    if error or not code:
        return back("error", "connection_not_active")
    try:
        grant = google.exchange_code(s, code, crypto.derive(s.jwt_signing_key, "pkce", state))
        if not grant.refresh_token:
            raise conflict("Google returned no refresh token", code="connection_not_active")
        if google.SCOPE not in grant.scopes:
            raise conflict("the Drive file permission was not granted", code="connection_not_active")
        label = google.account_email(grant.access_token)
        blob, key_id = crypto.encrypt_token(grant.refresh_token, s.token_keys, ws, conn_id)
    except Problem as exc:
        with st.db.workspace_tx(ws) as conn:
            conn.execute(
                "UPDATE biz.connections SET status = 'error', status_reason = %s, oauth_state_hash = NULL,"
                " updated_at = now() WHERE id = %s AND status = 'pending'",
                ((exc.detail or "Google sign-in failed")[:500], conn_id),
            )
        return back("error", exc.code)
    with st.db.workspace_tx(ws) as conn:
        done = conn.execute(
            "UPDATE biz.connections SET status = 'active', status_reason = NULL, oauth_state_hash = NULL,"
            " refresh_token_ciphertext = %s, refresh_token_key_id = %s, scopes = %s, account_label = %s,"
            " updated_at = now() WHERE id = %s AND status = 'pending' AND oauth_state_hash = %s RETURNING id",
            (blob, key_id, list(grant.scopes), label, conn_id, crypto.sha256(state)),
        ).fetchone()
        if done:
            audit(conn, ws, None, "connection.activated", "connection", str(conn_id), actor="webhook")
            events.publish(conn, ws, "connection.updated",
                           {"connection_id": str(conn_id), "status": "active", "status_reason": None})  # fmt: skip
    if not done:
        raise bad_request("unknown or already used OAuth state")
    google.access_tokens.put(str(conn_id), grant)
    work.connection_ready(st, ws)  # the Drive watch channel, while the access token is fresh
    return back("ok")


def _load(conn, connection_id: UUID) -> dict:
    row = conn.execute(f"SELECT {CONNECTION_FIELDS} FROM biz.connections WHERE id = %s", (connection_id,)).fetchone()
    if row is None:
        raise not_found("connection not found")
    return row


@router.post("/v1/connections/{connection_id}/google/picker-token")
def picker_token(connection_id: UUID, ctx: WsCtx) -> dict:
    s = ctx.settings
    with ctx.tx("editor") as conn:
        row = _load(conn, connection_id)
    if row["provider"] != "google_sheets":
        raise conflict("not a Google Sheets connection", code="connection_not_active")
    if row["status"] != "active":
        raise conflict("the connection is not active", code="connection_not_active")
    grant = google_access_token(s, ConnectionRef.from_row(row), fresh=True)
    return {
        "access_token": grant.access_token,
        "expires_at": ser.ts(min(grant.expires_at, auth.utcnow() + timedelta(hours=1)).replace(microsecond=0)),
        "developer_key": s.google_picker_api_key,
        "app_id": s.google_project_number,
        "scope": google.SCOPE,
    }


# --- Composio ---------------------------------------------------------------------------


class ComposioStart(BaseModel):
    app: str = Field(pattern=r"^[a-z0-9_]{1,60}$")


@router.post("/v1/connections/composio", status_code=201)
def start_composio(body: ComposioStart, ctx: WsCtx) -> dict:
    s = ctx.settings
    if not s.composio_enabled:
        raise UpstreamUnavailable("Composio", "Composio is not configured on this server")
    if body.app not in composio.available_apps(s):
        raise Problem(422, "validation_failed", "this tool cannot be connected here",
                      errors=[{"field": "/app", "message": "not an available provider"}])  # fmt: skip
    with ctx.tx("admin"):
        pass  # check the role before calling Composio
    conn_id = uuid.uuid4()
    link = composio.create_link(s, body.app, ctx.workspace_id, f"{s.web_origin}/connect?connection_id={conn_id}")
    with ctx.tx("admin") as conn:
        conn.execute(
            "INSERT INTO biz.connections (id, workspace_id, provider, display_name, status,"
            " composio_connected_account_id, composio_auth_config_id, created_by)"
            " VALUES (%s, %s, %s, %s, 'pending', %s, %s, %s)",
            (conn_id, ctx.workspace_id, f"composio:{body.app}", composio.APP_NAMES[body.app],
             link.connected_account_id, link.auth_config_id, ctx.user_id),
        )  # fmt: skip
        audit(conn, ctx.workspace_id, ctx.user_id, "connection.created", "connection", str(conn_id),
              {"provider": f"composio:{body.app}"})  # fmt: skip
    return {"connection_id": str(conn_id), "redirect_url": link.redirect_url}


def refresh_composio_status(st, ws: UUID, row: dict) -> dict | None:
    """Ask Composio about a pending account; store and announce a change. Returns the new status fields."""
    try:
        status, reason = composio.account_status(st.settings, row["composio_connected_account_id"])
    except Problem:
        return None
    if status == row["status"]:
        return None
    with st.db.workspace_tx(ws) as conn:
        conn.execute(
            "UPDATE biz.connections SET status = %s, status_reason = %s, updated_at = now() WHERE id = %s",
            (status, reason, row["id"]),
        )
        events.publish(conn, ws, "connection.updated",
                       {"connection_id": str(row["id"]), "status": status, "status_reason": reason})  # fmt: skip
    if status == "active":
        composio.subscribe_trigger(st.settings, row["provider"], row["composio_connected_account_id"], ws)
    return {"status": status, "status_reason": reason}


@router.get("/v1/connections/{connection_id}")
def get_connection(connection_id: UUID, ctx: WsCtx) -> dict:
    with ctx.tx() as conn:
        row = conn.execute(ser.CONNECTION_SELECT + " WHERE c.id = %s", (connection_id,)).fetchone()
        if row is None:
            raise not_found("connection not found")
    if row["status"] == "pending" and row["provider"].startswith("composio:") and row["composio_connected_account_id"]:
        if (changed := refresh_composio_status(ctx.state, ctx.workspace_id, row)) is not None:
            row = {**row, **changed}
    return ser.connection(row)


@router.delete("/v1/connections/{connection_id}", status_code=204)
def delete_connection(connection_id: UUID, ctx: WsCtx) -> Response:
    with ctx.tx("admin") as conn:
        row = _load(conn, connection_id)
    disconnect_external(ctx.settings, row)
    with ctx.tx("admin") as conn:
        conn.execute("DELETE FROM biz.connections WHERE id = %s", (connection_id,))
        audit(conn, ctx.workspace_id, ctx.user_id, "connection.deleted", "connection", str(connection_id),
              {"provider": row["provider"]})  # fmt: skip
    work.score(ctx.state, ctx.workspace_id)
    return Response(status_code=204)


@router.get("/v1/connections/{connection_id}/browse")
def browse(connection_id: UUID, ctx: WsCtx, parent_id: Annotated[str | None, Query(max_length=200)] = None) -> dict:
    with ctx.tx() as conn:
        row = _load(conn, connection_id)
    if not row["provider"].startswith("composio:"):
        raise conflict("Google Sheets uses the Google Picker instead", code="conflict")
    if row["status"] != "active":
        raise conflict("the connection is not active", code="connection_not_active")
    ref = ConnectionRef.from_row(row)
    if ref.app != "airtable":
        raise conflict(f"browsing {ref.app} is not supported yet")
    items = composio.airtable_browse(ctx.settings, ref.composio_connected_account_id or "", ctx.workspace_id, parent_id)
    return {"items": [{"id": i.id, "name": i.name, "kind": i.kind, "selectable": i.selectable} for i in items]}

