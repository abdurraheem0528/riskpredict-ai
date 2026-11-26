"""Composio connections, public feed and map layers, mapping rules, config, and
the guarantee that third-party services are mocked only inside tests."""

from __future__ import annotations

import json
import re
from decimal import Decimal
from pathlib import Path
from uuid import UUID

import pytest

pytest.importorskip("fastapi")  # the repository-wide CI job does not install the API

import httpx  # noqa: E402
from apiservice_fixtures import *  # noqa: E402,F401,F403  (fixtures)
from apiservice_helpers import hdr, sign_up  # noqa: E402

from api.core import composio, mapping  # noqa: E402
from api.core.config import ConfigError, parse_key_ring  # noqa: E402

ROOT = Path(__file__).resolve().parents[2]
COMPOSIO = "backend.composio.dev/api/v3.1"


# --- Composio --------------------------------------------------------------------------------


def airtable(fake, status: str = "ACTIVE"):
    state = {"status": status, "deleted": []}

    def link(req):
        body = json.loads(req.content)
        assert body["auth_config_id"] == "ac_test" and body["user_id"].startswith("ws_")
        assert req.headers["x-api-key"] == "test-composio-key"
        return httpx.Response(201, json={"connected_account_id": "ca_new", "redirect_url": "https://connect.composio.dev/link/x",
                                         "expires_at": "2026-11-24T13:00:00Z", "link_token": "lt"})  # fmt: skip

    def tool(req):
        slug = req.url.path.rsplit("/", 1)[1]
        args = json.loads(req.content)["arguments"]
        data = {
            "AIRTABLE_LIST_BASES": {"bases": [{"id": "appB", "name": "Ops"}]},
            "AIRTABLE_GET_BASE_SCHEMA": {"tables": [{"id": "tblS", "name": "Sites", "fields": [
                {"name": "Site"}, {"name": "Lat"}, {"name": "Lon"}, {"name": "Country"}]}]},
            "AIRTABLE_LIST_RECORDS": {"records": [
                {"id": "rec1", "fields": {"Site": "Osaka plant", "Lat": 34.69, "Lon": 135.5, "Country": "JP"}},
                {"id": "rec2", "fields": {"Site": "Lyon depot", "Country": ["France"]}}]},
        }[slug]  # fmt: skip
        if slug == "AIRTABLE_LIST_RECORDS":
            assert args["baseId"] == "appB" and args["tableIdOrName"] == "tblS"
        return httpx.Response(200, json={"data": data, "successful": True, "error": None})

    fake.on("POST", f"{COMPOSIO}/connected_accounts/link", link)
    fake.on("GET", f"{COMPOSIO}/connected_accounts/", lambda r: httpx.Response(200, json={"id": "ca_new", "status": state["status"]}))
    fake.on("DELETE", f"{COMPOSIO}/connected_accounts/", lambda r: (state["deleted"].append(r.url.path), httpx.Response(200, json={"success": True}))[1])
    fake.on("POST", f"{COMPOSIO}/tools/execute/", tool)
    return state


