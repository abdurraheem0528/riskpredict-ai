"""Retention: delete old rows so the Neon free tier (1 GB) does not fill up.

    python -m jobs.prune              # runs at most once a day; otherwise only prints the size
    python -m jobs.prune --force      # run now
    python -m jobs.prune --dry-run    # count what would be deleted, delete nothing

What goes (windows from the environment; defaults in brackets):

* ``feeds.events`` older than ``RETAIN_EVENTS_DAYS`` [90]. "Older" means both
  the event time (``published_at``) and the time we first stored it
  (``first_seen_at``) are past the window. Sources that re-publish old records
  on every run (the CISA KEV catalogue, OSV advisories) would otherwise be
  deleted each day and stored again 30 minutes later. Their ``feeds.event_tags``
  rows go with them (explicitly, and by the foreign key's ``ON DELETE CASCADE``).
* ``feeds.series`` observations older than ``RETAIN_SERIES_DAYS`` [400]. The
  newest ``RETAIN_SERIES_MIN_POINTS`` [60] of every series key are always kept,
  so slow annual or monthly series (World Bank) never lose their history.
* ``feeds.anomalies`` flags for points that are gone: news-volume flags
  (``gdelt.articles``) past the event window, and series flags past the series
  window whose observation was deleted.

Prints rows deleted per table and ``pg_database_size`` (counts and sizes only).
Postgres keeps the freed space for new rows (after VACUUM, which runs here when
something was deleted), so the database size stops growing rather than shrinking.

Exit codes: 0 done or not due, 1 database failure, 2 bad configuration.
"""

from __future__ import annotations

import argparse
import os
import sys
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone

import psycopg
from psycopg.types.json import Jsonb

from jobs import db

TASK = "prune"
EVERY = timedelta(hours=24)
SLACK = timedelta(minutes=30)  # the 30-min schedule drifts; don't slip a whole day
NEWS_SERIES = "gdelt.articles"  # anomalies on GDELT article counts (no feeds.series rows)


@dataclass(frozen=True)
class Settings:
    events_days: int = 90
    series_days: int = 400
    series_min_points: int = 60

    @classmethod
    def from_env(cls, env=os.environ) -> "Settings":
        values = {}
        for field, var in (
            ("events_days", "RETAIN_EVENTS_DAYS"),
            ("series_days", "RETAIN_SERIES_DAYS"),
            ("series_min_points", "RETAIN_SERIES_MIN_POINTS"),
        ):
            raw = (env.get(var) or "").strip()
            if not raw:
                continue
            if not raw.isdigit() or int(raw) < 1:
                raise ValueError(f"{var} must be a whole number of at least 1")
            values[field] = int(raw)
        return cls(**values)


@dataclass
class Deleted:
    events: int = 0
    event_tags: int = 0
    series: int = 0
    anomalies: int = 0


DELETE_TAGS = """
DELETE FROM feeds.event_tags t USING feeds.events e
WHERE t.source = e.source AND t.raw_id = e.raw_id
  AND greatest(e.published_at, e.first_seen_at) < %(events_cutoff)s
"""
DELETE_EVENTS = """
DELETE FROM feeds.events
WHERE greatest(published_at, first_seen_at) < %(events_cutoff)s
"""
DELETE_SERIES = """
DELETE FROM feeds.series s
USING (
    SELECT series, key, ts, source
    FROM (
        SELECT series, key, ts, source,
               row_number() OVER (PARTITION BY series, key, source ORDER BY ts DESC) AS newest
        FROM feeds.series
    ) ranked
    WHERE newest > %(min_points)s AND ts < %(series_cutoff)s
) old
WHERE (s.series, s.key, s.ts, s.source) = (old.series, old.key, old.ts, old.source)
"""
DELETE_ANOMALIES = """
DELETE FROM feeds.anomalies a
WHERE (a.series = %(news)s AND a.ts < %(events_cutoff)s)
   OR (a.series <> %(news)s AND a.ts < %(series_cutoff)s
       AND NOT EXISTS (
           SELECT 1 FROM feeds.series s WHERE s.series = a.series AND s.key = a.key AND s.ts = a.ts))
"""


