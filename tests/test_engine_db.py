"""The risk engine against a real Postgres, as an rp_app login under forced RLS.

Runs only when ``TEST_DATABASE_URL`` points at a throwaway database whose user can
create roles (CI's postgres:16 service). Every biz and feeds row here is test data.
"""

from __future__ import annotations

import importlib.util
import os
import secrets
import uuid
from contextlib import contextmanager
from dataclasses import replace
from datetime import timedelta
from pathlib import Path

import pytest

pytest.importorskip("rapidfuzz")
DSN = os.environ.get("TEST_DATABASE_URL")
pytestmark = pytest.mark.skipif(not DSN, reason="TEST_DATABASE_URL not set")

import psycopg  # noqa: E402
from conftest import reset_database  # noqa: E402
from psycopg import sql  # noqa: E402
from psycopg.conninfo import conninfo_to_dict, make_conninfo  # noqa: E402
from psycopg.types.json import Jsonb  # noqa: E402

import engine  # noqa: E402
from engine import db as edb  # noqa: E402
from engine_fixtures import NOW, event, news, ofac, quake, tag  # noqa: E402
from jobs import migrate  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
_spec = importlib.util.spec_from_file_location("create_login_roles", ROOT / "infra/scripts/create_login_roles.py")
roles_script = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(roles_script)
APP_LOGIN = "test_engine_app_login"


class Dsn(str):
    def __repr__(self) -> str:
        return f"<dsn for {conninfo_to_dict(self).get('user')}>"


@pytest.fixture(scope="module")
def app_dsn():
    reset_database(DSN)
    migrate.apply(DSN)
    pw = secrets.token_urlsafe(24)
    with psycopg.connect(DSN, autocommit=True) as conn:
        conn.execute(sql.SQL("DROP ROLE IF EXISTS {}").format(sql.Identifier(APP_LOGIN)))
        assert roles_script.ensure_login_role(conn, APP_LOGIN, "rp_app", pw) == "created"
    params = conninfo_to_dict(DSN)
    params.update(user=APP_LOGIN, password=pw)
    yield Dsn(make_conninfo(**params))
    reset_database(DSN)
    with psycopg.connect(DSN, autocommit=True) as conn:
        conn.execute(sql.SQL("DROP ROLE IF EXISTS {}").format(sql.Identifier(APP_LOGIN)))


@pytest.fixture
def app(app_dsn):
    reset_database(DSN)
    migrate.apply(DSN)
    edb._SANCTIONS_CACHE.clear()
    return app_dsn


@contextmanager
def tx(dsn: str, workspace=None, user=None):
    """One transaction with the context the worker sets; commits on success."""
    with psycopg.connect(dsn) as conn:
        if workspace is not None:
            conn.execute("SELECT set_config('app.workspace_id', %s, true)", (str(workspace),))
        if user is not None:
            conn.execute("SELECT set_config('app.user_id', %s, true)", (str(user),))
        yield conn


# --- fixture rows ------------------------------------------------------------------------------


def put_events(*events):
    """Write feed rows the way the jobs would (as the owner here)."""
    with psycopg.connect(DSN) as conn:
        for ev in events:
            conn.execute(
                "INSERT INTO feeds.events (source, raw_id, type, title, url, published_at, countries, ports,"
                " commodities, currencies, software, severity, lat, lon, attrs, first_seen_at)"
                " VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)",
                (ev.source, ev.raw_id, ev.type, ev.title, ev.url, ev.published_at, list(ev.countries), list(ev.ports),
                 list(ev.commodities), list(ev.currencies), list(ev.software), ev.severity, ev.lat, ev.lon,
                 Jsonb(dict(ev.attrs)), ev.first_seen_at),
            )  # fmt: skip
            if ev.tag is not None:
                conn.execute(
                    "INSERT INTO feeds.event_tags (source, raw_id, model, model_version, input_md5, entities,"
                    " event_type, type_confidence, type_source, tagged_at) VALUES (%s,%s,'m','v','x',%s,%s,%s,%s,%s)",
                    (ev.source, ev.raw_id, Jsonb(dict(ev.tag.entities)), ev.tag.event_type, ev.tag.type_confidence,
                     ev.tag.type_source, ev.first_seen_at),
                )  # fmt: skip


