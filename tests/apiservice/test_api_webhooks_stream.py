"""Inbound webhooks are idempotent and answer fast; SSE tickets are single use."""

from __future__ import annotations

import json
import socket
import threading
import time
import uuid

import pytest

pytest.importorskip("fastapi")  # the repository-wide CI job does not install the API

import httpx  # noqa: E402
from apiservice_fixtures import *  # noqa: E402,F401,F403  (fixtures)
from apiservice_helpers import hdr, sign_up  # noqa: E402

from api.core import composio, crypto  # noqa: E402

# --- Google Drive -------------------------------------------------------------------------


def drive_connection(db, ws: str, token: str) -> str:
    cid = str(uuid.uuid4())
    db.execute(
        "INSERT INTO biz.connections (id, workspace_id, provider, status, refresh_token_ciphertext, refresh_token_key_id,"
        " watch_channel_id, watch_resource_id, watch_token_hash, watch_expires_at)"
        " VALUES (%s, %s, 'google_sheets', 'active', %s, 'k1', %s, 'res-1', %s, now() + interval '6 days')",
        (cid, ws, b"\0" * 40, f"chan-{cid}", crypto.sha256(token)),
    )
    return cid


def drive_headers(cid: str, token: str, n: str = "7", state: str = "change") -> dict:
    return {"X-Goog-Channel-ID": f"chan-{cid}", "X-Goog-Channel-Token": token, "X-Goog-Resource-ID": "res-1",
            "X-Goog-Resource-State": state, "X-Goog-Message-Number": n}  # fmt: skip


def test_drive_notification_is_recorded_once(client, owner_db):
    me = sign_up(client)
    cid = drive_connection(owner_db, me["workspace_id"], "chan-token")
    for _ in range(3):
        assert client.post("/v1/webhooks/google-drive", headers=drive_headers(cid, "chan-token")).status_code == 200
    rows = owner_db.execute("SELECT delivery_id, status, provider FROM biz.webhook_events").fetchall()
    assert rows == [{"delivery_id": f"chan-{cid}:7", "status": "received", "provider": "google_drive"}]
    assert client.rp.hints.take() == {me["workspace_id"]: ["webhook"]}
    client.post("/v1/webhooks/google-drive", headers=drive_headers(cid, "chan-token", "8"))
    assert owner_db.execute("SELECT count(*) AS n FROM biz.webhook_events").fetchone()["n"] == 2


def test_drive_sync_message_is_recorded_and_ignored(client, owner_db):
    me = sign_up(client)
    cid = drive_connection(owner_db, me["workspace_id"], "t")
    assert client.post("/v1/webhooks/google-drive", headers=drive_headers(cid, "t", "1", "sync")).status_code == 200
    row = owner_db.execute("SELECT status, processed_at FROM biz.webhook_events").fetchone()
    assert row["status"] == "ignored" and row["processed_at"] is not None
    assert client.rp.hints.take() == {}


def test_drive_wrong_token_unknown_channel_and_bad_headers(client, owner_db):
    me = sign_up(client)
    cid = drive_connection(owner_db, me["workspace_id"], "right")
    assert client.post("/v1/webhooks/google-drive", headers=drive_headers(cid, "wrong")).status_code == 200
    h = drive_headers(cid, "right")
    h["X-Goog-Resource-ID"] = "other"
    assert client.post("/v1/webhooks/google-drive", headers=h).status_code == 200
    assert client.post("/v1/webhooks/google-drive", headers=drive_headers(str(uuid.uuid4()), "x")).status_code == 200
    assert owner_db.execute("SELECT count(*) AS n FROM biz.webhook_events").fetchone()["n"] == 0
    h = drive_headers(cid, "right")
    del h["X-Goog-Message-Number"]
    assert client.post("/v1/webhooks/google-drive", headers=h).status_code == 400
    assert client.post("/v1/webhooks/google-drive", headers=drive_headers(cid, "right", state="bogus")).status_code == 400


