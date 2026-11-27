"""Database rows → the JSON shapes in openapi.yaml.

Shared by the API (responses) and the worker (live-update event payloads), so a
`risk.created` event carries exactly the `Risk` the list endpoint returns.
"""

from __future__ import annotations

import functools
import re
import unicodedata
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any

OPEN_STATUSES = ("open", "acknowledged", "mitigating")
CATEGORIES = ("natural_hazard", "logistics", "geopolitical", "sanctions", "trade_policy", "cyber", "financial",
              "labour", "other")  # fmt: skip
RISK_STATUSES = ("open", "acknowledged", "mitigating", "resolved", "dismissed")


def entity_place(row: dict) -> str | None:
    """``"Hsinchu, Taiwan"``, ``"Kaohsiung, Taiwan"``, ``"Taiwan"``: where an entity is, in words."""
    from api.core import engine as engine_iface

    return engine_iface.place_label(row.get("city"), list(row["countries"]), list(row["ports"])[:1])


def risk_place(row: dict) -> str | None:
    """The entity's city line when it has a city, else the risk's first port and countries."""
    from api.core import engine as engine_iface

    if row.get("entity_city"):
        return engine_iface.place_label(row["entity_city"], list(row.get("entity_countries") or row["countries"]),
                                        list(row.get("entity_ports") or row["ports"])[:1])  # fmt: skip
    return engine_iface.place_label(None, list(row["countries"]), list(row["ports"])[:1])


def ts(value: datetime | None) -> str | None:
    if value is None:
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def r1(v: float | None) -> float | None:
    return None if v is None else round(float(v), 1)


_NEWS_NEWS = re.compile(r"\b([Nn])ews news\b")


def title_text(title: str) -> str:
    """A stored risk or alert title. Engine 1.1.0 wrote "Sanctions news news on …"; risks it
    no longer rescores (resolved) and alerts keep that text, so it is shown said once."""
    return _NEWS_NEWS.sub(r"\1ews", title) if title else title


def r3(v: float | None) -> float | None:
    return None if v is None else round(float(v), 3)


def dec(v: Decimal | None) -> str | None:
    return None if v is None else f"{v:.2f}"


def user(row: dict, prefix: str = "") -> dict:
    return {
        "id": str(row[f"{prefix}id"]),
        "email": row[f"{prefix}email"],
        "display_name": row[f"{prefix}display_name"],
        "created_at": ts(row[f"{prefix}created_at"]),
    }


def workspace(row: dict) -> dict:
    return {
        "id": str(row["id"]),
        "name": row["name"],
        "base_currency": row["base_currency"],
        "alert_min_score": r1(row["alert_min_score"]),
        "share_watch_terms": row["share_watch_terms"],
        "created_at": ts(row["created_at"]),
    }


MEMBER_SELECT = """
SELECT m.role, m.created_at AS m_created_at, u.id AS u_id, u.email AS u_email,
       u.display_name AS u_display_name, u.created_at AS u_created_at
FROM biz.memberships m JOIN biz.users u ON u.id = m.user_id
"""


def member(row: dict) -> dict:
    return {"user": user(row, "u_"), "role": row["role"], "created_at": ts(row["m_created_at"])}


CONNECTION_SELECT = """
SELECT c.*, (SELECT count(*) FROM biz.sources s WHERE s.workspace_id = c.workspace_id AND s.connection_id = c.id)
       AS source_count
FROM biz.connections c
"""


def connection(row: dict) -> dict:
    watch = None
    if row.get("watch_channel_id") and row.get("watch_expires_at"):
        watch = {"expires_at": ts(row["watch_expires_at"]), "renew_after": ts(row["watch_renew_after"] or row["watch_expires_at"])}
    return {
        "id": str(row["id"]),
        "provider": row["provider"],
        "display_name": row["display_name"] or _provider_name(row["provider"]),
        "status": row["status"],
        "status_reason": row["status_reason"],
        "account_label": row["account_label"],
        "source_count": int(row.get("source_count") or 0),
        "watch": watch if row["provider"] == "google_sheets" else None,
        "created_at": ts(row["created_at"]),
        "updated_at": ts(row["updated_at"]),
    }


