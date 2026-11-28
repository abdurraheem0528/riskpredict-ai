"""Risks listed once per place: `Risk.where`, `Risk.lead`, `story.entities[].kind/city` and the
grouping of `group=story` and the summary counts by category and place (serialize.place_key_sql).

Every row here is a test fixture: invented entities, and risk rows written the way the engine
writes them (factors.story and factors.explain.matched, evidence of the latest run).
"""

from __future__ import annotations

import json
import uuid

import pytest

pytest.importorskip("fastapi")  # the repository-wide CI job does not install the API

from apiservice_fixtures import *  # noqa: E402,F401,F403  (fixtures)
from apiservice_helpers import hdr, sign_up  # noqa: E402

from api.core import serialize as ser  # noqa: E402


def _source(db, ws: str) -> tuple[uuid.UUID, uuid.UUID]:
    conn, src, mapping = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    db.execute("INSERT INTO biz.connections (id, workspace_id, provider, status, composio_connected_account_id)"
               " VALUES (%s, %s, 'composio:airtable', 'active', 'ca_places')", (conn, ws))  # fmt: skip
    db.execute("INSERT INTO biz.sources (id, workspace_id, connection_id, kind, external_id, external_table_id, name,"
               " status) VALUES (%s, %s, %s, 'composio_table', 'appP', 'tbl', 'Sheet', 'active')", (src, ws, conn))  # fmt: skip
    db.execute("INSERT INTO biz.column_mappings (id, workspace_id, source_id, version, status, mapping, confirmed_at)"
               " VALUES (%s, %s, %s, 1, 'confirmed', '{\"entity_kind\": \"site\", \"columns\": []}', now())",
               (mapping, ws, src))  # fmt: skip
    return src, mapping


def _entity(db, ws, src, mapping, kind, name, countries=(), ports=(), city=None, criticality=None) -> uuid.UUID:
    return db.execute(
        "INSERT INTO biz.entities (workspace_id, kind, name, countries, ports, city, criticality, source_id, mapping_id,"
        " source_row_key, source_row_hash) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, 'h') RETURNING id",
        (ws, kind, name, list(countries), list(ports), city, criticality, src, mapping, f"row-{name}"),
    ).fetchone()["id"]


def _risk(db, ws, entity, category, score_l, impact, *, matched=None, story=None, reports=(), status="open") -> str:
    """A risk as the engine writes it; ``reports``: (raw_id, title, contribution) of the latest run."""
    factors: dict = {"engine_version": "test"}
    if story:
        factors["story"] = story
    if matched:
        factors["explain"] = {"matched": {"match_type": matched[0], "match_value": matched[1], "label": "x",
                                          "matched_text": None, "inferred_from": None, "supported_by": None},
                              "strongest": {}, "item_count": 1, "steps": []}  # fmt: skip
    rid = uuid.uuid4()
    db.execute(
        "INSERT INTO biz.risks (id, workspace_id, entity_id, fingerprint, category, title, likelihood, impact, factors,"
        " status, last_scored_at, engine_version) VALUES (%s, %s, %s, %s, %s, 'Template title', %s, %s, %s, %s,"
        " now() - interval '1 minute', 'test')",
        (rid, ws, entity, f"fp-{rid}", category, score_l, impact, json.dumps(factors), status),
    )  # fmt: skip
    for raw_id, title, contribution in reports:
        snap = {"title": title, "url": f"https://news.example.test/{raw_id}", "type": "news",
                "published_at": "2026-11-27T08:00:00Z"}  # fmt: skip
        db.execute(
            "INSERT INTO biz.risk_evidence (workspace_id, risk_id, kind, event_source, event_raw_id, match_type,"
            " match_value, match_confidence, signal, recency, contribution, snapshot)"
            " VALUES (%s, %s, 'event', 'newsdata', %s, %s, %s, 0.9, 0.8, 0.9, %s, %s)",
            (ws, rid, raw_id, matched[0] if matched else "country", matched[1] if matched else "NLD", contribution,
             json.dumps(snap)),
        )  # fmt: skip
    return str(rid)


