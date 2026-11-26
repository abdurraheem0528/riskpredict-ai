"""Work the worker used to be woken for, now done inside the request.

On Vercel nothing runs between requests, so:

* a confirmed mapping, "sync now", a changed or resumed source: sync and score
  in the request (``INLINE_BUDGET_SECONDS``, default 240 s; Vercel stops a
  request at 300 s), then the events are written;
* a deleted source or connection: score in the request;
* a Google connection that just became active: its Drive watch channel;
* a Drive or Composio webhook: recorded first, then processed with a short
  budget (``WEBHOOK_BUDGET_SECONDS``, default 20 s) under a per-workspace lease,
  so a burst of notifications runs the pass once.

Anything a budget cuts short stays queued in the database and the next worker
run (``python -m worker --once``, after each feeds run) finishes it. A failure
here never fails the request that asked for it: it is logged by type only.
"""

from __future__ import annotations

import logging
from uuid import UUID

from worker import passes, sync

log = logging.getLogger("riskpredict.work")


def _ctx(st) -> sync.Ctx:
    return sync.Ctx(st.settings, st.db)


def sync_and_score(st, ws: UUID, *, budget: float | None = None) -> passes.PassResult | None:
    try:
        return passes.workspace_pass(_ctx(st), ws, webhooks=False, connections=False, sources=True,
                                     budget=passes.Budget(budget or st.settings.inline_budget_seconds))  # fmt: skip
    except Exception as exc:
        log.warning("inline sync failed (%s)", type(exc).__name__)
        return None


def score(st, ws: UUID) -> passes.PassResult | None:
    try:
        return passes.workspace_pass(_ctx(st), ws, webhooks=False, connections=False, sources=False, score=True,
                                     budget=passes.Budget(st.settings.inline_budget_seconds))  # fmt: skip
    except Exception as exc:
        log.warning("inline scoring failed (%s)", type(exc).__name__)
        try:
            passes.set_score_pending(_ctx(st), ws, True)
        except Exception:
            pass
        return None


def connection_ready(st, ws: UUID) -> None:
    """A Google connection became active: open its Drive watch channel now."""
    try:
        ctx = _ctx(st)
        sync.ensure_watches(ctx, ws)
        ctx.flush()
    except Exception as exc:
        log.warning("inline watch setup failed (%s)", type(exc).__name__)


def after_webhook(st, ws: UUID) -> None:
    """Process the workspace's queued webhooks (and the syncs they cause) for a few seconds."""
    seconds = st.settings.webhook_budget_seconds
    budget = passes.Budget(seconds)
    try:
        if not passes.try_lease(st.db, ws, seconds + 60):
            return  # another request is on it; it looks again before it lets go
        try:
            for _ in range(3):
                passes.workspace_pass(_ctx(st), ws, webhooks=True, connections=False, sources=True, budget=budget)
                if budget.spent or not passes.pending_webhooks(st.db, ws):
                    break
        finally:
            passes.release_lease(st.db, ws)
    except Exception as exc:
        log.warning("inline webhook pass failed (%s)", type(exc).__name__)