def _provider_name(provider: str) -> str:
    if provider == "google_sheets":
        return "Google Sheets"
    return provider.split(":", 1)[-1].replace("_", " ").title()


SOURCE_SELECT = """
SELECT s.*, (SELECT count(*) FROM biz.entities e WHERE e.workspace_id = s.workspace_id AND e.source_id = s.id)
       AS entity_count
FROM biz.sources s
"""


def source(row: dict) -> dict:
    return {
        "id": str(row["id"]),
        "connection_id": str(row["connection_id"]),
        "kind": row["kind"],
        "external_id": row["external_id"],
        "external_table_id": row["external_table_id"],
        "name": row["name"],
        "url": row["url"],
        "status": row["status"],
        "status_reason": row["status_reason"],
        "header_row": row["header_row"],
        "columns": row["columns"] or [],
        "row_count": row["row_count"],
        "entity_count": int(row.get("entity_count") or 0),
        "last_synced_at": ts(row["last_synced_at"]),
        "last_change_at": ts(row["last_change_at"]),
        "created_at": ts(row["created_at"]),
    }


def column_mapping(row: dict) -> dict:
    spec = row["mapping"] if row["mapping"] is not None else row["suggestion"]
    return {
        "id": str(row["id"]),
        "source_id": str(row["source_id"]),
        "version": row["version"],
        "status": row["status"],
        "spec": spec or {"entity_kind": None, "columns": []},
        # For a confirmed version: the model whose suggestion was confirmed unchanged, else null (mapped by hand).
        "model": row["suggestion_model"],
        "created_at": ts(row["created_at"]),
        "confirmed_at": ts(row["confirmed_at"]),
    }


ENTITY_SELECT = """
SELECT e.*, s.name AS source_name, s.url AS source_url, s.kind AS source_kind, s.last_synced_at AS source_synced_at,
       m.version AS mapping_version,
       coalesce(rk.open_count, 0) AS open_risk_count, rk.max_score
FROM biz.entities e
JOIN biz.sources s ON s.workspace_id = e.workspace_id AND s.id = e.source_id
JOIN biz.column_mappings m ON m.workspace_id = e.workspace_id AND m.id = e.mapping_id
LEFT JOIN LATERAL (
    SELECT count(*) AS open_count, max(r.score) AS max_score FROM biz.risks r
    WHERE r.workspace_id = e.workspace_id AND r.entity_id = e.id AND r.status IN ('open', 'acknowledged', 'mitigating')
) rk ON true
"""


def entity(row: dict) -> dict:
    from api.core.providers import row_url

    return {
        "id": str(row["id"]),
        "kind": row["kind"],
        "name": row["name"],
        "countries": list(row["countries"]),
        "ports": list(row["ports"]),
        "currencies": list(row["currencies"]),
        "hs_codes": list(row["hs_codes"]),
        "commodities": list(row["commodities"]),
        "software": list(row["software"]),
        "lat": row["lat"],
        "lon": row["lon"],
        "city": row["city"],
        "place": entity_place(row),
        "address": row["address"],
        "exposure": {
            "annual_spend": dec(row["annual_spend"]),
            "spend_currency": row["spend_currency"],
            "revenue_share": float(row["revenue_share"]) if row["revenue_share"] is not None else None,
            "criticality": row["criticality"],
        },
        "attrs": row["attrs"] or {},
        "lineage": {
            "source_id": str(row["source_id"]),
            "source_name": row["source_name"],
            "row_key": row["source_row_key"],
            "row_number": row["source_row_number"],
            "mapping_version": row["mapping_version"],
            "synced_at": ts(row["updated_at"]),
            "url": row_url(row["source_url"], row["source_kind"], row["source_row_number"], row["source_row_key"]),
        },
        "open_risk_count": int(row["open_risk_count"] or 0),
        "max_score": r1(row["max_score"]),
    }


def _cp_label(s: str) -> str:
    """As ``api.core.mapping._norm_label``: casefold, accents and punctuation dropped, "strait of" ignored."""
    s = unicodedata.normalize("NFKD", s).casefold()
    return re.sub(r"[^a-z0-9]+", "", s.replace("strait of", "").replace("the ", ""))


