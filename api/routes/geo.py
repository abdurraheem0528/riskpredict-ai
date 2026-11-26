"""GeoJSON map layers and the read-only public feeds."""

from __future__ import annotations

import math
from datetime import datetime, timedelta
from typing import Annotated, Literal
from uuid import UUID

from fastapi import APIRouter, Query
from fastapi.responses import JSONResponse

from api import auth, pagination
from api.context import UserCtx, WsCtx
from api.core import engine as engine_iface
from api.core import serialize as ser
from api.core.errors import bad_request, not_found
from api.routes.data import EntityKind, csv_enum

router = APIRouter()
GEOJSON = "application/geo+json"
BBOX_RE = r"^-?\d+(\.\d+)?(,-?\d+(\.\d+)?){3}$"
Bbox = Annotated[str | None, Query(pattern=BBOX_RE)]
Limit = Annotated[int, Query(ge=1, le=200)]
Cursor = Annotated[str | None, Query(max_length=2000)]
MAX_ENTITY_FEATURES = 5000


def geo(body: dict) -> JSONResponse:
    return JSONResponse(body, media_type=GEOJSON)


def parse_bbox(bbox: str | None) -> tuple[float, float, float, float] | None:
    if not bbox:
        return None
    a, b, c, d = (float(x) for x in bbox.split(","))
    if not (-180 <= a <= 180 and -180 <= c <= 180 and -90 <= b <= d <= 90):
        raise bad_request("bbox must be minLon,minLat,maxLon,maxLat in WGS84")
    return a, b, c, d


def bbox_sql(alias: str = "e") -> str:
    """Positioned rows inside the box (the box may cross the antimeridian);
    rows without a position always pass, and the dashboard joins them by country."""
    return (
        f" AND (%(bx)s::float8 IS NULL OR {alias}.lat IS NULL OR ({alias}.lat BETWEEN %(by)s AND %(bY)s AND"
        f" (CASE WHEN %(bx)s <= %(bX)s THEN {alias}.lon BETWEEN %(bx)s AND %(bX)s"
        f" ELSE {alias}.lon >= %(bx)s OR {alias}.lon <= %(bX)s END)))"
    )


def bbox_params(box) -> dict:
    return dict(zip(("bx", "by", "bX", "bY"), box)) if box else {"bx": None, "by": None, "bX": None, "bY": None}


# --- workspace layers -----------------------------------------------------------------------


@router.get("/v1/map/entities", tags=["map"])
def map_entities(ctx: WsCtx, kind: EntityKind | None = None, source_id: UUID | None = None) -> JSONResponse:
    with ctx.tx() as conn:
        rows = conn.execute(
            ser.ENTITY_SELECT + " WHERE (%(k)s::text IS NULL OR e.kind = %(k)s) AND (%(s)s::uuid IS NULL OR e.source_id = %(s)s)"
            " ORDER BY e.kind, e.name, e.id LIMIT %(n)s",
            {"k": kind, "s": source_id, "n": MAX_ENTITY_FEATURES + 1},
        ).fetchall()
    return geo({
        "type": "FeatureCollection",
        "truncated": len(rows) > MAX_ENTITY_FEATURES,
        "features": [ser.entity_feature(r) for r in rows[:MAX_ENTITY_FEATURES]],
    })  # fmt: skip


@router.get("/v1/map/risks", tags=["map"])
def map_risks(
    ctx: WsCtx,
    status: str | None = None,
    category: str | None = None,
    min_score: Annotated[float, Query(ge=0, le=100)] = 0,
) -> JSONResponse:
    statuses = csv_enum(status, ser.RISK_STATUSES, ser.OPEN_STATUSES, "status")
    categories = csv_enum(category, ser.CATEGORIES, ser.CATEGORIES, "category")
    with ctx.tx() as conn:
        rows = conn.execute(
            f"SELECT r.id, r.entity_id, r.category, r.score, r.countries, r.ports, r.lat, r.lon, e.name AS entity_name,"
            f" {ser.STORY_KEY} AS story"
            " FROM biz.risks r JOIN biz.entities e ON e.workspace_id = r.workspace_id AND e.id = r.entity_id"
            " WHERE r.status = ANY(%s) AND r.category = ANY(%s) AND r.score >= %s ORDER BY r.score DESC, r.id",
            (statuses, categories, min_score),
        ).fetchall()
    places: dict[str, dict] = {}
    stories: dict[str, set[str]] = {}
    for r in rows:
        if r["lat"] is not None:
            key, ptype, pkey, label = f"point:{r['entity_id']}", "point", str(r["entity_id"]), r["entity_name"]
            geom = {"type": "Point", "coordinates": [r["lon"], r["lat"]]}
        elif r["ports"]:
            pkey = r["ports"][0]
            key, ptype, label, geom = f"port:{pkey}", "port", engine_iface.port_name(pkey), None
        elif r["countries"]:
            key, ptype, pkey, label, geom = f"country:{r['countries'][0]}", "country", r["countries"][0], None, None
        else:
            continue
        p = places.setdefault(key, {"type": "Feature", "id": key, "geometry": geom, "properties": {
            "place_type": ptype, "place_key": pkey, "label": label, "risk_count": 0, "story_count": 0,
            "max_score": 0.0, "top_categories": [], "top_risk_ids": []}})  # fmt: skip
        props = p["properties"]
        props["risk_count"] += 1
        props["max_score"] = max(props["max_score"], ser.r1(r["score"]) or 0.0)
        if r["category"] not in props["top_categories"]:
            props["top_categories"].append(r["category"])
        # One story reaching several entities at this place counts once (rows are score-ordered,
        # so its highest-scored risk is the one listed).
        seen = stories.setdefault(key, set())
        if r["story"] not in seen:
            seen.add(r["story"])
            props["story_count"] += 1
            if len(props["top_risk_ids"]) < 5:
                props["top_risk_ids"].append(str(r["id"]))
    return geo({"type": "FeatureCollection", "features": list(places.values())})


