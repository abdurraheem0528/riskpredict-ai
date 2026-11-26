"""Per-workspace sync work: the webhook queue, Drive watch channels, Composio
status, and reading sources into ``biz.entities``.

Every database step is one short transaction with ``app.workspace_id`` set;
provider calls (Google, Composio) happen between transactions, never inside one.
Logs carry ids and counts only.
"""

from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from uuid import UUID

from psycopg.types.json import Jsonb

from api import events
from api.core import composio, crypto, google, mapping, providers
from api.core.config import Settings
from api.core.db import Database
from api.core.errors import Problem, UpstreamAuthError
from api.core.providers import CONNECTION_COLUMNS, ConnectionRef, Table
from api.events import Outbox

log = logging.getLogger("riskpredict.worker.sync")
RENEW_BEFORE = timedelta(hours=24)


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


@dataclass
class Ctx:
    settings: Settings
    db: Database
    outbox: Outbox = field(default_factory=Outbox)

    def flush(self) -> None:
        """Write the collected live-update events (``biz.events``), after the step's commit."""
        try:
            self.outbox.write(self.db)
        except Exception as exc:  # the change itself is committed; the dashboard refetches on resync
            log.warning("event write failed (%s)", type(exc).__name__)


def create_alert(conn, outbox: Outbox, ws: UUID, kind: str, severity: str, title: str, body: str, dedupe_key: str,
                 risk_id: UUID | None = None, connection_id: UUID | None = None) -> None:  # fmt: skip
    from api.core import serialize as ser

    row = conn.execute(
        "INSERT INTO biz.alerts (workspace_id, risk_id, connection_id, kind, severity, title, body, dedupe_key)"
        " VALUES (%s, %s, %s, %s, %s, %s, %s, %s) ON CONFLICT (workspace_id, dedupe_key) DO NOTHING RETURNING *",
        (ws, risk_id, connection_id, kind, severity, title[:300], body[:2000], dedupe_key[:300]),
    ).fetchone()
    if row:
        outbox.add(ws, "alert.created", ser.alert(row))


def _mark_connection(ctx: Ctx, ws: UUID, connection_id: UUID, status: str, reason: str | None) -> None:
    with ctx.db.workspace_tx(ws) as conn:
        prev = conn.execute(
            "UPDATE biz.connections c SET status = %s, status_reason = %s, updated_at = now()"
            " FROM (SELECT id, status FROM biz.connections WHERE id = %s FOR UPDATE) old"
            " WHERE c.id = old.id AND old.status IS DISTINCT FROM %s RETURNING old.status",
            (status, reason, connection_id, status),
        ).fetchone()
        if prev is None:
            return
        if status in ("needs_reauth", "error"):
            create_alert(conn, ctx.outbox, ws, "connection_error", "warning", "A connected tool needs attention",
                         reason or "Reconnect it to keep your data up to date.",
                         f"connection_error:{connection_id}:{utcnow():%Y-%m-%d}", connection_id=connection_id)  # fmt: skip
    ctx.outbox.add(ws, "connection.updated", {"connection_id": str(connection_id), "status": status, "status_reason": reason})


def _connections(ctx: Ctx, ws: UUID) -> list[dict]:
    with ctx.db.workspace_tx(ws) as conn:
        return conn.execute(
            f"SELECT {CONNECTION_COLUMNS}, drive_page_token, watch_channel_id, watch_resource_id, watch_expires_at,"
            " watch_renew_after, watch_failures FROM biz.connections ORDER BY created_at"
        ).fetchall()


# --- webhooks --------------------------------------------------------------------------


