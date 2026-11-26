"""Google Sheets end to end, with Google and Groq replaced by test handlers:
OAuth (PKCE, drive.file) → encrypted refresh token → Picker token → source →
Groq mapping suggestion (and the heuristic fallback) → preview → confirm →
sync and engine run in the request → Drive watch → change notification →
re-sync in the webhook request → the worker's renewal and re-read."""

from __future__ import annotations

import json
from urllib.parse import parse_qs, urlsplit
from uuid import UUID

import pytest

pytest.importorskip("fastapi")  # the repository-wide CI job does not install the API

import engine  # noqa: E402
import httpx  # noqa: E402
from apiservice_fixtures import *  # noqa: E402,F401,F403  (fixtures)
from apiservice_helpers import event_names, groq_reply, hdr, sign_up  # noqa: E402

from api.core import crypto, google  # noqa: E402

SHEET_ID = "sheet-abc"
GRID = [
    ["Supplier", "Code", "Country", "Spend", "Currency", "Bogus"],
    ["Acme KK", "S1", "Japan", "1,250,000", "JPY", "x"],
    ["Beta GmbH", "S2", "Narnia", "10", "EUR", ""],
    ["", "", "", "", "", ""],
    ["Gamma", "S3", "DE", "lots", "EUR", ""],
]
MAPPING_REPLY = {
    "entity_kind": "supplier",
    "columns": [
        {"column": "Supplier", "field": "name", "confidence": 0.95, "reason": "company names"},
        {"column": "Country", "field": "country", "confidence": 0.9, "reason": "countries"},
        {"column": "Spend", "field": "annual_spend", "confidence": 0.8, "reason": "amounts"},
        {"column": "Currency", "field": "spend_currency", "confidence": 0.8, "reason": "ISO codes"},
        {"column": "Bogus", "field": "not_a_field", "confidence": 2},
        {"column": "Invented", "field": "name"},
    ],
}
SPEC = {
    "entity_kind": "supplier",
    "columns": [
        {"column": "Supplier", "field": "name"},
        {"column": "Code", "field": "row_key"},
        {"column": "Country", "field": "country"},
        {"column": "Spend", "field": "annual_spend"},
        {"column": "Currency", "field": "spend_currency"},
        {"column": "Bogus", "field": "ignore"},
    ],
}


class Google:
    """Just enough of Google's OAuth, Drive and Sheets APIs."""

    def __init__(self, fake):
        self.grid = [row[:] for row in GRID]
        self.changed_files: list[str] = []
        self.token_requests: list[dict] = []
        fake.on("POST", "oauth2.googleapis.com/token", self.token)
        fake.on("POST", "oauth2.googleapis.com/revoke", {})
        fake.on("GET", "www.googleapis.com/drive/v3/about", {"user": {"emailAddress": "owner@acme.test"}})
        fake.on("GET", f"sheets.googleapis.com/v4/spreadsheets/{SHEET_ID}",
                {"properties": {"title": "Suppliers"}, "sheets": [{"properties": {"sheetId": 0, "title": "Sheet1", "index": 0}}]})
        fake.on("GET", f"sheets.googleapis.com/v4/spreadsheets/{SHEET_ID}/values/", lambda r: httpx.Response(200, json={"values": self.grid}))
        fake.on("GET", "sheets.googleapis.com/v4/spreadsheets/unpicked", {"error": {"status": "PERMISSION_DENIED"}}, status=403)
        fake.on("GET", "www.googleapis.com/drive/v3/changes/startPageToken", {"startPageToken": "p1"})
        fake.on("POST", "www.googleapis.com/drive/v3/changes/watch", self.watch)
        fake.on("GET", "www.googleapis.com/drive/v3/changes", self.changes)
        fake.on("POST", "www.googleapis.com/drive/v3/channels/stop", {})

    def token(self, req: httpx.Request) -> httpx.Response:
        form = {k: v[0] for k, v in parse_qs(req.content.decode()).items()}
        self.token_requests.append(form)
        if form["grant_type"] == "authorization_code":
            return httpx.Response(200, json={"access_token": "at-1", "expires_in": 3599, "refresh_token": "rt-secret-value",
                                             "scope": google.SCOPE, "token_type": "Bearer"})  # fmt: skip
        assert form["refresh_token"] == "rt-secret-value"
        return httpx.Response(200, json={"access_token": "at-2", "expires_in": 3599, "scope": google.SCOPE})

    def watch(self, req: httpx.Request) -> httpx.Response:
        body = json.loads(req.content)
        assert body["type"] == "web_hook" and body["address"] == "https://api.example.test/v1/webhooks/google-drive"
        self.last_watch = body
        return httpx.Response(200, json={"resourceId": "res-9", "expiration": body["expiration"]})

    def changes(self, req: httpx.Request) -> httpx.Response:
        files, self.changed_files = self.changed_files, []
        return httpx.Response(200, json={"newStartPageToken": "p2", "changes": [{"fileId": f} for f in files]})