# --- public layers ----------------------------------------------------------------------------


@router.get("/v1/map/events", tags=["map"])
def map_events(
    ctx: UserCtx,
    bbox: Bbox = None,
    since: datetime | None = None,
    types: Annotated[str | None, Query(max_length=500)] = None,
    min_severity: Annotated[float | None, Query(ge=0, le=1)] = None,
    limit: Annotated[int, Query(ge=1, le=5000)] = 2000,
) -> JSONResponse:
    box = parse_bbox(bbox)
    since = since or auth.utcnow() - timedelta(days=7)
    prefixes = [t.strip() for t in (types or "").split(",") if t.strip()]
    patterns = [p.replace("%", "").replace("_", "\\_") + ("%" if p.endswith(".") else "") for p in prefixes]
    with ctx.user_tx() as conn:
        rows = conn.execute(
            ser.FEED_EVENT_SELECT
            + " WHERE (e.lat IS NOT NULL OR cardinality(e.countries) > 0)"
            " AND coalesce(e.published_at, e.first_seen_at) >= %(since)s"
            " AND (%(np)s = 0 OR e.type LIKE ANY(%(pat)s))"
            " AND (%(sev)s::real IS NULL OR e.severity >= %(sev)s)" + bbox_sql() +
            " ORDER BY coalesce(e.published_at, e.first_seen_at) DESC, e.source, e.raw_id LIMIT %(n)s",
            {"since": since, "np": len(patterns), "pat": patterns, "sev": min_severity, "n": limit + 1, **bbox_params(box)},
        ).fetchall()  # fmt: skip
    return geo({"type": "FeatureCollection", "truncated": len(rows) > limit,
                "features": [ser.feed_event_feature(r) for r in rows[:limit]]})  # fmt: skip


def _km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp, dl = p2 - p1, math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * 6371.0088 * math.asin(math.sqrt(min(1.0, a)))


def _status(severities: list[float | None]) -> str:
    if not severities:
        return "normal"
    return "disrupted" if any((s or 0) >= 0.66 for s in severities) else "watch"


def _in_box(lat: float, lon: float, box) -> bool:
    if box is None:
        return True
    x, y, X, Y = box
    return y <= lat <= Y and (x <= lon <= X if x <= X else (lon >= x or lon <= X))