def put_entries(*rows):
    """Full-list rows as the ofac_sdn connector writes them (test data; names made up)."""
    with psycopg.connect(DSN) as conn:
        for r in rows:
            r = {"dataset": "sdn", "entity_type": "Entity", "aliases": [], "weak_aliases": [], "countries": [],
                 "programs": ["SDGT"], "lists": ["SDN List"], **r}  # fmt: skip
            conn.execute(
                "INSERT INTO feeds.sanctions_entries (source, dataset, entity_id, entity_type, name, aliases,"
                " weak_aliases, countries, programs, lists, first_seen_at, updated_at)"
                " VALUES ('ofac_sdn', %(dataset)s, %(entity_id)s, %(entity_type)s, %(name)s, %(aliases)s,"
                " %(weak_aliases)s, %(countries)s, %(programs)s, %(lists)s, %(seen)s, %(seen)s)",
                {"seen": NOW - timedelta(days=200), **r},
            )


def make_workspace(dsn: str, label: str, entities: list[dict], base_currency: str | None = "EUR") -> dict:
    ws, user, conn_id, src, mapping = (uuid.uuid4() for _ in range(5))
    with tx(dsn, user=user) as c:
        c.execute("INSERT INTO biz.users (id, email, password_hash) VALUES (%s, %s, %s)",
                  (user, f"{label}@example.test", "$argon2id$v=19$m=19456,t=2,p=1$c2FsdA$aGFzaA"))  # fmt: skip
    with tx(dsn, workspace=ws, user=user) as c:
        c.execute("INSERT INTO biz.workspaces (id, name, base_currency) VALUES (%s, %s, %s)",
                  (ws, f"Workspace {label}", base_currency))  # fmt: skip
        c.execute("INSERT INTO biz.memberships (workspace_id, user_id, role) VALUES (%s, %s, 'owner')", (ws, user))
        c.execute("INSERT INTO biz.connections (id, workspace_id, provider) VALUES (%s, %s, 'composio:airtable')",
                  (conn_id, ws))  # fmt: skip
        c.execute("INSERT INTO biz.sources (id, workspace_id, connection_id, kind, external_id, name)"
                  " VALUES (%s, %s, %s, 'composio_table', %s, 'Things')", (src, ws, conn_id, f"base-{label}"))  # fmt: skip
        c.execute("INSERT INTO biz.column_mappings (id, workspace_id, source_id, version, status, mapping, confirmed_at)"
                  " VALUES (%s, %s, %s, 1, 'confirmed', '{}', now())", (mapping, ws, src))  # fmt: skip
        ids = {}
        for i, e in enumerate(entities):
            eid = uuid.uuid4()
            ids[e["name"]] = eid
            cols = {"countries": [], "ports": [], "currencies": [], "hs_codes": [], "software": [], **e}
            attrs = Jsonb(cols.pop("attrs", {}))
            keys = list(cols)
            c.execute(
                sql.SQL("INSERT INTO biz.entities (id, workspace_id, source_id, mapping_id, source_row_key,"
                        " source_row_hash, attrs, {}) VALUES (%s, %s, %s, %s, %s, 'h', %s, {})").format(
                    sql.SQL(", ").join(map(sql.Identifier, keys)), sql.SQL(", ").join(sql.Placeholder() * len(keys))
                ),
                [eid, ws, src, mapping, f"row:{i + 2}", attrs, *cols.values()],
            )  # fmt: skip
    return {"ws": ws, "user": user, "ids": ids}


ENTITIES = [
    {"kind": "site", "name": "Osaka plant", "lat": 34.69, "lon": 135.50, "countries": ["JPN"], "criticality": "high"},
    {"kind": "supplier", "name": "Acme Trading LLC", "countries": ["ARE"], "annual_spend": 100,
     "spend_currency": "EUR", "criticality": "critical"},
    {"kind": "supplier", "name": "Zeta Components GmbH", "countries": ["DEU", "MMR"], "annual_spend": 300,
     "spend_currency": "EUR"},
    {"kind": "software", "name": "Python HTTP client", "software": ["pkg:pypi/requests@2.31.0"]},
]  # fmt: skip


