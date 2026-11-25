"""Sources (a picked sheet tab or table) and their column mapping."""

from __future__ import annotations

import logging
from typing import Annotated, Any
from uuid import UUID

import psycopg
from fastapi import APIRouter, BackgroundTasks, Body, Query, Response
from psycopg.types.json import Jsonb
from pydantic import BaseModel, Field

from api.context import State, WsCtx, audit
from api.core import mapping, providers
from api.core import serialize as ser
from api.core.errors import conflict, invalid, not_found
from api.core.providers import CONNECTION_COLUMNS, ConnectionRef

log = logging.getLogger("riskpredict.sources")
router = APIRouter(tags=["sources"])

SAMPLE_ROWS = 200


class NewSource(BaseModel):
    connection_id: UUID
    external_id: str = Field(min_length=1, max_length=200)
    external_table_id: str = Field(default="", max_length=200)
    header_row: int = Field(default=1, ge=1, le=1000)


class SourcePatch(BaseModel):
    paused: bool | None = None
    header_row: int | None = Field(default=None, ge=1, le=1000)


def _source(conn, source_id: UUID, lock: bool = False) -> dict:
    row = conn.execute(ser.SOURCE_SELECT + " WHERE s.id = %s" + (" FOR UPDATE OF s" if lock else ""), (source_id,)).fetchone()
    if row is None:
        raise not_found("source not found")
    return row


def _connection(conn, connection_id: UUID) -> ConnectionRef:
    row = conn.execute(f"SELECT {CONNECTION_COLUMNS} FROM biz.connections WHERE id = %s", (connection_id,)).fetchone()
    if row is None:
        raise not_found("connection not found")
    return ConnectionRef.from_row(row)


def _require_active(ref: ConnectionRef) -> None:
    if ref.status != "active":
        raise conflict("the connection is not active", code="connection_not_active")


def _confirmed(conn, source_id: UUID) -> dict | None:
    return conn.execute(
        "SELECT * FROM biz.column_mappings WHERE source_id = %s AND status = 'confirmed'", (source_id,)
    ).fetchone()


def _latest_suggestion(conn, source_id: UUID) -> dict | None:
    return conn.execute(
        "SELECT * FROM biz.column_mappings WHERE source_id = %s AND status = 'suggested'"
        " ORDER BY version DESC LIMIT 1",
        (source_id,),
    ).fetchone()


def _next_version(conn, source_id: UUID) -> int:
    return conn.execute(
        "SELECT coalesce(max(version), 0) + 1 AS v FROM biz.column_mappings WHERE source_id = %s", (source_id,)
    ).fetchone()["v"]


def store_suggestion(st: State, workspace_id: UUID, source_id: UUID) -> dict | None:
    """Suggest (Groq, else heuristics), store as a new `suggested` version, announce it.
    Writes no entities."""
    with st.db.workspace_tx(workspace_id) as conn:
        src = conn.execute("SELECT columns FROM biz.sources WHERE id = %s", (source_id,)).fetchone()
    if src is None:
        return None
    spec, model = mapping.suggest(st.settings, src["columns"] or [])
    with st.db.workspace_tx(workspace_id) as conn:
        if conn.execute("SELECT 1 FROM biz.sources WHERE id = %s FOR UPDATE", (source_id,)).fetchone() is None:
            return None
        row = conn.execute(
            "INSERT INTO biz.column_mappings (workspace_id, source_id, version, status, suggestion, suggestion_model,"
            " suggested_at) VALUES (%s, %s, %s, 'suggested', %s, %s, now()) RETURNING *",
            (workspace_id, source_id, _next_version(conn, source_id), Jsonb(spec), model),
        ).fetchone()
        conn.execute(
            "UPDATE biz.sources SET status = 'mapping_suggested', updated_at = now()"
            " WHERE id = %s AND status IN ('new', 'mapping_suggested')",
            (source_id,),
        )
    st.hub.publish(workspace_id, "mapping.suggested",
                   {"source_id": str(source_id), "mapping_id": str(row["id"]), "version": row["version"]})  # fmt: skip
    return row


def _suggest_in_background(st: State, workspace_id: UUID, source_id: UUID) -> None:
    try:
        store_suggestion(st, workspace_id, source_id)
    except Exception as exc:  # never let a background task take the process down
        log.warning("background mapping suggestion failed (%s)", type(exc).__name__)