@functools.lru_cache(maxsize=1)
def _chokepoint_index() -> dict[str, str]:
    from api.core import engine as engine_iface

    index: dict[str, str] = {}
    for cp in engine_iface.chokepoints():
        for label in (cp.id, cp.name, *cp.aliases):
            index.setdefault(_cp_label(label), cp.id)
    return index


def route_chokepoints(attrs: Any) -> list[str]:
    """A route's declared chokepoints (``attrs.chokepoints``) as ids, in the order given, each once.

    Sync already stores ids (``api.core.mapping``); a name or alias is resolved here too, and a
    value that names no known chokepoint is left out."""
    raw = attrs.get("chokepoints") if isinstance(attrs, dict) else None
    out: list[str] = []
    for v in raw if isinstance(raw, list) else []:
        cid = _chokepoint_index().get(_cp_label(str(v))) if v else None
        if cid and cid not in out:
            out.append(cid)
    return out


def entity_feature(row: dict) -> dict:
    geom = {"type": "Point", "coordinates": [row["lon"], row["lat"]]} if row["lat"] is not None else None
    props = {
        "kind": row["kind"],
        "name": row["name"],
        "countries": list(row["countries"]),
        "ports": list(row["ports"]),
        "city": row["city"],
        "place": entity_place(row),
        "criticality": row["criticality"],
        "open_risk_count": int(row["open_risk_count"] or 0),
        "max_score": r1(row["max_score"]),
    }
    if row["kind"] == "route":
        # Routes only: the dashboard draws a route's sea lane through these, in order.
        props["chokepoints"] = route_chokepoints(row.get("attrs"))
    return {"type": "Feature", "id": str(row["id"]), "geometry": geom, "properties": props}


# --- stories: one row per place ----------------------------------------------------------------
# A *story* is what the dashboard lists once: every open risk of one category at one place, the
# place being what the risk's strongest item matched (`factors.explain.matched`, engine 1.2.0):
# "logistics|chokepoint:bab_el_mandeb", "trade_policy|country:NLD". Routes that pass Bab
# el-Mandeb and suppliers whose lanes pass it are one story, whichever report is strongest for
# each. A hazard matched by distance (`geo_radius`, whose value is a distance) stays the engine's
# story (`factors.story`, the event itself, risk-engine.md §5.9); a risk scored before engine
# 1.2.0 has neither and is placed by its entity's first country, else first port. The engine's
# keys are unchanged: this grouping is the API's (openapi.yaml `Risk.story`).
PLACE_MATCHES = ("country", "port", "chokepoint", "currency", "commodity", "software", "sanctions_name", "company_name")


def place_key_sql(r: str = "r", e: str = "e") -> str:
    """The story key of risk alias ``r`` (with its entity ``e``) as SQL; :func:`place_key` in Python."""
    m = f"{r}.factors->'explain'->'matched'"
    types = ", ".join(f"'{t}'" for t in PLACE_MATCHES)
    return (
        f"(CASE WHEN {m}->>'match_type' IN ({types}) AND coalesce({m}->>'match_value', '') <> ''"
        f" THEN {r}.category || '|' || ({m}->>'match_type') || ':' || ({m}->>'match_value')"
        f" WHEN ({r}.factors ? 'story') THEN {r}.factors->>'story'"
        f" WHEN NOT ({r}.factors ? 'explain') AND cardinality({e}.countries) > 0"
        f" THEN {r}.category || '|country:' || {e}.countries[1]"
        f" WHEN NOT ({r}.factors ? 'explain') AND cardinality({e}.ports) > 0"
        f" THEN {r}.category || '|port:' || {e}.ports[1]"
        f" ELSE 'risk:' || {r}.id::text END)"
    )


def _matched(row: dict) -> dict | None:
    factors = row.get("factors")
    explain = factors.get("explain") if isinstance(factors, dict) else None
    m = explain.get("matched") if isinstance(explain, dict) else None
    return m if isinstance(m, dict) else None