def groq(fake, mapping_status: int = 200, advice: str = "Confirm stock with the supplier and line up an alternative source."):
    def handler(req: httpx.Request) -> httpx.Response:
        body = json.loads(req.content)
        assert req.headers["authorization"] == "Bearer test-groq-key"
        if body["model"] == "test-mapping-model":
            if mapping_status != 200:
                return httpx.Response(mapping_status, json={"error": {"message": "rate limit"}})
            sent = json.loads(body["messages"][1]["content"])
            assert all(len(c["samples"]) <= 5 for c in sent["columns"])  # headers + ≤5 samples only
            return httpx.Response(200, json=groq_reply(MAPPING_REPLY))
        return httpx.Response(200, json=groq_reply(advice))

    fake.on("POST", "api.groq.com/openai/v1/chat/completions", handler)


def connect_google(client, owner_db, me) -> str:
    r = client.post("/v1/connections/google/start", headers=hdr(me), json={"return_path": "/connect"})
    assert r.status_code == 201, r.text
    conn_id = r.json()["connection_id"]
    q = {k: v[0] for k, v in parse_qs(urlsplit(r.json()["authorization_url"]).query).items()}
    assert q["scope"] == google.SCOPE and q["access_type"] == "offline" and q["prompt"] == "consent"
    assert q["code_challenge_method"] == "S256" and q["redirect_uri"] == "https://api.example.test/v1/connections/google/callback"
    cb = client.get("/v1/connections/google/callback", params={"state": q["state"], "code": "auth-code"})
    assert cb.status_code == 302
    assert cb.headers["location"] == f"https://dash.example.test/connect?connection_id={conn_id}&status=ok"
    # the same state cannot be used twice
    assert client.get("/v1/connections/google/callback", params={"state": q["state"], "code": "again"}).status_code == 400
    return conn_id, q


def test_google_oauth_encrypts_the_refresh_token(client, owner_db, fake_http, settings):
    g = Google(fake_http)
    me = sign_up(client)
    conn_id, q = connect_google(client, owner_db, me)
    exchange = g.token_requests[0]
    assert google.code_challenge(exchange["code_verifier"]) == q["code_challenge"]
    row = owner_db.execute("SELECT * FROM biz.connections WHERE id = %s", (conn_id,)).fetchone()
    assert row["status"] == "active" and row["account_label"] == "owner@acme.test" and row["oauth_state_hash"] is None
    blob = bytes(row["refresh_token_ciphertext"])
    assert b"rt-secret-value" not in blob and row["refresh_token_key_id"] == "k2"
    assert crypto.decrypt_token(blob, "k2", settings.token_keys, me["workspace_id"], conn_id) == "rt-secret-value"
    with pytest.raises(crypto.DecryptError):  # bound to its own row
        crypto.decrypt_token(blob, "k2", settings.token_keys, me["workspace_id"], me["workspace_id"])
    listed = client.get("/v1/connections", headers=hdr(me)).json()["items"]
    assert listed[0]["status"] == "active" and "refresh" not in json.dumps(listed)

    pick = client.post(f"/v1/connections/{conn_id}/google/picker-token", headers=hdr(me)).json()
    assert pick["access_token"] == "at-2" and pick["developer_key"] == "test-picker-key" and pick["app_id"] == "123456"
    assert pick["scope"] == google.SCOPE


