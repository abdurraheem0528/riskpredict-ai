"""Entities, risks (with evidence) and alerts."""

from __future__ import annotations

from datetime import datetime
from typing import Annotated, Literal
from uuid import UUID

from fastapi import APIRouter, Query, Response
from pydantic import BaseModel, Field

from api import events, pagination
from api.context import WsCtx, audit
from api.core import legacy
from api.core import serialize as ser
from api.core.errors import invalid, not_found

router = APIRouter()

Limit = Annotated[int, Query(ge=1, le=200)]
Cursor = Annotated[str | None, Query(max_length=2000)]
EntityKind = Literal["supplier", "site", "customer", "route", "port", "currency", "commodity", "software"]
Country = Annotated[str | None, Query(pattern=r"^[A-Z]{3}$")]
Locode = Annotated[str | None, Query(pattern=r"^[A-Z]{2}[A-Z2-9]{3}$")]


def _like(q: str) -> str:
    return "%" + q.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"


# --- entities ----------------------------------------------------------------------------


@router.get("/v1/entities", tags=["entities"])
def list_entities(
    ctx: WsCtx,
    limit: Limit = 50,
    cursor: Cursor = None,
    kind: EntityKind | None = None,
    country: Country = None,
    source_id: UUID | None = None,
    q: Annotated[str | None, Query(max_length=100)] = None,
) -> dict:
    after = pagination.decode(cursor, 3)
    with ctx.tx() as conn:
        rows = conn.execute(
            ser.ENTITY_SELECT
            + " WHERE (%(kind)s::text IS NULL OR e.kind = %(kind)s)"
            " AND (%(country)s::text IS NULL OR e.countries @> ARRAY[%(country)s::text])"
            " AND (%(source)s::uuid IS NULL OR e.source_id = %(source)s)"
            " AND (%(q)s::text IS NULL OR e.name ILIKE %(q)s)"
            " AND (%(ak)s::text IS NULL OR (e.kind, e.name, e.id) > (%(ak)s, %(an)s, %(ai)s::uuid))"
            " ORDER BY e.kind, e.name, e.id LIMIT %(n)s",
            {"kind": kind, "country": country, "source": source_id, "q": _like(q) if q else None,
             "ak": after[0] if after else None, "an": after[1] if after else None, "ai": after[2] if after else None,
             "n": limit + 1},
        ).fetchall()  # fmt: skip
    return pagination.page(rows, limit, ["kind", "name", "id"], ser.entity)


@router.get("/v1/entities/{entity_id}", tags=["entities"])
def get_entity(entity_id: UUID, ctx: WsCtx) -> dict:
    with ctx.tx() as conn:
        row = conn.execute(ser.ENTITY_SELECT + " WHERE e.id = %s", (entity_id,)).fetchone()
        if row is None:
            raise not_found("entity not found")
        risks = conn.execute(
            ser.RISK_SELECT + " WHERE r.entity_id = %s AND r.status IN ('open', 'acknowledged', 'mitigating')"
            " ORDER BY r.score DESC, r.id",
            (entity_id,),
        ).fetchall()
    return {**ser.entity(row), "risks": [ser.risk(r) for r in risks]}


# --- risks ---------------------------------------------------------------------------------

SORTS = {"-score": "score", "-last_seen_at": "last_seen_at", "-first_seen_at": "first_seen_at",
         "-likelihood": "likelihood", "-impact": "impact"}  # fmt: skip
CASTS = {"score": "real", "likelihood": "real", "impact": "real", "last_seen_at": "timestamptz", "first_seen_at": "timestamptz"}


def csv_enum(value: str | None, allowed: tuple[str, ...], default: tuple[str, ...], field: str) -> list[str]:
    if not value:
        return list(default)
    items = [v.strip() for v in value.split(",") if v.strip()]
    bad = [v for v in items if v not in allowed]
    if bad:
        raise invalid([{"field": field, "message": f"unknown value {bad[0][:40]!r}"}])
    return items