@router.get("/v1/sources")
def list_sources(ctx: WsCtx, connection_id: Annotated[UUID | None, Query()] = None) -> dict:
    with ctx.tx() as conn:
        rows = conn.execute(
            ser.SOURCE_SELECT + " WHERE (%s::uuid IS NULL OR s.connection_id = %s) ORDER BY s.created_at, s.id",
            (connection_id, connection_id),
        ).fetchall()
    return {"items": [ser.source(r) for r in rows]}


@router.post("/v1/sources", status_code=201)
def create_source(body: NewSource, ctx: WsCtx, tasks: BackgroundTasks) -> dict:
    with ctx.tx("editor") as conn:
        ref = _connection(conn, body.connection_id)
    _require_active(ref)
    kind = "google_sheet" if ref.provider == "google_sheets" else "composio_table"
    if kind == "composio_table" and not body.external_table_id:
        raise invalid([{"field": "/external_table_id", "message": "pick a table"}])
    table = providers.read(ctx.settings, ref, kind, body.external_id, body.external_table_id, body.header_row, SAMPLE_ROWS)
    table_id = body.external_table_id
    try:
        with ctx.tx("editor") as conn:
            row = conn.execute(
                "INSERT INTO biz.sources (workspace_id, connection_id, kind, external_id, external_table_id, name, url,"
                " header_row, columns, created_by) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s) RETURNING id",
                (ctx.workspace_id, ref.id, kind, body.external_id, table_id, table.name, table.url,
                 body.header_row, Jsonb(table.columns), ctx.user_id),
            ).fetchone()  # fmt: skip
            audit(conn, ctx.workspace_id, ctx.user_id, "source.created", "source", str(row["id"]))
            out = _source(conn, row["id"])
    except psycopg.errors.UniqueViolation:
        raise conflict("this sheet or table is already a source") from None
    tasks.add_task(_suggest_in_background, ctx.state, ctx.workspace_id, row["id"])
    return ser.source(out)


@router.get("/v1/sources/{source_id}")
def get_source(source_id: UUID, ctx: WsCtx) -> dict:
    with ctx.tx() as conn:
        return ser.source(_source(conn, source_id))


@router.patch("/v1/sources/{source_id}")
def update_source(source_id: UUID, body: SourcePatch, ctx: WsCtx) -> dict:
    columns = None
    if body.header_row is not None:
        with ctx.tx("editor") as conn:
            src = _source(conn, source_id)
            ref = _connection(conn, src["connection_id"])
        if body.header_row != src["header_row"]:
            _require_active(ref)
            table = providers.read(ctx.settings, ref, src["kind"], src["external_id"], src["external_table_id"],
                                   body.header_row, SAMPLE_ROWS)  # fmt: skip
            columns = table.columns
    with ctx.tx("editor") as conn:
        src = _source(conn, source_id, lock=True)
        status = src["status"]
        if body.paused is True:
            status = "paused"
        elif body.paused is False and status == "paused":
            status = "active" if _confirmed(conn, source_id) else ("mapping_suggested" if _latest_suggestion(conn, source_id) else "new")
        conn.execute(
            "UPDATE biz.sources SET status = %s, header_row = coalesce(%s, header_row),"
            " columns = coalesce(%s, columns), last_change_at = CASE WHEN %s THEN now() ELSE last_change_at END,"
            " updated_at = now() WHERE id = %s",
            (status, body.header_row, Jsonb(columns) if columns is not None else None,
             status == "active" and src["status"] != "active" or columns is not None, source_id),
        )  # fmt: skip
        out = _source(conn, source_id)
    if out["status"] == "active":
        ctx.state.hints.add(ctx.workspace_id, "sync")
    return ser.source(out)


@router.delete("/v1/sources/{source_id}", status_code=204)
def delete_source(source_id: UUID, ctx: WsCtx) -> Response:
    with ctx.tx("editor") as conn:
        _source(conn, source_id)
        conn.execute("DELETE FROM biz.sources WHERE id = %s", (source_id,))
        audit(conn, ctx.workspace_id, ctx.user_id, "source.deleted", "source", str(source_id))
    ctx.state.hints.add(ctx.workspace_id, "engine")
    return Response(status_code=204)


@router.post("/v1/sources/{source_id}/sync", status_code=202)
def sync_source(source_id: UUID, ctx: WsCtx) -> Response:
    with ctx.tx("editor") as conn:
        src = _source(conn, source_id, lock=True)
        if src["status"] == "paused":
            raise conflict("the source is paused")
        if _confirmed(conn, source_id) is None:
            raise conflict("confirm a column mapping first")
        conn.execute(
            "UPDATE biz.sources SET last_change_at = now(), updated_at = now(),"
            " status = CASE WHEN status = 'error' THEN 'active' ELSE status END WHERE id = %s",
            (source_id,),
        )
    ctx.state.hints.add(ctx.workspace_id, "sync")
    return Response(status_code=202)