def test_consent_declined_marks_the_connection(client, owner_db, fake_http):
    me = sign_up(client)
    r = client.post("/v1/connections/google/start", headers=hdr(me), json={}).json()
    state = parse_qs(urlsplit(r["authorization_url"]).query)["state"][0]
    cb = client.get("/v1/connections/google/callback", params={"state": state, "error": "access_denied"})
    assert cb.status_code == 302 and "status=error" in cb.headers["location"]
    assert owner_db.execute("SELECT status FROM biz.connections").fetchone()["status"] == "error"
    assert client.get("/v1/connections/google/callback", params={"state": "garbage"}).status_code == 400


def test_sheet_to_entities_to_risks(client, owner_db, fake_http, settings):
    from worker.main import Worker

    g = Google(fake_http)
    groq(fake_http)
    me = sign_up(client)
    ws = me["workspace_id"]
    conn_id, _ = connect_google(client, owner_db, me)

    assert client.post("/v1/sources", headers=hdr(me), json={"connection_id": conn_id, "external_id": "unpicked"}).status_code == 404
    r = client.post("/v1/sources", headers=hdr(me), json={"connection_id": conn_id, "external_id": SHEET_ID})
    assert r.status_code == 201, r.text
    src = r.json()
    # The mapping suggestion ran inside the request (serverless: no background task).
    assert src["name"] == "Suppliers" and src["status"] == "mapping_suggested"
    assert [c["name"] for c in src["columns"]] == GRID[0]
    assert src["columns"][2]["samples"] == ["Japan", "Narnia", "DE"]
    assert client.post("/v1/sources", headers=hdr(me), json={"connection_id": conn_id, "external_id": SHEET_ID}).status_code == 409

    # The suggestion came from Groq; invalid entries were dropped, missing ones filled by heuristics.
    m = client.get(f"/v1/sources/{src['id']}/mapping", headers=hdr(me)).json()
    assert m["confirmed"] is None and m["suggested"]["model"] == "groq:test-mapping-model"
    fields = {c["column"]: c["field"] for c in m["suggested"]["spec"]["columns"]}
    assert fields == {"Supplier": "name", "Code": "row_key", "Country": "country", "Spend": "annual_spend",
                      "Currency": "spend_currency", "Bogus": "ignore"}  # fmt: skip
    assert {f["field"] for f in m["fields"]} >= {"name", "route_origin_port", "software", "ignore"}
    assert client.get(f"/v1/sources/{src['id']}", headers=hdr(me)).json()["status"] == "mapping_suggested"

    pv = client.post(f"/v1/sources/{src['id']}/mapping/preview", headers=hdr(me), json=SPEC)
    assert pv.status_code == 200, pv.text
    pv = pv.json()
    assert pv["rows_read"] == 3 and len(pv["entities"]["features"]) == 3
    msgs = {(p["row_number"], p["message"]) for p in pv["problems"]}
    assert (3, "not a known country") in msgs and (5, "not a number") in msgs
    bad = client.post(f"/v1/sources/{src['id']}/mapping/confirm", headers=hdr(me),
                      json={"entity_kind": None, "columns": [{"column": "Nope", "field": "name"}]})  # fmt: skip
    assert bad.status_code == 422 and {e["field"] for e in bad.json()["errors"]} >= {"/columns/0/column", "/entity_kind"}
    assert owner_db.execute("SELECT count(*) AS n FROM biz.entities").fetchone()["n"] == 0  # nothing written yet
    assert event_names(owner_db, ws) == ["connection.updated", "mapping.suggested"]

    # A public feed event in Japan (test fixture) for the real engine to match by country: L = s 1.0
    # × r 1.0 × c 0.6 (supplier country match) = 0.6; I = 0.9 (natural_hazard) × 0.5 (default
    # criticality; no FX rates, so the JPY spend share is unknown) = 0.45; score 27.0 (§5–§6).
    owner_db.execute("INSERT INTO feeds.events (source, raw_id, type, title, countries, severity) VALUES"
                     " ('usgs', 'us1', 'hazard.earthquake', 'M6.1 near Osaka', '{JPN}', 1.0)")  # fmt: skip
    assert client.patch(f"/v1/workspaces/{ws}", headers=hdr(me), json={"alert_min_score": 25}).status_code == 200

    # Confirming syncs the sheet and runs the engine inside the request, then writes the events.
    ok = client.post(f"/v1/sources/{src['id']}/mapping/confirm", headers=hdr(me), json=SPEC)
    assert ok.status_code == 200 and ok.json()["status"] == "confirmed" and ok.json()["version"] == 2
    worker = Worker(settings)
    worker.db.open()
    try:
        ents = client.get("/v1/entities", headers=hdr(me)).json()["items"]
        assert [(e["name"], e["countries"], e["exposure"]["annual_spend"]) for e in ents] == [
            ("Acme KK", ["JPN"], "1250000.00"), ("Beta GmbH", [], "10.00"), ("Gamma", ["DEU"], None)]
        assert ents[0]["lineage"]["row_key"] == "S1" and ents[0]["lineage"]["row_number"] == 2
        assert ents[0]["lineage"]["url"].endswith("#gid=0&range=A2")
        names = event_names(owner_db, ws)
        assert names.index("source.sync_started") < names.index("source.synced") < names.index("risk.created")
        assert "alert.created" in names
        risks = client.get("/v1/risks", headers=hdr(me)).json()["items"]
        assert len(risks) == 1 and risks[0]["score"] == 27.0 and risks[0]["entity"]["name"] == "Acme KK"
        detail = client.get(f"/v1/risks/{risks[0]['id']}", headers=hdr(me)).json()
        assert detail["engine_version"] == engine.ENGINE_VERSION
        assert detail["evidence"][0]["match_type"] == "country" and detail["evidence"][0]["match_value"] == "JPN"
        assert detail["advice"]["source"] == "groq" and detail["advice"]["model"] == "groq:test-advice-model"
        assert detail["evidence"][0]["still_in_feeds"] is True
        alerts = client.get("/v1/alerts", headers=hdr(me)).json()
        assert alerts["unread_count"] == 1 and alerts["items"][0]["kind"] == "risk_new"
        assert owner_db.execute("SELECT 1 FROM feeds.watch_terms WHERE origin = 'workspace'").fetchone() is None  # opt-in only

        # The Drive watch channel was opened in the OAuth callback request.
        row = owner_db.execute("SELECT * FROM biz.connections WHERE id = %s", (conn_id,)).fetchone()
        assert row["watch_resource_id"] == "res-9" and row["drive_page_token"] == "p1"
        assert row["watch_renew_after"] < row["watch_expires_at"]
        assert bytes(row["watch_token_hash"]) == crypto.sha256(g.last_watch["token"])

        # Google says the file changed: the webhook request records it, re-reads and removes the deleted row.
        g.grid = GRID[:2] + GRID[4:]
        g.changed_files = [SHEET_ID]
        h = {"X-Goog-Channel-ID": row["watch_channel_id"], "X-Goog-Channel-Token": g.last_watch["token"],
             "X-Goog-Resource-ID": "res-9", "X-Goog-Resource-State": "change", "X-Goog-Message-Number": "2"}  # fmt: skip
        assert client.post("/v1/webhooks/google-drive", headers=h).status_code == 200
        assert [e["name"] for e in client.get("/v1/entities", headers=hdr(me)).json()["items"]] == ["Acme KK", "Gamma"]
        assert owner_db.execute("SELECT status FROM biz.webhook_events").fetchone()["status"] == "processed"
        assert owner_db.execute("SELECT drive_page_token FROM biz.connections").fetchone()["drive_page_token"] == "p2"

        # Renewal: once renew_after passes, a new channel replaces the old one, which is stopped.
        owner_db.execute("UPDATE biz.connections SET watch_renew_after = now() - interval '1 minute'")
        worker.workspace_pass(UUID(ws))
        assert owner_db.execute("SELECT watch_channel_id FROM biz.connections").fetchone()["watch_channel_id"] != row["watch_channel_id"]
        assert len(fake_http.sent_to("www.googleapis.com/drive/v3/channels/stop")) == 1

        # The worker's re-read: unchanged content touches nothing, changed content syncs.
        before = len(event_names(owner_db))
        worker.recheck()
        assert len(event_names(owner_db)) == before
        g.grid = GRID[:2]
        worker.recheck()
        assert [e["name"] for e in client.get("/v1/entities", headers=hdr(me)).json()["items"]] == ["Acme KK"]
    finally:
        worker.db.close()

    # Disconnecting revokes the Google grant and deletes its sources and entities.
    assert client.delete(f"/v1/connections/{conn_id}", headers=hdr(me)).status_code == 204
    assert fake_http.sent_to("oauth2.googleapis.com/revoke")
    assert owner_db.execute("SELECT count(*) AS n FROM biz.entities").fetchone()["n"] == 0


