"""Inbound webhooks are idempotent and processed in their own request; live updates
are polled from biz.events (GET /v1/events); rate limits hold across API instances."""

from __future__ import annotations

import json
import time
import uuid

import pytest

pytest.importorskip("fastapi")  # the repository-wide CI job does not install the API

import psycopg  # noqa: E402
from apiservice_fixtures import *  # noqa: E402,F401,F403  (fixtures)
from apiservice_fixtures import DSN  # noqa: E402
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


def test_drive_notification_is_recorded_and_processed_once(client, owner_db, fake_http):
    me = sign_up(client)
    cid = drive_connection(owner_db, me["workspace_id"], "chan-token")
    for _ in range(3):
        assert client.post("/v1/webhooks/google-drive", headers=drive_headers(cid, "chan-token")).status_code == 200
    rows = owner_db.execute("SELECT delivery_id, provider, status, processed_at FROM biz.webhook_events").fetchall()
    assert [(r["delivery_id"], r["provider"]) for r in rows] == [(f"chan-{cid}:7", "google_drive")]
    # processed in the first request (the fixture's refresh token is not real, so it failed), not again
    assert rows[0]["status"] == "failed" and rows[0]["processed_at"] is not None
    assert len(fake_http.calls) == 0  # the stored token could not even be decrypted: nothing was sent
    client.post("/v1/webhooks/google-drive", headers=drive_headers(cid, "chan-token", "8"))
    assert owner_db.execute("SELECT count(*) AS n FROM biz.webhook_events").fetchone()["n"] == 2
    lease = owner_db.execute("SELECT lease_until FROM biz.worker_state").fetchone()
    assert lease["lease_until"] is None  # given back after each pass


def test_a_held_lease_leaves_the_webhook_for_the_worker(client, owner_db, settings):
    me = sign_up(client)
    ws = me["workspace_id"]
    owner_db.execute("INSERT INTO biz.connections (workspace_id, provider, status, composio_connected_account_id)"
                     " VALUES (%s, 'composio:airtable', 'active', 'ca_l')", (ws,))  # fmt: skip
    owner_db.execute("INSERT INTO biz.worker_state (workspace_id, lease_until) VALUES (%s, now() + interval '1 minute')", (ws,))
    assert composio_post(client, settings, trigger("ca_l")).status_code == 200
    assert owner_db.execute("SELECT status FROM biz.webhook_events").fetchone()["status"] == "received"
    # The lease ran out (its holder died): the next webhook request takes it and does the work.
    owner_db.execute("UPDATE biz.worker_state SET lease_until = now() - interval '1 second'")
    assert composio_post(client, settings, trigger("ca_l"), msg_id="msg_2").status_code == 200
    assert {r["status"] for r in owner_db.execute("SELECT status FROM biz.webhook_events").fetchall()} == {"processed"}


def test_drive_sync_message_is_recorded_and_ignored(client, owner_db):
    me = sign_up(client)
    cid = drive_connection(owner_db, me["workspace_id"], "t")
    assert client.post("/v1/webhooks/google-drive", headers=drive_headers(cid, "t", "1", "sync")).status_code == 200
    row = owner_db.execute("SELECT status, processed_at FROM biz.webhook_events").fetchone()
    assert row["status"] == "ignored" and row["processed_at"] is not None
    assert owner_db.execute("SELECT count(*) AS n FROM biz.worker_state").fetchone()["n"] == 0  # no pass ran


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


# --- live updates: GET /v1/events ------------------------------------------------------------


def add_event(db, ws: str, event: str = "source.synced", data: dict | None = None) -> int:
    return db.execute(
        "INSERT INTO biz.events (workspace_id, event, data) VALUES (%s, %s, %s) RETURNING id",
        (ws, event, json.dumps(data or {"source_id": str(uuid.uuid4()), "status": "active"})),
    ).fetchone()["id"]


def test_events_need_auth_and_a_workspace(client):
    me = sign_up(client)
    assert client.get("/v1/events").status_code == 401
    r = client.get("/v1/events", headers={"Authorization": f"Bearer {me['access_token']}"})
    assert r.status_code == 400 and r.json()["code"] == "workspace_required"
    other = sign_up(client)
    assert client.get("/v1/events", headers=hdr(me, other["workspace_id"])).status_code == 404  # not a member
    assert client.get("/v1/events", headers=hdr(me), params={"after": -1}).status_code == 422


def test_events_poll_from_a_cursor_in_pages(client, owner_db):
    me, other = sign_up(client), sign_up(client)
    ws = me["workspace_id"]
    first = client.get("/v1/events", headers=hdr(me)).json()
    assert first == {"items": [], "next": 0, "more": False, "resync": False}  # starts from now
    ids = [add_event(owner_db, ws, data={"n": i}) for i in range(5)]
    add_event(owner_db, other["workspace_id"])  # another workspace's: never shown
    page = client.get("/v1/events", headers=hdr(me), params={"after": 0, "limit": 3}).json()
    assert [e["id"] for e in page["items"]] == ids[:3] and page["more"] is True and page["next"] == ids[2]
    assert page["items"][0]["event"] == "source.synced" and page["items"][0]["data"] == {"n": 0}
    assert page["items"][0]["created_at"].endswith("Z")
    rest = client.get("/v1/events", headers=hdr(me), params={"after": page["next"]}).json()
    assert [e["data"]["n"] for e in rest["items"]] == [3, 4] and rest["more"] is False
    idle = client.get("/v1/events", headers=hdr(me), params={"after": rest["next"]}).json()
    assert idle == {"items": [], "next": rest["next"], "more": False, "resync": False}
    # A fresh client starts from the newest event of its own workspace only.
    assert client.get("/v1/events", headers=hdr(me)).json()["next"] == ids[-1]


