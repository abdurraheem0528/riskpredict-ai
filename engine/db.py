"""The engine's database layer (risk-engine.md §2–§3).

``conn`` is a psycopg 3 connection logged in as an ``rp_app`` member, inside a
transaction in which the caller has already run
``set_config('app.workspace_id', <id>, true)`` (decision 3). The engine never
commits, never sets ``app.*``, never opens or keeps connections, and never
stores a foreign key to ``feeds`` (decision 10): evidence keeps a snapshot.

Query budget per workspace run (all bounded, all on indexes):

* ``load_entities``: 1 query (``biz.entities``, RLS-filtered).
* ``load_feeds``: events + newest tag (1), sanctions (1 cheap stamp query over
  ``sanction.*`` events and ``feeds.sanctions_entries``, plus 2 full reads only
  when either changed since the last read in this process), anomalies (1),
  series (1: fx history for the workspace's currencies + latest EPSS per
  matched CVE), source health (1).
* ``write_risks``: 1–2 reads, then batched writes (at most ``BATCH`` rows per
  statement) for inserts, updates, history, evidence and the evidence trim.
"""

from __future__ import annotations

import json
import math
import re
import uuid
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Any, Iterable, Mapping, Sequence
from uuid import UUID

from psycopg.rows import tuple_row
from psycopg.types.json import Jsonb

from engine.config import DEFAULT_CONFIG, EngineConfig
from engine.core import ENGINE_VERSION, assess, has_key, watch_term
from engine.models import Anomaly, Assessment, Entity, EventTag, FeedEvent, FeedSnapshot, RiskChange, RunResult
from engine.software import entity_keys

BATCH = 200
LIVE = ("open", "acknowledged", "mitigating")
RISK_NS = uuid.UUID("8f6f2f3e-4b1c-5d0e-9a51-7c2b3e4d5f60")  # uuid5 namespace for risk ids


class EngineContextError(RuntimeError):
    """The transaction's ``app.workspace_id`` is missing or is not the workspace being run."""


# --- helpers ---------------------------------------------------------------------------------


def _cur(conn):
    return conn.cursor(row_factory=tuple_row)


def _clean_json(v: Any) -> Any:
    if isinstance(v, float):
        return v if math.isfinite(v) else None
    if isinstance(v, Mapping):
        return {str(k): _clean_json(x) for k, x in v.items()}
    if isinstance(v, (list, tuple)):
        return [_clean_json(x) for x in v]
    if isinstance(v, (Decimal, UUID, datetime)):
        return str(v)
    return v


def _jsonb(v: Any) -> Jsonb:
    return Jsonb(_clean_json(v), dumps=lambda o: json.dumps(o, sort_keys=True, ensure_ascii=False))


def _chunks(rows: Sequence, n: int = BATCH) -> Iterable[Sequence]:
    for i in range(0, len(rows), n):
        yield rows[i : i + n]


def risk_id_for(workspace_id: UUID, fp: str) -> UUID:
    """Deterministic risk id (uuid5 of workspace + fingerprint)."""
    return uuid.uuid5(RISK_NS, f"{workspace_id}|{fp}")


def check_context(conn, workspace_id: UUID) -> None:
    got = _cur(conn).execute("SELECT nullif(current_setting('app.workspace_id', true), '')").fetchone()[0]
    if got is None or str(got) != str(workspace_id):
        raise EngineContextError(
            "app.workspace_id is not set to this workspace for the current transaction"
            " (set_config('app.workspace_id', <id>, true) first)"
        )


def base_currency(conn) -> str | None:
    row = _cur(conn).execute(
        "SELECT base_currency FROM biz.workspaces WHERE id = biz.current_workspace_id()"
    ).fetchone()
    return row[0] if row else None


# --- entities --------------------------------------------------------------------------------

_HS = re.compile(r"^[0-9]{2}([0-9]{2}([0-9]{2})?)?$")