def test_groq_down_falls_back_to_heuristics(client, owner_db, fake_http):
    Google(fake_http)
    groq(fake_http, mapping_status=429)
    me = sign_up(client)
    conn_id, _ = connect_google(client, owner_db, me)
    src = client.post("/v1/sources", headers=hdr(me), json={"connection_id": conn_id, "external_id": SHEET_ID}).json()
    r = client.post(f"/v1/sources/{src['id']}/mapping/suggest", headers=hdr(me))
    assert r.status_code == 200
    body = r.json()
    assert body["model"] == "heuristic" and body["status"] == "suggested"
    fields = {c["column"]: c["field"] for c in body["spec"]["columns"]}
    assert fields["Supplier"] == "name" and fields["Country"] == "country" and fields["Code"] == "row_key"
    assert body["spec"]["entity_kind"] == "supplier"


def test_advice_that_fails_validation_uses_the_template(client, owner_db, fake_http, settings):
    from worker import scoring
    from worker.main import Worker

    assert not scoring.valid_advice("See https://example.com for details.", [{"content": "x"}])
    assert not scoring.valid_advice("There is a 37% chance of delay.", [{"content": "Score 72.0"}])
    assert scoring.valid_advice("Score 72.0 is high; review stock.", [{"content": "Score 72.0"}])
    groq(fake_http, advice="Expect a 45% delay; read www.example.com.")
    me = sign_up(client)
    ws = me["workspace_id"]
    owner_db.execute("INSERT INTO feeds.events (source, raw_id, type, title, countries, severity) VALUES"
                     " ('gdacs', 'EQ-1', 'hazard.earthquake', 'Quake', '{JPN}', 1.0)")  # fmt: skip
    from test_api_isolation import seed_workspace

    seed_workspace(owner_db, ws, "t")
    owner_db.execute("DELETE FROM biz.risks")
    worker = Worker(settings)
    worker.db.open()
    try:
        worker.workspace_pass(UUID(ws), score=True)
    finally:
        worker.db.close()
    row = owner_db.execute("SELECT advice, advice_source, score FROM biz.risks").fetchone()
    assert row["advice_source"] == "template" and "45" not in row["advice"]
    # The supplier is positioned, the quake is not: L = 1.0 × 1.0 × 0.6 (country), I = 0.9 × 0.5.
    assert row["score"] == pytest.approx(27.0)
    assert row["advice"].startswith("Contact Secret supplier t to confirm")  # the engine's template



def test_scenarios_use_the_engine_or_fail_cleanly(client, owner_db, monkeypatch):
    me = sign_up(client)
    body = {"name": "Suez closed", "inputs": {"shocks": [{"type": "event", "category": "logistics", "severity": 0.9,
                                                         "chokepoints": ["suez"]}]}}  # fmt: skip
    r = client.post("/v1/scenarios", headers=hdr(me), json=body)
    assert r.status_code == 201, r.text
    assert r.json()["status"] == "completed" and r.json()["results"]["engine_version"] == engine.ENGINE_VERSION
    assert client.post("/v1/scenarios", headers=hdr(me), json={"name": "empty", "inputs": {}}).status_code == 422
    monkeypatch.setitem(__import__("sys").modules, "engine", None)  # as if C had not merged
    r = client.post("/v1/scenarios", headers=hdr(me), json=body)
    assert r.status_code == 201 and r.json()["status"] == "failed" and "not installed" in r.json()["error"]
    listed = client.get("/v1/scenarios", headers=hdr(me)).json()["items"]
    assert [s["status"] for s in listed] == ["failed", "completed"]
