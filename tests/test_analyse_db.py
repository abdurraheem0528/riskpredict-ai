"""Migration 003 and the analysis reads/writes against a real Postgres.

Runs only when ``TEST_DATABASE_URL`` is set (CI starts postgres:16); the
``feeds`` schema is dropped and recreated for every test.
"""

import dataclasses
import os
from datetime import datetime, timedelta, timezone

import psycopg
import pytest
from conftest import fixture_bytes, fixture_json, reset_database
from test_analyse_tagging import FakeModel, result

from jobs import db, migrate, run
from jobs.analyse import anomalies, runner, store, tagging
from jobs.connectors import frankfurter, gdacs, gdelt, ofac
from jobs.models import Event, FetchResult, Status

DSN = os.environ.get("TEST_DATABASE_URL")
NOW = datetime(2026, 11, 23, 20, tzinfo=timezone.utc)
pytestmark = pytest.mark.skipif(not DSN, reason="TEST_DATABASE_URL not set")
MODEL, VERSION = "fake/gliner2", tagging.model_version(FakeModel.revision)


@pytest.fixture
def dsn():
    reset_database(DSN)
    migrate.apply(DSN)
    return DSN


def query(dsn, sql, *args):
    with psycopg.connect(dsn) as conn:
        return conn.execute(sql, args).fetchall()


def store_events(dsn, *events):
    by_source = {}
    for e in events:
        by_source.setdefault(e.source, []).append(e)
    db.write_results(dsn, [FetchResult(s, Status.OK, events=evs) for s, evs in by_source.items()], NOW)


def fixture_events():
    return [
        *gdelt.parse_articles(fixture_json("gdelt_artlist.json"), "q1"),
        *gdacs.parse(fixture_json("gdacs_events4app_trimmed.json")),
        *ofac.parse(fixture_bytes("ofac_delta_trimmed.xml")),
        Event(source="gdelt", raw_id="es-1", type="news", title="Huelga en el puerto", attrs={"language": "Spanish"}),
        Event(source="usgs", raw_id="q-1", type="hazard.earthquake", title="M 5.0 - somewhere"),  # not tagged
    ]


def test_migration_003_tables(dsn):
    tables = {r[0] for r in query(dsn, "SELECT table_name FROM information_schema.tables WHERE table_schema='feeds'")}
    assert {"event_tags", "anomalies", "analysis_status"} <= tables
    assert migrate.apply(dsn) == []


def test_python_md5_matches_postgres(dsn):
    title = "Côte d’Ivoire: grève au port d’Abidjan — 港口"
    assert query(dsn, "SELECT md5(%s)", title)[0][0] == tagging.text_md5(title)


def test_pending_queue_and_retagging_rules(dsn):
    store_events(dsn, *fixture_events())
    pending = store.pending_tag_inputs(dsn, MODEL, VERSION, tagging.TAG_SOURCES, 100)
    assert [p.source for p in pending][:1] == ["gdelt"]
    assert {p.source for p in pending} == {"gdelt", "gdacs", "ofac"}
    assert len(pending) == 1 + 4 + 3  # Spanish GDELT title and USGS are not queued

    tags = [tagging.build_tag(p, result()) for p in pending]
    assert store.write_tags(dsn, tags, MODEL, VERSION, NOW) == 8
    assert store.pending_tag_inputs(dsn, MODEL, VERSION, tagging.TAG_SOURCES, 100) == []
    # Source fields are untouched.
    assert query(dsn, "SELECT count(*) FROM feeds.events WHERE updated_at <> first_seen_at")[0][0] == 0

    # A new model version re-queues everything; a changed title re-queues that event.
    assert len(store.pending_tag_inputs(dsn, MODEL, "other-version", tagging.TAG_SOURCES, 100)) == 8
    ev = next(e for e in fixture_events() if e.source == "gdacs")
    store_events(dsn, dataclasses.replace(ev, title=ev.title + " (updated)"))
    again = store.pending_tag_inputs(dsn, MODEL, VERSION, tagging.TAG_SOURCES, 100)
    assert [(p.source, p.raw_id) for p in again] == [("gdacs", ev.raw_id)]


def test_tags_follow_their_event(dsn):
    store_events(dsn, *fixture_events())
    pending = store.pending_tag_inputs(dsn, MODEL, VERSION, tagging.TAG_SOURCES, 100)
    tags = [tagging.build_tag(p, result()) for p in pending]
    gone = tags[0]
    query(dsn, "DELETE FROM feeds.events WHERE source = %s AND raw_id = %s RETURNING 1", gone.source, gone.raw_id)
    assert store.write_tags(dsn, tags, MODEL, VERSION, NOW) == 7  # the deleted event is skipped
    query(dsn, "DELETE FROM feeds.events WHERE source = 'ofac' RETURNING 1")
    assert query(dsn, "SELECT count(*) FROM feeds.event_tags")[0][0] == 4  # cascade


def test_event_type_check_constraint(dsn):
    store_events(dsn, *fixture_events())
    with pytest.raises(psycopg.errors.CheckViolation):
        query(
            dsn,
            "INSERT INTO feeds.event_tags (source, raw_id, model, model_version, input_md5, event_type) "
            "SELECT source, raw_id, 'm', 'v', 'x', 'weather' FROM feeds.events LIMIT 1",
        )