def place_key(row: dict) -> str:
    """:func:`place_key_sql` for a ``RISK_SELECT`` row (the SQL is what lists and counts use)."""
    m, factors = _matched(row), row.get("factors") if isinstance(row.get("factors"), dict) else {}
    if m and m.get("match_type") in PLACE_MATCHES and m.get("match_value"):
        return f"{row['category']}|{m['match_type']}:{m['match_value']}"
    if factors.get("story"):
        return str(factors["story"])
    countries, ports = list(row.get("entity_countries") or []), list(row.get("entity_ports") or [])
    if "explain" not in factors and countries:
        return f"{row['category']}|country:{countries[0]}"
    if "explain" not in factors and ports:
        return f"{row['category']}|port:{ports[0]}"
    return f"risk:{row['id']}"


# Countries English names with "the", and pycountry names that read like a form ("Russian
# Federation"). Others are the engine's name (pycountry's common name).
PROSE_COUNTRIES = {
    "NLD": "the Netherlands", "USA": "the United States", "GBR": "the United Kingdom", "PHL": "the Philippines",
    "ARE": "the United Arab Emirates", "CZE": "the Czech Republic", "DOM": "the Dominican Republic",
    "BHS": "the Bahamas", "GMB": "the Gambia", "MDV": "the Maldives", "MHL": "the Marshall Islands",
    "SLB": "the Solomon Islands", "CAF": "the Central African Republic", "COM": "the Comoros",
    "COD": "the Democratic Republic of the Congo", "RUS": "Russia", "SYR": "Syria", "VEN": "Venezuela",
    "BOL": "Bolivia", "TZA": "Tanzania", "LAO": "Laos", "MDA": "Moldova", "PRK": "North Korea", "VAT": "Vatican City",
}  # fmt: skip


def country_prose(code: str) -> str:
    """``"NLD"`` → ``"the Netherlands"``, ``"TWN"`` → ``"Taiwan"``: a country as running text names it."""
    from api.core import engine as engine_iface

    return PROSE_COUNTRIES.get(code) or engine_iface.country_name(code)


@functools.lru_cache(maxsize=1)
def _chokepoint_names() -> dict[str, str]:
    from api.core import engine as engine_iface

    return {cp.id: cp.name for cp in engine_iface.chokepoints()}


def where_label(match_type: str, match_value: str, row: dict) -> str:
    """The place or thing a match names, in words: "the Netherlands", "Bab el-Mandeb", "Rotterdam"."""
    from api.core import engine as engine_iface

    if match_type == "country" and re.fullmatch(r"[A-Z]{3}", match_value):
        return country_prose(match_value)
    if match_type == "port":
        return engine_iface.port_name(match_value)
    if match_type == "chokepoint":
        return _chokepoint_names().get(match_value) or match_value.replace("_", " ").capitalize()
    if match_type == "geo_radius":  # the value is a distance; the place is the entity's
        return row.get("entity_city") or risk_place(row) or row["entity_name"]
    if match_type == "software":  # "PyPI:requests", "cpe:2.3:a:vendor:product:1.0"
        if match_value.startswith("cpe:"):
            parts = match_value.split(":")
            return parts[4].replace("_", " ") if len(parts) > 4 else match_value
        return match_value.split(":", 1)[-1]
    return match_value


def where(row: dict) -> dict | None:
    """``{match_type, match_value, label}`` of the risk's strongest item; for a risk scored before
    engine 1.2.0 (no ``factors.explain``), its entity's first country, else first port; else null."""
    m = _matched(row)
    if m and m.get("match_type") and m.get("match_value"):
        mt, mv = str(m["match_type"]), str(m["match_value"])
    elif m is None:
        countries, ports = list(row.get("entity_countries") or []), list(row.get("entity_ports") or [])
        if countries:
            mt, mv = "country", countries[0]
        elif ports:
            mt, mv = "port", ports[0]
        else:
            return None
    else:
        return None
    return {"match_type": mt, "match_value": mv, "label": where_label(mt, mv, row)}