def process_webhooks(ctx: Ctx, ws: UUID) -> int:
    """Turn received webhook rows into source changes. Returns how many were handled."""
    with ctx.db.workspace_tx(ws) as conn:
        events = conn.execute(
            "SELECT id, connection_id, provider, event_type, payload FROM biz.webhook_events"
            " WHERE status = 'received' ORDER BY received_at, id LIMIT 500"
        ).fetchall()
    if not events:
        return 0
    conns = {c["id"]: c for c in _connections(ctx, ws)}
    by_conn: dict[UUID | None, list[dict]] = {}
    for e in events:
        by_conn.setdefault(e["connection_id"], []).append(e)
    for conn_id, evs in by_conn.items():
        row = conns.get(conn_id)
        status, error = "processed", None
        try:
            if row is None:
                status = "ignored"
            elif row["provider"] == "google_sheets":
                _drive_changes(ctx, ws, row)
            else:
                lifecycle = [e for e in evs if str((e["payload"] or {}).get("type", "")).startswith("composio.connected_account")]
                if lifecycle and row["composio_connected_account_id"]:
                    new_status, reason = composio.account_status(ctx.settings, row["composio_connected_account_id"])
                    _mark_connection(ctx, ws, row["id"], new_status, reason)
                if len(lifecycle) < len(evs):
                    with ctx.db.workspace_tx(ws) as conn:
                        conn.execute(
                            "UPDATE biz.sources SET last_change_at = now() WHERE connection_id = %s AND status = 'active'",
                            (row["id"],),
                        )
        except UpstreamAuthError as exc:
            status, error = "failed", exc.detail
            _mark_connection(ctx, ws, row["id"], "needs_reauth", exc.detail)
        except Problem as exc:
            status, error = "failed", exc.detail or exc.code
        with ctx.db.workspace_tx(ws) as conn:
            conn.execute(
                "UPDATE biz.webhook_events SET status = %s, error = %s, processed_at = now() WHERE id = ANY(%s)",
                (status, (error or None) and error[:500], [e["id"] for e in evs]),
            )
    return len(events)


def _access(ctx: Ctx, row: dict) -> str:
    tok = providers.google_access_token(ctx.settings, ConnectionRef.from_row(row))
    return tok if isinstance(tok, str) else tok.access_token


def _drive_changes(ctx: Ctx, ws: UUID, row: dict) -> None:
    token = _access(ctx, row)
    page = row["drive_page_token"]
    if not page:
        page = google.start_page_token(token)
        files: set[str] = set()
        mark_all = True  # no cursor yet: re-read every source of this connection
    else:
        files, page = google.list_changes(token, page)
        mark_all = False
    with ctx.db.workspace_tx(ws) as conn:
        conn.execute("UPDATE biz.connections SET drive_page_token = %s WHERE id = %s", (page, row["id"]))
        conn.execute(
            "UPDATE biz.sources SET last_change_at = now() WHERE connection_id = %s AND status = 'active'"
            " AND (%s OR external_id = ANY(%s))",
            (row["id"], mark_all, sorted(files)),
        )


def housekeeping(ctx: Ctx, ws: UUID) -> None:
    """Prune handled webhook rows after 30 days, Google sign-ins never finished after a day,
    and live-update events after 24 hours."""
    with ctx.db.workspace_tx(ws) as conn:
        events.prune(conn, ws)
        conn.execute("DELETE FROM biz.webhook_events WHERE status <> 'received' AND received_at < now() - interval '30 days'")
        conn.execute(
            "DELETE FROM biz.connections WHERE provider = 'google_sheets' AND status = 'pending'"
            " AND created_at < now() - interval '1 day'"
        )


# --- Drive watch channels ---------------------------------------------------------------


def ensure_watches(ctx: Ctx, ws: UUID, conns: list[dict] | None = None) -> None:
    if not ctx.settings.drive_watch_enabled:
        return
    now = utcnow()
    for row in conns if conns is not None else _connections(ctx, ws):
        if row["provider"] != "google_sheets" or row["status"] != "active":
            continue
        if row["watch_channel_id"] and row["watch_renew_after"] and row["watch_renew_after"] > now:
            continue
        try:
            token = _access(ctx, row)
            page = row["drive_page_token"] or google.start_page_token(token)
            channel_id, channel_token = str(uuid.uuid4()), crypto.random_token(32)
            chan = google.watch_changes(token, page, channel_id, channel_token,
                                        f"{ctx.settings.public_api_url}/v1/webhooks/google-drive", now)  # fmt: skip
        except UpstreamAuthError as exc:
            _mark_connection(ctx, ws, row["id"], "needs_reauth", exc.detail)
            continue
        except Problem as exc:
            with ctx.db.workspace_tx(ws) as conn:
                conn.execute("UPDATE biz.connections SET watch_failures = watch_failures + 1 WHERE id = %s", (row["id"],))
            log.info("watch renewal failed for a connection (%s)", exc.code)
            continue
        with ctx.db.workspace_tx(ws) as conn:
            conn.execute(
                "UPDATE biz.connections SET watch_channel_id = %s, watch_resource_id = %s, watch_token_hash = %s,"
                " watch_expires_at = %s, watch_renew_after = %s, watch_last_renewed_at = now(), watch_failures = 0,"
                " drive_page_token = %s, updated_at = now() WHERE id = %s",
                (channel_id, chan.resource_id, crypto.sha256(channel_token), chan.expires_at,
                 chan.expires_at - RENEW_BEFORE, page, row["id"]),
            )  # fmt: skip
        if row["watch_channel_id"]:
            google.stop_channel(token, row["watch_channel_id"], row["watch_resource_id"])