def clean_entity(e: Entity) -> tuple[Entity, int]:
    """Normalise match keys with ``jobs.codes``; drop (and count) values that cannot be."""
    from jobs import codes

    dropped = 0

    def keep(values, ok) -> tuple[str, ...]:
        nonlocal dropped
        out = []
        for v in values or ():
            n = ok(v)
            if n:
                out.append(n)
            else:
                dropped += 1
        return tuple(dict.fromkeys(out))

    countries = keep(e.countries, lambda v: v if isinstance(v, str) and codes.is_country(v) else None)
    ports = keep(e.ports, codes.locode)
    currencies = keep(e.currencies, lambda v: v if isinstance(v, str) and codes.is_currency(v) else None)
    hs = keep(e.hs_codes, lambda v: v if isinstance(v, str) and _HS.match(v) else None)
    soft = keep(e.software, lambda v: v if isinstance(v, str) and v.startswith(("pkg:", "cpe:2.3:")) else None)
    commodities = tuple(dict.fromkeys(c.strip().casefold() for c in e.commodities or () if c and c.strip()))
    spend_ccy = e.spend_currency if e.spend_currency and codes.is_currency(e.spend_currency) else None
    from dataclasses import replace

    return (
        replace(e, countries=countries, ports=ports, currencies=currencies, hs_codes=hs, software=soft,
                commodities=commodities, spend_currency=spend_ccy),
        dropped,
    )  # fmt: skip


ENTITY_SQL = """
SELECT id, kind, name, countries, ports, currencies, hs_codes, commodities, software,
       lat, lon, annual_spend, spend_currency, revenue_share, criticality, attrs
FROM biz.entities
WHERE workspace_id = biz.current_workspace_id()
ORDER BY id
"""


def _load_entities(conn) -> tuple[list[Entity], int]:
    out, dropped = [], 0
    for r in _cur(conn).execute(ENTITY_SQL):
        e = Entity(
            id=r[0], kind=r[1], name=r[2], countries=tuple(r[3] or ()), ports=tuple(r[4] or ()),
            currencies=tuple(r[5] or ()), hs_codes=tuple(r[6] or ()), commodities=tuple(r[7] or ()),
            software=tuple(r[8] or ()), lat=r[9], lon=r[10], annual_spend=r[11], spend_currency=r[12],
            revenue_share=r[13], criticality=r[14], attrs=r[15] or {},
        )  # fmt: skip
        e, d = clean_entity(e)
        out.append(e)
        dropped += d
    return out, dropped


def load_entities(conn) -> list[Entity]:
    """The current workspace's entities (empty when no workspace is set), keys normalised."""
    return _load_entities(conn)[0]


# --- feeds -----------------------------------------------------------------------------------

EVENT_COLS = (
    "e.source, e.raw_id, e.type, e.title, e.url, e.published_at, e.first_seen_at, e.countries, e.ports,"
    " e.commodities, e.currencies, e.software, e.severity, e.lat, e.lon, e.attrs"
)

