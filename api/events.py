"""Live updates in the database (``biz.events``), polled by the dashboard.

No shared memory, no stream, no long poll (contracts decision 10): whoever
changes something (an API request, an inline webhook pass, the worker) writes
an event row after its own transaction commits, and the dashboard asks
``GET /v1/events?after=<id>`` about every 15 s while its tab is visible. The
names and payloads are the ones the SSE stream used to carry.

Ordering: ids come from one identity sequence, so two transactions can commit
out of id order. ``poll`` returns only rows whose writing transaction is older
than every transaction still running (``pg_snapshot_xmin``), so a client that
has seen id 12 can never miss an id 11 that commits a moment later.

Retention: the worker deletes rows older than ``RETENTION_HOURS`` and records
the highest deleted id per workspace (``biz.worker_state``); a poll from below
that answers ``resync``. This module imports nothing from FastAPI (the worker
uses it too).
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field
from uuid import UUID

import psycopg
from psycopg.types.json import Jsonb

EVENT_NAMES = (
    "risk.created", "risk.updated", "risk.resolved", "alert.created", "source.sync_started", "source.synced",
    "source.sync_failed", "mapping.suggested", "connection.updated", "feeds.updated",
)  # fmt: skip
RETENTION_HOURS = 24
PAGE = 200

_INSERT = "INSERT INTO biz.events (workspace_id, event, data) VALUES (%s, %s, %s)"


def publish(conn: psycopg.Connection, workspace_id: UUID | str, event: str, data: dict) -> None:
    """Write one event in the caller's transaction (``app.workspace_id`` must be set)."""
    if event not in EVENT_NAMES:
        raise ValueError(f"unknown event {event!r}")
    conn.execute(_INSERT, (str(workspace_id), event, Jsonb(data)))


@dataclass
class Outbox:
    """Events collected during one unit of work, written after its commit."""

    events: list[tuple[str, str, dict]] = field(default_factory=list)

    def add(self, workspace_id: UUID | str, event: str, data: dict) -> None:
        if event not in EVENT_NAMES:
            raise ValueError(f"unknown event {event!r}")
        self.events.append((str(workspace_id), event, data))

    def write(self, db) -> int:
        """One short transaction per workspace. Returns how many rows were written."""
        if not self.events:
            return 0
        events, self.events = self.events, []
        by_ws: dict[str, list[tuple[str, str, Jsonb]]] = defaultdict(list)
        for ws, name, data in events:
            by_ws[ws].append((ws, name, Jsonb(data)))
        for ws, rows in by_ws.items():
            with db.workspace_tx(ws) as conn, conn.cursor() as cur:
                cur.executemany(_INSERT, rows)
        return len(events)


def poll(conn: psycopg.Connection, after: int | None, limit: int = PAGE) -> dict:
    """The body of ``GET /v1/events``; ``conn`` is in a transaction with the workspace set."""
    horizon = conn.execute(
        "SELECT pg_snapshot_xmin(pg_current_snapshot()) AS xmin, (SELECT last_value FROM biz.events_id_seq) AS last"
    ).fetchone()
    latest = conn.execute(
        "SELECT coalesce(max(id), 0) AS id FROM biz.events WHERE tx < %s", (horizon["xmin"],)
    ).fetchone()["id"]
    if after is None:  # first poll: start from now, the page itself was just loaded
        return {"items": [], "next": latest, "more": False, "resync": False}
    state = conn.execute("SELECT events_pruned_through FROM biz.worker_state").fetchone()
    pruned = state["events_pruned_through"] if state else 0
    if after < pruned or after > max(horizon["last"] or 0, latest):
        # Older than the retention, or from a database that was reset: refetch everything.
        return {"items": [], "next": latest, "more": False, "resync": True}
    rows = conn.execute(
        "SELECT id, event, data, created_at FROM biz.events WHERE id > %s AND tx < %s ORDER BY id LIMIT %s",
        (after, horizon["xmin"], limit + 1),
    ).fetchall()
    more = len(rows) > limit
    rows = rows[:limit]
    items = [{"id": r["id"], "event": r["event"], "data": r["data"],
              "created_at": r["created_at"].isoformat().replace("+00:00", "Z")} for r in rows]  # fmt: skip
    return {"items": items, "next": rows[-1]["id"] if rows else after, "more": more, "resync": False}


def prune(conn: psycopg.Connection, workspace_id: UUID | str, hours: int = RETENTION_HOURS) -> int:
    """Delete this workspace's events older than ``hours``; remember the highest id deleted."""
    row = conn.execute(
        "WITH gone AS (DELETE FROM biz.events WHERE created_at < now() - make_interval(hours => %s) RETURNING id)"
        " SELECT count(*) AS n, max(id) AS top FROM gone",
        (hours,),
    ).fetchone()
    if row["top"] is not None:
        conn.execute(
            "INSERT INTO biz.worker_state (workspace_id, events_pruned_through) VALUES (%s, %s)"
            " ON CONFLICT (workspace_id) DO UPDATE SET updated_at = now(),"
            " events_pruned_through = greatest(biz.worker_state.events_pruned_through, EXCLUDED.events_pruned_through)",
            (str(workspace_id), row["top"]),
        )
    return row["n"]
