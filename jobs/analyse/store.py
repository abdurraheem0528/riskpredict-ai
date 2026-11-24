"""Reads and writes for the analysis tables (migration 003).

Same rules as :mod:`jobs.db`: every function opens one short-lived
connection, and nothing raised from here contains row data or the DSN.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Sequence

from psycopg.types.json import Jsonb

from jobs.analyse.anomalies import NEWS_SERIES, RULES, Detection, Flag, rule_for
from jobs.analyse.tagging import Tag, TagInput
from jobs.db import connect

# --- state -------------------------------------------------------------------


def last_success(dsn: str) -> dict[str, datetime | None]:
    with connect(dsn) as conn:
        rows = conn.execute(
            "SELECT analysis, last_success_at FROM feeds.analysis_status WHERE subject = '*'"
        ).fetchall()
    return dict(rows)


# --- tagging -------------------------------------------------------------------

PENDING_TAGS = """
SELECT e.source, e.raw_id, e.title
FROM feeds.events e
WHERE e.source = ANY(%(sources)s)
  AND e.title <> ''
  AND (e.source <> 'gdelt' OR coalesce(e.attrs->>'language', 'English') = 'English')
  AND NOT EXISTS (
        SELECT 1 FROM feeds.event_tags t
        WHERE t.source = e.source AND t.raw_id = e.raw_id AND t.model = %(model)s
          AND t.model_version = %(version)s AND t.input_md5 = md5(e.title))
ORDER BY (e.source = 'gdelt') DESC, e.published_at DESC NULLS LAST
LIMIT %(limit)s
"""

UPSERT_TAG = """
INSERT INTO feeds.event_tags AS t
    (source, raw_id, model, model_version, input_md5, entities, event_type, type_confidence, type_source, tagged_at)
VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
ON CONFLICT (source, raw_id, model) DO UPDATE SET
    model_version = EXCLUDED.model_version, input_md5 = EXCLUDED.input_md5, entities = EXCLUDED.entities,
    event_type = EXCLUDED.event_type, type_confidence = EXCLUDED.type_confidence,
    type_source = EXCLUDED.type_source, tagged_at = EXCLUDED.tagged_at
"""


def pending_tag_inputs(dsn: str, model: str, version: str, sources: Sequence[str], limit: int) -> list[TagInput]:
    """Events not yet tagged by this model version (or whose title changed).

    GDELT first (it is the main text source), newest first. Only English GDELT
    titles are queued, since the model is English-only.
    """
    with connect(dsn) as conn:
        rows = conn.execute(
            PENDING_TAGS, {"sources": list(sources), "model": model, "version": version, "limit": limit}
        ).fetchall()
    return [TagInput(*r) for r in rows]


def write_tags(dsn: str, tags: Sequence[Tag], model: str, version: str, now: datetime) -> int:
    if not tags:
        return 0
    rows = [
        (t.source, t.raw_id, model, version, t.input_md5, Jsonb(t.entities), t.event_type,
         t.type_confidence, t.type_source, now)
        for t in tags
    ]
    with connect(dsn) as conn, conn.transaction(), conn.cursor() as cur:
        # An event may have been deleted since it was read: skip it, don't fail the batch.
        cur.execute("CREATE TEMP TABLE _tag_keys (source text, raw_id text) ON COMMIT DROP")
        cur.executemany("INSERT INTO _tag_keys VALUES (%s, %s)", [(t.source, t.raw_id) for t in tags])
        live = {
            tuple(r)
            for r in cur.execute(
                "SELECT k.source, k.raw_id FROM _tag_keys k JOIN feeds.events e USING (source, raw_id)"
            ).fetchall()
        }
        rows = [r for r in rows if (r[0], r[1]) in live]
        if rows:
            cur.executemany(UPSERT_TAG, rows)
    return len(rows)


# --- anomalies -----------------------------------------------------------------


def load_series(dsn: str, now: datetime) -> dict[tuple[str, str], list[tuple[datetime, float]]]:
    """History for every series a rule scores, each read back only as far as it needs."""
    out: dict[tuple[str, str], list[tuple[datetime, float]]] = {}
    with connect(dsn) as conn:
        series_names = [r[0] for r in conn.execute("SELECT DISTINCT series FROM feeds.series").fetchall()]
        for name in series_names:
            rule = rule_for(name)
            if rule.skip_reason:
                out[(name, "*")] = []  # reported once as skipped; history not read
                continue
            since = now - timedelta(days=rule.window_days + rule.eval_days + 7)
            rows = conn.execute(
                "SELECT key, ts, avg(value) FROM feeds.series WHERE series = %s AND ts >= %s "
                "GROUP BY key, ts ORDER BY key, ts",
                (name, since),
            ).fetchall()
            for key, ts, value in rows:
                out.setdefault((name, key), []).append((ts, float(value)))
    return out


NEWS_COUNTS = """
SELECT e.attrs->>'query' AS term, date_trunc('day', e.published_at) AS day, count(*)
FROM feeds.events e
WHERE e.source = 'gdelt' AND e.published_at >= %s AND e.published_at < %s AND e.attrs ? 'query'
GROUP BY 1, 2
"""


def load_news_counts(dsn: str, now: datetime) -> tuple[list[tuple[str, datetime, int]], list[str]]:
    """Daily GDELT article counts per watch term for complete UTC days only."""
    rule = next(r for r in RULES if r.prefixes == (NEWS_SERIES,))
    today = now.replace(hour=0, minute=0, second=0, microsecond=0)
    since = today - timedelta(days=rule.window_days + rule.eval_days + 1)
    with connect(dsn) as conn:
        rows = conn.execute(NEWS_COUNTS, (since, today)).fetchall()
        terms = [
            r[0]
            for r in conn.execute(
                "SELECT term FROM feeds.watch_terms WHERE kind = 'gdelt_query' AND enabled ORDER BY id"
            ).fetchall()
        ]
    return [(t, d, int(n)) for t, d, n in rows], terms


UPSERT_ANOMALY = """
INSERT INTO feeds.anomalies AS a (series, key, ts, method, value, baseline, score, attrs, detected_at)
VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
ON CONFLICT (series, key, ts, method) DO UPDATE SET
    value = EXCLUDED.value, baseline = EXCLUDED.baseline, score = EXCLUDED.score, attrs = EXCLUDED.attrs,
    detected_at = EXCLUDED.detected_at