EVENTS_SQL = f"""
SELECT {EVENT_COLS}, t.event_type, t.type_confidence, t.type_source, t.entities
FROM feeds.events e
LEFT JOIN LATERAL (
    SELECT event_type, type_confidence, type_source, entities FROM feeds.event_tags t
    WHERE t.source = e.source AND t.raw_id = e.raw_id
    ORDER BY t.tagged_at DESC, t.model LIMIT 1
) t ON true
WHERE coalesce(e.published_at, e.first_seen_at) >= %(oldest)s
  AND NOT (e.type LIKE 'sanction.%%')
  AND (
    -- hazards and port disruptions: by position, country or port; ongoing ones (to_date ahead) too
    ((e.type LIKE 'hazard.%%' OR e.type = 'port.disruption')
      AND (coalesce(e.published_at, e.first_seen_at) >= %(geo_since)s OR (e.attrs->>'to_date') COLLATE "C" > %(now_text)s)
      AND (e.lat IS NOT NULL OR e.countries && %(countries)s::text[] OR e.ports && %(ports)s::text[]))
    -- vulnerabilities: software keys are compared after normalisation, in Python
    OR (%(has_software)s AND e.type LIKE 'cyber.%%' AND e.software <> '{{}}'
        AND coalesce(e.published_at, e.first_seen_at) >= %(cyber_since)s)
    -- tagged text (news): countries, company names, commodities, chokepoint place names
    OR (t.event_type IS NOT NULL AND e.type NOT LIKE 'hazard.%%' AND e.type NOT LIKE 'cyber.%%'
        AND e.type <> 'port.disruption'
        AND coalesce(e.published_at, e.first_seen_at) >= CASE t.event_type
              WHEN 'sanctions' THEN %(sanctions_since)s WHEN 'cyber_incident' THEN %(cyber_since)s
              WHEN 'port_shipping_disruption' THEN %(logistics_since)s ELSE %(news_since)s END
        AND (e.countries && %(countries)s::text[]
             OR coalesce(t.entities->'countries', '[]') ?| %(countries)s::text[]
             OR (%(has_names)s AND jsonb_array_length(coalesce(t.entities->'companies', '[]')) > 0)
             OR (%(has_commodities)s AND (jsonb_array_length(coalesce(t.entities->'commodities', '[]')) > 0
                                          OR e.commodities <> '{{}}'))
             OR (%(has_routes)s AND jsonb_array_length(coalesce(t.entities->'places', '[]')) > 0)))
  )
ORDER BY coalesce(e.published_at, e.first_seen_at) DESC, e.source, e.raw_id
LIMIT %(limit)s
"""

# One round trip: has anything in either sanctions source changed since the last read?
SANCTIONS_STAMP_SQL = """
SELECT ev.n, ev.at, se.n, se.at
FROM (SELECT count(*) AS n, max(updated_at) AS at FROM feeds.events WHERE type LIKE 'sanction.%') ev,
     (SELECT count(*) AS n, max(updated_at) AS at FROM feeds.sanctions_entries) se
"""
SANCTIONS_SQL = f"""
SELECT {EVENT_COLS} FROM feeds.events e
WHERE e.type LIKE 'sanction.%%'
ORDER BY e.source, e.raw_id
LIMIT %(limit)s
"""
# The full lists (migration 030), current state. Weak aliases are not read: OFAC itself
# flags them as too low quality to screen on.
SANCTIONS_ENTRIES_SQL = """
SELECT source, dataset, entity_id, entity_type, name, aliases, countries, programs, lists, listed_on, first_seen_at
FROM feeds.sanctions_entries
ORDER BY source, dataset, entity_id
LIMIT %(limit)s
"""

ANOMALIES_SQL = """
SELECT series, key, ts, method, value, baseline, score, attrs FROM feeds.anomalies
WHERE ts >= %(oldest)s AND (
       (series = 'fx.rate' AND key = ANY(%(fx_keys)s::text[]) AND ts >= %(fx_since)s)
    OR (series LIKE 'wb.%%' AND key = ANY(%(countries)s::text[]) AND ts >= %(wb_since)s)
    OR (series LIKE 'portwatch.%%' AND key = ANY(%(countries)s::text[]) AND ts >= %(logistics_since)s)
    OR (series = 'gdelt.articles' AND key = ANY(%(terms)s::text[]) AND ts >= %(news_since)s))
ORDER BY ts DESC, series, key, method
LIMIT %(limit)s
"""

SERIES_SQL = """
SELECT series, key, ts, value FROM feeds.series
WHERE series = 'fx.rate' AND key = ANY(%(fx_keys)s::text[]) AND ts >= %(fx_hist)s
UNION ALL
SELECT * FROM (
    SELECT DISTINCT ON (key) series, key, ts, value FROM feeds.series
    WHERE series = 'epss.score' AND key = ANY(%(cves)s::text[])
    ORDER BY key, ts DESC
) epss
"""

# Sanctions rows change rarely (hourly delta, daily full list) and are the largest read, so a
# worker process keeps the last read and reuses it while (row count, max updated_at) is the same.
_SANCTIONS_CACHE: dict[str, Any] = {}