def test_an_event_that_commits_late_is_not_skipped(client, owner_db):
    """Ids are allocated before commit: a later id may become visible first."""
    me = sign_up(client)
    ws = me["workspace_id"]
    with psycopg.connect(DSN) as slow:  # a transaction that takes its id now and commits later
        early = slow.execute("INSERT INTO biz.events (workspace_id, event, data) VALUES (%s, 'alert.created', '{}')"
                             " RETURNING id", (ws,)).fetchone()[0]  # fmt: skip
        late = add_event(owner_db, ws)  # committed at once, with a higher id
        assert late > early
        held = client.get("/v1/events", headers=hdr(me), params={"after": 0}).json()
        assert held["items"] == [] and held["next"] == 0  # the newer row waits for the older transaction
        slow.commit()
    both = client.get("/v1/events", headers=hdr(me), params={"after": 0}).json()
    assert [e["id"] for e in both["items"]] == [early, late]


def test_a_cursor_older_than_the_retention_resyncs(client, owner_db, settings):
    from api.core.db import Database
    from worker import sync

    me = sign_up(client)
    ws = me["workspace_id"]
    old = add_event(owner_db, ws)
    owner_db.execute("UPDATE biz.events SET created_at = now() - interval '25 hours' WHERE id = %s", (old,))
    kept = add_event(owner_db, ws)
    db = Database(settings.database_url, max_size=1)
    try:
        sync.housekeeping(sync.Ctx(settings, db), uuid.UUID(ws))  # the worker's 24 h retention
    finally:
        db.close()
    assert [r["id"] for r in owner_db.execute("SELECT id FROM biz.events").fetchall()] == [kept]
    assert owner_db.execute("SELECT events_pruned_through FROM biz.worker_state").fetchone()["events_pruned_through"] == old
    stale = client.get("/v1/events", headers=hdr(me), params={"after": old - 1}).json()
    assert stale == {"items": [], "next": kept, "more": False, "resync": True}
    fine = client.get("/v1/events", headers=hdr(me), params={"after": old}).json()
    assert [e["id"] for e in fine["items"]] == [kept] and fine["resync"] is False
    # a cursor from a database that was reset (beyond any id ever issued) resyncs too
    assert client.get("/v1/events", headers=hdr(me), params={"after": 10**12}).json()["resync"] is True


def test_api_changes_write_events(client, owner_db):
    from test_api_isolation import seed_workspace

    me = sign_up(client)
    ws = me["workspace_id"]
    seed_workspace(owner_db, ws, "e")
    rid = owner_db.execute("SELECT id FROM biz.risks").fetchone()["id"]
    assert client.patch(f"/v1/risks/{rid}", headers=hdr(me), json={"status": "acknowledged"}).status_code == 200
    page = client.get("/v1/events", headers=hdr(me), params={"after": 0}).json()
    assert [e["event"] for e in page["items"]] == ["risk.updated"]
    assert page["items"][0]["data"]["risk"]["status"] == "acknowledged"


def test_the_stream_and_internal_endpoints_are_gone(client):
    me = sign_up(client)
    assert client.post("/v1/stream/ticket", headers=hdr(me)).status_code == 404
    assert client.get("/v1/stream", params={"ticket": "x"}).status_code == 404
    assert client.post("/internal/events", json={"events": []}).status_code == 404
    assert client.get("/internal/work").status_code == 404


# --- rate limits across instances -------------------------------------------------------------


def test_rate_limits_hold_across_api_instances(settings, owner_db, fake_http):
    """Two app instances (as two Vercel instances): the counter is shared in the database."""
    from fastapi.testclient import TestClient

    from api.main import create_app

    with TestClient(create_app(settings), base_url="https://api.example.test") as a, \
         TestClient(create_app(settings), base_url="https://api.example.test") as b:  # fmt: skip
        me = sign_up(a)
        codes = []
        for i in range(11):
            c = (a, b)[i % 2]
            codes.append(c.post("/v1/auth/sign-in", json={"email": me["email"], "password": "wrong-password!!"}).status_code)
        assert codes[:10] == [401] * 10 and codes[10] == 429
        r = b.post("/v1/auth/sign-in", json={"email": me["email"], "password": "wrong-password!!"})
        assert r.status_code == 429 and 1 <= int(r.headers["Retry-After"]) <= 60
    stored = owner_db.execute("SELECT key_hash, hits FROM biz.rate_limits").fetchall()
    assert stored and all(len(bytes(r["key_hash"])) == 32 for r in stored)
    raw = b"".join(bytes(r["key_hash"]) for r in stored)
    assert me["email"].encode() not in raw and b"testclient" not in raw  # hashed, never in clear


def test_rate_limits_use_the_edge_client_ip_header(settings, owner_db, fake_http):
    """CLIENT_IP_HEADER (x-real-ip on Vercel): each client address has its own counter."""
    import dataclasses

    from fastapi.testclient import TestClient

    from api.main import create_app

    app = create_app(dataclasses.replace(settings, client_ip_header="x-real-ip"))
    with TestClient(app, base_url="https://api.example.test") as c:
        body = {"refresh_token": "not-a-real-token-0000000000000000000000000"}
        for _ in range(60):
            c.post("/v1/auth/refresh", json=body, headers={"x-real-ip": "203.0.113.1"})
        assert c.post("/v1/auth/refresh", json=body, headers={"x-real-ip": "203.0.113.1"}).status_code == 429
        assert c.post("/v1/auth/refresh", json=body, headers={"x-real-ip": "203.0.113.2"}).status_code == 401