@router.get("/v1/map/disruptions", tags=["map"])
def map_disruptions(ctx: UserCtx, bbox: Bbox = None) -> JSONResponse:
    box = parse_bbox(bbox)
    since = auth.utcnow() - timedelta(days=7)
    chokepoints = [c for c in engine_iface.chokepoints() if c.lat is not None and c.lon is not None]
    with ctx.user_tx() as conn:
        ports = conn.execute(
            "SELECT * FROM feeds.events e WHERE e.type = 'port.disruption' AND e.lat IS NOT NULL"
            " AND (coalesce(e.published_at, e.first_seen_at) >= %(since)s"
            "      OR (e.attrs->>'to_date') >= to_char(now() AT TIME ZONE 'UTC', 'YYYY-MM-DD'))"
            " ORDER BY coalesce(e.published_at, e.first_seen_at) DESC LIMIT 2000",
            {"since": since},
        ).fetchall()
        positioned = conn.execute(
            "SELECT source, raw_id, lat, lon, severity, coalesce(published_at, first_seen_at) AS at FROM feeds.events"
            " WHERE lat IS NOT NULL AND (type = 'port.disruption' OR type LIKE 'hazard.%%')"
            " AND coalesce(published_at, first_seen_at) >= %s LIMIT 20000",
            (since,),
        ).fetchall() if chokepoints else []
        tagged = conn.execute(
            "SELECT e.source, e.raw_id, e.severity, t.entities, coalesce(e.published_at, e.first_seen_at) AS at"
            " FROM feeds.event_tags t JOIN feeds.events e ON e.source = t.source AND e.raw_id = t.raw_id"
            " WHERE t.event_type IN ('port_shipping_disruption', 'conflict')"
            " AND coalesce(e.published_at, e.first_seen_at) >= %s LIMIT 20000",
            (since,),
        ).fetchall() if chokepoints else []
    features = []
    for cp in chokepoints:
        if not _in_box(cp.lat, cp.lon, box):
            continue
        aliases = {a.casefold() for a in (cp.name, *cp.aliases)}
        hits = [e for e in positioned if cp.radius_km and _km(cp.lat, cp.lon, e["lat"], e["lon"]) <= cp.radius_km]
        for t in tagged:
            places = (t["entities"] or {}).get("places") or []
            if any(isinstance(p, dict) and str(p.get("text", "")).casefold() in aliases for p in places):
                hits.append({**t, "lat": None, "lon": None})
        features.append({
            "type": "Feature", "id": f"chokepoint:{cp.id}",
            "geometry": {"type": "Point", "coordinates": [cp.lon, cp.lat]},
            "properties": {"kind": "chokepoint", "name": cp.name, "status": _status([h["severity"] for h in hits]),
                           "severity": max((h["severity"] for h in hits if h["severity"] is not None), default=None),
                           "radius_km": cp.radius_km, "ports": [], "countries": [],
                           "event_ids": [f"{h['source']}:{h['raw_id']}" for h in hits][:50],
                           "since": ser.ts(min((h["at"] for h in hits), default=None))},
        })  # fmt: skip
    for e in ports:
        if not _in_box(e["lat"], e["lon"], box):
            continue
        features.append({
            "type": "Feature", "id": f"portwatch:{e['raw_id']}",
            "geometry": {"type": "Point", "coordinates": [e["lon"], e["lat"]]},
            "properties": {"kind": "port_disruption", "name": e["title"] or e["raw_id"],
                           "status": _status([e["severity"]]), "severity": ser.r3(e["severity"]), "radius_km": None,
                           "ports": list(e["ports"]), "countries": list(e["countries"]),
                           "event_ids": [f"{e['source']}:{e['raw_id']}"], "since": ser.ts(e["published_at"])},
        })  # fmt: skip
    return geo({"type": "FeatureCollection", "features": features})


# --- feeds ----------------------------------------------------------------------------------------


@router.get("/v1/feeds/events", tags=["feeds"])
def list_feed_events(
    ctx: UserCtx,
    limit: Limit = 50,
    cursor: Cursor = None,
    source: Annotated[str | None, Query(max_length=60)] = None,
    type: Annotated[str | None, Query(max_length=100)] = None,
    country: Annotated[str | None, Query(pattern=r"^[A-Z]{3}$")] = None,
    port: Annotated[str | None, Query(pattern=r"^[A-Z]{2}[A-Z2-9]{3}$")] = None,
    software: Annotated[str | None, Query(max_length=300)] = None,
    event_type: Literal["conflict", "protest", "sanctions", "trade_policy", "port_shipping_disruption", "natural_hazard",
                        "cyber_incident", "labour_action", "financial_stress", "other"] | None = None,  # fmt: skip
    since: datetime | None = None,
    bbox: Bbox = None,
) -> dict:
    box = parse_bbox(bbox)
    after = pagination.decode(cursor, 3)
    type_pat = None
    if type:
        type_pat = type.replace("%", "").replace("_", "\\_") + ("%" if type.endswith(".") else "")
    with ctx.user_tx() as conn:
        rows = conn.execute(
            "SELECT * FROM (" + ser.FEED_EVENT_SELECT + ") e WHERE true"
            " AND (%(src)s::text IS NULL OR e.source = %(src)s)"
            " AND (%(tp)s::text IS NULL OR e.type LIKE %(tp)s)"
            " AND (%(c)s::text IS NULL OR e.countries @> ARRAY[%(c)s::text])"
            " AND (%(p)s::text IS NULL OR e.ports @> ARRAY[%(p)s::text])"
            " AND (%(sw)s::text IS NULL OR e.software @> ARRAY[%(sw)s::text])"
            " AND (%(et)s::text IS NULL OR e.tag_event_type = %(et)s)"
            " AND (%(since)s::timestamptz IS NULL OR coalesce(e.published_at, e.first_seen_at) >= %(since)s)"
            + bbox_sql() +
            " AND (%(at)s::text IS NULL OR (coalesce(e.published_at, e.first_seen_at), e.source, e.raw_id)"
            "      < (%(at)s::timestamptz, %(as)s, %(ar)s))"
            " ORDER BY coalesce(e.published_at, e.first_seen_at) DESC, e.source DESC, e.raw_id DESC LIMIT %(n)s",
            {"src": source, "tp": type_pat, "c": country, "p": port, "sw": software, "et": event_type, "since": since,
             "at": str(after[0]) if after else None, "as": after[1] if after else None,
             "ar": after[2] if after else None, "n": limit + 1, **bbox_params(box)},
        ).fetchall()  # fmt: skip
    for r in rows:
        r["_at"] = r["published_at"] or r["first_seen_at"]
    return pagination.page(rows, limit, ["_at", "source", "raw_id"], ser.feed_event)


