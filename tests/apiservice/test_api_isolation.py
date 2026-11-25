"""Tenant isolation through the API: workspace A cannot see B, even with a forged header."""

from __future__ import annotations

import uuid

import pytest

pytest.importorskip("fastapi")  # the repository-wide CI job does not install the API

from apiservice_fixtures import *  # noqa: E402,F401,F403  (fixtures)
from apiservice_helpers import hdr, sign_up  # noqa: E402


def seed_workspace(db, ws: str, label: str) -> dict:
    """Connection, source, mapping, entity, risk, evidence, alert and scenario rows (test fixtures)."""
    ids = {k: uuid.uuid4() for k in ("conn", "source", "mapping", "entity", "risk", "alert", "scenario")}
    db.execute("INSERT INTO biz.connections (id, workspace_id, provider, status, composio_connected_account_id)"
               " VALUES (%s, %s, 'composio:airtable', 'active', %s)", (ids["conn"], ws, f"ca_{label}"))
    db.execute("INSERT INTO biz.sources (id, workspace_id, connection_id, kind, external_id, external_table_id, name, status)"
               " VALUES (%s, %s, %s, 'composio_table', %s, 'tbl', %s, 'active')",
               (ids["source"], ws, ids["conn"], f"app{label}", f"Suppliers {label}"))
    db.execute("INSERT INTO biz.column_mappings (id, workspace_id, source_id, version, status, mapping, confirmed_at)"
               " VALUES (%s, %s, %s, 1, 'confirmed', '{\"entity_kind\": \"supplier\", \"columns\": []}', now())",
               (ids["mapping"], ws, ids["source"]))
    db.execute("INSERT INTO biz.entities (id, workspace_id, kind, name, countries, lat, lon, source_id, mapping_id,"
               " source_row_key, source_row_hash) VALUES (%s, %s, 'supplier', %s, '{JPN}', 34.6, 135.5, %s, %s, 'rec1', 'h')",
               (ids["entity"], ws, f"Secret supplier {label}", ids["source"], ids["mapping"]))
    db.execute("INSERT INTO biz.risks (id, workspace_id, entity_id, fingerprint, category, title, likelihood, impact,"
               " countries, lat, lon, engine_version) VALUES (%s, %s, %s, %s, 'natural_hazard', %s, 0.5, 0.8, '{JPN}',"
               " 34.6, 135.5, 'test')", (ids["risk"], ws, ids["entity"], f"fp-{label}", f"Quake near {label}"))
    db.execute("INSERT INTO biz.risk_evidence (workspace_id, risk_id, kind, event_source, event_raw_id, match_type,"
               " match_value, match_confidence, signal, recency, contribution) VALUES (%s, %s, 'event', 'usgs', %s,"
               " 'geo_radius', 'near', 0.9, 0.7, 0.9, 0.5)", (ws, ids["risk"], f"us-{label}"))
    db.execute("INSERT INTO biz.alerts (id, workspace_id, risk_id, kind, severity, title, dedupe_key)"
               " VALUES (%s, %s, %s, 'risk_new', 'warning', %s, %s)", (ids["alert"], ws, ids["risk"], f"Alert {label}", f"k{label}"))
    db.execute("INSERT INTO biz.scenarios (id, workspace_id, name, inputs) VALUES (%s, %s, %s, '{}')",
               (ids["scenario"], ws, f"Scenario {label}"))  # fmt: skip
    return {k: str(v) for k, v in ids.items()}


@pytest.fixture
def two(client, owner_db):
    a, b = sign_up(client, workspace="A"), sign_up(client, workspace="B")
    return a, b, seed_workspace(owner_db, a["workspace_id"], "a"), seed_workspace(owner_db, b["workspace_id"], "b")


LISTS = ["/v1/connections", "/v1/sources", "/v1/entities", "/v1/risks", "/v1/risks/summary", "/v1/alerts",
         "/v1/scenarios", "/v1/map/entities", "/v1/map/risks"]  # fmt: skip


