"""SSE stream + tickets, inbound webhooks (Drive, Composio), the internal
worker endpoints, and health."""

from __future__ import annotations

import asyncio
import hmac
import json
import logging
from datetime import datetime, timezone
from typing import Annotated, Literal
from uuid import UUID

from fastapi import APIRouter, Header, Query, Request, Response
from fastapi.responses import StreamingResponse
from psycopg.types.json import Jsonb
from pydantic import BaseModel, Field

from api import __version__
from api.context import StateDep, WsCtx
from api.core import composio, crypto
from api.core import serialize as ser
from api.core.errors import bad_request, unauthenticated

log = logging.getLogger("riskpredict.live")
router = APIRouter()
PING_SECONDS = 25.0

EventName = Literal["risk.created", "risk.updated", "risk.resolved", "alert.created", "source.sync_started",
                    "source.synced", "source.sync_failed", "mapping.suggested", "connection.updated",
                    "feeds.updated", "resync"]  # fmt: skip


@router.get("/v1/health", tags=["system"])
def health(st: StateDep) -> dict:
    # No database query here: health checks must not keep Neon awake.
    return {"status": "ok", "version": __version__, "schema_version": st.schema_version or "unknown"}


# --- stream -------------------------------------------------------------------------


@router.post("/v1/stream/ticket", tags=["stream"], status_code=201)
def stream_ticket(ctx: WsCtx) -> dict:
    with ctx.tx():
        pass  # membership and session check
    token, expires = ctx.state.tickets.issue(ctx.user_id, ctx.workspace_id)
    return {"ticket": token, "expires_at": ser.ts(datetime.fromtimestamp(int(expires), tz=timezone.utc))}


@router.get("/v1/stream", tags=["stream"])
async def stream(
    request: Request,
    st: StateDep,
    ticket: Annotated[str, Query(max_length=200)],
    last_event_id: Annotated[str | None, Header(alias="Last-Event-ID", max_length=100)] = None,
) -> StreamingResponse:
    t = st.tickets.redeem(ticket)
    if t is None:
        raise unauthenticated("the stream ticket is invalid, expired or already used")
    queue, replay = st.hub.subscribe(t.workspace_id, last_event_id)

    async def events():
        try:
            yield ": connected\n\n"
            for m in replay:
                yield m.encode()
            while True:
                if await request.is_disconnected():
                    break
                try:
                    msg = await asyncio.wait_for(queue.get(), PING_SECONDS)
                    yield msg.encode()
                except asyncio.TimeoutError:
                    yield ": ping\n\n"
        finally:
            st.hub.unsubscribe(t.workspace_id, queue)

    return StreamingResponse(
        events(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no", "Connection": "keep-alive"},
    )


# --- webhooks ------------------------------------------------------------------------

DRIVE_STATES = {"sync", "change", "add", "remove", "update", "trash", "untrash"}


@router.post("/v1/webhooks/google-drive", tags=["webhooks"])
def drive_notification(
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
        st.hints.add(ws, "webhook")
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
        st.hints.add(ws, "webhook")
    return Response(status_code=200)


# --- internal (worker → api) --------------------------------------------------------------


def _internal_auth(request: Request, st) -> None:
    token = st.settings.internal_token
    header = request.headers.get("authorization", "")
    scheme, _, value = header.partition(" ")
    if not token or scheme.lower() != "bearer" or not hmac.compare_digest(value.strip().encode(), token.encode()):
        raise unauthenticated("invalid internal token")


class InternalEvent(BaseModel):
    workspace_id: UUID
    event: EventName
    data: dict


class InternalEvents(BaseModel):
    events: list[InternalEvent] = Field(max_length=500)


@router.post("/internal/events", tags=["internal"], status_code=202, include_in_schema=False)
def push_events(body: InternalEvents, request: Request, st: StateDep) -> Response:
    _internal_auth(request, st)
    for e in body.events:
        st.hub.publish(e.workspace_id, e.event, e.data)
    return Response(status_code=202)


@router.get("/internal/work", tags=["internal"], include_in_schema=False)
async def take_work(request: Request, st: StateDep, wait: Annotated[float, Query(ge=0, le=55)] = 25) -> dict:
    _internal_auth(request, st)
    hints = await st.hints.wait(wait)
    return {"items": [{"workspace_id": ws, "reasons": reasons} for ws, reasons in hints.items()]}