def test_composio_connect_browse_source_and_sync(client, owner_db, fake_http, settings):
    from worker.main import Worker
    from test_api_google_flow import RecordingApi

    state = airtable(fake_http, status="INITIATED")
    me = sign_up(client)
    providers = client.get("/v1/connections/providers", headers=hdr(me)).json()["items"]
    assert {"id": "composio:airtable", "name": "Airtable", "kind": "composio", "available": True} in providers
    assert providers[0] == {"id": "google_sheets", "name": "Google Sheets", "kind": "direct", "available": True}
    assert client.post("/v1/connections/composio", headers=hdr(me), json={"app": "notion"}).status_code == 422
    r = client.post("/v1/connections/composio", headers=hdr(me), json={"app": "airtable"})
    assert r.status_code == 201 and r.json()["redirect_url"].startswith("https://connect.composio.dev/")
    cid = r.json()["connection_id"]
    row = owner_db.execute("SELECT * FROM biz.connections WHERE id = %s", (cid,)).fetchone()
    assert row["composio_connected_account_id"] == "ca_new" and row["refresh_token_ciphertext"] is None
    assert client.get(f"/v1/connections/{cid}", headers=hdr(me)).json()["status"] == "pending"
    assert client.get(f"/v1/connections/{cid}/browse", headers=hdr(me)).json()["code"] == "connection_not_active"

    state["status"] = "ACTIVE"
    assert client.get(f"/v1/connections/{cid}", headers=hdr(me)).json()["status"] == "active"
    bases = client.get(f"/v1/connections/{cid}/browse", headers=hdr(me)).json()["items"]
    assert bases == [{"id": "appB", "name": "Ops", "kind": "base", "selectable": False}]
    tables = client.get(f"/v1/connections/{cid}/browse", headers=hdr(me), params={"parent_id": "appB"}).json()["items"]
    assert tables == [{"id": "tblS", "name": "Sites", "kind": "table", "selectable": True}]

    src = client.post("/v1/sources", headers=hdr(me), json={"connection_id": cid, "external_id": "appB", "external_table_id": "tblS"})
    assert src.status_code == 201, src.text
    src = src.json()
    assert src["kind"] == "composio_table" and src["url"] == "https://airtable.com/appB/tblS"
    spec = {"entity_kind": "site", "columns": [{"column": "Site", "field": "name"}, {"column": "Lat", "field": "lat"},
                                               {"column": "Lon", "field": "lon"}, {"column": "Country", "field": "country"}]}  # fmt: skip
    assert client.post(f"/v1/sources/{src['id']}/mapping/confirm", headers=hdr(me), json=spec).status_code == 200
    worker = Worker(settings, api=RecordingApi())
    worker.db.open()
    try:
        worker.workspace_pass(UUID(me["workspace_id"]), {"sync"})
    finally:
        worker.db.close()
    fc = client.get("/v1/map/entities", headers=hdr(me)).json()
    feats = {f["properties"]["name"]: f for f in fc["features"]}
    assert feats["Osaka plant"]["geometry"] == {"type": "Point", "coordinates": [135.5, 34.69]}
    assert feats["Lyon depot"]["geometry"] is None and feats["Lyon depot"]["properties"]["countries"] == ["FRA"]
    ent = client.get("/v1/entities", headers=hdr(me), params={"q": "osaka"}).json()["items"][0]
    assert ent["lineage"]["row_key"] == "rec1" and ent["lineage"]["url"] == "https://airtable.com/appB/tblS/rec1"

    assert client.delete(f"/v1/connections/{cid}", headers=hdr(me)).status_code == 204
    assert state["deleted"] == ["/api/v3.1/connected_accounts/ca_new"]


def test_composio_expiry_webhook_marks_the_connection(client, owner_db, fake_http, settings):
    from worker.main import Worker
    from test_api_google_flow import RecordingApi
    from test_api_webhooks_stream import composio_post

    airtable(fake_http, status="EXPIRED")
    me = sign_up(client)
    owner_db.execute("INSERT INTO biz.connections (workspace_id, provider, status, composio_connected_account_id)"
                     " VALUES (%s, 'composio:airtable', 'active', 'ca_new')", (me["workspace_id"],))  # fmt: skip
    event = {"id": "msg_x", "type": "composio.connected_account.expired", "data": {"id": "ca_new", "status": "EXPIRED"}}
    assert composio_post(client, settings, event, msg_id="msg_x").status_code == 200
    api = RecordingApi()
    worker = Worker(settings, api=api)
    worker.db.open()
    try:
        worker.workspace_pass(UUID(me["workspace_id"]), {"webhook"})
    finally:
        worker.db.close()
    assert owner_db.execute("SELECT status FROM biz.connections").fetchone()["status"] == "needs_reauth"
    assert owner_db.execute("SELECT status FROM biz.webhook_events").fetchone()["status"] == "processed"
    assert "connection.updated" in api.names() and "alert.created" in api.names()


