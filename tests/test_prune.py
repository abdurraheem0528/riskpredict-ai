"""Retention (jobs.prune) against a real, throwaway Postgres (TEST_DATABASE_URL)."""

import os
from datetime import datetime, timedelta, timezone

import psycopg
import pytest

from jobs import db, migrate, prune
from jobs.models import Event, FetchResult, SeriesPoint, Status

DSN = os.environ.get("TEST_DATABASE_URL")
NOW = datetime(2026, 11, 24, 12, tzinfo=timezone.utc)
DAY = timedelta(days=1)

pytestmark = pytest.mark.skipif(not DSN, reason="TEST_DATABASE_URL not set")


@pytest.fixture
def dsn():
    with psycopg.connect(DSN, autocommit=True) as conn:
        conn.execute("DROP SCHEMA IF EXISTS feeds CASCADE")
    migrate.apply(DSN)
    return DSN


def q(dsn, sql, *args):
    with psycopg.connect(dsn) as conn:
        return conn.execute(sql, args).fetchall()


def ev(raw_id, published):
    return Event(source="gdelt", raw_id=raw_id, type="news", title=raw_id, published_at=published)


def seed(dsn):
    events = [ev("new", NOW - 5 * DAY), ev("old", NOW - 120 * DAY), ev("edge", NOW - 89 * DAY), ev("undated", None)]
    # A catalogue entry published long ago but first stored recently (like CISA KEV).
    kev = Event(source="cisa_kev", raw_id="CVE-2021-1", type="cyber.exploited_vulnerability", title="t",
                published_at=NOW - 900 * DAY)
    daily = [SeriesPoint("frankfurter", "fx.rate", "EUR/USD", NOW - k * DAY, 1.0 + k / 1000) for k in range(500)]
    annual = [SeriesPoint("worldbank", "wb.cpi", "DEU", datetime(2026 - k, 1, 1, tzinfo=timezone.utc), 2.0) for k in range(70)]
    db.write_results(dsn, [
        FetchResult("gdelt", Status.OK, events=events),
        FetchResult("cisa_kev", Status.OK, events=[kev]),
        FetchResult("frankfurter", Status.OK, series=daily),
        FetchResult("worldbank", Status.OK, series=annual),
    ], NOW)
    with psycopg.connect(dsn) as conn:
        # Stored long ago (first_seen_at is set by the database on insert).
        conn.execute("UPDATE feeds.events SET first_seen_at = %s WHERE raw_id IN ('old', 'edge', 'undated')",
                     (NOW - 200 * DAY,))
        conn.execute("UPDATE feeds.events SET first_seen_at = %s WHERE raw_id = 'undated'", (NOW - 95 * DAY,))
        for raw_id in ("new", "old"):
            conn.execute(
                """INSERT INTO feeds.event_tags (source, raw_id, model, model_version, input_md5, event_type)
                   VALUES ('gdelt', %s, 'm', 'v', 'x', 'other')""", (raw_id,))
        rows = [
            ("gdelt.articles", "q1", NOW - 100 * DAY),  # news flag past the event window: goes
            ("gdelt.articles", "q1", NOW - 10 * DAY),
            ("fx.rate", "EUR/USD", NOW - 450 * DAY),  # its point is deleted: goes
            ("fx.rate", "EUR/USD", NOW - 10 * DAY),
            ("wb.cpi", "DEU", datetime(2000, 1, 1, tzinfo=timezone.utc)),  # point kept (newest 60): stays
        ]
        for series, key, ts in rows:
            conn.execute(
                """INSERT INTO feeds.anomalies (series, key, ts, method, value, baseline, score)
                   VALUES (%s, %s, %s, 'robust_z', 1, 0, 5)""", (series, key, ts))
        conn.commit()