@pytest.fixture
def places(client, owner_db):
    me = sign_up(client, workspace="Places")
    ws = me["workspace_id"]
    src, mapping = _source(owner_db, ws)
    ids = {
        "eindhoven": _entity(owner_db, ws, src, mapping, "site", "Test Eindhoven Plant", ["NLD"], [], "Eindhoven", "critical"),
        "rotterdam": _entity(owner_db, ws, src, mapping, "site", "Test Rotterdam Depot", ["NLD"], ["NLRTM"], "Rotterdam"),
        "route": _entity(owner_db, ws, src, mapping, "route", "Test Busan lane", ["KOR", "NLD"], ["KRPUS", "NLRTM"]),
        "supplier": _entity(owner_db, ws, src, mapping, "supplier", "Test Penang Works", ["MYS"], ["MYPEN"], "Bayan Lepas"),
    }
    # Shipping through Bab el-Mandeb: the route and the supplier, each with a different strongest
    # report (so two engine stories), are one story by place.
    choke = ("chokepoint", "bab_el_mandeb")
    r_route = _risk(owner_db, ws, ids["route"], "logistics", 0.6, 0.57, matched=choke,
                    story="logistics|event:newsdata:a1",
                    reports=[("a1", "Houthis strike ships in the Red Sea - Example Times", 0.5)])  # fmt: skip
    r_sup = _risk(owner_db, ws, ids["supplier"], "logistics", 0.4, 0.55, matched=choke,
                  story="logistics|event:newsdata:b1", reports=[("b1", "Carriers divert around the Cape", 0.4)])  # fmt: skip
    # Trade policy in the Netherlands: two sites; the strongest headline was cut by the feed.
    r_ein = _risk(owner_db, ws, ids["eindhoven"], "trade_policy", 0.5, 0.6, matched=("country", "NLD"),
                  story="trade_policy|event:newsdata:c1",
                  reports=[("c1", "Dutch chipmakers react to new US Export Curbs: 'We Will", 0.6),
                           ("c2", "Dutch Government Lobbies Against US Chip Export Bill", 0.5),
                           ("c3", "Netherlands weighs export licences for…", 0.4)])  # fmt: skip
    r_rot = _risk(owner_db, ws, ids["rotterdam"], "trade_policy", 0.4, 0.45, matched=("country", "NLD"),
                  story="trade_policy|event:newsdata:c1", reports=[("c1", "Dutch chipmakers react…", 0.6)])  # fmt: skip
    # Scored before engine 1.2.0: no explain, no story; placed by its entity's first country.
    r_old = _risk(owner_db, ws, ids["supplier"], "sanctions", 0.3, 0.5)
    return me, ids, {"route": r_route, "supplier": r_sup, "eindhoven": r_ein, "rotterdam": r_rot, "old": r_old}


def test_one_row_per_place_and_the_counts_follow(client, owner_db, places):
    me, ids, r = places
    grouped = client.get("/v1/risks", headers=hdr(me), params={"group": "story"}).json()["items"]
    # Five risks, three places: Bab el-Mandeb shipping (two engine stories), Dutch trade policy, Malaysia.
    assert [g["id"] for g in grouped] == [r["route"], r["eindhoven"], r["old"]]  # each its group's highest
    ship = grouped[0]
    assert ship["story"]["key"] == "logistics|chokepoint:bab_el_mandeb"
    assert ship["story"]["risk_count"] == 2 and ship["story"]["entity_count"] == 2
    assert ship["story"]["kind_counts"] == {"route": 1, "supplier": 1}
    assert [e["id"] for e in ship["story"]["entities"]] == [str(ids["route"]), str(ids["supplier"])]  # highest first

    summary = client.get("/v1/risks/summary", headers=hdr(me)).json()
    assert summary["open_count"] == 5 and summary["story_count"] == 3
    assert summary["stories_by_category"]["logistics"] == 1 and summary["stories_by_category"]["trade_policy"] == 1
    assert summary["story_scores"] == [34.2, 30.0, 15.0]

    # By the engine's key the Bab el-Mandeb risks were two stories: the place grouping drops one.
    by_engine = owner_db.execute(
        "SELECT count(DISTINCT coalesce(factors->>'story', 'risk:' || id::text)) AS n FROM biz.risks"
        " WHERE workspace_id = %s AND status = 'open'", (me["workspace_id"],)).fetchone()["n"]  # fmt: skip
    assert by_engine == summary["story_count"] + 1


def test_where_names_the_place(client, places):
    me, ids, r = places
    risks = {x["id"]: x for x in client.get("/v1/risks", headers=hdr(me)).json()["items"]}
    assert risks[r["route"]]["where"] == {"match_type": "chokepoint", "match_value": "bab_el_mandeb", "label": "Bab el-Mandeb"}
    assert risks[r["eindhoven"]]["where"] == {"match_type": "country", "match_value": "NLD", "label": "the Netherlands"}
    # Before engine 1.2.0: the entity's first country.
    assert risks[r["old"]]["where"] == {"match_type": "country", "match_value": "MYS", "label": "Malaysia"}
    assert risks[r["old"]]["story"]["key"] == "sanctions|country:MYS"