@router.get("/v1/risks", tags=["risks"])
def list_risks(
    ctx: WsCtx,
    limit: Limit = 50,
    cursor: Cursor = None,
    status: str | None = None,
    category: str | None = None,
    min_score: Annotated[float | None, Query(ge=0, le=100)] = None,
    entity_id: UUID | None = None,
    entity_kind: EntityKind | None = None,
    country: Country = None,
    port: Locode = None,
    since: datetime | None = None,
    sort: Literal["-score", "-last_seen_at", "-first_seen_at", "-likelihood", "-impact"] = "-score",
    group: Literal["story"] | None = None,
) -> dict:
    statuses = csv_enum(status, ser.RISK_STATUSES, ser.OPEN_STATUSES, "status")
    categories = csv_enum(category, ser.CATEGORIES, ser.CATEGORIES, "category")
    col = SORTS[sort]
    after = pagination.decode(cursor, 2)
    where = (
        " WHERE r.status = ANY(%(st)s) AND r.category = ANY(%(cat)s)"
        " AND (%(min)s::real IS NULL OR r.score >= %(min)s)"
        " AND (%(eid)s::uuid IS NULL OR r.entity_id = %(eid)s)"
        " AND (%(ek)s::text IS NULL OR e.kind = %(ek)s)"
        " AND (%(c)s::text IS NULL OR r.countries @> ARRAY[%(c)s::text])"
        " AND (%(p)s::text IS NULL OR r.ports @> ARRAY[%(p)s::text])"
        " AND (%(since)s::timestamptz IS NULL OR r.last_seen_at >= %(since)s)"
    )
    page_sql = (
        f" (%(av)s::text IS NULL OR (x.{col}, x.id) < (%(av)s::{CASTS[col]}, %(ai)s::uuid))"
        f" ORDER BY x.{col} DESC, x.id DESC LIMIT %(n)s"
    )
    if group == "story":
        # One row per story: its highest-scored risk among those that match the filters.
        sql = (
            f"SELECT * FROM (SELECT q.*, row_number() OVER (PARTITION BY q.story_key ORDER BY q.score DESC, q.id DESC)"
            f" AS story_rank FROM (SELECT x.*, {ser.STORY_KEY.replace('r.', 'x.')} AS story_key"
            f" FROM ({ser.RISK_SELECT}{where}) x) q) x WHERE x.story_rank = 1 AND" + page_sql
        )
    else:
        sql = f"SELECT * FROM ({ser.RISK_SELECT}{where}) x WHERE" + page_sql
    with ctx.tx() as conn:
        rows = conn.execute(
            sql,
            {"st": statuses, "cat": categories, "min": min_score, "eid": entity_id, "ek": entity_kind,
             "c": country, "p": port, "since": since, "av": str(after[0]) if after else None,
             "ai": after[1] if after else None, "n": limit + 1},
        ).fetchall()  # fmt: skip
    return pagination.page(rows, limit, [col, "id"], ser.risk)


@router.get("/v1/risks/summary", tags=["risks"])
def risk_summary(ctx: WsCtx) -> dict:
    with ctx.tx() as conn:
        counts = conn.execute("SELECT category, status, count(*) AS n FROM biz.risks GROUP BY category, status").fetchall()
        stories = conn.execute(
            f"SELECT r.category, count(DISTINCT {ser.STORY_KEY}) AS n FROM biz.risks r"
            " WHERE r.status IN ('open', 'acknowledged', 'mitigating') GROUP BY r.category"
        ).fetchall()
        top = conn.execute(
            ser.RISK_SELECT + " WHERE r.status IN ('open', 'acknowledged', 'mitigating') ORDER BY r.score DESC, r.id LIMIT 5"
        ).fetchall()
        agg = conn.execute(
            "SELECT (SELECT max(score) FROM biz.risks WHERE status IN ('open', 'acknowledged', 'mitigating')) AS max_score,"
            " (SELECT max(last_scored_at) FROM biz.risks) AS last_scored_at,"
            " (SELECT max(last_success_at) FROM feeds.source_health) AS feeds_as_of,"
            f" (SELECT count(DISTINCT {ser.STORY_KEY}) FROM biz.risks r"
            "   WHERE r.status IN ('open', 'acknowledged', 'mitigating')) AS story_count"
        ).fetchone()
    by_category = {c: 0 for c in ser.CATEGORIES}
    by_status = {s: 0 for s in ser.RISK_STATUSES}
    for r in counts:
        by_status[r["status"]] += r["n"]
        if r["status"] in ser.OPEN_STATUSES:
            by_category[r["category"]] += r["n"]
    stories_by_category = {c: 0 for c in ser.CATEGORIES}
    for r in stories:
        stories_by_category[r["category"]] = r["n"]
    return {
        "open_count": sum(by_status[s] for s in ser.OPEN_STATUSES),
        "story_count": int(agg["story_count"] or 0),
        "by_category": by_category,
        "stories_by_category": stories_by_category,
        "by_status": by_status,
        "max_score": ser.r1(agg["max_score"]),
        "top": [ser.risk(r) for r in top],
        "last_scored_at": ser.ts(agg["last_scored_at"]),
        "feeds_as_of": ser.ts(agg["feeds_as_of"]),
    }