WHERE (a.value, a.baseline, a.score, a.attrs) IS DISTINCT FROM
      (EXCLUDED.value, EXCLUDED.baseline, EXCLUDED.score, EXCLUDED.attrs)
"""

# Flags in a re-evaluated range that no longer hold (e.g. the source revised a value).
DELETE_STALE = """
DELETE FROM feeds.anomalies a
WHERE a.series = %s AND a.key = %s AND a.ts > %s
  AND NOT EXISTS (SELECT 1 FROM unnest(%s::timestamptz[], %s::text[]) AS keep(ts, method)
                  WHERE keep.ts = a.ts AND keep.method = a.method)
"""

UPSERT_STATUS = """
INSERT INTO feeds.analysis_status AS s
    (analysis, subject, status, last_run_at, last_success_at, last_error, history_points, needed_points, notes)
VALUES (%(analysis)s, %(subject)s, %(status)s, %(now)s, CASE WHEN %(success)s THEN %(now)s END,
        %(error)s, %(history)s, %(needed)s, %(notes)s)
ON CONFLICT (analysis, subject) DO UPDATE SET
    status = EXCLUDED.status, last_run_at = EXCLUDED.last_run_at,
    last_success_at = COALESCE(EXCLUDED.last_success_at, s.last_success_at),
    last_error = EXCLUDED.last_error, history_points = EXCLUDED.history_points,
    needed_points = EXCLUDED.needed_points, notes = EXCLUDED.notes
"""

SUCCESS_STATUSES = {"ok", "empty", "partial", "insufficient_history"}


def status_row(analysis: str, subject: str, status: str, now: datetime, *, error: str | None = None,
               history: int | None = None, needed: int | None = None, notes: dict | None = None) -> dict:
    return {
        "analysis": analysis,
        "subject": subject,
        "status": status,
        "now": now,
        "success": status in SUCCESS_STATUSES,
        "error": error[:500] if error else None,
        "history": history,
        "needed": needed,
        "notes": Jsonb(notes or {}),
    }


def write_status(dsn: str, rows: Sequence[dict]) -> None:
    if rows:
        with connect(dsn) as conn, conn.transaction(), conn.cursor() as cur:
            cur.executemany(UPSERT_STATUS, list(rows))


def detection_status(d: Detection, now: datetime) -> dict:
    notes: dict = {**d.notes, "flags": len(d.flags), "rule": d.rule.name}
    if d.rule.skip_reason:
        notes["reason"] = d.rule.skip_reason
        return status_row("anomalies", d.subject, "skipped", now, notes=notes)
    return status_row("anomalies", d.subject, d.status, now, history=d.history_points, needed=d.needed_points,
                      notes=notes)


def _flag_row(f: Flag, now: datetime) -> tuple:
    return (f.series, f.key, f.ts, f.method, f.value, f.baseline, f.score, Jsonb(f.attrs), now)


def write_detections(dsn: str, detections: Sequence[Detection], job_status: dict, now: datetime) -> int:
    """Upsert flags, drop stale ones in re-scored ranges, and record each key's status."""
    changed = 0
    with connect(dsn) as conn, conn.transaction(), conn.cursor() as cur:
        flags = [f for d in detections for f in d.flags]
        if flags:
            cur.executemany(UPSERT_ANOMALY, [_flag_row(f, now) for f in flags])
            changed = max(cur.rowcount, 0)
        stale = []
        for d in detections:
            if d.status == "ok" and d.eval_from is not None:
                keep_ts = [f.ts for f in d.flags]
                keep_m = [f.method for f in d.flags]
                stale.append((d.series, d.key, d.eval_from, keep_ts, keep_m))
        if stale:
            cur.executemany(DELETE_STALE, stale)
        rows = [detection_status(d, now) for d in detections]
        rows.append(job_status)
        cur.executemany(UPSERT_STATUS, rows)
    return changed
