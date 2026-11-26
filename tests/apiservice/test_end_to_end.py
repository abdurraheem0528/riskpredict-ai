"""End to end through every seam, with the real ``engine/`` and nothing replaced but Groq:
workspace (API sign-up) → entities → public feed rows (events and the full sanctions list) →
the worker's database pass (feeds moved → engine run) → risks, evidence, alerts and GeoJSON
read back through the API → a delisting resolves the sanctions risk on the next pass.

Every row here is a test fixture (made-up names). Groq has no test route, so it is
"unreachable" and the engine's template words the advice.
"""

from __future__ import annotations

import uuid
from uuid import UUID

import pytest

pytest.importorskip("fastapi")  # the repository-wide CI job does not install the API
engine = pytest.importorskip("engine")

from apiservice_fixtures import *  # noqa: E402,F401,F403  (fixtures)
from apiservice_helpers import event_names, hdr, sign_up  # noqa: E402

GEOJSON = "application/geo+json"
ENTITIES = [
    # kind, name, extra columns
    ("supplier", "Kansai Precision Parts", {"countries": ["JPN"], "lat": 34.6, "lon": 135.5, "criticality": "high"}),
    ("customer", "Nordwind Holding", {"countries": ["ARE"]}),
    ("software", "Python HTTP client", {"software": ["pkg:pypi/requests@2.31.0"]}),
    ("route", "Asia-Europe lane", {"ports": ["SGSIN", "NLRTM"], "attrs": '{"chokepoints": ["Suez Canal"]}'}),
]


def add_entities(db, ws: str) -> dict[str, UUID]:
    """A synced source's entities, as the worker's sync step writes them (test fixtures)."""
    conn, src, mapping = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    db.execute("INSERT INTO biz.connections (id, workspace_id, provider, status, composio_connected_account_id)"
               " VALUES (%s, %s, 'composio:airtable', 'active', 'ca_e2e')", (conn, ws))  # fmt: skip
    db.execute("INSERT INTO biz.sources (id, workspace_id, connection_id, kind, external_id, external_table_id, name,"
               " status, last_synced_at) VALUES (%s, %s, %s, 'composio_table', 'appE2E', 'tbl', 'Things', 'active',"
               " now())", (src, ws, conn))  # fmt: skip (already synced: no re-read is due)
    db.execute("INSERT INTO biz.column_mappings (id, workspace_id, source_id, version, status, mapping, confirmed_at)"
               " VALUES (%s, %s, %s, 1, 'confirmed', '{\"entity_kind\": \"supplier\", \"columns\": []}', now())",
               (mapping, ws, src))  # fmt: skip
    ids = {}
    for i, (kind, name, extra) in enumerate(ENTITIES):
        cols = ["workspace_id", "kind", "name", "source_id", "mapping_id", "source_row_key", "source_row_hash", *extra]
        vals = [ws, kind, name, src, mapping, f"rec{i}", f"h{i}", *extra.values()]
        row = db.execute(f"INSERT INTO biz.entities ({', '.join(cols)}) VALUES ({', '.join(['%s'] * len(vals))})"
                         " RETURNING id", vals).fetchone()  # fmt: skip
        ids[name] = row["id"]
    return ids


def add_feed_rows(db) -> None:
    """What the scheduled jobs would have stored (test fixtures)."""
    db.execute("""
        INSERT INTO feeds.events (source, raw_id, type, title, published_at, countries, ports, severity, lat, lon,
                                  software, attrs) VALUES
         ('usgs', 'us-e2e-1', 'hazard.earthquake', 'M6.5 near Osaka', now() - interval '2 hours', '{JPN}', '{}',
          0.8, 34.65, 135.45, '{}', '{"magnitude": 6.5}'),
         ('osv', 'GHSA-e2e-0001', 'cyber.vulnerability', 'requests leaks credentials', now() - interval '2 days',
          '{}', '{}', 0.75, NULL, NULL, '{PyPI:requests}', '{}'),
         ('portwatch', 'pw-e2e-1', 'port.disruption', 'Suez Canal transits halted', now() - interval '6 hours',
          '{EGY}', '{EGPSD}', 1.0, 30.9, 32.3, '{}', '{}')
    """)  # fmt: skip
    db.execute("INSERT INTO feeds.sanctions_entries (source, dataset, entity_id, entity_type, name, aliases, countries,"
               " programs, lists) VALUES ('ofac_sdn', 'sdn', '36001', 'Entity', 'NORTHWIND HOLDINGS',"
               " '{\"Nordwind Holding AG\"}', '{ARE}', '{SDGT}', '{\"SDN List\"}')")  # fmt: skip
    feeds_ran(db)