# True while an evidence row's feed row exists: the event (full sanctions lists: raw_id
# '<dataset>:<entity_id>', §5.8) or the anomaly. The same test as engine.db.STILL_IN_FEEDS, which
# prunes rejected evidence by it (tests/apiservice/test_api_real_engine.py checks the two agree).
STILL_IN_FEEDS = """
CASE WHEN ev.kind = 'event' THEN EXISTS (SELECT 1 FROM feeds.events f
                                         WHERE f.source = ev.event_source AND f.raw_id = ev.event_raw_id)
                                  OR EXISTS (SELECT 1 FROM feeds.sanctions_entries s
                                             WHERE s.source = ev.event_source
                                               AND s.dataset = split_part(ev.event_raw_id, ':', 1)
                                               AND s.entity_id = substr(ev.event_raw_id,
                                                                        strpos(ev.event_raw_id, ':') + 1))
     ELSE EXISTS (SELECT 1 FROM feeds.anomalies a WHERE a.series = ev.anomaly_series AND a.key = ev.anomaly_key
                  AND a.ts = ev.anomaly_ts AND a.method = ev.anomaly_method) END
"""

# The rows linked by the risk's latest scoring come first (they are what it was scored from, and
# what `evidence_count` counts), then by contribution; an older row keeps its old contribution.
EVIDENCE_SELECT = f"""
SELECT ev.*, (ev.last_linked_at >= r.last_scored_at) AS latest_run, {STILL_IN_FEEDS} AS still_in_feeds
FROM biz.risk_evidence ev
JOIN biz.risks r ON r.workspace_id = ev.workspace_id AND r.id = ev.risk_id
WHERE ev.risk_id = %s
ORDER BY (ev.last_linked_at >= r.last_scored_at) DESC, ev.contribution DESC, ev.id
"""


def shown_evidence(rows: list[dict]) -> list[dict]:
    """The rows to show, in order: every row of the latest scoring (the engine already merged
    copies of one news report into one item, 1.3.3), and an older row only when it is not a copy
    (same normalised title or canonical URL) of a row shown before it."""
    from api.core import engine as engine_iface

    seen: set[str] = set()
    out = []
    for e in rows:
        keys: tuple[str, ...] = ()
        if e["kind"] == "event":
            snap = e["snapshot"] or {}
            keys = engine_iface.copy_keys(e["event_source"], snap.get("type"), snap.get("title"), snap.get("url"))
        if e["latest_run"] or seen.isdisjoint(keys):
            out.append(e)
        seen.update(keys)
    return out


def risk_detail(conn, risk_id: UUID) -> dict:
    row = conn.execute(ser.RISK_SELECT + " WHERE r.id = %s", (risk_id,)).fetchone()
    if row is None:
        raise not_found("risk not found")
    evidence = shown_evidence(conn.execute(EVIDENCE_SELECT, (risk_id,)).fetchall())
    history = conn.execute(
        "SELECT * FROM (SELECT * FROM biz.risk_history WHERE risk_id = %s ORDER BY recorded_at DESC, id DESC LIMIT 200) h"
        " ORDER BY recorded_at, id",
        (risk_id,),
    ).fetchall()
    advice = None
    if row["advice"] is not None:
        advice = {"text": row["advice"], "source": row["advice_source"], "model": row["advice_model"],
                  "generated_at": ser.ts(row["advice_generated_at"] or row["last_scored_at"])}  # fmt: skip
    factors = row["factors"] or {}
    explanation = row["explanation"]
    if legacy.written_by_old_engine(row):  # e.g. resolved before engine 1.2.0: raw-formula text
        ent = conn.execute("SELECT kind, name, hs_codes FROM biz.entities WHERE id = %s",
                           (row["entity_id"],)).fetchone()  # fmt: skip
        scored = [e for e in evidence if e["latest_run"]] or evidence  # what it was last scored from
        explanation = legacy.explanation(row, scored, ent) or explanation
    return {
        **ser.risk(row),
        "explanation": explanation,
        "factors": factors,
        "advice": advice,
        "evidence": [ser.evidence(e) for e in evidence],
        "history": [ser.history(h) for h in history],
        "status_note": row["status_note"],
        "engine_version": row["engine_version"],
    }


@router.get("/v1/risks/{risk_id}", tags=["risks"])
def get_risk(risk_id: UUID, ctx: WsCtx) -> dict:
    with ctx.tx() as conn:
        return risk_detail(conn, risk_id)


class RiskPatch(BaseModel):
    status: Literal["open", "acknowledged", "mitigating", "dismissed"]
    note: str | None = Field(default=None, max_length=2000)


