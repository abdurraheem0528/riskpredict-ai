"""The API and worker with the real ``engine/``."""

from __future__ import annotations

from uuid import UUID, uuid4

import pytest

pytest.importorskip("fastapi")  # the repository-wide CI job does not install the API
engine = pytest.importorskip("engine")

from apiservice_fixtures import *  # noqa: E402,F401,F403  (fixtures)
from apiservice_helpers import hdr, sign_up  # noqa: E402

from api.core import engine as engine_iface  # noqa: E402


def quake_near_osaka(db) -> None:
    db.execute(
        "INSERT INTO feeds.events (source, raw_id, type, title, published_at, countries, severity, lat, lon, attrs)"
        " VALUES ('usgs', 'us-real-1', 'hazard.earthquake', 'M6.5 near Osaka', now() - interval '2 hours', '{JPN}',"
        " 0.8, 34.65, 135.45, '{\"magnitude\": 6.5}')"
    )


def test_worker_scores_with_the_real_engine(client, owner_db, settings, fake_http):
    from test_api_google_flow import RecordingApi
    from test_api_isolation import seed_workspace

    from worker.main import Worker

    me = sign_up(client)
    seed_workspace(owner_db, me["workspace_id"], "r")  # a supplier at 34.6, 135.5 (test fixture)
    owner_db.execute("DELETE FROM biz.risks")
    quake_near_osaka(owner_db)
    api = RecordingApi()
    worker = Worker(settings, api=api)
    worker.db.open()
    try:
        worker.workspace_pass(UUID(me["workspace_id"]), {"engine"})
    finally:
        worker.db.close()
    risks = client.get("/v1/risks", headers=hdr(me)).json()["items"]
    assert len(risks) == 1, api.names()
    risk = risks[0]
    assert risk["category"] == "natural_hazard" and 0 < risk["score"] <= 100
    detail = client.get(f"/v1/risks/{risk['id']}", headers=hdr(me)).json()
    assert detail["engine_version"] == engine.ENGINE_VERSION
    assert detail["evidence"][0]["evidence_key"] == "event:usgs:us-real-1"
    assert detail["factors"]["likelihood"]["method"] == "noisy_or"
    # Groq is not reachable in this test, so the engine's template words the advice.
    assert detail["advice"]["source"] == "template" and detail["advice"]["text"]
    assert "risk.created" in api.names()


def test_scenarios_with_the_real_engine(client, owner_db):
    from test_api_isolation import seed_workspace

    me = sign_up(client)
    seed_workspace(owner_db, me["workspace_id"], "s")
    body = {"name": "Quake in Japan", "inputs": {"shocks": [
        {"type": "event", "category": "natural_hazard", "severity": 0.9, "countries": ["JPN"]}]}}  # fmt: skip
    r = client.post("/v1/scenarios", headers=hdr(me), json=body)
    assert r.status_code == 201, r.text
    out = r.json()
    assert out["status"] == "completed" and out["results"]["engine_version"] == engine.ENGINE_VERSION
    assert {"baseline", "scenario", "changes", "as_of"} <= set(out["results"])
    # An entity from no workspace of ours: the engine rejects it, the API answers 422 and keeps no row.
    bad = {"name": "x", "inputs": {"shocks": [{"type": "sanction", "entity_id": str(uuid4())}]}}
    r = client.post("/v1/scenarios", headers=hdr(me), json=bad)
    assert r.status_code == 422 and r.json()["code"] == "validation_failed", r.text
    names = [s["name"] for s in client.get("/v1/scenarios", headers=hdr(me)).json()["items"]]
    assert "Quake in Japan" in names and "x" not in names


def test_chokepoints_come_from_the_engine(client, owner_db):
    from api.core import mapping

    table = {c.id: c for c in engine_iface.chokepoints()}
    assert table["suez"].lat is not None and table["suez"].radius_km
    resolve = mapping.chokepoint_resolver()
    assert resolve("Suez Canal") == "suez" and resolve("bab el mandeb") == "bab_el_mandeb" and resolve("Atlantis") is None
    me = sign_up(client)
    fc = client.get("/v1/map/disruptions", headers=hdr(me["access_token"])).json()
    ids = {f["id"] for f in fc["features"]}
    assert {f"chokepoint:{cid}" for cid in table} <= ids
    suez = next(f for f in fc["features"] if f["id"] == "chokepoint:suez")
    assert suez["properties"]["status"] == "normal" and suez["geometry"]["coordinates"] == [table["suez"].lon, table["suez"].lat]
