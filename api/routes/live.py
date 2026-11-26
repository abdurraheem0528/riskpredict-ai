"""Live updates (``GET /v1/events``, polled), inbound webhooks (Drive, Composio), and health.

There is no stream and no worker endpoint: on a serverless platform each request
may land on a different instance, so events live in ``biz.events`` and webhooks
are processed in the request that records them (``api/work.py``)."""

from __future__ import annotations

import asyncio
import hmac
import json
import logging
from typing import Annotated

from fastapi import APIRouter, Header, Query, Request, Response
from psycopg.types.json import Jsonb

from api import __version__, events, work
from api.context import StateDep, WsCtx
from api.core import composio, crypto
from api.core.errors import bad_request, unauthenticated

log = logging.getLogger("riskpredict.live")
router = APIRouter()


@router.get("/v1/health", tags=["system"])
def health(st: StateDep) -> dict:
    # No database query here: health checks must not wake Neon. The schema version is
    # known once this instance has served a request that used the database.
    return {"status": "ok", "version": __version__, "schema_version": st.schema_version or "unknown"}


@router.get("/healthz", include_in_schema=False)
def healthz() -> dict:
    # Liveness for Deploy's check. Outside the /v1 contract: no database, no settings.
    return {"status": "ok"}


# --- live updates (polling) -----------------------------------------------------------


@router.get("/v1/events", tags=["events"])
def list_events(
    ctx: WsCtx,
    after: Annotated[int | None, Query(ge=0, le=2**62)] = None,
    limit: Annotated[int, Query(ge=1, le=events.PAGE)] = 100,
) -> dict:
    with ctx.tx() as conn:
        return events.poll(conn, after, limit)


# --- webhooks ------------------------------------------------------------------------

DRIVE_STATES = {"sync", "change", "add", "remove", "update", "trash", "untrash"}


@router.post("/v1/webhooks/google-drive", tags=["webhooks"])
async def drive_notification(
    st: StateDep,
    x_goog_channel_id: Annotated[str | None, Header(max_length=200)] = None,
    x_goog_channel_token: Annotated[str | None, Header(max_length=500)] = None,
    x_goog_resource_id: Annotated[str | None, Header(max_length=300)] = None,
    x_goog_resource_state: Annotated[str | None, Header(max_length=40)] = None,
    x_goog_message_number: Annotated[str | None, Header(max_length=40)] = None,
) -> Response:
    if not (x_goog_channel_id and x_goog_channel_token and x_goog_resource_id and x_goog_resource_state
            and x_goog_message_number) or x_goog_resource_state not in DRIVE_STATES:  # fmt: skip
        raise bad_request("missing or invalid X-Goog-* headers")
    return await asyncio.to_thread(_record_drive, st, x_goog_channel_id, x_goog_channel_token, x_goog_resource_id,
                                   x_goog_resource_state, x_goog_message_number)  # fmt: skip


def _record_drive(st, x_goog_channel_id: str, x_goog_channel_token: str, x_goog_resource_id: str,
                  x_goog_resource_state: str, x_goog_message_number: str) -> Response:  # fmt: skip
    ok = Response(status_code=200)
    with st.db.tx() as conn:
        route = conn.execute("SELECT * FROM biz.route_drive_channel(%s)", (x_goog_channel_id,)).fetchone()
    if route is None:
        return ok  # unknown channel: answer like any other, learn nothing
    if not crypto.equal(route["watch_token_hash"], crypto.sha256(x_goog_channel_token)) or not hmac.compare_digest(
        (route["watch_resource_id"] or "").encode(), x_goog_resource_id.encode()
    ):
        return ok
    ws = route["workspace_id"]
    is_sync = x_goog_resource_state == "sync"
    with st.db.workspace_tx(ws) as conn:
        inserted = conn.execute(
            "INSERT INTO biz.webhook_events (workspace_id, connection_id, provider, delivery_id, event_type, status,"
            " processed_at, payload) VALUES (%s, %s, 'google_drive', %s, %s, %s, CASE WHEN %s THEN now() END, %s)"
            " ON CONFLICT (workspace_id, provider, delivery_id) DO NOTHING RETURNING id",
            (ws, route["connection_id"], f"{x_goog_channel_id}:{x_goog_message_number}", x_goog_resource_state,
             "ignored" if is_sync else "received", is_sync,
             Jsonb({"resource_state": x_goog_resource_state, "message_number": x_goog_message_number})),
        ).fetchone()  # fmt: skip
    if inserted and not is_sync:
        work.after_webhook(st, ws)  # a few seconds at most; the rest waits for the worker
    return ok


@router.post("/v1/webhooks/composio", tags=["webhooks"])
async def composio_trigger(
    request: Request,
    st: StateDep,
    webhook_id: Annotated[str | None, Header(alias="webhook-id", max_length=300)] = None,
    webhook_timestamp: Annotated[str | None, Header(alias="webhook-timestamp", max_length=40)] = None,
    webhook_signature: Annotated[str | None, Header(alias="webhook-signature", max_length=2000)] = None,
) -> Response:
    body = await request.body()
    if len(body) > 1_000_000:
        raise bad_request("payload too large")
    if not composio.verify(st.settings.composio_webhook_secret, webhook_id or "", webhook_timestamp or "",
                           webhook_signature or "", body):  # fmt: skip
        raise unauthenticated("invalid webhook signature")
    try:
        payload = json.loads(body)
    except ValueError:
        raise bad_request("payload is not JSON") from None
    if not isinstance(payload, dict):
        raise bad_request("payload is not a JSON object")
    account = composio.account_id_of(payload)
    if not account:
        return Response(status_code=200)
    return await asyncio.to_thread(_record_composio, st, account, webhook_id or "", payload)


def _record_composio(st, account: str, delivery_id: str, payload: dict) -> Response:
    with st.db.tx() as conn:
        route = conn.execute("SELECT * FROM biz.route_composio_account(%s)", (account,)).fetchone()
    if route is None:
        return Response(status_code=200)  # unknown account: dropped
    ws = route["workspace_id"]
    meta = payload.get("metadata") if isinstance(payload.get("metadata"), dict) else {}
    data = payload.get("data") if isinstance(payload.get("data"), dict) else {}
    kind = str(payload.get("type") or "")
    event_type = str(meta.get("trigger_slug") or data.get("trigger_slug") or kind or "unknown")[:200]
    minimal = {"type": kind[:100], "trigger_slug": meta.get("trigger_slug"), "status": data.get("status"),
               "connected_account_id": account}  # fmt: skip
    with st.db.workspace_tx(ws) as conn:
        inserted = conn.execute(
            "INSERT INTO biz.webhook_events (workspace_id, connection_id, provider, delivery_id, event_type, payload)"
            " VALUES (%s, %s, 'composio', %s, %s, %s)"
            " ON CONFLICT (workspace_id, provider, delivery_id) DO NOTHING RETURNING id",
            (ws, route["connection_id"], delivery_id[:300], event_type, Jsonb(minimal)),
        ).fetchone()
    if inserted:
        work.after_webhook(st, ws)
    return Response(status_code=200)