def feed_rows():
    return [
        quake("us7000test", 34.75, 135.55, magnitude=6.4, severity=0.7, age_h=6, countries=["JPN"]),
        ofac("9001:designation:2026-11-23", "ACME TRADING L.L.C.", countries=["ARE"], entity_id="9001"),
        event("osv", "GHSA-test-0001", "cyber.vulnerability", software=["PyPI:requests"], severity=0.75, age_h=48),
        news("gdelt-test-1", tag("conflict", 0.9, countries=["MMR"]), age_h=12, title="Clashes reported"),
    ]


def run(dsn, ws, now=NOW, **kw):
    with tx(dsn, workspace=ws) as c:
        return engine.run_workspace(c, ws, now=now, **kw)


def dump(ws) -> dict:
    """Every engine-written biz row of one workspace (as the owner, so RLS does not hide anything)."""
    with psycopg.connect(DSN) as c:
        return {
            "risks": c.execute("SELECT id, fingerprint, status, likelihood, impact, score, title, explanation, factors,"
                               " countries, ports, lat, lon, last_seen_at, last_scored_at, status_changed_at,"
                               " engine_version FROM biz.risks WHERE workspace_id = %s ORDER BY fingerprint",
                               (ws,)).fetchall(),
            "evidence": c.execute("SELECT risk_id, kind, event_source, event_raw_id, anomaly_key, match_type,"
                                  " match_value, contribution, snapshot, first_linked_at, last_linked_at"
                                  " FROM biz.risk_evidence WHERE workspace_id = %s ORDER BY id", (ws,)).fetchall(),
            "history": c.execute("SELECT risk_id, recorded_at, score, status, reason FROM biz.risk_history"
                                 " WHERE workspace_id = %s ORDER BY id", (ws,)).fetchall(),
        }  # fmt: skip


# --- tests -------------------------------------------------------------------------------------


def test_run_writes_risks_evidence_and_history(app):
    put_events(*feed_rows())
    a = make_workspace(app, "a", ENTITIES)
    res = run(app, a["ws"])
    assert res.engine_version == engine.ENGINE_VERSION and res.scored_entities == 4
    assert res.counts == {"created": 4, "rescored": 0, "unchanged": 0, "resolved": 0, "reopened": 0,
                          "evidence_pruned": 0, "invalid_keys": 0}
    assert {c.change for c in res.changes} == {"created"}
    d = dump(a["ws"])
    cats = {r[1].split("|")[1]: r for r in d["risks"]}
    assert set(cats) == {"natural_hazard", "sanctions", "cyber", "geopolitical"}
    for r in d["risks"]:
        assert r[2] == "open" and r[16] == engine.ENGINE_VERSION
        assert r[5] == pytest.approx(round(100 * r[3] * r[4], 1), abs=0.11)  # the generated column
        assert r[8]["likelihood"]["method"] == "noisy_or" and r[8]["engine_version"] == engine.ENGINE_VERSION
    sanc = cats["sanctions"]
    assert sanc[6] == "Sanctions designation may match Acme Trading LLC" and sanc[9] == ["ARE"]
    assert cats["natural_hazard"][11] == pytest.approx(34.69)
    assert len(d["evidence"]) == 4 and len(d["history"]) == 4
    assert {h[4] for h in d["history"]} == {"created"}
    ev = next(e for e in d["evidence"] if e[2] == "ofac")
    assert ev[8]["title"] == "ofac 9001:designation:2026-11-23" and ev[8]["matched_name"] == "ACME TRADING L.L.C."
    by_id = {c.risk_id: c for c in res.changes}
    assert by_id[sanc[0]].new_score == pytest.approx(sanc[5], abs=0.11)
    # Ids are deterministic per (workspace, fingerprint)
    assert sanc[0] == edb.risk_id_for(a["ws"], sanc[1])


def test_engine_tables_hold_no_foreign_key_to_feeds(app):
    with psycopg.connect(DSN) as c:
        n = c.execute(
            "SELECT count(*) FROM pg_constraint WHERE contype = 'f' AND connamespace = 'biz'::regnamespace"
            " AND confrelid::regclass::text LIKE 'feeds.%%'"
        ).fetchone()[0]
    assert n == 0