def _event_from_row(r, tag_row=None) -> FeedEvent:
    tag = None
    if tag_row is not None and tag_row[0] is not None:
        tag = EventTag(tag_row[0], tag_row[1], tag_row[2], tag_row[3] or {})
    return FeedEvent(
        source=r[0], raw_id=r[1], type=r[2], title=r[3] or "", url=r[4], published_at=r[5], first_seen_at=r[6],
        countries=tuple(r[7] or ()), ports=tuple(r[8] or ()), commodities=tuple(r[9] or ()),
        currencies=tuple(r[10] or ()), software=tuple(r[11] or ()),
        severity=None if r[12] is None else float(r[12]), lat=r[13], lon=r[14], attrs=r[15] or {}, tag=tag,
    )  # fmt: skip


def entry_event(r) -> FeedEvent:
    """A ``feeds.sanctions_entries`` row as a standing listing (``sanction.listed``, §5.5).

    ``raw_id`` is ``<dataset>:<entity_id>`` (evidence key ``event:ofac_sdn:sdn:12345``).
    ``first_seen_at`` is when the entry (re)appeared, so a ``sanction.removal`` delta
    published after it cancels it until the next full read deletes the row, and a
    re-listing after a removal counts again.
    """
    source, dataset, entity_id, entity_type, name, aliases, countries, programs, lists, listed_on, first_seen = r
    label = "SDN" if dataset == "sdn" else dataset
    title = f"OFAC {label} list: {name}"
    if programs:
        title += f" [{', '.join(programs)}]"
    return FeedEvent(
        source=source, raw_id=f"{dataset}:{entity_id}", type="sanction.listed", title=title,
        published_at=None, first_seen_at=first_seen, countries=tuple(countries or ()),
        attrs={"entity_id": entity_id, "entity_type": entity_type, "name": name, "aliases": list(aliases or ()),
               "programs": list(programs or ()), "lists": list(lists or ()), "dataset": dataset,
               "listed_on": listed_on.isoformat() if listed_on else None, "full_list": True},
    )  # fmt: skip


def _sanctions_rows(conn, limit: int, entries_limit: int) -> tuple[FeedEvent, ...]:
    stamp = (tuple(_cur(conn).execute(SANCTIONS_STAMP_SQL).fetchone()), limit, entries_limit)
    if _SANCTIONS_CACHE.get("stamp") == stamp:
        return _SANCTIONS_CACHE["rows"]
    rows = tuple(_event_from_row(r) for r in _cur(conn).execute(SANCTIONS_SQL, {"limit": limit}))
    rows += tuple(entry_event(r) for r in _cur(conn).execute(SANCTIONS_ENTRIES_SQL, {"limit": entries_limit}))
    _SANCTIONS_CACHE.clear()
    _SANCTIONS_CACHE.update(stamp=stamp, rows=rows)
    return rows


def _since(now: datetime, config: EngineConfig, key: str) -> datetime:
    return now - timedelta(hours=config.lookback_hours(key))