# --- Composio -------------------------------------------------------------------------------


def composio_post(client, settings, payload: dict, msg_id: str = "msg_1", ts: int | None = None, secret: str | None = None):
    body = json.dumps(payload).encode()
    ts_s = str(ts if ts is not None else int(time.time()))
    sig = composio.sign(secret or settings.composio_webhook_secret, msg_id, ts_s, body)
    return client.post("/v1/webhooks/composio", content=body, headers={
        "webhook-id": msg_id, "webhook-timestamp": ts_s, "webhook-signature": sig, "content-type": "application/json"})  # fmt: skip


def trigger(account: str) -> dict:
    return {"id": "msg_1", "type": "composio.trigger.message", "timestamp": "2026-11-24T12:00:00Z",
            "metadata": {"trigger_slug": "AIRTABLE_RECORD_CHANGED", "connected_account_id": account, "toolkit_slug": "airtable",
                         "trigger_id": "ti_1", "auth_config_id": "ac_test", "user_id": "ws_x", "log_id": "log_1"},
            "data": {"record": {"id": "rec1", "fields": {"Name": "never stored"}}}}  # fmt: skip


def test_composio_signature_and_idempotency(client, settings, owner_db):
    me = sign_up(client)
    owner_db.execute(
        "INSERT INTO biz.connections (workspace_id, provider, status, composio_connected_account_id)"
        " VALUES (%s, 'composio:airtable', 'active', 'ca_1')",
        (me["workspace_id"],),
    )
    assert composio_post(client, settings, trigger("ca_1"), secret="wrong").status_code == 401
    assert composio_post(client, settings, trigger("ca_1"), ts=int(time.time()) - 600).status_code == 401  # too old
    for _ in range(2):
        assert composio_post(client, settings, trigger("ca_1")).status_code == 200
    rows = owner_db.execute("SELECT delivery_id, event_type, payload FROM biz.webhook_events").fetchall()
    assert len(rows) == 1 and rows[0]["delivery_id"] == "msg_1" and rows[0]["event_type"] == "AIRTABLE_RECORD_CHANGED"
    assert "never stored" not in json.dumps(rows[0]["payload"])  # row contents are not kept
    assert composio_post(client, settings, trigger("ca_unknown"), msg_id="msg_2").status_code == 200
    assert owner_db.execute("SELECT count(*) AS n FROM biz.webhook_events").fetchone()["n"] == 1


def test_composio_lifecycle_event_routes_by_data_id(client, settings, owner_db):
    me = sign_up(client)
    owner_db.execute("INSERT INTO biz.connections (workspace_id, provider, status, composio_connected_account_id)"
                     " VALUES (%s, 'composio:airtable', 'active', 'ca_9')", (me["workspace_id"],))  # fmt: skip
    event = {"id": "msg_9", "type": "composio.connected_account.expired", "metadata": {"org_id": "ok_1"},
             "data": {"id": "ca_9", "toolkit": {"slug": "airtable"}, "status": "EXPIRED"}}  # fmt: skip
    assert composio_post(client, settings, event, msg_id="msg_9").status_code == 200
    assert owner_db.execute("SELECT event_type FROM biz.webhook_events").fetchone()["event_type"] == "composio.connected_account.expired"


def test_composio_signature_unit():
    body = b'{"a":1}'
    sig = composio.sign("s3cret", "msg_1", "1700000000", body)
    assert sig.startswith("v1,")
    assert composio.verify("s3cret", "msg_1", "1700000000", sig, body, now=1700000100)
    assert composio.verify("s3cret", "msg_1", "1700000000", f"v1,bogus {sig}", body, now=1700000100)  # rotation
    assert not composio.verify("s3cret", "msg_1", "1700000000", sig, b'{"a":2}', now=1700000100)
    assert not composio.verify("s3cret", "msg_2", "1700000000", sig, body, now=1700000100)
    assert not composio.verify("s3cret", "msg_1", "1700000000", sig, body, now=1700000400)
    assert not composio.verify("", "msg_1", "1700000000", sig, body, now=1700000100)