def test_rerun_with_no_new_data_changes_nothing(app):
    put_events(*feed_rows())
    a = make_workspace(app, "a", ENTITIES)
    run(app, a["ws"])
    first = dump(a["ws"])
    again = run(app, a["ws"])
    assert again.changes == () and again.counts["unchanged"] == 4 and again.counts["created"] == 0
    assert dump(a["ws"]) == first
    # An hour later: recency moves a little, but no status changes and no new history rows.
    later = run(app, a["ws"], now=NOW + timedelta(hours=1))
    assert later.counts["created"] == later.counts["resolved"] == later.counts["reopened"] == 0
    assert len(dump(a["ws"])["history"]) == len(first["history"]) + later.counts["rescored"]
    assert {r[2] for r in dump(a["ws"])["risks"]} == {"open"}


def test_aged_out_risks_are_resolved_and_found_again_reopened(app):
    put_events(*feed_rows())
    a = make_workspace(app, "a", ENTITIES)
    run(app, a["ws"])
    res = run(app, a["ws"], now=NOW + timedelta(days=20))  # quake (12 d) and news (20 d) windows passed
    resolved = {c.fingerprint.split("|")[1] for c in res.changes if c.change == "resolved"}
    assert resolved == {"natural_hazard", "geopolitical"}
    assert all(c.new_score is None and c.old_score > 0 for c in res.changes if c.change == "resolved")
    with psycopg.connect(DSN) as c:
        rows = c.execute("SELECT fingerprint, status, status_changed_by FROM biz.risks WHERE workspace_id = %s",
                         (a["ws"],)).fetchall()  # fmt: skip
        hist = c.execute("SELECT reason, status FROM biz.risk_history WHERE workspace_id = %s AND reason <> 'created'",
                         (a["ws"],)).fetchall()  # fmt: skip
    status = {fp.split("|")[1]: (s, by) for fp, s, by in rows}
    assert status["natural_hazard"] == ("resolved", None) and status["sanctions"][0] == "open"
    assert hist.count(("resolved_by_engine", "resolved")) == 2
    assert set(hist) <= {("resolved_by_engine", "resolved"), ("rescored", "open")}
    assert run(app, a["ws"], now=NOW + timedelta(days=20)).changes == ()  # nothing more to resolve
    # Found again: a new quake near the plant reopens the same risk (same fingerprint).
    put_events(quake("us7000again", 34.7, 135.5, magnitude=6.0, age_h=-20 * 24 + 1))
    res = run(app, a["ws"], now=NOW + timedelta(days=20))
    assert [(c.change, c.fingerprint.split("|")[1]) for c in res.changes] == [("reopened", "natural_hazard")]


def test_user_statuses_are_kept_and_dismissed_reopens_only_on_new_evidence(app):
    put_events(*feed_rows())
    a = make_workspace(app, "a", ENTITIES)
    run(app, a["ws"])
    with tx(app, workspace=a["ws"], user=a["user"]) as c:
        c.execute("UPDATE biz.risks SET status = 'acknowledged', status_changed_by = %s WHERE category = 'cyber'",
                  (a["user"],))  # fmt: skip
        c.execute("UPDATE biz.risks SET status = 'dismissed', status_changed_by = %s WHERE category = 'sanctions'",
                  (a["user"],))  # fmt: skip
    res = run(app, a["ws"], now=NOW + timedelta(minutes=30))
    assert res.counts["reopened"] == 0
    status = {r[1].split("|")[1]: r[2] for r in dump(a["ws"])["risks"]}
    assert status["cyber"] == "acknowledged" and status["sanctions"] == "dismissed"
    # A second, never-linked designation of the same name reopens the dismissed risk.
    put_events(ofac("9002:designation:2026-11-24", "Acme Trading", countries=["ARE"], entity_id="9002", age_h=1))
    res = run(app, a["ws"], now=NOW + timedelta(hours=1))
    assert [(c.change, c.category) for c in res.changes if c.change == "reopened"] == [("reopened", "sanctions")]
    with psycopg.connect(DSN) as c:
        s, by = c.execute("SELECT status, status_changed_by FROM biz.risks WHERE workspace_id = %s"
                          " AND category = 'sanctions'", (a["ws"],)).fetchone()  # fmt: skip
    assert (s, by) == ("open", None)