def load_feeds(conn, entities: Sequence[Entity], *, now: datetime, config: EngineConfig = DEFAULT_CONFIG) -> FeedSnapshot:
    """Only the feed rows that can match these entities, within the lookback windows (§5.5)."""
    now = now.astimezone(timezone.utc)
    countries = sorted({c for e in entities if has_key(e, "country") for c in e.countries})
    ports = sorted({p for e in entities if has_key(e, "port") for p in e.ports})
    has_names = any(has_key(e, "name") for e in entities)
    has_software = any(has_key(e, "software") and entity_keys(e.software) for e in entities)
    has_commodities = any(has_key(e, "commodity") and (e.hs_codes or e.commodities) for e in entities)
    has_routes = any(e.kind == "route" and e.attrs.get("chokepoints") for e in entities)
    has_geo = any(has_key(e, "geo") and e.has_position for e in entities) or has_routes

    s = {k: _since(now, config, k) for k in config.half_life_hours}
    params = {
        "oldest": now - timedelta(days=config.max_age_days),
        "now_text": now.strftime("%Y-%m-%dT%H:%M:%S"),
        "geo_since": min(s["natural_hazard"], s["logistics"]),
        "cyber_since": min(s["cyber"], s["cyber.advisory"]),
        "sanctions_since": s["sanctions"],
        "logistics_since": s["logistics"],
        "news_since": min(s[k] for k in ("geopolitical", "labour", "trade_policy", "other", "financial", "natural_hazard")),
        "countries": countries,
        "ports": ports,
        "has_names": has_names,
        "has_software": has_software,
        "has_commodities": has_commodities,
        "has_routes": has_routes,
        "limit": config.max_events,
    }
    events: list[FeedEvent] = []
    if countries or ports or has_names or has_software or has_commodities or has_geo:
        for r in _cur(conn).execute(EVENTS_SQL, params):
            events.append(_event_from_row(r[:16], r[16:]))
    if has_names:
        events.extend(_sanctions_rows(conn, config.max_events * 10, config.max_sanctions_entries))

    currencies = sorted({c for e in entities if has_key(e, "currency") for c in e.currencies})
    spend = {e.spend_currency for e in entities if e.spend_currency}
    base = base_currency(conn)
    fx_keys = sorted({f"EUR/{c}" for c in {*currencies, *spend, *([base] if base else [])} if c != "EUR"})
    terms = sorted({t for e in entities if has_key(e, "name") and (t := watch_term(e, config))})
    anomalies: list[Anomaly] = []
    if fx_keys or countries or terms:
        a_params = {
            "oldest": now - timedelta(days=config.max_age_days),
            "fx_keys": fx_keys,
            "fx_since": s["financial"],
            "countries": countries,
            "wb_since": s["financial.wb"],
            "logistics_since": s["logistics"],
            "terms": terms,
            "news_since": s["other"],
            "limit": config.max_anomalies,
        }
        for r in _cur(conn).execute(ANOMALIES_SQL, a_params):
            anomalies.append(Anomaly(r[0], r[1], r[2], r[3], float(r[4]), float(r[5]), float(r[6]), r[7] or {}))

    cves = sorted(
        {ev.raw_id for ev in events if ev.source == "cisa_kev"}
        | {a for ev in events if ev.type.startswith("cyber.") for a in (ev.attrs.get("aliases") or ())
           if isinstance(a, str) and a.startswith("CVE-")}
    )  # fmt: skip
    series: dict[tuple[str, str], list[tuple[datetime, float]]] = {}
    if fx_keys or cves:
        for name, key, ts, value in _cur(conn).execute(
            SERIES_SQL, {"fx_keys": fx_keys, "fx_hist": now - timedelta(days=config.fx_history_days), "cves": cves}
        ):
            series.setdefault((name, key), []).append((ts, float(value)))
    health = {src: ts for src, ts in _cur(conn).execute("SELECT source, last_success_at FROM feeds.source_health")}
    return FeedSnapshot(
        now=now,
        events=tuple(events),
        anomalies=tuple(anomalies),
        series={k: tuple(sorted(v)) for k, v in sorted(series.items())},
        source_health=dict(sorted(health.items())),
    )


# --- writes ----------------------------------------------------------------------------------

INSERT_RISK = """
INSERT INTO biz.risks (id, workspace_id, entity_id, fingerprint, category, title, explanation, likelihood, impact,
                       factors, countries, ports, lat, lon, status, status_changed_at, status_changed_by,
                       first_seen_at, last_seen_at, last_scored_at, engine_version)
VALUES (%(id)s, %(ws)s, %(entity_id)s, %(fingerprint)s, %(category)s, %(title)s, %(explanation)s,
        %(likelihood)s, %(impact)s, %(factors)s, %(countries)s, %(ports)s, %(lat)s, %(lon)s, 'open', %(now)s, NULL,
        %(now)s, %(now)s, %(now)s, %(engine_version)s)
"""

