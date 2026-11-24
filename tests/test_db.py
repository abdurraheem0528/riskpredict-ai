"""Migrations and writer against a real Postgres.

Runs only when ``TEST_DATABASE_URL`` points at a throwaway database (CI starts
a postgres:16 service for this). The ``feeds`` schema in it is dropped and
recreated by every test.
"""

import os
import re
from datetime import datetime, timedelta, timezone

import psycopg
import pytest
from conftest import fixture_json

from jobs import db, migrate
from jobs.connectors import gdacs, usgs
from jobs.models import Event, FetchResult, Status
from jobs.watch_defaults import DEFAULT_TERMS

DSN = os.environ.get("TEST_DATABASE_URL")
NOW = datetime(2026, 11, 23, 20, tzinfo=timezone.utc)


def test_seed_migration_matches_python_defaults():
    sql = (migrate.MIGRATIONS_DIR / "002_seed_watch_terms.sql").read_text()
    rows = re.findall(r"\('((?:[^']|'')*)', '((?:[^']|'')*)', '((?:[^']|'')*)'\)", sql)
    seeded = [(k, t.replace("''", "'"), s) for k, t, s in rows]
    assert seeded == [(t.kind, t.term, t.scope) for t in DEFAULT_TERMS]


needs_db = pytest.mark.skipif(not DSN, reason="TEST_DATABASE_URL not set")


@pytest.fixture
def dsn():
    with psycopg.connect(DSN, autocommit=True) as conn:
        conn.execute("DROP SCHEMA IF EXISTS feeds CASCADE")
    assert migrate.apply(DSN) == ["001_feeds_schema", "002_seed_watch_terms", "003_analysis", "004_maintenance"]
    return DSN


def query(dsn, sql, *args):
    with psycopg.connect(dsn) as conn:
        return conn.execute(sql, args).fetchall()


@needs_db
def test_migrations_are_idempotent(dsn):
    assert migrate.apply(dsn) == []
    assert migrate.apply(dsn, status_only=True) == []
    terms, health, notes = db.load_state(dsn)
    assert len(terms) == len(DEFAULT_TERMS) and health == {} and notes == {}


@needs_db
def test_write_is_idempotent_and_tracks_health(dsn):
    quakes = usgs.parse(fixture_json("usgs_4.5_day.geojson"))
    result = FetchResult("usgs", Status.OK, events=quakes, duration_ms=12)
    assert db.write_results(dsn, [result], NOW) == {"usgs": 21}
    # Same data again: nothing rewritten.
    assert db.write_results(dsn, [result], NOW + timedelta(minutes=15)) == {"usgs": 0}

    row = query(
        dsn,
        "SELECT countries, severity, lat, lon, attrs->>'place' FROM feeds.events "
        "WHERE source = 'usgs' AND raw_id = 'us6000ucyd'",
    )[0]
    assert row[0] == ["PLW"] and row[1] == pytest.approx(0.354) and row[4] == "209 km SSE of Tobi Village, Palau"

    health = query(
        dsn,
        "SELECT status, last_success_at, consecutive_failures, records_last_run, records_changed "
        "FROM feeds.source_health WHERE source = 'usgs'",
    )[0]
    assert health == ("ok", NOW + timedelta(minutes=15), 0, 21, 0)


@needs_db
def test_failures_keep_last_success_and_count_up(dsn):
    events = gdacs.parse(fixture_json("gdacs_events4app_trimmed.json"))
    db.write_results(dsn, [FetchResult("gdacs", Status.OK, events=events)], NOW)
    for i in (1, 2):
        db.write_results(dsn, [FetchResult("gdacs", Status.ERROR, error="HTTP 503 from www.gdacs.org")], NOW + timedelta(hours=i))
    status, last_ok, failures, err = query(
        dsn, "SELECT status, last_success_at, consecutive_failures, last_error FROM feeds.source_health"
    )[0]
    assert (status, last_ok, failures, err) == ("error", NOW, 2, "HTTP 503 from www.gdacs.org")
    assert query(dsn, "SELECT count(*) FROM feeds.events")[0][0] == 4  # failed runs delete nothing
    _, health, _ = db.load_state(dsn)
    assert health == {"gdacs": NOW}


@needs_db
def test_duplicate_keys_in_one_batch_are_merged(dsn):
    a = Event(source="osv", raw_id="X-1", type="cyber.vulnerability", title="t", software=["PyPI:a"])
    b = Event(source="osv", raw_id="X-1", type="cyber.vulnerability", title="t", software=["npm:b"])
    db.write_results(dsn, [FetchResult("osv", Status.OK, events=[a, b])], NOW)
    assert query(dsn, "SELECT software FROM feeds.events")[0][0] == ["PyPI:a", "npm:b"]


@needs_db
def test_changed_records_are_updated(dsn):
    ev = Event(source="s", raw_id="1", type="t", title="old", severity=0.2)
    db.write_results(dsn, [FetchResult("s", Status.OK, events=[ev])], NOW)
    ev2 = Event(source="s", raw_id="1", type="t", title="new", severity=0.2)
    assert db.write_results(dsn, [FetchResult("s", Status.OK, events=[ev2])], NOW) == {"s": 1}
    assert query(dsn, "SELECT title FROM feeds.events")[0][0] == "new"


@needs_db
def test_database_constraints_back_up_the_models(dsn):
    with pytest.raises(psycopg.errors.CheckViolation):
        query(dsn, "INSERT INTO feeds.events (source, raw_id, type, severity) VALUES ('s', '1', 't', 2)")