def test_acknowledged_risk_is_resolved_when_its_evidence_ages_out(app):
    put_events(quake("q", 34.75, 135.55, age_h=1))
    a = make_workspace(app, "a", ENTITIES[:1])
    run(app, a["ws"])
    with tx(app, workspace=a["ws"], user=a["user"]) as c:
        c.execute("UPDATE biz.risks SET status = 'mitigating', status_changed_by = %s", (a["user"],))
    res = run(app, a["ws"], now=NOW + timedelta(days=30))
    assert [c.change for c in res.changes] == ["resolved"]


def test_context_is_required_and_other_workspaces_see_nothing(app):
    put_events(*feed_rows())
    a = make_workspace(app, "a", ENTITIES)
    b = make_workspace(app, "b", [])
    with tx(app) as c, pytest.raises(engine.EngineContextError):
        engine.run_workspace(c, a["ws"], now=NOW)  # no app.workspace_id
    with tx(app, workspace=b["ws"]) as c, pytest.raises(engine.EngineContextError):
        engine.run_workspace(c, a["ws"], now=NOW)  # context is another workspace
    with psycopg.connect(app) as c:  # without context biz is empty
        assert engine.load_entities(c) == []
    run(app, a["ws"])
    res_b = run(app, b["ws"])
    assert res_b.scored_entities == 0 and res_b.changes == ()
    with tx(app, workspace=b["ws"]) as c:
        assert c.execute("SELECT count(*) FROM biz.risks").fetchone()[0] == 0
        assert c.execute("SELECT count(*) FROM biz.risk_evidence").fetchone()[0] == 0
    # rp_app cannot write a risk into another workspace even through the engine's SQL
    with tx(app, workspace=b["ws"]) as c, pytest.raises(psycopg.errors.InsufficientPrivilege):
        c.execute("INSERT INTO biz.risks (workspace_id, entity_id, fingerprint, category, title, likelihood, impact,"
                  " engine_version) VALUES (%s, %s, 'x', 'other', 't', 0.1, 0.1, 'v')",
                  (a["ws"], a["ids"]["Osaka plant"]))  # fmt: skip


def test_feed_retention_keeps_the_evidence_snapshot(app):
    put_events(*feed_rows())
    a = make_workspace(app, "a", ENTITIES)
    run(app, a["ws"])
    with psycopg.connect(DSN) as c:
        c.execute("DELETE FROM feeds.events WHERE source = 'usgs'")  # what the daily prune does
    res = run(app, a["ws"], now=NOW + timedelta(minutes=10))
    assert [c.change for c in res.changes] == ["resolved"]
    ev = [e for e in dump(a["ws"])["evidence"] if e[2] == "usgs"]
    assert len(ev) == 1 and ev[0][8]["lat"] == pytest.approx(34.75)


def test_evidence_is_capped_per_risk(app):
    put_events(*[quake(f"q{i:02}", 34.69, 135.50 + i * 0.001, severity=0.3, age_h=i) for i in range(60)])
    a = make_workspace(app, "a", ENTITIES[:1])
    run(app, a["ws"])
    d = dump(a["ws"])
    assert len(d["risks"]) == 1 and len(d["evidence"]) == 50
    assert d["risks"][0][8]["likelihood"]["items"][0]["evidence_key"] == "event:usgs:q00"


def test_invalid_keys_are_reported_not_guessed(app):
    put_events(*feed_rows())
    a = make_workspace(app, "a", [{"kind": "site", "name": "Nowhere", "countries": ["XXX", "JPN"]}])
    with tx(app, workspace=a["ws"]) as c:
        [e] = engine.load_entities(c)
    assert e.countries == ("JPN",)
    assert run(app, a["ws"]).counts["invalid_keys"] == 1