def database_size(conn: psycopg.Connection) -> int:
    return conn.execute("SELECT pg_database_size(current_database())").fetchone()[0]


def last_run(conn: psycopg.Connection) -> datetime | None:
    row = conn.execute("SELECT last_run_at FROM feeds.maintenance WHERE task = %s", (TASK,)).fetchone()
    return row[0] if row else None


def is_due(last: datetime | None, now: datetime) -> bool:
    return last is None or now - last >= EVERY - SLACK


def prune(conn: psycopg.Connection, now: datetime, settings: Settings, *, dry_run: bool = False) -> Deleted:
    """Delete (or, with ``dry_run``, count and roll back) in one transaction."""
    params = {
        "events_cutoff": now - timedelta(days=settings.events_days),
        "series_cutoff": now - timedelta(days=settings.series_days),
        "min_points": settings.series_min_points,
        "news": NEWS_SERIES,
    }
    out = Deleted()
    with conn.transaction(force_rollback=dry_run), conn.cursor() as cur:
        cur.execute(DELETE_TAGS, params)
        out.event_tags = cur.rowcount
        cur.execute(DELETE_EVENTS, params)
        out.events = cur.rowcount
        cur.execute(DELETE_SERIES, params)
        out.series = cur.rowcount
        cur.execute(DELETE_ANOMALIES, params)
        out.anomalies = cur.rowcount
        if not dry_run:
            cur.execute(
                """INSERT INTO feeds.maintenance (task, last_run_at, notes) VALUES (%s, %s, %s)
                   ON CONFLICT (task) DO UPDATE SET last_run_at = EXCLUDED.last_run_at, notes = EXCLUDED.notes""",
                (TASK, now, Jsonb({"deleted": asdict(out), "settings": asdict(settings)})),
            )
    if not dry_run:
        # Plain VACUUM (no lock on readers) so the freed pages are reused by new rows.
        for table, n in (("events", out.events), ("event_tags", out.event_tags),
                         ("series", out.series), ("anomalies", out.anomalies)):
            if n:
                conn.execute(f"VACUUM (ANALYZE) feeds.{table}")
    return out


def mb(n: int) -> str:
    return f"{n / 2**20:.1f} MB"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m jobs.prune", description=__doc__.splitlines()[0])
    parser.add_argument("--force", action="store_true", help="run even if it ran in the last day")
    parser.add_argument("--dry-run", action="store_true", help="count what would be deleted; delete nothing")
    args = parser.parse_args(argv)
    try:
        settings = Settings.from_env()
    except ValueError as exc:
        print(f"prune: {exc}", file=sys.stderr)
        return 2
    dsn = db.dsn_from_env()
    if not dsn:
        print(f"{db.ENV_VAR} is not set", file=sys.stderr)
        return 2
    now = datetime.now(timezone.utc)
    try:
        with db.connect(dsn) as conn:
            before = database_size(conn)
            last = last_run(conn)
            if not (args.force or args.dry_run or is_due(last, now)):
                print(f"prune: not due (last run {last:%Y-%m-%d %H:%M} UTC); database size {mb(before)}")
                return 0
            deleted = prune(conn, now, settings, dry_run=args.dry_run)
            after = database_size(conn)
    except db.DatabaseError as exc:
        print(f"prune: database: {exc}", file=sys.stderr)
        return 1
    except psycopg.Error as exc:  # e.g. migration 004 not applied
        print(f"prune: database: failed ({type(exc).__name__})", file=sys.stderr)
        return 1
    verb = "would delete" if args.dry_run else "deleted"
    print(
        f"prune: {verb} events={deleted.events} (event_tags={deleted.event_tags}) series={deleted.series} "
        f"anomalies={deleted.anomalies}; windows: events {settings.events_days} d, series "
        f"{settings.series_days} d keeping the newest {settings.series_min_points} per key"
    )
    print(f"prune: database size {mb(before)} before, {mb(after)} after")
    return 0


if __name__ == "__main__":
    sys.exit(main())