@router.get("/v1/feeds/events/{source}/{raw_id:path}", tags=["feeds"])
def get_feed_event(source: str, raw_id: str, ctx: UserCtx) -> dict:
    with ctx.user_tx() as conn:
        row = conn.execute(ser.FEED_EVENT_SELECT + " WHERE e.source = %s AND e.raw_id = %s", (source, raw_id)).fetchone()
    if row is None:
        raise not_found("event not found (it may have been removed by feed retention)")
    return ser.feed_event(row)


@router.get("/v1/feeds/anomalies", tags=["feeds"])
def list_anomalies(
    ctx: UserCtx,
    limit: Limit = 50,
    cursor: Cursor = None,
    series: Annotated[str | None, Query(max_length=100)] = None,
    key: Annotated[str | None, Query(max_length=200)] = None,
    method: Literal["robust_z", "isolation_forest"] | None = None,
    since: datetime | None = None,
) -> dict:
    after = pagination.decode(cursor, 4)
    sp = None
    if series:
        sp = series.replace("%", "").replace("_", "\\_") + ("%" if series.endswith(".") else "")
    with ctx.user_tx() as conn:
        rows = conn.execute(
            "SELECT * FROM feeds.anomalies a WHERE (%(s)s::text IS NULL OR a.series LIKE %(s)s)"
            " AND (%(k)s::text IS NULL OR a.key = %(k)s) AND (%(m)s::text IS NULL OR a.method = %(m)s)"
            " AND (%(since)s::timestamptz IS NULL OR a.ts >= %(since)s)"
            " AND (%(at)s::text IS NULL OR (a.ts, a.series, a.key, a.method) < (%(at)s::timestamptz, %(as)s, %(ak)s, %(am)s))"
            " ORDER BY a.ts DESC, a.series DESC, a.key DESC, a.method DESC LIMIT %(n)s",
            {"s": sp, "k": key, "m": method, "since": since, "at": str(after[0]) if after else None,
             "as": after[1] if after else None, "ak": after[2] if after else None,
             "am": after[3] if after else None, "n": limit + 1},
        ).fetchall()  # fmt: skip
    return pagination.page(rows, limit, ["ts", "series", "key", "method"], ser.anomaly)


@router.get("/v1/feeds/series", tags=["feeds"])
def get_series(
    ctx: UserCtx,
    series: Annotated[str, Query(max_length=100)],
    key: Annotated[str, Query(max_length=200)],
    since: datetime | None = None,
) -> dict:
    since = since or auth.utcnow() - timedelta(days=120)
    with ctx.user_tx() as conn:
        points = conn.execute(
            "SELECT DISTINCT ON (ts) ts, value, unit FROM feeds.series WHERE series = %s AND key = %s AND ts >= %s"
            " ORDER BY ts, source",
            (series, key, since),
        ).fetchall()
        anomalies = conn.execute(
            "SELECT * FROM feeds.anomalies WHERE series = %s AND key = %s AND ts >= %s ORDER BY ts, method",
            (series, key, since),
        ).fetchall()
    return {
        "series": series,
        "key": key,
        "unit": points[-1]["unit"] if points else None,
        "points": [{"ts": ser.ts(p["ts"]), "value": p["value"]} for p in points],
        "anomalies": [ser.anomaly(a) for a in anomalies],
    }


@router.get("/v1/feeds/health", tags=["feeds"])
def feed_health(ctx: UserCtx) -> dict:
    with ctx.user_tx() as conn:
        sources = conn.execute("SELECT * FROM feeds.source_health ORDER BY source").fetchall()
        analyses = conn.execute(
            "SELECT analysis, status, last_run_at, last_success_at FROM feeds.analysis_status WHERE subject = '*'"
            " ORDER BY analysis"
        ).fetchall()
    return {
        "sources": [ser.source_health(s) for s in sources],
        "analyses": [{"analysis": a["analysis"], "status": a["status"], "last_run_at": ser.ts(a["last_run_at"]),
                      "last_success_at": ser.ts(a["last_success_at"])} for a in analyses],  # fmt: skip
    }