def test_sanctions_rows_are_read_once_per_change(app):
    put_events(*feed_rows())
    a = make_workspace(app, "a", ENTITIES)
    run(app, a["ws"])
    rows = edb._SANCTIONS_CACHE["rows"]
    run(app, a["ws"])
    assert edb._SANCTIONS_CACHE["rows"] is rows  # unchanged: reused
    put_events(ofac("9100:designation:2026-11-24", "Someone Else Entirely"))
    run(app, a["ws"])
    assert edb._SANCTIONS_CACHE["rows"] is not rows and len(edb._SANCTIONS_CACHE["rows"]) == 2


def test_run_scenario_reads_only(app):
    put_events(*feed_rows())
    a = make_workspace(app, "a", ENTITIES)
    run(app, a["ws"])
    before = dump(a["ws"])
    acme = a["ids"]["Acme Trading LLC"]
    inputs = {"shocks": [{"type": "event", "category": "natural_hazard", "severity": 1.0, "countries": ["DEU"]}],
              "entity_overrides": [{"entity_id": str(acme), "exclude": True}]}  # fmt: skip
    with tx(app, workspace=a["ws"]) as c:
        res = engine.run_scenario(c, a["ws"], inputs, now=NOW)
    assert dump(a["ws"]) == before
    by = {c["category"]: c for c in res["changes"]}
    assert by["sanctions"]["scenario_score"] is None and by["sanctions"]["risk_id"] is not None
    assert by["natural_hazard"]["risk_id"] is None and by["natural_hazard"]["entity_name"] == "Zeta Components GmbH"
    with tx(app, workspace=a["ws"]) as c, pytest.raises(engine.ScenarioError):
        engine.run_scenario(c, a["ws"], {}, now=NOW)


def test_base_currency_cross_rates_from_stored_series(app):
    """With base USD the engine builds USD/TRY from the two EUR pairs and flags the jump in memory."""
    from engine_fixtures import fx_history

    series = {**fx_history("TRY", start=30.0, jump_last=0.08), **fx_history("USD", start=1.1)}
    with psycopg.connect(DSN) as c:
        for (name, key), pts in series.items():
            for ts, v in pts:
                c.execute("INSERT INTO feeds.series (series, key, ts, source, value) VALUES (%s,%s,%s,'frankfurter',%s)",
                          (name, key, ts, v))  # fmt: skip
    a = make_workspace(app, "a", [{"kind": "currency", "name": "Lira exposure", "currencies": ["TRY"]}],
                       base_currency="USD")  # fmt: skip
    res = run(app, a["ws"])
    [c] = res.changes
    assert c.fingerprint.endswith("|financial|currency:USD/TRY")
    ev = dump(a["ws"])["evidence"]
    assert {e[4] for e in ev} == {"USD/TRY"} and {e[1] for e in ev} == {"anomaly"}


def test_full_sanctions_lists_are_screened(app):
    put_entries(
        {"entity_id": "36001", "name": "NORTHWIND HOLDINGS", "aliases": ["Nordwind Holding AG"], "countries": ["ARE"]},
        {"entity_id": "36002", "name": "UNRELATED MARITIME", "weak_aliases": ["Zeta Components"]},
        {"entity_id": "36003", "name": "OTHER LISTED NAME", "dataset": "consolidated", "lists": ["SSI List"]},
    )
    a = make_workspace(app, "a", [
        {"kind": "customer", "name": "Nordwind Holding", "countries": ["ARE"]},
        {"kind": "supplier", "name": "Zeta Components GmbH", "countries": ["DEU"]},
    ])  # fmt: skip
    res = run(app, a["ws"])
    assert res.counts["created"] == 1  # a weak a.k.a. is never screened
    d = dump(a["ws"])
    [risk] = d["risks"]
    assert risk[6] == "Sanctions listing may match Nordwind Holding" and risk[9] == ["ARE"]
    [ev] = d["evidence"]
    assert (ev[1], ev[2], ev[3], ev[5]) == ("event", "ofac_sdn", "sdn:36001", "sanctions_name")
    assert ev[8]["matched_name"] == "Nordwind Holding AG" and ev[8]["type"] == "sanction.listed"
    # Read once per change; delisting (the connector deletes the row) re-reads and resolves the risk.
    rows = edb._SANCTIONS_CACHE["rows"]
    assert len(rows) == 3 and run(app, a["ws"]).changes == () and edb._SANCTIONS_CACHE["rows"] is rows
    with psycopg.connect(DSN) as c:
        c.execute("DELETE FROM feeds.sanctions_entries WHERE entity_id = '36001'")
    res = run(app, a["ws"])
    assert [c.change for c in res.changes] == ["resolved"] and len(edb._SANCTIONS_CACHE["rows"]) == 2