def test_lead_is_the_strongest_whole_headline(client, places):
    me, ids, r = places
    risks = {x["id"]: x for x in client.get("/v1/risks", headers=hdr(me)).json()["items"]}
    # The strongest is cut (an unclosed quote), so the next strongest leads.
    assert risks[r["eindhoven"]]["lead"] == {
        "title": "Dutch Government Lobbies Against US Chip Export Bill", "source": "newsdata",
        "published_at": "2026-11-27T08:00:00Z", "url": "https://news.example.test/c2", "type": "news"}  # fmt: skip
    # The outlet is dropped, as the engine reads the headline.
    assert risks[r["route"]]["lead"]["title"] == "Houthis strike ships in the Red Sea"
    # Every headline cut: the strongest is used all the same.
    assert risks[r["rotterdam"]]["lead"]["title"] == "Dutch chipmakers react…"
    # No news linked: null.
    assert risks[r["old"]]["lead"] is None
    detail = client.get(f"/v1/risks/{r['eindhoven']}", headers=hdr(me)).json()
    assert detail["lead"]["url"] == "https://news.example.test/c2" and detail["where"]["label"] == "the Netherlands"


def test_story_entities_carry_kind_and_city(client, places):
    me, ids, r = places
    risk = client.get(f"/v1/risks/{r['rotterdam']}", headers=hdr(me)).json()
    assert risk["story"]["entities"] == [
        {"id": str(ids["eindhoven"]), "name": "Test Eindhoven Plant", "kind": "site", "city": "Eindhoven", "criticality": "critical"},
        {"id": str(ids["rotterdam"]), "name": "Test Rotterdam Depot", "kind": "site", "city": "Rotterdam", "criticality": None},
    ]  # fmt: skip
    assert risk["story"]["kind_counts"] == {"site": 2}
    # A risk alone at its place lists its own entity.
    old = client.get(f"/v1/risks/{r['old']}", headers=hdr(me)).json()
    assert old["story"] == {"key": "sanctions|country:MYS", "risk_count": 1, "entity_count": 1, "kind_counts": {"supplier": 1},
                            "entities": [{"id": str(ids["supplier"]), "name": "Test Penang Works", "kind": "supplier",
                                          "city": "Bayan Lepas", "criticality": None}]}  # fmt: skip


def test_place_key_in_python_matches_the_sql(client, owner_db, places):
    me, _, _ = places
    with owner_db.transaction():
        owner_db.execute("SELECT set_config('app.workspace_id', %s, true)", (me["workspace_id"],))
        rows = owner_db.execute(ser.RISK_SELECT + " WHERE r.workspace_id = %s", (me["workspace_id"],)).fetchall()
    assert rows and all(ser.place_key(row) == row["place_key"] for row in rows)


@pytest.mark.parametrize(("title", "cut"), [
    ("Dutch chipmakers react to new US Export Curbs: 'We Will", True),
    ("Netherlands weighs export licences for…", True),
    ("Netherlands weighs export licences for...", True),
    ('Minister says "no deal', True),
    ("European Leaders Aren't Banal, They are Evil", False),
    ("Dock workers' strike at Belgian ports delays cargo", False),
    ("Chipmakers: 'We will adapt' to new curbs", False),
    ("“No deals with blacklisted firms”, companies told", False),
])  # fmt: skip
def test_cut_headlines(title, cut):
    assert ser.is_cut(title) is cut


def test_event_evidence_carries_its_headline_and_whether_it_was_cut(client, owner_db, places):
    me, _, r = places
    detail = client.get(f"/v1/risks/{r['eindhoven']}", headers=hdr(me)).json()
    got = [(e["snapshot"]["title"], e["headline"], e["cut"]) for e in detail["evidence"]]
    assert got == [
        ("Dutch chipmakers react to new US Export Curbs: 'We Will", "Dutch chipmakers react to new US Export Curbs: 'We Will", True),
        ("Dutch Government Lobbies Against US Chip Export Bill", "Dutch Government Lobbies Against US Chip Export Bill", False),
        ("Netherlands weighs export licences for…", "Netherlands weighs export licences for…", True),
    ]  # fmt: skip
    # The headline as the engine reads it: the outlet dropped, the same text the lead uses.
    route = client.get(f"/v1/risks/{r['route']}", headers=hdr(me)).json()
    (ev,) = route["evidence"]
    assert ev["headline"] == route["lead"]["title"] == "Houthis strike ships in the Red Sea" and ev["cut"] is False
    assert ev["snapshot"]["title"] == "Houthis strike ships in the Red Sea - Example Times"
    # A market move is not a report: no headline and no cut.
    owner_db.execute(
        "INSERT INTO biz.risk_evidence (workspace_id, risk_id, kind, anomaly_series, anomaly_key, anomaly_ts,"
        " anomaly_method, match_type, match_value, match_confidence, signal, recency, contribution, snapshot)"
        " VALUES (%s, %s, 'anomaly', 'fx.rate', 'EUR/MYR', '2026-11-26T00:00:00Z', 'robust_z', 'currency', 'EUR/MYR',"
        " 0.9, 0.5, 0.9, 0.4, '{\"title\": \"EUR/MYR moved\"}')",
        (me["workspace_id"], r["old"]),
    )  # fmt: skip
    old = client.get(f"/v1/risks/{r['old']}", headers=hdr(me)).json()
    (an,) = old["evidence"]
    assert an["kind"] == "anomaly" and "headline" not in an and "cut" not in an