def test_providers_without_composio(client, settings):
    from dataclasses import replace

    from api.routes import connections

    me = sign_up(client)
    client.rp.settings = replace(settings, composio_api_key="")
    try:
        assert [p["id"] for p in client.get("/v1/connections/providers", headers=hdr(me)).json()["items"]] == ["google_sheets"]
        r = client.post("/v1/connections/composio", headers=hdr(me), json={"app": "airtable"})
        assert r.status_code == 503 and r.json()["code"] == "upstream_unavailable"
    finally:
        client.rp.settings = settings
    assert connections  # imported for the patch target


# --- feeds and map layers ----------------------------------------------------------------------


@pytest.fixture
def feeds(owner_db):
    owner_db.execute("""
        INSERT INTO feeds.events (source, raw_id, type, title, published_at, countries, ports, severity, lat, lon, software) VALUES
         ('usgs', 'us1', 'hazard.earthquake', 'M6 Osaka', now() - interval '1 day', '{JPN}', '{}', 0.7, 34.6, 135.5, '{}'),
         ('gdacs', 'TC-1', 'hazard.tropical_cyclone', 'Cyclone', now() - interval '2 days', '{PHL}', '{}', 0.34, 14.6, 121.0, '{}'),
         ('portwatch', 'p-1', 'port.disruption', 'Port closed', now() - interval '1 day', '{EGY}', '{EGPSD}', 1.0, 30.9, 32.3, '{}'),
         ('ofac', 'x:designation:2026', 'sanction.designation', 'ACME TRADING', now() - interval '3 days', '{IRN}', '{}', NULL, NULL, NULL, '{}'),
         ('osv', 'GHSA-1', 'cyber.vulnerability', 'requests bug', now() - interval '20 days', '{}', '{}', 0.6, NULL, NULL, '{PyPI:requests}')
    """)  # fmt: skip
    owner_db.execute("INSERT INTO feeds.event_tags (source, raw_id, model, model_version, input_md5, event_type, entities)"
                     " VALUES ('ofac', 'x:designation:2026', 'm', 'v', 'h', 'sanctions', '{\"countries\": [\"IRN\"]}')")  # fmt: skip
    owner_db.execute("INSERT INTO feeds.series (series, key, ts, source, value, unit) VALUES"
                     " ('fx.rate', 'EUR/TRY', now() - interval '2 days', 'frankfurter', 35.1, 'TRY per EUR'),"
                     " ('fx.rate', 'EUR/TRY', now() - interval '1 day', 'frankfurter', 37.9, 'TRY per EUR')")  # fmt: skip
    owner_db.execute("INSERT INTO feeds.anomalies (series, key, ts, method, value, baseline, score) VALUES"
                     " ('fx.rate', 'EUR/TRY', now() - interval '1 day', 'robust_z', 37.9, 35.0, 4.2)")  # fmt: skip
    owner_db.execute("INSERT INTO feeds.source_health (source, status, last_run_at, last_success_at) VALUES"
                     " ('usgs', 'ok', now(), now()), ('ofac', 'error', now(), NULL)")  # fmt: skip
    owner_db.execute("INSERT INTO feeds.analysis_status (analysis, subject, status, last_run_at) VALUES"
                     " ('tagging', '*', 'ok', now()), ('anomalies', 'fx.rate|EUR/TRY', 'ok', now())")  # fmt: skip


def test_map_events_and_bbox(client, feeds):
    me = sign_up(client)
    fc = client.get("/v1/map/events", headers=hdr(me["access_token"])).json()
    ids = [f["id"] for f in fc["features"]]
    assert "osv:GHSA-1" not in ids  # no position and no country
    assert set(ids[:2]) == {"usgs:us1", "portwatch:p-1"} and fc["truncated"] is False  # newest first
    ofac = next(f for f in fc["features"] if f["id"] == "ofac:x:designation:2026")
    assert ofac["geometry"] is None and ofac["properties"]["event_type"] == "sanctions"
    box = client.get("/v1/map/events", headers=hdr(me["access_token"]), params={"bbox": "120,10,140,40"}).json()
    assert {f["id"] for f in box["features"]} == {"usgs:us1", "gdacs:TC-1", "ofac:x:designation:2026"}
    typed = client.get("/v1/map/events", headers=hdr(me["access_token"]), params={"types": "hazard.", "min_severity": 0.5}).json()
    assert [f["id"] for f in typed["features"]] == ["usgs:us1"]
    assert client.get("/v1/map/events", params={"bbox": "1,2"}).status_code in (401, 422)
    assert client.get("/v1/map/events", headers=hdr(me["access_token"]), params={"bbox": "1,2"}).status_code == 422
    assert client.get("/v1/map/events").status_code == 401