def test_full_list_read_is_bounded(app):
    names = ["Alpha Shipping", "Bravo Metals", "Charlie Foods", "Delta Chemicals", "Echo Logistics"]
    put_entries(*({"entity_id": str(i), "name": n} for i, n in enumerate(names)))
    a = make_workspace(app, "a", [{"kind": "supplier", "name": "Echo Logistics Ltd"}])
    small = engine.EngineConfig(max_sanctions_entries=2)
    with tx(app, workspace=a["ws"]) as c:
        feeds = engine.load_feeds(c, engine.load_entities(c), now=NOW, config=small)
    assert [e.raw_id for e in feeds.events] == ["sdn:0", "sdn:1"]
    assert run(app, a["ws"]).counts["created"] == 1  # the default bound reads all of them


# --- evidence the engine no longer links, and copies of one report (engine 1.3.3) -----------------
# The live case of 27 Nov 2026, as test data: a China sanctions risk for a PCB supplier kept rows
# that engines before 1.3.0 had linked (articles that do not aim sanctions at China), shown first
# with their old contributions, and six copies of one syndicated article.

PEARL = [{"kind": "supplier", "name": "Pearl River PCB Works", "countries": ["CHN"], "criticality": "high"}]
NVIDIA = "Nvidia's Most Advanced AI Chips Reach Chinese Military despite US sanctions on China"
EVIL = "European Leaders Aren't Banal, They are Evil"
CARTEL = "Trump administration expands anti-cartel campaign as legal challenges mount"


def sanctions_news(raw_id: str, title: str, *, age_h: float = 6.0, url: str | None = None):
    """GDELT news tagged as sanctions with China among its countries (test data)."""
    return replace(news(raw_id, tag("sanctions", 0.9, countries=["CHN"]), age_h=age_h, title=title), url=url)


def old_row(ws, risk_id, raw_id: str, title: str, contribution: float, linked_at) -> None:
    """An evidence row as an engine before 1.3.0 left it: linked on an earlier run, never since."""
    with psycopg.connect(DSN) as c:
        c.execute(
            "INSERT INTO biz.risk_evidence (workspace_id, risk_id, kind, event_source, event_raw_id, match_type,"
            " match_value, match_confidence, signal, recency, contribution, snapshot, first_linked_at, last_linked_at)"
            " VALUES (%s, %s, 'event', 'gdelt', %s, 'country', 'CHN', 0.36, 1.0, 1.0, %s, %s, %s, %s)",
            (ws, risk_id, raw_id, contribution, Jsonb({"title": title, "type": "news", "url": None}), linked_at,
             linked_at),
        )  # fmt: skip


def rows_of(risk_id) -> list[tuple]:
    """(raw_id, last_linked_at, linked by the latest scoring) per evidence row, as the owner."""
    with psycopg.connect(DSN) as c:
        return c.execute(
            "SELECT ev.event_raw_id, ev.last_linked_at, ev.last_linked_at >= r.last_scored_at FROM biz.risk_evidence ev"
            " JOIN biz.risks r ON r.id = ev.risk_id WHERE ev.risk_id = %s ORDER BY ev.event_raw_id", (risk_id,)
        ).fetchall()  # fmt: skip


def test_evidence_rejected_while_still_in_the_feeds_is_pruned(app):
    put_events(sanctions_news("b-nvidia", NVIDIA), sanctions_news("a-evil", EVIL))
    a = make_workspace(app, "p", PEARL)
    run(app, a["ws"], now=NOW - timedelta(hours=1))
    [risk] = dump(a["ws"])["risks"]
    assert [r[0] for r in rows_of(risk[0])] == ["b-nvidia"]  # this engine never links a-evil (§5.10)
    old_row(a["ws"], risk[0], "a-evil", EVIL, 0.35, NOW - timedelta(days=1))  # an older engine did
    res = run(app, a["ws"])
    assert res.counts["evidence_pruned"] == 1
    assert rows_of(risk[0]) == [("b-nvidia", NOW, True)]
    again = run(app, a["ws"])
    assert again.counts["evidence_pruned"] == 0 and again.changes == ()  # idempotent