def test_series_round_trip_and_detection_writes(dsn):
    points = frankfurter.parse(fixture_json("frankfurter_history.json"))
    db.write_results(dsn, [FetchResult("frankfurter", Status.OK, series=points)], NOW)
    series = store.load_series(dsn, NOW)
    assert {k for k in series} == {("fx.rate", k) for k in ("EUR/USD", "EUR/JPY", "EUR/CHF", "EUR/TRY")}
    # Only as far back as the fx rule needs: 90-day window + 14-day evaluation range + 7 days slack.
    since = NOW - timedelta(days=90 + 14 + 7)
    expected = sum(1 for p in points if p.key == "EUR/USD" and p.ts >= since)
    assert 70 < expected < 146
    assert all(len(v) == expected for v in series.values())

    # Spike the last real EUR/USD value in a copy, then write the detections.
    usd = list(series[("fx.rate", "EUR/USD")])
    usd[-1] = (usd[-1][0], usd[-1][1] * 1.03)
    series[("fx.rate", "EUR/USD")] = usd
    series[("epss.score", "*")] = []
    res, detections = runner.detect_all(series, [], ["q1"])
    job = store.status_row("anomalies", "*", res.status, NOW)
    first = store.write_detections(dsn, detections, job, NOW)
    assert first == res.outputs >= 2
    assert store.write_detections(dsn, detections, job, NOW + timedelta(hours=6)) == 0  # unchanged: no rewrite

    rows = dict(query(dsn, "SELECT method, count(*) FROM feeds.anomalies WHERE key = 'EUR/USD' GROUP BY 1"))
    assert rows["robust_z"] == 1 and rows["isolation_forest"] >= 1
    status = dict(query(dsn, "SELECT subject, status FROM feeds.analysis_status WHERE analysis = 'anomalies'"))
    assert status["fx.rate|EUR/USD"] == "ok" and status["epss.score|*"] == "skipped"
    assert status[f"{anomalies.NEWS_SERIES}|q1"] == "empty" and status["*"] == "ok"

    # Re-scoring with the real (unspiked) value removes the stale flag.
    series[("fx.rate", "EUR/USD")] = store.load_series(dsn, NOW)[("fx.rate", "EUR/USD")]
    _, detections = runner.detect_all(series, [], ["q1"])
    store.write_detections(dsn, detections, job, NOW)
    assert query(dsn, "SELECT count(*) FROM feeds.anomalies WHERE key = 'EUR/USD' AND method = 'robust_z'")[0][0] == 0


def test_short_history_is_recorded_as_insufficient(dsn):
    points = frankfurter.parse(fixture_json("frankfurter_range.json"))  # 2 real days
    db.write_results(dsn, [FetchResult("frankfurter", Status.OK, series=points)], NOW)
    res, detections = runner.detect_all(store.load_series(dsn, NOW), [], [])
    store.write_detections(dsn, detections, store.status_row("anomalies", "*", res.status, NOW), NOW)
    assert res.status == "insufficient_history" and res.outputs == 0
    rows = query(
        dsn,
        "SELECT status, history_points, needed_points, last_success_at FROM feeds.analysis_status "
        "WHERE subject = 'fx.rate|EUR/USD'",
    )
    assert rows == [("insufficient_history", 0, 40, NOW)]
    assert query(dsn, "SELECT count(*) FROM feeds.anomalies")[0][0] == 0


def test_news_counts_use_complete_days_only(dsn):
    day = NOW.replace(hour=0)
    events = [
        Event(source="gdelt", raw_id=f"a{i}", type="news", title="t", published_at=day - timedelta(days=1, hours=i),
              attrs={"query": "q1"})
        for i in range(3)
    ] + [Event(source="gdelt", raw_id="today", type="news", title="t", published_at=NOW, attrs={"query": "q1"})]
    store_events(dsn, *events)
    rows, terms = store.load_news_counts(dsn, NOW)
    assert len(terms) == 18  # seeded default GDELT queries: 8 phrase terms (002), 10 theme terms (041)
    # 00:00 on the 22nd is one article on the 22nd; 23:00 and 22:00 are two on the 21st.
    assert sorted((d.day, n) for t, d, n in rows if t == "q1") == [(21, 2), (22, 1)]
    assert all(d < day for _, d, _ in rows)  # today's partial count is left out


def test_cli_analyse_writes_and_prints_counts_only(dsn, monkeypatch, capsys):
    store_events(dsn, *fixture_events())
    monkeypatch.setenv("DATABASE_URL_FEEDS", dsn)
    monkeypatch.setattr(run, "load_model", lambda: FakeModel({"Flood in Ireland": result(location=["Ireland"])}))
    assert run.main(["--analyse"]) == 0
    out = capsys.readouterr().out
    assert "tagging" in out and "anomalies" in out and "written to feeds schema" in out
    for leaked in ("Stock Market", "Ireland", "H N B TRADING", "KALHOR"):
        assert leaked not in out
    assert query(dsn, "SELECT count(*) FROM feeds.event_tags")[0][0] == 8
    tagging_row = query(dsn, "SELECT status, notes->>'model' FROM feeds.analysis_status WHERE analysis='tagging'")
    assert tagging_row == [("ok", MODEL)]
    countries = query(dsn, "SELECT entities->'countries' FROM feeds.event_tags WHERE raw_id LIKE 'FL-1104295%%'")
    assert countries == [(["IRL"],)]

    # Second run: nothing pending, spike detection not due for 6 hours.
    assert run.main(["--analyse"]) == 0
    out = capsys.readouterr().out
    assert "empty" in out and "not due yet" in out