def test_disruptions_use_the_engine_chokepoints(client, feeds):
    me = sign_up(client)
    fc = client.get("/v1/map/disruptions", headers=hdr(me["access_token"])).json()
    by_id = {f["id"]: f["properties"] for f in fc["features"]}
    assert by_id["chokepoint:suez"]["status"] == "disrupted" and by_id["chokepoint:suez"]["event_ids"] == ["portwatch:p-1"]
    assert by_id["portwatch:p-1"]["kind"] == "port_disruption" and by_id["portwatch:p-1"]["ports"] == ["EGPSD"]


def test_feed_lists_series_and_health(client, feeds):
    me = sign_up(client)
    h = hdr(me["access_token"])
    page = client.get("/v1/feeds/events", headers=h, params={"limit": 2}).json()
    assert len(page["items"]) == 2 and page["next_cursor"]
    rest = client.get("/v1/feeds/events", headers=h, params={"limit": 10, "cursor": page["next_cursor"]}).json()
    assert len(rest["items"]) == 3 and rest["next_cursor"] is None
    assert [e["raw_id"] for e in client.get("/v1/feeds/events", headers=h, params={"software": "PyPI:requests"}).json()["items"]] == ["GHSA-1"]
    assert [e["source"] for e in client.get("/v1/feeds/events", headers=h, params={"type": "hazard."}).json()["items"]] == ["usgs", "gdacs"]
    assert client.get("/v1/feeds/events", headers=h, params={"event_type": "sanctions"}).json()["items"][0]["tag"]["event_type"] == "sanctions"
    one = client.get("/v1/feeds/events/ofac/x:designation:2026", headers=h).json()
    assert one["title"] == "ACME TRADING"
    assert client.get("/v1/feeds/events/ofac/missing", headers=h).status_code == 404
    s = client.get("/v1/feeds/series", headers=h, params={"series": "fx.rate", "key": "EUR/TRY"}).json()
    assert s["unit"] == "TRY per EUR" and [p["value"] for p in s["points"]] == [35.1, 37.9] and len(s["anomalies"]) == 1
    an = client.get("/v1/feeds/anomalies", headers=h, params={"series": "fx."}).json()["items"]
    assert an[0]["key"] == "EUR/TRY" and an[0]["method"] == "robust_z"
    health = client.get("/v1/feeds/health", headers=h).json()
    assert [x["source"] for x in health["sources"]] == ["ofac", "usgs"]
    assert health["analyses"] == [{"analysis": "tagging", "status": "ok", "last_run_at": health["analyses"][0]["last_run_at"],
                                   "last_success_at": None}]  # fmt: skip
    summary = client.get("/v1/risks/summary", headers=hdr(me)).json()
    assert summary["feeds_as_of"] is not None


def test_health_needs_no_auth_and_no_database_query(client):
    r = client.get("/v1/health")
    assert r.status_code == 200 and r.json()["status"] == "ok"
    assert r.json()["schema_version"] >= "007_biz_lookups"


def test_healthz_is_minimal_and_needs_no_auth(client):
    r = client.get("/healthz")
    assert r.status_code == 200 and r.json() == {"status": "ok"}


