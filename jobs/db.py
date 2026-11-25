"""Postgres access for the public ``feeds`` schema.

Neon's free tier bills compute by the hour it is awake, so every function
here opens its own short-lived connection, does its work and closes it. There
is no pool and no connection kept across the fetch phase.
"""

from __future__ import annotations

import dataclasses
import os
from contextlib import contextmanager
from datetime import datetime
from typing import Iterator

import psycopg
from psycopg.types.json import Jsonb

from jobs.connectors.base import WatchTerm
from jobs.models import Event, FetchResult, SanctionEntry, SeriesPoint, Status

ENV_VAR = "DATABASE_URL_FEEDS"
SUCCESS = {Status.OK, Status.EMPTY, Status.PARTIAL}
FAILURE = {Status.ERROR, Status.THROTTLED}


class DatabaseError(Exception):
    """Safe-to-log database failure (never contains the DSN or row data)."""


def dsn_from_env() -> str | None:
    return os.environ.get(ENV_VAR) or None


@contextmanager
def connect(dsn: str) -> Iterator[psycopg.Connection]:
    try:
        # autocommit: each `with conn.transaction()` block is then a real BEGIN/COMMIT.
        conn = psycopg.connect(dsn, connect_timeout=20, application_name="riskpredict-jobs", autocommit=True)
    except psycopg.Error as exc:
        raise DatabaseError(f"could not connect ({type(exc).__name__})") from None
    try:
        yield conn
    finally:
        conn.close()


# --- reads -------------------------------------------------------------------


def load_state(dsn: str) -> tuple[list[WatchTerm], dict[str, datetime | None], dict[str, dict]]:
    """Enabled watch terms, each source's last successful run and the notes of
    its last run (e.g. the GDELT file window stored), in one trip."""
    with connect(dsn) as conn, conn.cursor() as cur:
        cur.execute("SELECT kind, term, scope FROM feeds.watch_terms WHERE enabled ORDER BY id")
        terms = [WatchTerm(kind, term, scope) for kind, term, scope in cur.fetchall()]
        cur.execute("SELECT source, last_success_at, notes FROM feeds.source_health")
        rows = cur.fetchall()
    return terms, {s: t for s, t, _ in rows}, {s: n or {} for s, _, n in rows}


# --- writes ------------------------------------------------------------------

_EVENT_COLS = (
    "type",
    "title",
    "url",
    "published_at",
    "countries",
    "ports",
    "commodities",
    "currencies",
    "software",
    "severity",
    "lat",
    "lon",
    "attrs",
)
UPSERT_EVENT = f"""
INSERT INTO feeds.events AS e (source, raw_id, {", ".join(_EVENT_COLS)})
VALUES (%s, %s, {", ".join(["%s"] * len(_EVENT_COLS))})
ON CONFLICT (source, raw_id) DO UPDATE SET
    {", ".join(f"{c} = EXCLUDED.{c}" for c in _EVENT_COLS)},
    updated_at = now()
WHERE ({", ".join(f"e.{c}" for c in _EVENT_COLS)})
      IS DISTINCT FROM ({", ".join(f"EXCLUDED.{c}" for c in _EVENT_COLS)})
"""

UPSERT_SERIES = """
INSERT INTO feeds.series AS s (series, key, ts, source, value, unit, attrs)
VALUES (%s, %s, %s, %s, %s, %s, %s)
ON CONFLICT (series, key, ts, source) DO UPDATE SET
    value = EXCLUDED.value, unit = EXCLUDED.unit, attrs = EXCLUDED.attrs, updated_at = now()
WHERE (s.value, s.unit, s.attrs) IS DISTINCT FROM (EXCLUDED.value, EXCLUDED.unit, EXCLUDED.attrs)
"""

_SANCTION_COLS = (
    "entity_type",
    "name",
    "aliases",
    "weak_aliases",
    "countries",
    "programs",
    "lists",
    "listed_on",
)
UPSERT_SANCTION = f"""
INSERT INTO feeds.sanctions_entries AS s (source, dataset, entity_id, {", ".join(_SANCTION_COLS)})
VALUES (%s, %s, %s, {", ".join(["%s"] * len(_SANCTION_COLS))})
ON CONFLICT (source, dataset, entity_id) DO UPDATE SET
    {", ".join(f"{c} = EXCLUDED.{c}" for c in _SANCTION_COLS)},
    updated_at = now()
WHERE ({", ".join(f"s.{c}" for c in _SANCTION_COLS)})
      IS DISTINCT FROM ({", ".join(f"EXCLUDED.{c}" for c in _SANCTION_COLS)})
"""

# Replace, don't accumulate: an entity that is no longer on a list read in full
# is deleted (that is what "delisted" means for this table).
DELETE_DELISTED = """
DELETE FROM feeds.sanctions_entries
WHERE source = %s AND dataset = %s AND NOT (entity_id = ANY(%s))
"""