def test_each_user_sees_only_their_workspace(client, two):
    a, b, ida, idb = two
    for path in LISTS:
        body = client.get(path, headers=hdr(a)).text
        assert "Secret supplier b" not in body and idb["risk"] not in body and idb["source"] not in body, path
    assert [r["id"] for r in client.get("/v1/risks", headers=hdr(a)).json()["items"]] == [ida["risk"]]
    assert client.get("/v1/entities", headers=hdr(b)).json()["items"][0]["name"] == "Secret supplier b"


@pytest.mark.parametrize("path", LISTS + ["/v1/stream/ticket"])
def test_forged_workspace_header_is_404(client, two, path):
    a, b, _, _ = two
    method = client.post if path == "/v1/stream/ticket" else client.get
    r = method(path, headers=hdr(a, workspace=b["workspace_id"]))
    assert r.status_code == 404, path
    assert r.json()["code"] == "not_found"
    assert method(path, headers=hdr(a, workspace=str(uuid.uuid4()))).status_code == 404


def test_ids_from_another_workspace_are_404(client, two):
    a, _, _, idb = two
    for path in (f"/v1/risks/{idb['risk']}", f"/v1/entities/{idb['entity']}", f"/v1/sources/{idb['source']}",
                 f"/v1/sources/{idb['source']}/mapping", f"/v1/connections/{idb['conn']}", f"/v1/scenarios/{idb['scenario']}"):
        assert client.get(path, headers=hdr(a)).status_code == 404, path
    assert client.patch(f"/v1/risks/{idb['risk']}", headers=hdr(a), json={"status": "dismissed"}).status_code == 404
    assert client.patch(f"/v1/alerts/{idb['alert']}", headers=hdr(a), json={"read": True}).status_code == 404
    assert client.delete(f"/v1/sources/{idb['source']}", headers=hdr(a)).status_code == 404
    assert client.delete(f"/v1/connections/{idb['conn']}", headers=hdr(a)).status_code == 404
    # read-all only touches the caller's workspace
    assert client.post("/v1/alerts/read-all", headers=hdr(a)).status_code == 204
    _, b, _, _ = two
    assert client.get("/v1/alerts?unread=true", headers=hdr(b)).json()["unread_count"] == 1


def test_workspace_path_routes_use_the_path_not_the_header(client, two):
    a, b, _, _ = two
    assert client.get(f"/v1/workspaces/{b['workspace_id']}", headers=hdr(a)).status_code == 404
    assert client.get(f"/v1/workspaces/{b['workspace_id']}/members", headers=hdr(a)).status_code == 404
    # a header for B does not unlock A's path either way
    r = client.get(f"/v1/workspaces/{a['workspace_id']}", headers=hdr(a, workspace=b["workspace_id"]))
    assert r.status_code == 200 and r.json()["id"] == a["workspace_id"]
    assert client.patch(f"/v1/workspaces/{b['workspace_id']}", headers=hdr(a), json={"name": "pwned"}).status_code == 404
    assert client.delete(f"/v1/workspaces/{b['workspace_id']}", headers=hdr(a)).status_code == 404
    assert [w["id"] for w in client.get("/v1/workspaces", headers=hdr(a)).json()["items"]] == [a["workspace_id"]]


def test_missing_or_bad_header(client, two):
    a, _, _, _ = two
    r = client.get("/v1/risks", headers=hdr(a["access_token"]))
    assert r.status_code == 400 and r.json()["code"] == "workspace_required"
    assert client.get("/v1/risks", headers={**hdr(a["access_token"]), "X-Workspace-Id": "nope"}).status_code == 400