# A headline the feed cut off: it ends in an ellipsis, or a quote opens and never closes
# (Currents, 27 Nov: "… Export Curbs: 'We Will"). An apostrophe inside or after a word
# ("Aren't", "workers' strike") is neither. No connector cuts titles at a fixed length:
# NewsData and Currents titles are stored as the API sends them (jobs/connectors/newsdata.py,
# currents.py), and NewsData documents no title limit; NVD's own cut ends in "...".
_OPEN_QUOTE = re.compile(r"(?:^|(?<=[\s(\[:;—–-]))['\"‘“](?=\w)")
_CLOSE_QUOTE = re.compile(r"(?<=[\w.,!?…)])['\"’”](?=$|[\s.,;:!?)\]—–-])")


def is_cut(title: str) -> bool:
    t = title.rstrip()
    if t.endswith(("…", "...")):
        return True
    return len(_OPEN_QUOTE.findall(t)) > len(_CLOSE_QUOTE.findall(t))


def lead(row: dict) -> dict | None:
    """``{title, source, published_at, url, type}``: the strongest report linked by the latest run, as
    its headline (outlet and wire tags dropped, ``engine.headline``); a cut headline is skipped
    for the next strongest, and when every one is cut the strongest is used. Null when the risk
    has no news or other event (a market anomaly)."""
    from api.core import engine as engine_iface

    picks = []
    for it in row.get("lead_items") or []:
        raw = (it.get("title") or "").strip()
        title = (engine_iface.headline(raw, it.get("url")) or raw).strip()
        if title:
            picks.append((title, it))
    if not picks:
        return None
    title, it = next((p for p in picks if not is_cut(p[0])), picks[0])
    return {"title": title, "source": it.get("source") or "", "published_at": it.get("published_at") or None,
            "url": it.get("url") or None, "type": it.get("type") or None}  # fmt: skip


# `evidence_count` counts the rows linked by the risk's latest scoring (`last_linked_at` is that
# run's time), the items `factors.likelihood` was computed from. A row from an earlier run whose
# feed row has gone is still shown ("no longer in the feed") but not counted (risk-engine.md §13).
# The story subquery runs once per statement (hash join), over the workspace's live risks: one row
# per (story, entity), then per story. `lead_items` are the latest run's event rows, strongest
# first (at most 8: enough to step past cut headlines).
STORY_KEY = place_key_sql("r", "e")
RISK_SELECT = f"""
SELECT r.*, e.kind AS entity_kind, e.name AS entity_name, e.city AS entity_city, e.countries AS entity_countries,
       e.ports AS entity_ports, e.criticality AS entity_criticality, {STORY_KEY} AS place_key,
       (SELECT count(*) FROM biz.risk_evidence ev WHERE ev.workspace_id = r.workspace_id AND ev.risk_id = r.id
          AND ev.last_linked_at >= r.last_scored_at) AS evidence_count,
       st.n AS story_size, st.entities AS story_entities,
       (SELECT jsonb_agg(l ORDER BY l.contribution DESC, l.id) FROM (
          SELECT ev.id, ev.contribution, ev.event_source AS source, ev.snapshot->>'title' AS title,
                 ev.snapshot->>'url' AS url, ev.snapshot->>'published_at' AS published_at, ev.snapshot->>'type' AS type
          FROM biz.risk_evidence ev
          WHERE ev.workspace_id = r.workspace_id AND ev.risk_id = r.id AND ev.kind = 'event'
            AND ev.last_linked_at >= r.last_scored_at
          ORDER BY ev.contribution DESC, ev.id LIMIT 8) l) AS lead_items
FROM biz.risks r
JOIN biz.entities e ON e.workspace_id = r.workspace_id AND e.id = r.entity_id
LEFT JOIN (
    SELECT g.key, sum(g.n) AS n,
           jsonb_agg(jsonb_build_object('id', g.entity_id, 'name', ge.name, 'kind', ge.kind, 'city', ge.city,
                                        'criticality', ge.criticality) ORDER BY g.score DESC, g.entity_id) AS entities
    FROM (
        SELECT {place_key_sql("s", "se")} AS key, s.workspace_id, s.entity_id, max(s.score) AS score, count(*) AS n
        FROM biz.risks s JOIN biz.entities se ON se.workspace_id = s.workspace_id AND se.id = s.entity_id
        WHERE s.status IN ('open', 'acknowledged', 'mitigating')
        GROUP BY 1, 2, 3
    ) g JOIN biz.entities ge ON ge.workspace_id = g.workspace_id AND ge.id = g.entity_id
    GROUP BY g.key
) st ON st.key = {STORY_KEY}
"""
MAX_STORY_ENTITIES = 10


