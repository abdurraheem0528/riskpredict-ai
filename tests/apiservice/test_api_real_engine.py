"""The API and worker with the real ``engine/``."""

from __future__ import annotations

from uuid import UUID, uuid4

import pytest

pytest.importorskip("fastapi")  # the repository-wide CI job does not install the API
engine = pytest.importorskip("engine")

from apiservice_fixtures import *  # noqa: E402,F401,F403  (fixtures)
from apiservice_helpers import event_names, hdr, sign_up  # noqa: E402

from psycopg.types.json import Jsonb  # noqa: E402

from api.core import engine as engine_iface  # noqa: E402


def quake_near_osaka(db) -> None:
    db.execute(
        "INSERT INTO feeds.events (source, raw_id, type, title, published_at, countries, severity, lat, lon, attrs)"
        " VALUES ('usgs', 'us-real-1', 'hazard.earthquake', 'M6.5 near Osaka', now() - interval '2 hours', '{JPN}',"
        " 0.8, 34.65, 135.45, '{\"magnitude\": 6.5}')"
    )


def test_worker_scores_with_the_real_engine(client, owner_db, settings, fake_http):
    from test_api_isolation import seed_workspace

    from worker.main import Worker

    me = sign_up(client)
    seed_workspace(owner_db, me["workspace_id"], "r")  # a supplier at 34.6, 135.5 (test fixture)
    owner_db.execute("DELETE FROM biz.risks")
    quake_near_osaka(owner_db)
    worker = Worker(settings)
    worker.db.open()
    try:
        worker.workspace_pass(UUID(me["workspace_id"]), score=True)
    finally:
        worker.db.close()
    risks = client.get("/v1/risks", headers=hdr(me)).json()["items"]
    assert len(risks) == 1, event_names(owner_db)
    risk = risks[0]
    assert risk["category"] == "natural_hazard" and 0 < risk["score"] <= 100
    detail = client.get(f"/v1/risks/{risk['id']}", headers=hdr(me)).json()
    assert detail["engine_version"] == engine.ENGINE_VERSION
    assert detail["evidence"][0]["evidence_key"] == "event:usgs:us-real-1"
    assert detail["factors"]["likelihood"]["method"] == "noisy_or"
    # Groq is not reachable in this test, so the engine's template words the advice.
    assert detail["advice"]["source"] == "template" and detail["advice"]["text"]
    assert "risk.created" in event_names(owner_db, me["workspace_id"])


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


# --- "Why this reaches you" after engine 1.3.3 ------------------------------------------------------
# The live case of 27 Nov 2026, as test data: a China sanctions risk for a PCB supplier also held
# rows that engines before 1.3.0 had linked and that the current engine rejects (shown first, with
# their old contributions), and several copies of one syndicated article.

NVIDIA = "Nvidia's Most Advanced AI Chips Reach Chinese Military despite US sanctions on China"
SHIPMENTS = "FT: Nvidia stops AI shipments to Malaysia, Singapore under new US sanctions on China"
EVIL = "European Leaders Aren't Banal, They are Evil"
ETHIOPIA = "Ethiopia's Calculated Use of Maritime Diplomacy"
CARTEL = "Trump administration expands anti-cartel campaign as legal challenges mount"


def china_sanctions_news(db, raw_id: str, title: str, hours_ago: int) -> None:
    db.execute(
        "INSERT INTO feeds.events (source, raw_id, type, title, published_at, first_seen_at)"
        " VALUES ('gdelt', %s, 'news', %s, now() - make_interval(hours => %s), now() - make_interval(hours => %s))",
        (raw_id, title, hours_ago, hours_ago),
    )
    db.execute(
        "INSERT INTO feeds.event_tags (source, raw_id, model, model_version, input_md5, entities, event_type,"
        " type_confidence, type_source, tagged_at) VALUES ('gdelt', %s, 'm', 'v', 'x', %s, 'sanctions', 0.9, 'model',"
        " now())",
        (raw_id, Jsonb({"countries": ["CHN"], "companies": [], "places": [], "commodities": []})),
    )


def older_engine_row(db, ws, risk_id, raw_id: str, title: str, contribution: float) -> None:
    """A row as an engine before 1.3.0 left it: linked 14 hours before the latest scoring, never since."""
    db.execute(
        "INSERT INTO biz.risk_evidence (workspace_id, risk_id, kind, event_source, event_raw_id, match_type,"
        " match_value, match_confidence, signal, recency, contribution, snapshot, first_linked_at, last_linked_at)"
        " SELECT %s, %s, 'event', 'gdelt', %s, 'country', 'CHN', 0.36, 1.0, 1.0, %s, %s,"
        " r.last_scored_at - interval '14 hours', r.last_scored_at - interval '14 hours'"
        " FROM biz.risks r WHERE r.id = %s",
        (ws, risk_id, raw_id, contribution, Jsonb({"title": title, "type": "news", "url": None}), risk_id),
    )