def test_roles(client, two, owner_db):
    a, _, ida, _ = two
    viewer = sign_up(client, workspace="V")
    r = client.post(f"/v1/workspaces/{a['workspace_id']}/members", headers=hdr(a), json={"email": viewer["email"], "role": "viewer"})
    assert r.status_code == 201 and r.json()["role"] == "viewer"
    v = hdr(viewer, workspace=a["workspace_id"])
    assert client.get(f"/v1/risks/{ida['risk']}", headers=v).status_code == 200
    assert client.patch(f"/v1/risks/{ida['risk']}", headers=v, json={"status": "dismissed"}).json()["code"] == "forbidden"
    assert client.post("/v1/connections/google/start", headers=v, json={}).status_code == 403
    assert client.patch(f"/v1/workspaces/{a['workspace_id']}", headers=v, json={"name": "x"}).status_code == 403
    # editor may change a risk's status, and that is recorded
    client.patch(f"/v1/workspaces/{a['workspace_id']}/members/{viewer['user']['id']}", headers=hdr(a), json={"role": "editor"})
    r = client.patch(f"/v1/risks/{ida['risk']}", headers=v, json={"status": "acknowledged", "note": "on it"})
    assert r.status_code == 200 and r.json()["status"] == "acknowledged"
    detail = client.get(f"/v1/risks/{ida['risk']}", headers=v).json()
    assert detail["status_note"] == "on it" and detail["history"][-1]["reason"] == "status_changed"
    assert detail["evidence"][0]["still_in_feeds"] is False
    assert client.patch(f"/v1/risks/{ida['risk']}", headers=v, json={"status": "resolved"}).status_code == 422
    # the last owner cannot leave or be demoted
    r = client.delete(f"/v1/workspaces/{a['workspace_id']}/members/{a['user']['id']}", headers=hdr(a))
    assert r.status_code == 409 and r.json()["code"] == "last_owner"
    r = client.patch(f"/v1/workspaces/{a['workspace_id']}/members/{a['user']['id']}", headers=hdr(a), json={"role": "admin"})
    assert r.json()["code"] == "last_owner"
    # members can leave by themselves; then they are no longer let in
    assert client.delete(f"/v1/workspaces/{a['workspace_id']}/members/{viewer['user']['id']}", headers=v).status_code == 204
    assert client.get("/v1/risks", headers=v).status_code == 404


def test_new_workspace_is_empty(client):
    a = sign_up(client)
    h = hdr(a)
    assert client.get("/v1/connections", headers=h).json() == {"items": []}
    assert client.get("/v1/risks", headers=h).json() == {"items": [], "next_cursor": None}
    s = client.get("/v1/risks/summary", headers=h).json()
    assert s["open_count"] == 0 and s["top"] == [] and set(s["by_category"].values()) == {0}
    assert client.get("/v1/map/entities", headers=h).json() == {"type": "FeatureCollection", "features": [], "truncated": False}
    assert client.get("/v1/alerts", headers=h).json()["unread_count"] == 0


def test_pagination_is_stable(client, owner_db):
    a = sign_up(client)
    ids = seed_workspace(owner_db, a["workspace_id"], "p")
    for i in range(4):
        owner_db.execute(
            "INSERT INTO biz.risks (workspace_id, entity_id, fingerprint, category, title, likelihood, impact, engine_version)"
            " VALUES (%s, %s, %s, 'cyber', %s, %s, 0.5, 'test')", (a["workspace_id"], ids["entity"], f"fp{i}", f"R{i}", 0.1 * (i + 1)),
        )  # fmt: skip
    seen, cursor = [], None
    while True:
        r = client.get("/v1/risks", headers=hdr(a), params={"limit": 2, **({"cursor": cursor} if cursor else {})}).json()
        seen += [x["score"] for x in r["items"]]
        cursor = r["next_cursor"]
        if not cursor:
            break
    assert len(seen) == 5 and seen == sorted(seen, reverse=True)
    assert client.get("/v1/risks", headers=hdr(a), params={"cursor": "garbage"}).status_code == 400