def _story_entity(e: dict) -> dict:
    return {"id": str(e["id"]), "name": e["name"], "kind": e.get("kind"), "city": e.get("city"),
            "criticality": e.get("criticality")}  # fmt: skip


def story(row: dict) -> dict:
    """``{key, risk_count, entity_count, kind_counts, entities}``: the risk's story (one category at
    one place) and the entities it reaches (≤ 10 listed, all of them counted by kind)."""
    key = row.get("place_key") or place_key(row)
    own = _story_entity({"id": row["entity_id"], "name": row["entity_name"], "kind": row["entity_kind"],
                         "city": row.get("entity_city"), "criticality": row.get("entity_criticality")})  # fmt: skip
    ents = [_story_entity(e) for e in (row.get("story_entities") or [])] if row.get("story_size") else []
    if not ents:
        return {"key": key, "risk_count": 1, "entity_count": 1, "kind_counts": {own["kind"]: 1}, "entities": [own]}
    kinds: dict[str, int] = {}
    for e in ents:
        kinds[e["kind"]] = kinds.get(e["kind"], 0) + 1
    return {"key": key, "risk_count": int(row["story_size"]), "entity_count": len(ents), "kind_counts": kinds,
            "entities": ents[:MAX_STORY_ENTITIES]}  # fmt: skip


def risk(row: dict) -> dict:
    return {
        "id": str(row["id"]),
        "entity": {"id": str(row["entity_id"]), "kind": row["entity_kind"], "name": row["entity_name"],
                   "city": row.get("entity_city")},
        "category": row["category"],
        "title": title_text(row["title"]),
        "likelihood": r3(row["likelihood"]),
        "impact": r3(row["impact"]),
        "score": r1(row["score"]),
        "status": row["status"],
        "countries": list(row["countries"]),
        "ports": list(row["ports"]),
        "lat": row["lat"],
        "lon": row["lon"],
        "place": risk_place(row),
        "evidence_count": int(row["evidence_count"] or 0),
        "first_seen_at": ts(row["first_seen_at"]),
        "last_seen_at": ts(row["last_seen_at"]),
        "status_changed_at": ts(row["status_changed_at"]),
        "story": story(row),
        "where": where(row),
        "lead": lead(row),
    }


def evidence_key(row: dict) -> str:
    if row["kind"] == "event":
        return f"event:{row['event_source']}:{row['event_raw_id']}"
    return f"anomaly:{row['anomaly_series']}|{row['anomaly_key']}|{ts(row['anomaly_ts'])}|{row['anomaly_method']}"


def evidence(row: dict) -> dict:
    out: dict[str, Any] = {
        "evidence_key": evidence_key(row),
        "kind": row["kind"],
        "match_type": row["match_type"],
        "match_value": row["match_value"],
        "match_confidence": r3(row["match_confidence"]),
        "signal": r3(row["signal"]),
        "recency": r3(row["recency"]),
        "contribution": r3(row["contribution"]),
        "snapshot": {"title": "", **(row["snapshot"] or {})},
        "still_in_feeds": bool(row.get("still_in_feeds")),
        "last_linked_at": ts(row["last_linked_at"]),
    }
    if row["kind"] == "event":
        out["event"] = {"source": row["event_source"], "raw_id": row["event_raw_id"]}
    else:
        out["anomaly"] = {"series": row["anomaly_series"], "key": row["anomaly_key"], "ts": ts(row["anomaly_ts"]),
                          "method": row["anomaly_method"]}  # fmt: skip
    return out