# --- mapping ---------------------------------------------------------------------------


@router.get("/v1/sources/{source_id}/mapping")
def get_mapping(source_id: UUID, ctx: WsCtx) -> dict:
    with ctx.tx() as conn:
        _source(conn, source_id)
        sug, conf = _latest_suggestion(conn, source_id), _confirmed(conn, source_id)
    return {
        "suggested": ser.column_mapping(sug) if sug else None,
        "confirmed": ser.column_mapping(conf) if conf else None,
        "fields": mapping.field_infos(),
    }


@router.post("/v1/sources/{source_id}/mapping/suggest")
def suggest_mapping(source_id: UUID, ctx: WsCtx) -> dict:
    with ctx.tx("editor") as conn:
        _source(conn, source_id)
    row = store_suggestion(ctx.state, ctx.workspace_id, source_id)
    if row is None:
        raise not_found("source not found")
    return ser.column_mapping(row)


def _validated(spec: Any, src: dict) -> dict:
    clean, errors = mapping.validate_spec(spec, [c["name"] for c in src["columns"] or []])
    if errors:
        raise invalid(errors, "the mapping is not valid")
    return clean


@router.post("/v1/sources/{source_id}/mapping/preview")
def preview_mapping(
    source_id: UUID, ctx: WsCtx, spec: Annotated[dict, Body()], limit: Annotated[int, Query(ge=1, le=200)] = 100
) -> dict:
    with ctx.tx() as conn:
        src = _source(conn, source_id)
        ref = _connection(conn, src["connection_id"])
    clean = _validated(spec, src)
    _require_active(ref)
    table = providers.read(ctx.settings, ref, src["kind"], src["external_id"], src["external_table_id"], src["header_row"], limit)
    result = mapping.apply(clean, table.rows)
    features = []
    for i, e in enumerate(result.entities):
        n = e.row_number or i + 1
        features.append({
            "type": "Feature",
            "id": f"row:{n}",
            "geometry": {"type": "Point", "coordinates": [e.lon, e.lat]} if e.lat is not None else None,
            "properties": {"kind": e.kind, "name": e.name, "countries": e.countries, "ports": e.ports,
                           "criticality": e.criticality, "open_risk_count": 0, "max_score": None, "row_number": e.row_number},
        })  # fmt: skip
    return {
        "rows_read": result.rows_read,
        "entities": {"type": "FeatureCollection", "features": features, "truncated": False},
        "problems": result.problems,
    }


@router.post("/v1/sources/{source_id}/mapping/confirm")
def confirm_mapping(source_id: UUID, ctx: WsCtx, spec: Annotated[dict, Body()]) -> dict:
    with ctx.tx("editor") as conn:
        src = _source(conn, source_id, lock=True)
        clean = _validated(spec, src)
        sug = _latest_suggestion(conn, source_id)
        same = sug is not None and [(c["column"], c["field"]) for c in (sug["suggestion"] or {}).get("columns", [])] == [
            (c["column"], c["field"]) for c in clean["columns"]
        ] and (sug["suggestion"] or {}).get("entity_kind") == clean["entity_kind"]
        conn.execute(
            "UPDATE biz.column_mappings SET status = 'superseded' WHERE source_id = %s AND status = 'confirmed'",
            (source_id,),
        )
        row = conn.execute(
            "INSERT INTO biz.column_mappings (workspace_id, source_id, version, status, suggestion, suggestion_model,"
            " suggested_at, mapping, confirmed_by, confirmed_at)"
            " VALUES (%s, %s, %s, 'confirmed', %s, %s, %s, %s, %s, now()) RETURNING *",
            (ctx.workspace_id, source_id, _next_version(conn, source_id),
             Jsonb(sug["suggestion"]) if same else None, sug["suggestion_model"] if same else None,
             sug["suggested_at"] if same else None, Jsonb(clean), ctx.user_id),
        ).fetchone()  # fmt: skip
        conn.execute(
            "UPDATE biz.sources SET status = CASE WHEN status = 'paused' THEN 'paused' ELSE 'active' END,"
            " status_reason = NULL, last_change_at = now(), updated_at = now() WHERE id = %s",
            (source_id,),
        )
        audit(conn, ctx.workspace_id, ctx.user_id, "mapping.confirmed", "source", str(source_id), {"version": row["version"]})
    ctx.state.hints.add(ctx.workspace_id, "sync")
    return ser.column_mapping(row)