def test_prune_deletes_by_window_and_keeps_slow_series(dsn):
    seed(dsn)
    with db.connect(dsn) as conn:
        deleted = prune.prune(conn, NOW, prune.Settings())
    assert deleted == prune.Deleted(events=2, event_tags=1, series=99 + 10, anomalies=2)

    left = {r[0] for r in q(dsn, "SELECT raw_id FROM feeds.events")}
    assert left == {"new", "edge", "CVE-2021-1"}  # 'old' and 'undated' (stored 95 days ago) went
    assert q(dsn, "SELECT raw_id FROM feeds.event_tags") == [("new",)]
    # Daily series: the last 400 days kept (day 0..400), the 99 older points deleted.
    fx = q(dsn, "SELECT count(*), min(ts) FROM feeds.series WHERE series = 'fx.rate'")[0]
    assert fx == (401, NOW - 400 * DAY)
    # Annual series: 70 years old, but the newest 60 points always stay.
    assert q(dsn, "SELECT count(*) FROM feeds.series WHERE series = 'wb.cpi'")[0][0] == 60
    assert sorted((s, ts.date().isoformat()) for s, ts in q(dsn, "SELECT series, ts FROM feeds.anomalies")) == [
        ("fx.rate", (NOW - 10 * DAY).date().isoformat()),
        ("gdelt.articles", (NOW - 10 * DAY).date().isoformat()),
        ("wb.cpi", "2000-01-01"),
    ]
    # Idempotent.
    with db.connect(dsn) as conn:
        assert prune.prune(conn, NOW, prune.Settings()) == prune.Deleted()


def test_dry_run_deletes_nothing(dsn):
    seed(dsn)
    with db.connect(dsn) as conn:
        counted = prune.prune(conn, NOW, prune.Settings(), dry_run=True)
    assert counted.events == 2 and counted.series == 109
    assert q(dsn, "SELECT count(*) FROM feeds.events")[0][0] == 5
    assert q(dsn, "SELECT count(*) FROM feeds.maintenance")[0][0] == 0


def test_windows_come_from_the_environment(dsn):
    s = prune.Settings.from_env({"RETAIN_EVENTS_DAYS": "3", "RETAIN_SERIES_DAYS": "30", "RETAIN_SERIES_MIN_POINTS": "5"})
    assert s == prune.Settings(3, 30, 5)
    assert prune.Settings.from_env({}) == prune.Settings(90, 400, 60)
    for bad in ("0", "-1", "ten", "1.5"):
        with pytest.raises(ValueError):
            prune.Settings.from_env({"RETAIN_EVENTS_DAYS": bad})
    seed(dsn)
    with db.connect(dsn) as conn:
        deleted = prune.prune(conn, NOW, s)
    assert deleted.series == (499 - 30) + (70 - 5)  # every annual point is > 30 days old


def test_cli_runs_once_a_day_and_prints_counts_and_size(dsn, monkeypatch, capsys):
    seed(dsn)
    monkeypatch.setenv("DATABASE_URL_FEEDS", dsn)
    for var in ("RETAIN_EVENTS_DAYS", "RETAIN_SERIES_DAYS", "RETAIN_SERIES_MIN_POINTS"):
        monkeypatch.delenv(var, raising=False)
    assert prune.main([]) == 0
    out = capsys.readouterr().out
    assert "deleted events=" in out and "series=" in out and "database size" in out and " MB" in out
    assert dsn not in out and "test:test" not in out
    notes = q(dsn, "SELECT notes FROM feeds.maintenance WHERE task = 'prune'")[0][0]
    assert notes["settings"] == {"events_days": 90, "series_days": 400, "series_min_points": 60}

    assert prune.main([]) == 0  # second run the same day: only the size
    out = capsys.readouterr().out
    assert out.startswith("prune: not due") and "database size" in out
    assert prune.main(["--force"]) == 0
    assert "deleted events=0" in capsys.readouterr().out


def test_cli_bad_config_and_missing_url(monkeypatch, capsys):
    monkeypatch.setenv("RETAIN_SERIES_DAYS", "soon")
    assert prune.main([]) == 2
    monkeypatch.delenv("RETAIN_SERIES_DAYS")
    monkeypatch.delenv("DATABASE_URL_FEEDS", raising=False)
    assert prune.main([]) == 2


def test_is_due():
    assert prune.is_due(None, NOW)
    assert not prune.is_due(NOW - timedelta(hours=20), NOW)
    assert prune.is_due(NOW - timedelta(hours=23, minutes=40), NOW)  # a run that starts a little early