def history(row: dict) -> dict:
    return {
        "recorded_at": ts(row["recorded_at"]),
        "likelihood": r3(row["likelihood"]),
        "impact": r3(row["impact"]),
        "score": r1(row["score"]),
        "status": row["status"],
        "reason": row["reason"],
    }


def alert(row: dict) -> dict:
    return {
        "id": str(row["id"]),
        "kind": row["kind"],
        "severity": row["severity"],
        "title": title_text(row["title"]),
        "body": row["body"],
        "risk_id": str(row["risk_id"]) if row["risk_id"] else None,
        "connection_id": str(row["connection_id"]) if row["connection_id"] else None,
        "created_at": ts(row["created_at"]),
        "read_at": ts(row["read_at"]),
        "risk_status": row.get("risk_status"),
    }


def scenario_summary(row: dict) -> dict:
    return {
        "id": str(row["id"]),
        "name": row["name"],
        "description": row["description"],
        "status": row["status"],
        "created_at": ts(row["created_at"]),
        "completed_at": ts(row["completed_at"]),
    }


def scenario(row: dict) -> dict:
    return {**scenario_summary(row), "inputs": row["inputs"], "results": row["results"], "error": row["error"]}


# --- feeds ----------------------------------------------------------------------

FEED_EVENT_SELECT = """
SELECT e.*, t.event_type AS tag_event_type, t.type_confidence AS tag_type_confidence,
       t.type_source AS tag_type_source, t.entities AS tag_entities, t.model AS tag_model
FROM feeds.events e
LEFT JOIN LATERAL (
    SELECT * FROM feeds.event_tags g WHERE g.source = e.source AND g.raw_id = e.raw_id
    ORDER BY g.tagged_at DESC LIMIT 1
) t ON true
"""


def feed_event(row: dict) -> dict:
    tag = None
    if row.get("tag_event_type"):
        tag = {
            "event_type": row["tag_event_type"],
            "type_confidence": r3(row["tag_type_confidence"]),
            "type_source": row["tag_type_source"],
            "entities": row["tag_entities"] or {},
            "model": row["tag_model"],
        }
    return {
        "source": row["source"],
        "raw_id": row["raw_id"],
        "type": row["type"],
        "title": row["title"],
        "url": row["url"],
        "published_at": ts(row["published_at"]),
        "countries": list(row["countries"]),
        "ports": list(row["ports"]),
        "commodities": list(row["commodities"]),
        "currencies": list(row["currencies"]),
        "software": list(row["software"]),
        "severity": r3(row["severity"]),
        "lat": row["lat"],
        "lon": row["lon"],
        "attrs": row["attrs"] or {},
        "first_seen_at": ts(row["first_seen_at"]),
        "updated_at": ts(row["updated_at"]),
        "tag": tag,
    }


def feed_event_feature(row: dict) -> dict:
    geom = {"type": "Point", "coordinates": [row["lon"], row["lat"]]} if row["lat"] is not None else None
    return {
        "type": "Feature",
        "id": f"{row['source']}:{row['raw_id']}",
        "geometry": geom,
        "properties": {
            "source": row["source"],
            "raw_id": row["raw_id"],
            "type": row["type"],
            "title": row["title"],
            "url": row["url"],
            "published_at": ts(row["published_at"]),
            "severity": r3(row["severity"]),
            "countries": list(row["countries"]),
            "ports": list(row["ports"]),
            "event_type": row.get("tag_event_type"),
        },
    }


def anomaly(row: dict) -> dict:
    return {
        "series": row["series"],
        "key": row["key"],
        "ts": ts(row["ts"]),
        "method": row["method"],
        "value": row["value"],
        "baseline": row["baseline"],
        "score": row["score"],
        "attrs": row["attrs"] or {},
        "detected_at": ts(row["detected_at"]),
    }


def source_health(row: dict) -> dict:
    return {
        "source": row["source"],
        "status": row["status"],
        "last_run_at": ts(row["last_run_at"]),
        "last_success_at": ts(row["last_success_at"]),
        "last_error": row["last_error"],
        "consecutive_failures": row["consecutive_failures"],
        "records_last_run": row["records_last_run"],
    }