def test_workspace_delete_removes_everything(client, owner_db):
    from test_api_isolation import seed_workspace

    me = sign_up(client)
    seed_workspace(owner_db, me["workspace_id"], "d")
    assert client.patch(f"/v1/workspaces/{me['workspace_id']}", headers=hdr(me), json={"base_currency": "XXZ"}).status_code == 422
    w = client.patch(f"/v1/workspaces/{me['workspace_id']}", headers=hdr(me), json={"base_currency": "EUR", "alert_min_score": 40}).json()
    assert w["base_currency"] == "EUR" and w["alert_min_score"] == 40
    assert client.delete(f"/v1/workspaces/{me['workspace_id']}", headers=hdr(me)).status_code == 204
    for t in ("workspaces", "connections", "entities", "risks", "alerts", "memberships"):
        assert owner_db.execute(f"SELECT count(*) AS n FROM biz.{t}").fetchone()["n"] == 0, t
    assert client.get("/v1/me", headers=hdr(me)).json()["memberships"] == []


# --- mapping rules -------------------------------------------------------------------------------


def rows(*values: dict) -> list[mapping.RawRow]:
    return [mapping.RawRow(row_number=i + 2, record_id=None, values=v) for i, v in enumerate(values)]


def test_normalisation_follows_the_codes_and_never_guesses():
    spec = {"entity_kind": None, "columns": [
        {"column": "N", "field": "name"}, {"column": "K", "field": "kind"}, {"column": "C", "field": "countries"},
        {"column": "O", "field": "route_origin_port"}, {"column": "D", "field": "route_destination_port"},
        {"column": "V", "field": "via_chokepoints", "split": ";"}, {"column": "HS", "field": "hs_code"},
        {"column": "SW", "field": "software"}, {"column": "R", "field": "revenue_share"}, {"column": "X", "field": "criticality"},
        {"column": "CUR", "field": "currency"}]}  # fmt: skip
    res = mapping.apply(spec, rows(
        {"N": "Shanghai–Rotterdam", "K": "Routes", "C": "China, Netherlands", "O": "cn sha", "D": "NL RTM",
         "V": "Suez Canal; strait of malacca", "HS": "7208.10", "X": "4", "R": "12%", "CUR": "usd"},
        {"N": "requests", "K": "software", "SW": "pkg:pypi/requests@2.31.0", "HS": "123", "R": "12", "X": "urgent", "CUR": "$"},
        {"N": "", "K": "supplier"},
        {"N": "Mystery", "K": "planet"},
    ), chokepoints=mapping.chokepoint_resolver())
    route, sw = res.entities
    assert route.kind == "route" and route.countries == ["CHN", "NLD"] and route.ports == ["CNSHA", "NLRTM"]
    assert route.attrs["chokepoints"] == ["suez", "malacca"] and route.hs_codes == ["720810"]
    assert route.criticality == "critical" and route.revenue_share == Decimal("0.1200") and route.currencies == ["USD"]
    assert sw.software == ["pkg:pypi/requests@2.31.0"] and sw.hs_codes == [] and sw.revenue_share is None
    msgs = {(p["row_number"], p["message"]) for p in res.problems}
    assert {(3, "not an HS code (2, 4 or 6 digits)"), (3, "not a share between 0 and 1 (or a percentage)"),
            (3, "not low, medium, high or critical"), (3, "not an ISO 4217 currency code"), (4, "missing name"),
            (5, "not a known entity kind")} <= msgs  # fmt: skip


def test_spec_validation():
    cols = ["Name", "Lat", "Lon", "Other"]
    _, errs = mapping.validate_spec({"entity_kind": "site", "columns": [{"column": "Name", "field": "name"},
                                                                        {"column": "Lat", "field": "lat"}]}, cols)  # fmt: skip
    assert any("lat and lon" in e["message"] for e in errs)
    _, errs = mapping.validate_spec({"entity_kind": "site", "columns": [{"column": "Name", "field": "name"},
                                                                        {"column": "Other", "field": "name"}]}, cols)  # fmt: skip
    assert errs == [{"field": "/columns/1/field", "message": "only one column can be name"}]
    clean, errs = mapping.validate_spec({"entity_kind": "site", "columns": [{"column": "Name", "field": "name"}]}, cols)
    assert errs == [] and clean == {"entity_kind": "site", "columns": [{"column": "Name", "field": "name"}]}