def check_pending_composio(ctx: Ctx, ws: UUID, conns: list[dict]) -> None:
    for row in conns:
        if row["status"] == "pending" and row["provider"].startswith("composio:") and row["composio_connected_account_id"]:
            try:
                status, reason = composio.account_status(ctx.settings, row["composio_connected_account_id"])
            except Problem:
                continue
            if status != "pending":
                _mark_connection(ctx, ws, row["id"], status, reason)
                if status == "active":
                    composio.subscribe_trigger(ctx.settings, row["provider"], row["composio_connected_account_id"], ws)


# --- sources ------------------------------------------------------------------------------

SOURCE_FIELDS = "id, workspace_id, connection_id, kind, external_id, external_table_id, header_row, status, content_hash"


def due_sources(ctx: Ctx, ws: UUID) -> list[dict]:
    with ctx.db.workspace_tx(ws) as conn:
        return conn.execute(
            f"SELECT {SOURCE_FIELDS} FROM biz.sources s WHERE s.status = 'active'"
            " AND EXISTS (SELECT 1 FROM biz.column_mappings m WHERE m.source_id = s.id AND m.status = 'confirmed')"
            " AND (s.last_synced_at IS NULL OR s.last_change_at > s.last_synced_at) ORDER BY s.created_at"
        ).fetchall()


def active_sources(ctx: Ctx, ws: UUID) -> list[dict]:
    with ctx.db.workspace_tx(ws) as conn:
        return conn.execute(
            f"SELECT {SOURCE_FIELDS} FROM biz.sources s WHERE s.status = 'active'"
            " AND EXISTS (SELECT 1 FROM biz.column_mappings m WHERE m.source_id = s.id AND m.status = 'confirmed')"
        ).fetchall()


ENTITY_COLS = ("kind", "name", "countries", "ports", "currencies", "hs_codes", "commodities", "software", "lat", "lon",
               "city", "address", "annual_spend", "spend_currency", "revenue_share", "criticality", "attrs")  # fmt: skip
UPSERT_ENTITY = f"""
INSERT INTO biz.entities (workspace_id, source_id, mapping_id, source_row_key, source_row_number, source_row_hash,
                          {", ".join(ENTITY_COLS)})
VALUES (%s, %s, %s, %s, %s, %s, {", ".join(["%s"] * len(ENTITY_COLS))})
ON CONFLICT (workspace_id, source_id, source_row_key) DO UPDATE SET
    mapping_id = EXCLUDED.mapping_id, source_row_number = EXCLUDED.source_row_number,
    source_row_hash = EXCLUDED.source_row_hash, {", ".join(f"{c} = EXCLUDED.{c}" for c in ENTITY_COLS)},
    updated_at = now()
"""


def _entity_params(ws, source_id, mapping_id, e: mapping.EntityRow) -> tuple:
    return (ws, source_id, mapping_id, e.row_key, e.row_number, e.row_hash, e.kind, e.name, e.countries, e.ports,
            e.currencies, e.hs_codes, e.commodities, e.software, e.lat, e.lon, e.city, e.address, e.annual_spend,
            e.spend_currency, e.revenue_share, e.criticality, Jsonb(e.attrs))  # fmt: skip


@dataclass
class SyncResult:
    source_id: UUID
    ok: bool
    upserted: int = 0
    deleted: int = 0
    problems: int = 0

    @property
    def changed(self) -> bool:
        return self.ok and (self.upserted + self.deleted) > 0