UPDATE_RISK = """
UPDATE biz.risks SET
    title = %(title)s, explanation = %(explanation)s, likelihood = %(likelihood)s::real, impact = %(impact)s::real,
    factors = %(factors)s, countries = %(countries)s, ports = %(ports)s, lat = %(lat)s, lon = %(lon)s,
    last_seen_at = %(now)s, last_scored_at = %(now)s, engine_version = %(engine_version)s,
    status = CASE WHEN %(reopen)s THEN 'open' ELSE status END,
    status_changed_at = CASE WHEN %(reopen)s THEN %(now)s ELSE status_changed_at END,
    status_changed_by = CASE WHEN %(reopen)s THEN NULL ELSE status_changed_by END
WHERE workspace_id = %(ws)s AND id = %(id)s
  AND (%(reopen)s OR (title, explanation, likelihood, impact, factors, countries, ports, lat, lon,
                      last_seen_at, last_scored_at, engine_version)
       IS DISTINCT FROM (%(title)s, %(explanation)s, %(likelihood)s::real, %(impact)s::real, %(factors)s::jsonb,
                         %(countries)s::text[], %(ports)s::text[], %(lat)s::float8, %(lon)s::float8,
                         %(now)s::timestamptz, %(now)s::timestamptz, %(engine_version)s::text))
"""

RESOLVE_RISK = """
UPDATE biz.risks SET status = 'resolved', status_changed_at = %(now)s, status_changed_by = NULL
WHERE workspace_id = %(ws)s AND id = %(id)s AND status IN ('open', 'acknowledged', 'mitigating')
"""

INSERT_HISTORY = """
INSERT INTO biz.risk_history (workspace_id, risk_id, recorded_at, likelihood, impact, status, reason, changed_by,
                              engine_version)
VALUES (%(ws)s, %(id)s, %(now)s, %(likelihood)s, %(impact)s, %(status)s, %(reason)s, NULL, %(engine_version)s)
"""

UPSERT_EVIDENCE = """
INSERT INTO biz.risk_evidence AS r (workspace_id, risk_id, kind, event_source, event_raw_id, anomaly_series,
    anomaly_key, anomaly_ts, anomaly_method, match_type, match_value, match_confidence, signal, recency,
    contribution, snapshot, first_linked_at, last_linked_at)
VALUES (%(ws)s, %(risk_id)s, %(kind)s, %(event_source)s, %(event_raw_id)s, %(anomaly_series)s, %(anomaly_key)s,
        %(anomaly_ts)s, %(anomaly_method)s, %(match_type)s, %(match_value)s, %(match_confidence)s, %(signal)s,
        %(recency)s, %(contribution)s, %(snapshot)s, %(now)s, %(now)s)
ON CONFLICT (risk_id, kind, event_source, event_raw_id, anomaly_series, anomaly_key, anomaly_ts, anomaly_method)
DO UPDATE SET match_type = EXCLUDED.match_type, match_value = EXCLUDED.match_value,
    match_confidence = EXCLUDED.match_confidence, signal = EXCLUDED.signal, recency = EXCLUDED.recency,
    contribution = EXCLUDED.contribution, snapshot = EXCLUDED.snapshot, last_linked_at = EXCLUDED.last_linked_at
WHERE (r.match_type, r.match_value, r.match_confidence, r.signal, r.recency, r.contribution, r.snapshot,
       r.last_linked_at)
      IS DISTINCT FROM (EXCLUDED.match_type, EXCLUDED.match_value, EXCLUDED.match_confidence, EXCLUDED.signal,
                        EXCLUDED.recency, EXCLUDED.contribution, EXCLUDED.snapshot, EXCLUDED.last_linked_at)
"""

# Keep at most `max_evidence` rows per risk: evidence linked in this run first, then by contribution.
TRIM_EVIDENCE = """
DELETE FROM biz.risk_evidence WHERE workspace_id = %(ws)s AND id IN (
    SELECT id FROM (
        SELECT id, row_number() OVER (PARTITION BY risk_id
                                      ORDER BY (last_linked_at = %(now)s) DESC, contribution DESC, id) AS rn
        FROM biz.risk_evidence WHERE workspace_id = %(ws)s AND risk_id = ANY(%(ids)s::uuid[])
    ) ranked WHERE rn > %(keep)s)
"""