UPSERT_HEALTH = """
INSERT INTO feeds.source_health AS h (
    source, status, last_run_at, last_success_at, last_error,
    consecutive_failures, records_last_run, records_changed, duration_ms, notes)
VALUES (%(source)s, %(status)s, %(now)s,
        CASE WHEN %(success)s THEN %(now)s END, %(error)s,
        CASE WHEN %(failure)s THEN 1 ELSE 0 END,
        %(records)s, %(changed)s, %(duration_ms)s, %(notes)s)
ON CONFLICT (source) DO UPDATE SET
    status = EXCLUDED.status,
    last_run_at = EXCLUDED.last_run_at,
    last_success_at = COALESCE(EXCLUDED.last_success_at, h.last_success_at),
    last_error = EXCLUDED.last_error,
    consecutive_failures = CASE
        WHEN %(success)s THEN 0
        WHEN %(failure)s THEN h.consecutive_failures + 1
        ELSE h.consecutive_failures END,
    records_last_run = EXCLUDED.records_last_run,
    records_changed = EXCLUDED.records_changed,
    duration_ms = EXCLUDED.duration_ms,
    notes = EXCLUDED.notes
"""


def dedupe_events(events: list[Event]) -> list[Event]:
    """One row per (source, raw_id); array fields of duplicates are merged.

    Postgres rejects an INSERT ... ON CONFLICT that touches the same key twice.
    """
    merged: dict[tuple[str, str], Event] = {}
    for ev in events:
        key = (ev.source, ev.raw_id)
        prev = merged.get(key)
        if prev is None:
            merged[key] = ev
            continue
        fields = {f.name: getattr(ev, f.name) for f in dataclasses.fields(Event)}
        for f in ("countries", "ports", "commodities", "currencies", "software"):
            fields[f] = getattr(prev, f) + getattr(ev, f)
        fields["attrs"] = {**prev.attrs, **ev.attrs}
        merged[key] = Event(**fields)
    return list(merged.values())


def dedupe_series(points: list[SeriesPoint]) -> list[SeriesPoint]:
    return list({(p.series, p.key, p.ts, p.source): p for p in points}.values())


def _event_row(ev: Event) -> tuple:
    return (
        ev.source,
        ev.raw_id,
        ev.type,
        ev.title,
        ev.url,
        ev.published_at,
        list(ev.countries),
        list(ev.ports),
        list(ev.commodities),
        list(ev.currencies),
        list(ev.software),
        ev.severity,
        ev.lat,
        ev.lon,
        Jsonb(ev.attrs),
    )


def _series_row(p: SeriesPoint) -> tuple:
    return (p.series, p.key, p.ts, p.source, p.value, p.unit, Jsonb(p.attrs))


def dedupe_sanctions(entries: list[SanctionEntry]) -> list[SanctionEntry]:
    return list({(e.source, e.dataset, e.entity_id): e for e in entries}.values())


def _sanction_row(e: SanctionEntry) -> tuple:
    return (
        e.source,
        e.dataset,
        e.entity_id,
        e.entity_type,
        e.name,
        list(e.aliases),
        list(e.weak_aliases),
        list(e.countries),
        list(e.programs),
        list(e.lists),
        e.listed_on,
    )


def write_result(conn: psycopg.Connection, result: FetchResult, now: datetime) -> int:
    """Upsert one source's records and its health row in a single transaction.

    Returns the number of rows inserted or changed. For sanctions lists, which
    live in ``feeds.sanctions_entries`` rather than events or series, it also sets
    ``result.notes`` ``sanctions_upserted`` (entries inserted or changed) and
    ``sanctions_delisted`` (entries deleted), stored with the health row and
    shown in the run table.
    """
    changed = 0
    with conn.transaction(), conn.cursor() as cur:
        events = dedupe_events(result.events)
        if events:
            cur.executemany(UPSERT_EVENT, [_event_row(e) for e in events])
            changed += max(cur.rowcount, 0)
        points = dedupe_series(result.series)
        if points:
            cur.executemany(UPSERT_SERIES, [_series_row(p) for p in points])
            changed += max(cur.rowcount, 0)
        entries = dedupe_sanctions(result.sanctions)
        upserted = delisted = 0
        if entries:
            # Unchanged entries match the ON CONFLICT ... WHERE and are not rewritten.
            cur.executemany(UPSERT_SANCTION, [_sanction_row(e) for e in entries])
            upserted = max(cur.rowcount, 0)
        for dataset in result.replace_datasets:
            ids = [e.entity_id for e in entries if e.dataset == dataset]
            if not ids:  # never empty a list because a file came back empty
                continue
            cur.execute(DELETE_DELISTED, (result.source, dataset, ids))
            delisted += max(cur.rowcount, 0)
        if entries or result.replace_datasets:
            result.notes.update(sanctions_upserted=upserted, sanctions_delisted=delisted)
        changed += upserted + delisted
        cur.execute(
            UPSERT_HEALTH,
            {
                "source": result.source,
                "status": result.status.value,
                "now": now,
                "success": result.status in SUCCESS,
                "failure": result.status in FAILURE,
                "error": (result.error or None) and result.error[:500],
                "records": result.record_count,
                "changed": changed,
                "duration_ms": result.duration_ms,
                "notes": Jsonb(result.notes),
            },
        )
    return changed


def write_results(dsn: str, results: list[FetchResult], now: datetime) -> dict[str, int | str]:
    """Write every result over one short connection. A failing source is
    rolled back on its own and reported; the others are still written."""
    out: dict[str, int | str] = {}
    with connect(dsn) as conn:
        for r in results:
            try:
                out[r.source] = write_result(conn, r, now)
            except psycopg.Error as exc:
                out[r.source] = f"db error ({type(exc).__name__})"
                try:
                    # Record the failure itself, without the records.
                    failed = FetchResult(
                        r.source, Status.ERROR, error=f"db write failed ({type(exc).__name__})", duration_ms=r.duration_ms
                    )
                    write_result(conn, failed, now)
                except psycopg.Error:
                    pass
    return out