@router.patch("/v1/risks/{risk_id}", tags=["risks"])
def update_risk(risk_id: UUID, body: RiskPatch, ctx: WsCtx) -> dict:
    with ctx.tx("editor") as conn:
        cur = conn.execute("SELECT * FROM biz.risks WHERE id = %s FOR UPDATE", (risk_id,)).fetchone()
        if cur is None:
            raise not_found("risk not found")
        changed = cur["status"] != body.status
        conn.execute(
            "UPDATE biz.risks SET status = %s, status_note = %s,"
            " status_changed_at = CASE WHEN %s THEN now() ELSE status_changed_at END,"
            " status_changed_by = CASE WHEN %s THEN %s ELSE status_changed_by END WHERE id = %s",
            (body.status, body.note, changed, changed, ctx.user_id, risk_id),
        )
        if changed:
            conn.execute(
                "INSERT INTO biz.risk_history (workspace_id, risk_id, likelihood, impact, status, reason, changed_by,"
                " engine_version) VALUES (%s, %s, %s, %s, %s, 'status_changed', %s, %s)",
                (ctx.workspace_id, risk_id, cur["likelihood"], cur["impact"], body.status, ctx.user_id, cur["engine_version"]),
            )
            audit(conn, ctx.workspace_id, ctx.user_id, "risk.status_changed", "risk", str(risk_id),
                  {"from": cur["status"], "to": body.status})  # fmt: skip
            if body.status == "dismissed":  # its alerts are dealt with too
                conn.execute("UPDATE biz.alerts SET read_at = now(), read_by = %s"
                             " WHERE risk_id = %s AND read_at IS NULL", (ctx.user_id, risk_id))  # fmt: skip
        out = ser.risk(conn.execute(ser.RISK_SELECT + " WHERE r.id = %s", (risk_id,)).fetchone())
        if changed:
            events.publish(conn, ctx.workspace_id, "risk.updated", {"risk": out, "previous_score": out["score"]})
    return out


# --- alerts ----------------------------------------------------------------------------------

# An alert with its risk's current status (``Alert.risk_status``), so a resolved risk's alert
# can say so; the worker also marks those alerts read (worker/scoring.py).
ALERT_SELECT = (
    "SELECT a.*, r.status AS risk_status FROM biz.alerts a"
    " LEFT JOIN biz.risks r ON r.workspace_id = a.workspace_id AND r.id = a.risk_id"
)


@router.get("/v1/alerts", tags=["alerts"])
def list_alerts(ctx: WsCtx, limit: Limit = 50, cursor: Cursor = None, unread: bool | None = None) -> dict:
    after = pagination.decode(cursor, 2)
    with ctx.tx() as conn:
        rows = conn.execute(
            ALERT_SELECT + " WHERE (%(u)s::boolean IS NULL OR (a.read_at IS NULL) = %(u)s)"
            " AND (%(at)s::text IS NULL OR (a.created_at, a.id) < (%(at)s::timestamptz, %(ai)s::uuid))"
            " ORDER BY a.created_at DESC, a.id DESC LIMIT %(n)s",
            {"u": unread, "at": str(after[0]) if after else None, "ai": after[1] if after else None, "n": limit + 1},
        ).fetchall()
        unread_count = conn.execute("SELECT count(*) AS n FROM biz.alerts WHERE read_at IS NULL").fetchone()["n"]
    return {**pagination.page(rows, limit, ["created_at", "id"], ser.alert), "unread_count": unread_count}


class AlertPatch(BaseModel):
    read: bool


@router.patch("/v1/alerts/{alert_id}", tags=["alerts"])
def update_alert(alert_id: UUID, body: AlertPatch, ctx: WsCtx) -> dict:
    with ctx.tx() as conn:
        row = conn.execute(
            "WITH a AS (UPDATE biz.alerts SET read_at = CASE WHEN %(r)s THEN coalesce(read_at, now()) END,"
            " read_by = CASE WHEN %(r)s THEN coalesce(read_by, %(u)s) END WHERE id = %(id)s RETURNING *)"
            " SELECT a.*, r.status AS risk_status FROM a"
            " LEFT JOIN biz.risks r ON r.workspace_id = a.workspace_id AND r.id = a.risk_id",
            {"r": body.read, "u": ctx.user_id, "id": alert_id},
        ).fetchone()
        if row is None:
            raise not_found("alert not found")
    return ser.alert(row)


@router.post("/v1/alerts/read-all", tags=["alerts"], status_code=204)
def read_all_alerts(ctx: WsCtx) -> Response:
    with ctx.tx() as conn:
        conn.execute("UPDATE biz.alerts SET read_at = now(), read_by = %s WHERE read_at IS NULL", (ctx.user_id,))
    return Response(status_code=204)