EXISTING_SQL = """
SELECT id, fingerprint, status, likelihood, impact, score, category FROM biz.risks
WHERE workspace_id = %(ws)s AND (status <> 'resolved' OR fingerprint = ANY(%(fps)s::text[]))
"""

LINKED_SQL = """
SELECT risk_id, kind, event_source, event_raw_id, anomaly_series, anomaly_key, anomaly_ts, anomaly_method
FROM biz.risk_evidence WHERE workspace_id = %(ws)s AND risk_id = ANY(%(ids)s::uuid[])
"""


def _evidence_key(kind, src, raw, series, key, ts, method) -> str:
    from engine.models import rfc3339

    if kind == "event":
        return f"event:{src}:{raw}"
    return f"anomaly:{series}|{key}|{rfc3339(ts)}|{method}"


def _risk_params(ws: UUID, rid: UUID, a: Assessment, now: datetime) -> dict:
    return {
        "id": rid, "ws": ws, "entity_id": a.entity_id, "fingerprint": a.fingerprint, "category": a.category,
        "title": a.title, "explanation": a.explanation, "likelihood": a.likelihood, "impact": a.impact,
        "factors": _jsonb(a.factors), "countries": list(a.countries), "ports": list(a.ports),
        "lat": a.lat, "lon": a.lon, "now": now, "engine_version": ENGINE_VERSION, "reopen": False,
    }  # fmt: skip