def feeds_ran(db) -> None:
    """A feed job finished: the worker's database pass sees feeds.source_health move on."""
    db.execute("INSERT INTO feeds.source_health (source, status, last_run_at, last_success_at)"
               " VALUES ('usgs', 'ok', clock_timestamp(), clock_timestamp())"
               " ON CONFLICT (source) DO UPDATE SET last_run_at = EXCLUDED.last_run_at")  # fmt: skip


def test_workspace_to_map_through_the_real_engine(client, owner_db, settings):
    from worker.main import Worker

    me = sign_up(client, workspace="E2E")
    ws = me["workspace_id"]
    ids = add_entities(owner_db, ws)
    add_feed_rows(owner_db)
    worker = Worker(settings)
    worker.db.open()
    try:
        worker.db_pass()  # first pass: feeds "moved" (never seen), so every workspace is scored
        seen = owner_db.execute("SELECT feeds_seen_at FROM biz.worker_state WHERE workspace_id = %s", (ws,)).fetchone()
        assert seen["feeds_seen_at"] is not None  # persisted: the next run-once worker knows

        # --- risks and evidence ----------------------------------------------------------
        risks = client.get("/v1/risks", headers=hdr(me), params={"limit": 50}).json()["items"]
        by_cat = {r["category"]: r for r in risks if r["entity"]["name"] != "Kansai Precision Parts" or r["category"] != "logistics"}
        assert set(by_cat) == {"natural_hazard", "sanctions", "cyber", "logistics"}, risks
        # The Suez closure reaches the route (declared) and the Japanese supplier (its lane to
        # Europe is inferred from its country): one story, two risks.
        assert len(risks) == 5
        [inferred] = [r for r in risks if r["category"] == "logistics" and r["entity"]["name"] == "Kansai Precision Parts"]
        assert inferred["story"] == by_cat["logistics"]["story"]
        assert inferred["story"]["risk_count"] == 2 and inferred["story"]["key"] == "logistics|event:portwatch:pw-e2e-1"
        assert {e["name"] for e in inferred["story"]["entities"]} == {"Kansai Precision Parts", "Asia-Europe lane"}
        assert inferred["score"] < by_cat["logistics"]["score"]  # inferred counts at half a declared route
        grouped = client.get("/v1/risks", headers=hdr(me), params={"group": "story"}).json()["items"]
        assert len(grouped) == 4 and by_cat["logistics"]["id"] in {r["id"] for r in grouped}
        assert {r["entity"]["name"] for r in risks} == {name for _, name, _ in ENTITIES}
        for r in risks:
            assert r["status"] == "open" and r["score"] == round(100 * r["likelihood"] * r["impact"], 1)
        assert [r["score"] for r in risks] == sorted((r["score"] for r in risks), reverse=True)

        sanc = client.get(f"/v1/risks/{by_cat['sanctions']['id']}", headers=hdr(me)).json()
        assert sanc["title"] == "Sanctions listing may match Nordwind Holding"
        assert sanc["engine_version"] == engine.ENGINE_VERSION
        [ev] = sanc["evidence"]
        assert ev["evidence_key"] == "event:ofac_sdn:sdn:36001" and ev["match_type"] == "sanctions_name"
        assert ev["still_in_feeds"] is True and ev["recency"] == 1.0  # a standing listing does not decay
        assert ev["snapshot"]["title"] == "OFAC SDN list: NORTHWIND HOLDINGS [SDGT]"
        assert sanc["advice"]["source"] == "template" and "compliance" in sanc["advice"]["text"]
        assert [h["reason"] for h in sanc["history"]] == ["created"]

        quake = client.get(f"/v1/risks/{by_cat['natural_hazard']['id']}", headers=hdr(me)).json()
        assert quake["evidence"][0]["evidence_key"] == "event:usgs:us-e2e-1"
        assert quake["evidence"][0]["match_type"] == "geo_radius"
        assert quake["factors"]["likelihood"]["method"] == "noisy_or"
        lane = client.get(f"/v1/risks/{by_cat['logistics']['id']}", headers=hdr(me)).json()
        assert lane["evidence"][0]["match_type"] == "chokepoint" and lane["evidence"][0]["match_value"] == "suez"
        assert lane["factors"]["explain"]["matched"]["inferred_from"] is None
        far = client.get(f"/v1/risks/{inferred['id']}", headers=hdr(me)).json()
        assert far["evidence"][0]["snapshot"]["inferred_from"] == "country JPN"
        assert far["factors"]["explain"]["matched"]["inferred_from"] == "country JPN"
        assert "likely pass (inferred from Japan)" in far["explanation"]
        cyber = client.get(f"/v1/risks/{by_cat['cyber']['id']}", headers=hdr(me)).json()
        assert cyber["evidence"][0]["match_value"] == "PyPI:requests"

        # --- alerts and live events -------------------------------------------------------
        alerts = client.get("/v1/alerts", headers=hdr(me)).json()["items"]
        assert ("sanctions_match", "critical") in {(a["kind"], a["severity"]) for a in alerts}
        names = event_names(owner_db, ws)
        assert names.count("risk.created") == 5 and "alert.created" in names

        # --- GeoJSON ------------------------------------------------------------------------
        r = client.get("/v1/map/risks", headers=hdr(me))
        assert r.headers["content-type"].startswith(GEOJSON)
        places = {f["id"]: f for f in r.json()["features"]}
        point = places[f"point:{ids['Kansai Precision Parts']}"]
        assert point["geometry"] == {"type": "Point", "coordinates": [135.5, 34.6]}
        assert point["properties"]["top_risk_ids"] == [by_cat["natural_hazard"]["id"], inferred["id"]]
        assert point["properties"]["story_count"] == 2
        assert places["country:ARE"]["properties"]["top_categories"] == ["sanctions"]
        assert places["port:NLRTM"]["properties"]["top_categories"] == ["logistics"]  # a route's ports: sorted
        assert places["port:NLRTM"]["properties"]["label"] == "Rotterdam"  # the port's display name
        assert len(places) == 3  # the software risk has no place
        assert by_cat["sanctions"]["place"] == "United Arab Emirates" and by_cat["sanctions"]["entity"]["city"] is None
        ents = client.get("/v1/map/entities", headers=hdr(me)).json()["features"]
        assert len(ents) == len(ENTITIES)
        assert {f["properties"]["name"]: f["properties"]["place"] for f in ents}["Kansai Precision Parts"] == "Japan"
        # A route's declared chokepoints, as ids ("Suez Canal" was written as a name); none on other kinds.
        props = {f["properties"]["name"]: f["properties"] for f in ents}
        assert props["Asia-Europe lane"]["chokepoints"] == ["suez"]
        assert "chokepoints" not in props["Kansai Precision Parts"]
        dis = {f["id"]: f["properties"] for f in client.get("/v1/map/disruptions", headers=hdr(me)).json()["features"]}
        suez = dis["chokepoint:suez"]
        assert suez["status"] == "disrupted" and suez["event_ids"] == ["portwatch:pw-e2e-1"]
        summary = client.get("/v1/risks/summary", headers=hdr(me)).json()
        assert summary["open_count"] == 5 and summary["by_category"]["sanctions"] == 1
        assert summary["story_count"] == 4 and summary["stories_by_category"]["logistics"] == 1

        # --- the next pass with nothing new changes nothing ----------------------------------
        before = len(event_names(owner_db, ws))
        feeds_ran(owner_db)
        Worker(settings).db_pass()  # a fresh process, as each `--once` run is
        assert event_names(owner_db, ws)[before:] == ["feeds.updated"]

        # --- a new ENGINE_VERSION re-scores the workspace on the next run, with no new feed rows --
        owner_db.execute("UPDATE biz.risks SET engine_version = '1.1.0' WHERE workspace_id = %s", (ws,))
        Worker(settings).db_pass()
        versions = owner_db.execute("SELECT DISTINCT engine_version FROM biz.risks WHERE workspace_id = %s", (ws,))
        assert [r["engine_version"] for r in versions.fetchall()] == [engine.ENGINE_VERSION]

        # An alert the earlier rule raised for sanctions *news* (not a list name match): the
        # next pass marks it read (only a name match is a "Possible sanctions match").
        owner_db.execute(
            "INSERT INTO biz.alerts (workspace_id, risk_id, kind, severity, title, dedupe_key) VALUES"
            " (%s, %s, 'sanctions_match', 'critical', 'Possible sanctions match: Sanctions news news on chips', 'old:1')",
            (ws, by_cat["natural_hazard"]["id"]),
        )  # fmt: skip
        [noisy] = [a for a in client.get("/v1/alerts", headers=hdr(me)).json()["items"] if a["title"].endswith("chips")]
        assert noisy["title"] == "Possible sanctions match: Sanctions news on chips"  # the old doubling, said once
        assert noisy["risk_status"] == "open" and noisy["read_at"] is None

        # --- delisting: the full-list job deletes the entry; the risk is resolved --------------
        owner_db.execute("DELETE FROM feeds.sanctions_entries WHERE entity_id = '36001'")
        feeds_ran(owner_db)
        worker.db_pass()
        assert "risk.resolved" in event_names(owner_db, ws)
        gone = client.get(f"/v1/risks/{by_cat['sanctions']['id']}", headers=hdr(me)).json()
        assert gone["status"] == "resolved" and gone["evidence"][0]["still_in_feeds"] is False
        assert [h["reason"] for h in gone["history"]] == ["created", "resolved_by_engine"]
        assert client.get("/v1/risks/summary", headers=hdr(me)).json()["open_count"] == 4
        # its alert is read now and says the risk is resolved; the noisy one is read too
        by_title = {a["title"]: a for a in client.get("/v1/alerts", headers=hdr(me)).json()["items"]}
        match = by_title["Possible sanctions match: Sanctions listing may match Nordwind Holding"]
        assert match["risk_status"] == "resolved" and match["read_at"] is not None
        assert by_title["Possible sanctions match: Sanctions news on chips"]["read_at"] is not None

        # A resolved risk an older engine wrote keeps its raw-formula explanation in the
        # database; the API words it from the stored evidence with the engine's own texts.
        owner_db.execute("UPDATE biz.risks SET factors = factors - 'explain', explanation = '1 item(s): noisy-OR 0.9'"
                         " WHERE id = %s", (by_cat["sanctions"]["id"],))  # fmt: skip
        old = client.get(f"/v1/risks/{by_cat['sanctions']['id']}", headers=hdr(me)).json()
        assert old["explanation"] == "Nordwind Holding closely matches “Nordwind Holding AG”, an alias on a US sanctions list."

        # A user dismissing a risk deals with its alerts too.
        owner_db.execute(
            "INSERT INTO biz.alerts (workspace_id, risk_id, kind, severity, title, dedupe_key) VALUES"
            " (%s, %s, 'risk_new', 'warning', 'New risk: requests', 'old:2')", (ws, by_cat["cyber"]["id"]),
        )  # fmt: skip
        client.patch(f"/v1/risks/{by_cat['cyber']['id']}", headers=hdr(me), json={"status": "dismissed"}).raise_for_status()
        [dismissed] = [a for a in client.get("/v1/alerts", headers=hdr(me)).json()["items"] if a["title"] == "New risk: requests"]
        assert dismissed["read_at"] is not None and dismissed["risk_status"] == "dismissed"
    finally:
        worker.db.close()