def test_heuristics():
    cols = [{"name": n, "samples": s} for n, s in [
        ("Vendor name", ["Acme"]), ("Vendor ID", ["V1"]), ("Country of origin", ["Japan"]), ("Annual spend", ["100"]),
        ("Spend currency", ["EUR"]), ("Tier", ["1"]), ("Package", ["pkg:npm/left-pad@1.0.0"]), ("Misc", ["NLRTM", "JPTYO"])]]  # fmt: skip
    got = {c["column"]: c["field"] for c in mapping.heuristic_suggest(cols)["columns"]}
    assert got == {"Vendor name": "name", "Vendor ID": "row_key", "Country of origin": "country", "Annual spend": "annual_spend",
                   "Spend currency": "spend_currency", "Tier": "criticality", "Package": "software", "Misc": "port"}  # fmt: skip
    assert mapping.heuristic_suggest(cols)["entity_kind"] == "supplier"


def test_key_ring_parsing():
    import base64

    k = base64.b64encode(b"\1" * 32).decode()
    assert [i for i, _ in parse_key_ring(f"k2:{k}, k1:{k}")] == ["k2", "k1"]
    for bad in (f"k1:{k},k1:{k}", "k1:short", "nocolon"):
        with pytest.raises(ConfigError):
            parse_key_ring(bad)


def test_composio_account_id_locations():
    assert composio.account_id_of({"type": "composio.trigger.message", "metadata": {"connected_account_id": "ca_1"}}) == "ca_1"
    assert composio.account_id_of({"type": "composio.connected_account.expired", "data": {"id": "ca_2"}}) == "ca_2"
    assert composio.account_id_of({"type": "github_commit_event", "data": {"connection_id": "ca_3"}}) == "ca_3"
    assert composio.account_id_of({"type": "x"}) is None


# --- third-party services are mocked only in tests -------------------------------------------------


def test_no_mocks_outside_tests():
    pattern = re.compile(r"MockTransport|unittest\.mock|from mock |respx|monkeypatch|FakeHttp")
    for d in ("api", "worker"):
        for path in (ROOT / d).rglob("*.py"):
            assert not pattern.search(path.read_text()), f"{path} mentions a mock"


def test_outbound_transport_is_real_by_default():
    import subprocess
    import sys

    code = "from api.core import http; c = http.client(); assert http.transport is None; print(type(c._transport).__name__)"
    out = subprocess.run([sys.executable, "-c", code], cwd=ROOT, capture_output=True, text=True, check=True)
    assert out.stdout.strip() == "HTTPTransport"


def test_housekeeping_prunes_only_this_workspace(client, owner_db, settings):
    from worker import sync
    from worker.main import Worker
    from test_api_google_flow import RecordingApi

    a, b = sign_up(client), sign_up(client)
    for ws in (a["workspace_id"], b["workspace_id"]):
        owner_db.execute(
            "INSERT INTO biz.webhook_events (workspace_id, provider, delivery_id, event_type, status, received_at) VALUES"
            " (%(ws)s, 'composio', 'old-done', 't', 'processed', now() - interval '40 days'),"
            " (%(ws)s, 'composio', 'old-open', 't', 'received', now() - interval '40 days'),"
            " (%(ws)s, 'composio', 'new-done', 't', 'processed', now())",
            {"ws": ws},
        )
        owner_db.execute("INSERT INTO biz.connections (workspace_id, provider, status, created_at)"
                         " VALUES (%s, 'google_sheets', 'pending', now() - interval '2 days')", (ws,))  # fmt: skip
    worker = Worker(settings, api=RecordingApi())
    worker.db.open()
    try:
        sync.housekeeping(worker.ctx, UUID(a["workspace_id"]))
    finally:
        worker.db.close()
    left = owner_db.execute("SELECT workspace_id::text AS ws, delivery_id FROM biz.webhook_events ORDER BY 1, 2").fetchall()
    assert [r["delivery_id"] for r in left if r["ws"] == a["workspace_id"]] == ["new-done", "old-open"]
    assert len([r for r in left if r["ws"] == b["workspace_id"]]) == 3
    assert owner_db.execute("SELECT count(*) AS n FROM biz.connections").fetchone()["n"] == 1