def shown(detail: dict) -> list[tuple[str, bool]]:
    return [(e["event"]["raw_id"], e["still_in_feeds"]) for e in detail["evidence"]]


def test_risk_page_shows_this_runs_evidence_first_and_counts_only_it(client, owner_db, settings, fake_http):
    from test_api_isolation import seed_workspace

    from api.routes import data
    from engine import db as edb
    from worker import scoring
    from worker.main import Worker

    me = sign_up(client)
    ws = me["workspace_id"]
    ids = seed_workspace(owner_db, ws, "p")
    owner_db.execute("DELETE FROM biz.risks")
    owner_db.execute(
        "INSERT INTO biz.entities (workspace_id, kind, name, countries, criticality, source_id, mapping_id,"
        " source_row_key, source_row_hash) VALUES (%s, 'supplier', 'Pearl River PCB Works', '{CHN}', 'high', %s, %s,"
        " 'rec2', 'h')",
        (ws, ids["source"], ids["mapping"]),
    )
    china_sanctions_news(owner_db, "b-nvidia", NVIDIA, 2)
    china_sanctions_news(owner_db, "b-shipments", SHIPMENTS, 3)
    china_sanctions_news(owner_db, "a-evil", EVIL, 30)  # still in the feeds; China is not its target
    for i in range(1, 5):
        china_sanctions_news(owner_db, f"d-cartel-{i}", CARTEL, 30 + i)  # four copies, still in the feeds
    worker = Worker(settings)
    worker.db.open()
    try:
        worker.workspace_pass(UUID(ws), score=True)
        [risk] = client.get("/v1/risks", headers=hdr(me)).json()["items"]
        assert risk["category"] == "sanctions" and risk["evidence_count"] == 2
        # The rows an older engine left: a rejected article, one whose event has left the feeds, and
        # six copies of one article (four still in the feeds, two gone), all with higher contributions.
        older_engine_row(owner_db, ws, risk["id"], "a-evil", EVIL, 0.35)
        older_engine_row(owner_db, ws, risk["id"], "a-ethiopia", ETHIOPIA, 0.33)
        for i in range(1, 7):
            older_engine_row(owner_db, ws, risk["id"], f"d-cartel-{i}", CARTEL, 0.27 if i != 6 else 0.26)

        # Before the next run (just after Deploy): this run's rows first, the others not counted,
        # and the copies of the cartel article shown once.
        detail = client.get(f"/v1/risks/{risk['id']}", headers=hdr(me)).json()
        assert detail["evidence_count"] == 2
        assert len(detail["factors"]["likelihood"]["items"]) == 2
        assert shown(detail) == [("b-nvidia", True), ("b-shipments", True), ("a-evil", True),
                                 ("a-ethiopia", False), ("d-cartel-1", True)]  # fmt: skip
        # The advice check compares with the rows the last scoring linked, not the older ones.
        prev = scoring.previous_top_keys(owner_db)[UUID(risk["id"])]
        assert prev == {"event:gdelt:b-nvidia", "event:gdelt:b-shipments"}
        assert not engine.needs_new_advice(risk["score"], risk["score"], prev, set(prev))

        # The next run prunes every row the engine rejected while its event is still in the feeds.
        worker.workspace_pass(UUID(ws), score=True)
    finally:
        worker.db.close()
    detail = client.get(f"/v1/risks/{risk['id']}", headers=hdr(me)).json()
    assert detail["evidence_count"] == 2 and detail["engine_version"] == engine.ENGINE_VERSION
    assert shown(detail) == [("b-nvidia", True), ("b-shipments", True), ("a-ethiopia", False), ("d-cartel-5", False)]
    left = owner_db.execute("SELECT event_raw_id FROM biz.risk_evidence WHERE risk_id = %s ORDER BY 1",
                            (risk["id"],)).fetchall()  # fmt: skip
    assert [r["event_raw_id"] for r in left] == ["a-ethiopia", "b-nvidia", "b-shipments", "d-cartel-5", "d-cartel-6"]
    # The API's "still in the feeds" and the engine's prune use the same test.
    assert " ".join(data.STILL_IN_FEEDS.split()) == " ".join(edb.STILL_IN_FEEDS.split())