def write_risks(
    conn, workspace_id: UUID, assessments: Sequence[Assessment], *, now: datetime,
    config: EngineConfig = DEFAULT_CONFIG,
) -> RunResult:  # fmt: skip
    """Upsert risks, evidence and history for one workspace, applying the lifecycle in §3.

    Idempotent: the same assessments at the same ``now`` change no row.
    """
    check_context(conn, workspace_id)
    now = now.astimezone(timezone.utc)
    ws = workspace_id
    assessed = {a.fingerprint: a for a in assessments if a.score >= config.min_score}
    cur = _cur(conn)
    existing = {
        r[1]: {"id": r[0], "status": r[2], "likelihood": r[3], "impact": r[4], "score": r[5], "category": r[6]}
        for r in cur.execute(EXISTING_SQL, {"ws": ws, "fps": sorted(assessed)})
    }
    dismissed_ids = [existing[fp]["id"] for fp in assessed if fp in existing and existing[fp]["status"] == "dismissed"]
    linked: dict[UUID, set[str]] = {}
    if dismissed_ids:
        for r in cur.execute(LINKED_SQL, {"ws": ws, "ids": dismissed_ids}):
            linked.setdefault(r[0], set()).add(_evidence_key(*r[1:]))

    inserts, updates, history, changes = [], [], [], []
    counts = {"created": 0, "rescored": 0, "unchanged": 0, "resolved": 0, "reopened": 0}
    for fp in sorted(assessed):
        a = assessed[fp]
        old = existing.get(fp)
        if old is None:
            rid = risk_id_for(ws, fp)
            inserts.append(_risk_params(ws, rid, a, now))
            history.append({"ws": ws, "id": rid, "now": now, "likelihood": a.likelihood, "impact": a.impact,
                            "status": "open", "reason": "created", "engine_version": ENGINE_VERSION})  # fmt: skip
            changes.append(RiskChange(rid, fp, "created", None, a.score, a.category))
            counts["created"] += 1
            continue
        rid, status = old["id"], old["status"]
        p = _risk_params(ws, rid, a, now)
        reopen = status == "resolved" or (
            status == "dismissed" and any(k not in linked.get(rid, set()) for k in a.evidence_keys)
        )
        p["reopen"] = reopen
        updates.append(p)
        old_score = None if old["score"] is None else round(float(old["score"]), 1)
        if reopen:
            history.append({"ws": ws, "id": rid, "now": now, "likelihood": a.likelihood, "impact": a.impact,
                            "status": "open", "reason": "reopened", "engine_version": ENGINE_VERSION})  # fmt: skip
            changes.append(RiskChange(rid, fp, "reopened", old_score, a.score, a.category))
            counts["reopened"] += 1
        elif old_score is None or abs(a.score - old_score) >= config.rescore_min_delta:
            history.append({"ws": ws, "id": rid, "now": now, "likelihood": a.likelihood, "impact": a.impact,
                            "status": status, "reason": "rescored", "engine_version": ENGINE_VERSION})  # fmt: skip
            changes.append(RiskChange(rid, fp, "rescored", old_score, a.score, a.category))
            counts["rescored"] += 1
        else:
            counts["unchanged"] += 1

    resolves = []
    for fp in sorted(existing):
        old = existing[fp]
        if fp in assessed or old["status"] not in LIVE:
            continue
        resolves.append({"ws": ws, "id": old["id"], "now": now})
        history.append({"ws": ws, "id": old["id"], "now": now, "likelihood": old["likelihood"],
                        "impact": old["impact"], "status": "resolved", "reason": "resolved_by_engine",
                        "engine_version": ENGINE_VERSION})  # fmt: skip
        old_score = None if old["score"] is None else round(float(old["score"]), 1)
        changes.append(RiskChange(old["id"], fp, "resolved", old_score, None, old["category"]))
        counts["resolved"] += 1

    evidence = []
    for fp in sorted(assessed):
        a = assessed[fp]
        rid = existing[fp]["id"] if fp in existing else risk_id_for(ws, fp)
        for ev in a.evidence:
            evidence.append({
                "ws": ws, "risk_id": rid, "kind": ev.kind, "event_source": ev.event_source,
                "event_raw_id": ev.event_raw_id, "anomaly_series": ev.anomaly_series, "anomaly_key": ev.anomaly_key,
                "anomaly_ts": ev.anomaly_ts, "anomaly_method": ev.anomaly_method, "match_type": ev.match_type,
                "match_value": ev.match_value, "match_confidence": ev.match_confidence, "signal": ev.signal,
                "recency": ev.recency, "contribution": ev.contribution, "snapshot": _jsonb(ev.snapshot), "now": now,
            })  # fmt: skip

    for sql, rows in ((INSERT_RISK, inserts), (UPDATE_RISK, updates), (RESOLVE_RISK, resolves),
                      (INSERT_HISTORY, history), (UPSERT_EVIDENCE, evidence)):  # fmt: skip
        for chunk in _chunks(rows):
            cur.executemany(sql, chunk)
    touched = sorted({r["risk_id"] for r in evidence}, key=str)
    for chunk in _chunks(touched):
        cur.execute(TRIM_EVIDENCE, {"ws": ws, "now": now, "ids": list(chunk), "keep": config.max_evidence})

    order = {"created": 0, "reopened": 1, "rescored": 2, "resolved": 3}
    changes.sort(key=lambda c: (order[c.change], c.fingerprint))
    return RunResult(ENGINE_VERSION, len({a.entity_id for a in assessed.values()}), tuple(changes), counts)


def run_workspace(conn, workspace_id: UUID, *, now: datetime | None = None,
                  config: EngineConfig = DEFAULT_CONFIG) -> RunResult:  # fmt: skip
    """load_entities → load_feeds → assess → write_risks, inside the caller's transaction."""
    check_context(conn, workspace_id)
    now = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    # One engine run per workspace at a time (released at the caller's commit/rollback).
    _cur(conn).execute("SELECT pg_advisory_xact_lock(hashtextextended('riskpredict-engine:' || %s, 0))",
                       (str(workspace_id),))  # fmt: skip
    entities, invalid = _load_entities(conn)
    feeds = load_feeds(conn, entities, now=now, config=config)
    assessments = assess(entities, feeds, base_currency=base_currency(conn), config=config)
    res = write_risks(conn, workspace_id, assessments, now=now, config=config)
    kept = tuple(a for a in assessments if a.score >= config.min_score)
    return RunResult(res.engine_version, len(entities), res.changes, {**res.counts, "invalid_keys": invalid},
                     assessments=kept, entities=tuple(entities))  # fmt: skip