def test_evidence_whose_event_left_the_feeds_stays_and_does_not_count(app):
    put_events(sanctions_news("b-nvidia", NVIDIA))
    a = make_workspace(app, "p", PEARL)
    run(app, a["ws"], now=NOW - timedelta(hours=1))
    [risk] = dump(a["ws"])["risks"]
    gone_at = NOW - timedelta(days=1)
    # not in feeds.events: retention removed it
    old_row(a["ws"], risk[0], "a-gone", "Ethiopia's Calculated Use of Maritime Diplomacy", 0.33, gone_at)
    res = run(app, a["ws"])
    assert res.counts["evidence_pruned"] == 0
    assert rows_of(risk[0]) == [("a-gone", gone_at, False), ("b-nvidia", NOW, True)]
    [after] = dump(a["ws"])["risks"]
    assert [i["evidence_key"] for i in after[8]["likelihood"]["items"]] == ["event:gdelt:b-nvidia"]


def test_resolved_risk_keeps_its_evidence(app):
    put_events(sanctions_news("b-nvidia", NVIDIA), sanctions_news("a-evil", EVIL))
    a = make_workspace(app, "p", PEARL)
    run(app, a["ws"], now=NOW - timedelta(hours=1))
    [risk] = dump(a["ws"])["risks"]
    old_row(a["ws"], risk[0], "a-evil", EVIL, 0.35, NOW - timedelta(days=1))
    before = rows_of(risk[0])
    with psycopg.connect(DSN) as c:  # retention deletes b-nvidia; a-evil is still in the feeds
        c.execute("DELETE FROM feeds.events WHERE raw_id = 'b-nvidia'")
    res = run(app, a["ws"])
    assert [c.change for c in res.changes] == ["resolved"] and res.counts["evidence_pruned"] == 0
    assert rows_of(risk[0]) == before
    assert run(app, a["ws"], now=NOW + timedelta(hours=1)).counts["evidence_pruned"] == 0
    assert rows_of(risk[0]) == before


def test_dismissed_risk_keeps_its_evidence(app):
    # Still re-scored, but its rows are what "never linked before" (§3, rule 3) remembers.
    put_events(sanctions_news("b-nvidia", NVIDIA), sanctions_news("a-evil", EVIL))
    a = make_workspace(app, "p", PEARL)
    run(app, a["ws"], now=NOW - timedelta(hours=1))
    [risk] = dump(a["ws"])["risks"]
    old_row(a["ws"], risk[0], "a-evil", EVIL, 0.35, NOW - timedelta(days=1))
    with tx(app, workspace=a["ws"], user=a["user"]) as c:
        c.execute("UPDATE biz.risks SET status = 'dismissed', status_changed_by = %s", (a["user"],))
    res = run(app, a["ws"])
    assert res.counts["evidence_pruned"] == 0 and res.counts["reopened"] == 0
    assert rows_of(risk[0]) == [("a-evil", NOW - timedelta(days=1), False), ("b-nvidia", NOW, True)]


def test_copies_of_one_report_are_one_evidence_row(app):
    title = "New US sanctions on China over chip exports"
    copies = [sanctions_news(f"cartel-{i}", title, age_h=6 + i) for i in range(6)]
    put_events(*copies)
    a = make_workspace(app, "p", PEARL)
    run(app, a["ws"])
    d = dump(a["ws"])
    [risk] = d["risks"]
    assert [e[3] for e in d["evidence"]] == ["cartel-0"]  # the strongest copy: the most recent one
    items = risk[8]["likelihood"]["items"]
    assert len(items) == 1 and risk[3] == pytest.approx(items[0]["contribution"], abs=1e-6)
    assert risk[8]["explain"]["item_count"] == 1