def sync_source(ctx: Ctx, ws: UUID, source_id: UUID, table: Table | None = None) -> SyncResult:
    with ctx.db.workspace_tx(ws) as conn:
        src = conn.execute(f"SELECT {SOURCE_FIELDS} FROM biz.sources WHERE id = %s", (source_id,)).fetchone()
        m = conn.execute("SELECT id, mapping FROM biz.column_mappings WHERE source_id = %s AND status = 'confirmed'",
                         (source_id,)).fetchone()  # fmt: skip
        crow = conn.execute(f"SELECT {CONNECTION_COLUMNS} FROM biz.connections WHERE id = %s",
                            (src["connection_id"],)).fetchone() if src else None  # fmt: skip
    if src is None or m is None or crow is None or src["status"] != "active":
        return SyncResult(source_id, ok=False)
    ref = ConnectionRef.from_row(crow)
    ctx.outbox.add(ws, "source.sync_started", {"source_id": str(source_id), "status": src["status"]})
    try:
        if ref.status != "active":
            raise UpstreamAuthError("the tool", "the connection is not active")
        if table is None:
            table = providers.read(ctx.settings, ref, src["kind"], src["external_id"], src["external_table_id"],
                                   src["header_row"])  # fmt: skip
    except Problem as exc:
        return _sync_failed(ctx, ws, src, ref, exc)
    result = mapping.apply(m["mapping"], table.rows)
    keep = {e.row_key for e in result.entities}
    upserted = deleted = 0
    with ctx.db.workspace_tx(ws) as conn:
        if conn.execute("SELECT 1 FROM biz.sources WHERE id = %s AND status = 'active' FOR UPDATE", (source_id,)).fetchone() is None:
            return SyncResult(source_id, ok=False)
        existing = {r["source_row_key"]: r for r in conn.execute(
            "SELECT source_row_key, source_row_hash, mapping_id, source_row_number FROM biz.entities WHERE source_id = %s",
            (source_id,)).fetchall()}  # fmt: skip
        todo = [e for e in result.entities
                if (old := existing.get(e.row_key)) is None or old["source_row_hash"] != e.row_hash
                or old["mapping_id"] != m["id"] or old["source_row_number"] != e.row_number]  # fmt: skip
        if todo:
            with conn.cursor() as cur:
                cur.executemany(UPSERT_ENTITY, [_entity_params(ws, source_id, m["id"], e) for e in todo])
            upserted = len(todo)
        gone = [k for k in existing if k not in keep]
        if gone:
            deleted = conn.execute("DELETE FROM biz.entities WHERE source_id = %s AND source_row_key = ANY(%s)",
                                   (source_id, gone)).rowcount  # fmt: skip
        reason = f"{result.problem_count} values could not be read" if result.problem_count else None
        conn.execute(
            "UPDATE biz.sources SET row_count = %s, content_hash = %s, columns = %s, last_synced_at = now(),"
            " status_reason = %s, updated_at = now() WHERE id = %s",
            (len(table.rows), table.content_hash(), Jsonb(table.columns), reason, source_id),
        )
    ctx.outbox.add(ws, "source.synced", {"source_id": str(source_id), "status": "active", "entities_upserted": upserted,
                                         "entities_deleted": deleted, "problems": result.problem_count, "error": None})  # fmt: skip
    log.info("source synced: %d rows, %d upserted, %d deleted, %d problems", len(table.rows), upserted, deleted,
             result.problem_count)  # fmt: skip
    return SyncResult(source_id, ok=True, upserted=upserted, deleted=deleted, problems=result.problem_count)


def _sync_failed(ctx: Ctx, ws: UUID, src: dict, ref: ConnectionRef, exc: Problem) -> SyncResult:
    detail = (exc.detail or "the source could not be read")[:500]
    if isinstance(exc, UpstreamAuthError) and ref.status == "active":
        _mark_connection(ctx, ws, ref.id, "needs_reauth", detail)
    transient = exc.status == 503
    with ctx.db.workspace_tx(ws) as conn:
        conn.execute(
            "UPDATE biz.sources SET status = CASE WHEN %(t)s THEN status ELSE 'error' END, status_reason = %(d)s,"
            " last_synced_at = CASE WHEN %(t)s THEN last_synced_at ELSE now() END, updated_at = now() WHERE id = %(id)s",
            {"t": transient, "d": detail, "id": src["id"]},
        )
        create_alert(conn, ctx.outbox, ws, "source_sync_failed", "warning", "A source could not be synced", detail,
                     f"source_sync_failed:{src['id']}:{utcnow():%Y-%m-%d}")  # fmt: skip
    ctx.outbox.add(ws, "source.sync_failed", {"source_id": str(src["id"]), "status": "active" if transient else "error",
                                              "error": detail})  # fmt: skip
    log.info("source sync failed (%s)", exc.code)
    return SyncResult(src["id"], ok=False)


def read_for_recheck(ctx: Ctx, ws: UUID, src: dict, crow: dict) -> Table | None:
    """The periodic re-read: fetch from the tool without touching the database."""
    try:
        return providers.read(ctx.settings, ConnectionRef.from_row(crow), src["kind"], src["external_id"],
                              src["external_table_id"], src["header_row"])  # fmt: skip
    except Problem:
        return None