# --- SSE --------------------------------------------------------------------------------------


def test_stream_ticket_is_single_use_and_bound(client):
    me = sign_up(client)
    r = client.post("/v1/stream/ticket", headers=hdr(me))
    assert r.status_code == 201
    ticket = r.json()["ticket"]
    t = client.rp.tickets.redeem(ticket)
    assert t is not None and str(t.workspace_id) == me["workspace_id"] and str(t.user_id) == me["user"]["id"]
    assert client.rp.tickets.redeem(ticket) is None
    r = client.get("/v1/stream", params={"ticket": ticket})
    assert r.status_code == 401
    assert client.get("/v1/stream", params={"ticket": "made-up"}).status_code == 401


def test_internal_endpoints_need_the_token(client, settings):
    me = sign_up(client)
    ev = {"events": [{"workspace_id": me["workspace_id"], "event": "resync", "data": {}}]}
    assert client.post("/internal/events", json=ev).status_code == 401
    assert client.post("/internal/events", json=ev, headers={"Authorization": "Bearer wrong"}).status_code == 401
    assert client.get("/internal/work?wait=0", headers={"Authorization": "Bearer wrong"}).status_code == 401
    ok = {"Authorization": f"Bearer {settings.internal_token}"}
    assert client.post("/internal/events", json=ev, headers=ok).status_code == 202
    client.rp.hints.add(me["workspace_id"], "sync")
    assert client.get("/internal/work?wait=0", headers=ok).json() == {"items": [{"workspace_id": me["workspace_id"], "reasons": ["sync"]}]}


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture
def live_server(settings, owner_db, fake_http):
    import uvicorn

    from api.main import create_app

    app = create_app(settings)
    port = _free_port()
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning", access_log=False))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    for _ in range(100):
        if server.started:
            break
        time.sleep(0.05)
    yield f"http://127.0.0.1:{port}", app
    server.should_exit = True
    thread.join(5)


def test_stream_delivers_worker_events_and_resumes(live_server, settings):
    base, app = live_server
    with httpx.Client(base_url=base, timeout=10) as c:
        me = c.post("/v1/auth/sign-up", json={"email": "sse@example.test", "password": "x" * 12, "workspace_name": "S"}).json()
        ticket = c.post("/v1/stream/ticket", headers=hdr(me)).json()["ticket"]
        received: list[str] = []
        with c.stream("GET", "/v1/stream", params={"ticket": ticket}) as resp:
            assert resp.status_code == 200 and resp.headers["content-type"].startswith("text/event-stream")
            lines = resp.iter_lines()
            assert next(lines) == ": connected"
            push = {"events": [{"workspace_id": me["workspace_id"], "event": "source.synced",
                                "data": {"source_id": str(uuid.uuid4()), "status": "active"}}]}  # fmt: skip
            assert c.post("/internal/events", json=push, headers={"Authorization": f"Bearer {settings.internal_token}"}).status_code == 202
            for line in lines:
                if line == "" and not received:
                    continue  # the blank line that ends the ": connected" comment
                received.append(line)
                if line == "":
                    break
        assert received[0].startswith("id: ") and received[1] == "event: source.synced"
        assert json.loads(received[2].removeprefix("data: "))["status"] == "active"
        first_id = received[0].removeprefix("id: ")
        # the ticket was used up by the first connection
        assert c.get("/v1/stream", params={"ticket": ticket}).status_code == 401
    hub = app.state.rp.hub
    hub.publish(me["workspace_id"], "resync", {})
    _, replay = hub.subscribe(me["workspace_id"], first_id)
    assert [m.event for m in replay] == ["resync"]
    _, replay = hub.subscribe(me["workspace_id"], "123-1")  # an id from before a restart
    assert [m.event for m in replay] == ["resync"] and replay[0].data == {}
