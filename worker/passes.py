"""One pass over one workspace, shared by the run-once worker and the API.

Serverless has no background loop, so the work the worker used to be woken for
runs where it is asked for:

* **in the API request** (``api/work.py``): a confirmed mapping or "sync now"
  syncs and scores within the request; a Drive or Composio webhook is recorded,
  then processed with a short time budget;
* **in the worker** (``python -m worker --once`` after each feeds run): every
  workspace, everything, plus housekeeping.

Every step is isolated (a Drive problem must not stop the syncs) and logged by
exception type only. A :class:`Budget` is checked between steps; whatever is
left (webhook rows still ``received``, sources still due, ``score_pending``)
is durable in the database and picked up by the next worker run.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from uuid import UUID

from worker import scoring, sync

log = logging.getLogger("riskpredict.worker.pass")


class Budget:
    def __init__(self, seconds: float | None) -> None:
        self.end = None if seconds is None else time.monotonic() + float(seconds)

    def left(self) -> float:
        return float("inf") if self.end is None else self.end - time.monotonic()

    @property
    def spent(self) -> bool:
        return self.left() <= 0


@dataclass
class PassResult:
    exists: bool = True
    webhooks: int = 0
    synced: int = 0
    changed: bool = False
    scored: bool = False
    incomplete: bool = False


def _step(name: str, fn, *args):
    """(ok, value); a failure is logged by type only: messages can carry row values."""
    try:
        return True, fn(*args)
    except Exception as exc:
        log.warning("%s failed (%s)", name, type(exc).__name__)
        return False, None


def workspace_pass(
    ctx: sync.Ctx,
    ws: UUID,
    *,
    webhooks: bool = True,
    connections: bool = True,
    sources: bool = True,
    housekeeping: bool = False,
    score: bool = False,
    budget: Budget | None = None,
) -> PassResult:
    budget = budget or Budget(None)
    out = PassResult()
    try:
        with ctx.db.workspace_tx(ws) as conn:
            if conn.execute("SELECT 1 FROM biz.workspaces WHERE id = %s", (ws,)).fetchone() is None:
                out.exists = False
                return out
            state = conn.execute("SELECT score_pending FROM biz.worker_state").fetchone()
        score = score or bool(state and state["score_pending"])
        if webhooks:
            ok, n = _step("webhooks", sync.process_webhooks, ctx, ws)
            out.webhooks = n or 0
            ctx.flush()
        if connections and not budget.spent:
            conns = sync._connections(ctx, ws)
            _step("composio status", sync.check_pending_composio, ctx, ws, conns)
            _step("drive watch", sync.ensure_watches, ctx, ws, conns)
            ctx.flush()
        if housekeeping and not budget.spent:
            _step("housekeeping", sync.housekeeping, ctx, ws)
        if sources:
            for src in sync.due_sources(ctx, ws):
                if budget.spent:
                    out.incomplete = True
                    break
                ok, result = _step("source sync", sync.sync_source, ctx, ws, src["id"])
                out.synced += 1
                out.changed |= bool(result and result.changed)
                ctx.flush()
        if out.changed or score:
            if budget.spent:
                out.incomplete = True
                set_score_pending(ctx, ws, True)
            else:
                ok, _ = _step("engine run", scoring.run_workspace, ctx, ws)
                out.scored = ok
                set_score_pending(ctx, ws, not ok)
    finally:
        ctx.flush()
    return out


# --- biz.worker_state ----------------------------------------------------------------

_UPSERT = (
    "INSERT INTO biz.worker_state (workspace_id, {col}) VALUES (%s, %s)"
    " ON CONFLICT (workspace_id) DO UPDATE SET {col} = EXCLUDED.{col}, updated_at = now()"
)


def set_score_pending(ctx: sync.Ctx, ws: UUID, pending: bool) -> None:
    with ctx.db.workspace_tx(ws) as conn:
        if pending or conn.execute("SELECT 1 FROM biz.worker_state WHERE score_pending").fetchone():
            conn.execute(_UPSERT.format(col="score_pending"), (ws, pending))


def set_feeds_seen(ctx: sync.Ctx, ws: UUID, seen) -> None:
    with ctx.db.workspace_tx(ws) as conn:
        conn.execute(_UPSERT.format(col="feeds_seen_at"), (ws, seen))


def try_lease(db, ws: UUID, seconds: float) -> bool:
    """Take the workspace's short lease unless someone holds it. Not a lock: it only
    keeps a burst of webhooks from running the same pass many times at once."""
    with db.workspace_tx(ws) as conn:
        return conn.execute(
            "INSERT INTO biz.worker_state (workspace_id, lease_until) VALUES (%s, now() + make_interval(secs => %s))"
            " ON CONFLICT (workspace_id) DO UPDATE SET lease_until = EXCLUDED.lease_until, updated_at = now()"
            " WHERE biz.worker_state.lease_until IS NULL OR biz.worker_state.lease_until < now() RETURNING 1",
            (ws, float(seconds)),
        ).fetchone() is not None


def release_lease(db, ws: UUID) -> None:
    with db.workspace_tx(ws) as conn:
        conn.execute("UPDATE biz.worker_state SET lease_until = NULL, updated_at = now() WHERE workspace_id = %s", (ws,))


def pending_webhooks(db, ws: UUID) -> int:
    with db.workspace_tx(ws) as conn:
        return conn.execute("SELECT count(*) AS n FROM biz.webhook_events WHERE status = 'received'").fetchone()["n"]
