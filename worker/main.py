"""The worker: one database pass over every workspace, then the content-hash re-read.

    python -m worker --once   # GitHub Actions (.github/workflows/worker.yml), after each feeds run
    python -m worker          # docker compose: the same work in a loop

No web server, no Redis, no long-lived database connection. With ``--once`` it
runs right after ``feeds.yml`` finishes, while Neon is still awake from the
feed jobs, so it adds no wake-up of its own (contracts decision 10).

**The database pass**, for every workspace (``biz.worker_workspace_ids()``):
queued webhooks, pending Composio accounts, Drive watch renewal, due syncs,
housekeeping (including the 24 h retention of ``biz.events``), and a re-score
when the feeds moved on since the workspace was last scored
(``biz.worker_state.feeds_seen_at`` against ``max(feeds.source_health.last_run_at)``)
or a request left ``score_pending``. A ``feeds.updated`` event follows.

**The re-read**: every active source is read again from its tool *without*
touching the database; only a changed content hash leads to a sync and a
re-score (the fallback for missed Drive / Composio notifications).

The API does the urgent part itself, in the request (``api/work.py``); this
pass picks up whatever a request left for later.
"""

from __future__ import annotations

import argparse
import logging
import signal
import threading
import time
from uuid import UUID

from api.core import engine as engine_iface
from api.core.config import Settings
from api.core.db import Database
from api.core.providers import CONNECTION_COLUMNS
from worker import passes, scoring, sync

log = logging.getLogger("riskpredict.worker")


class Worker:
    def __init__(self, settings: Settings, db: Database | None = None):
        self.settings = settings
        self.db = db or Database(settings.database_url, max_size=2, max_idle=settings.db_max_idle_seconds,
                                 application_name="riskpredict-worker")  # fmt: skip
        self.ctx = sync.Ctx(settings, self.db)
        self.stop = threading.Event()
        # source id → (workspace id, source row, connection row): for the re-read
        self.cache: dict[UUID, tuple[UUID, dict, dict]] = {}

    # --- units of work -------------------------------------------------------------

    def workspace_pass(self, ws: UUID, *, score: bool = False, budget: passes.Budget | None = None) -> passes.PassResult:
        try:
            result = passes.workspace_pass(self.ctx, ws, housekeeping=True, score=score, budget=budget)
            if not result.exists:
                self._forget(ws)
            else:
                self._refresh_cache(ws)
            return result
        except Exception as exc:  # one workspace must not stop the others
            log.warning("workspace pass failed (%s)", type(exc).__name__)
            return passes.PassResult(incomplete=True)

    def _forget(self, ws: UUID) -> None:
        for sid in [k for k, v in self.cache.items() if v[0] == ws]:
            del self.cache[sid]

    def _refresh_cache(self, ws: UUID) -> None:
        sources = sync.active_sources(self.ctx, ws)
        with self.db.workspace_tx(ws) as conn:
            conns = {c["id"]: c for c in conn.execute(f"SELECT {CONNECTION_COLUMNS} FROM biz.connections").fetchall()}
        self._forget(ws)
        for s in sources:
            if (c := conns.get(s["connection_id"])) is not None and c["status"] == "active":
                self.cache[s["id"]] = (ws, s, c)

    def db_pass(self, budget: passes.Budget | None = None) -> dict:
        budget = budget or passes.Budget(None)
        with self.db.tx() as conn:
            health = {r["source"]: r["last_run_at"] for r in conn.execute(
                "SELECT source, last_run_at FROM feeds.source_health WHERE last_run_at IS NOT NULL").fetchall()}  # fmt: skip
            workspaces = [r["workspace_id"] for r in conn.execute("SELECT workspace_id FROM biz.worker_workspace_ids()").fetchall()]
        latest = max(health.values(), default=None)
        eng = engine_iface.load()
        version = getattr(eng, "ENGINE_VERSION", None)
        stats = {"workspaces": len(workspaces), "done": 0, "scored": 0, "skipped": 0}
        for ws in workspaces:
            if self.stop.is_set() or budget.spent:
                stats["skipped"] = len(workspaces) - stats["done"]
                break
            with self.db.workspace_tx(ws) as conn:
                state = conn.execute("SELECT feeds_seen_at FROM biz.worker_state").fetchone()
                # Risks written by an older engine are re-scored on the next run, so a new
                # ENGINE_VERSION cleans the workspace up without waiting for new feed rows.
                stale = version is not None and conn.execute(
                    "SELECT EXISTS (SELECT 1 FROM biz.risks WHERE status IN ('open', 'acknowledged', 'mitigating')"
                    " AND engine_version IS DISTINCT FROM %s) AS stale", (version,)
                ).fetchone()["stale"]  # fmt: skip
            seen = state["feeds_seen_at"] if state else None
            moved = latest is not None and (seen is None or latest > seen)
            result = self.workspace_pass(ws, score=moved or stale, budget=budget)
            stats["done"] += 1
            if not result.exists:
                continue
            stats["scored"] += int(result.scored)
            if moved and result.scored:
                if seen is not None:
                    advanced = sorted(s for s, t in health.items() if t > seen)
                    self.ctx.outbox.add(ws, "feeds.updated", {"as_of": latest.isoformat().replace("+00:00", "Z"),
                                                              "sources": advanced})  # fmt: skip
                    self.ctx.flush()
                passes.set_feeds_seen(self.ctx, ws, latest)
        log.info("database pass: %(workspaces)d workspaces, %(done)d done, %(scored)d scored, %(skipped)d left for the next run",
                 stats)  # fmt: skip
        return stats

    def recheck(self, budget: passes.Budget | None = None) -> int:
        """Re-read every cached source from its tool; sync only those that changed."""
        budget = budget or passes.Budget(None)
        by_ws: dict[UUID, bool] = {}
        for sid, (ws, src, crow) in list(self.cache.items()):
            if self.stop.is_set() or budget.spent:
                break
            table = sync.read_for_recheck(self.ctx, ws, src, crow)
            if table is None or table.content_hash() == src.get("content_hash"):
                continue
            try:
                result = sync.sync_source(self.ctx, ws, sid, table=table)
                by_ws[ws] = by_ws.get(ws, False) or result.changed
            except Exception as exc:
                log.warning("re-read sync failed (%s)", type(exc).__name__)
            self.ctx.flush()
        for ws, changed in by_ws.items():
            if changed:
                try:
                    scoring.run_workspace(self.ctx, ws)
                except Exception as exc:
                    log.warning("engine run failed (%s)", type(exc).__name__)
                    passes.set_score_pending(self.ctx, ws, True)
                self.ctx.flush()
            self._refresh_cache(ws)
        log.info("re-read: %d sources, %d changed", len(self.cache), len(by_ws))
        return len(by_ws)

    # --- entry points -------------------------------------------------------------------

    def run_once(self) -> int:
        """One database pass and one re-read, within WORKER_ONCE_BUDGET_SECONDS; then exit."""
        budget = passes.Budget(self.settings.worker_once_budget_seconds)
        self.db.open()
        log.info("worker --once; integrations: %s", self.settings.describe())
        try:
            self.db_pass(budget)
            self.recheck(budget)
        finally:
            self.db.close()
        return 0

    def run_forever(self) -> None:
        """docker compose: the same work on a timer (the API does the urgent part itself)."""
        self.db.open()
        log.info("worker started; integrations: %s", self.settings.describe())
        next_db = 0.0
        next_recheck = time.monotonic() + self.settings.source_recheck_seconds
        while not self.stop.is_set():
            now = time.monotonic()
            if now >= next_db:
                try:
                    self.db_pass()
                except Exception as exc:
                    log.warning("database pass failed (%s)", type(exc).__name__)
                next_db = time.monotonic() + self.settings.worker_db_interval_seconds
            if now >= next_recheck:
                self.recheck()
                next_recheck = time.monotonic() + self.settings.source_recheck_seconds
            self.stop.wait(max(1.0, min(next_db, next_recheck) - time.monotonic()))
        self.db.close()


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m worker", description="RiskPredict worker")
    ap.add_argument("--once", action="store_true", help="one database pass and one re-read, then exit")
    args = ap.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    logging.getLogger("httpx").setLevel(logging.WARNING)  # its INFO lines include URLs
    try:
        settings = Settings.from_env()
    except Exception as exc:  # ConfigError names the variable, never the value
        log.error("configuration: %s", exc)
        return 2
    if not settings.database_url:
        log.error("DATABASE_URL_APP is not set")
        return 2
    worker = Worker(settings)
    signal.signal(signal.SIGTERM, lambda *_: worker.stop.set())
    signal.signal(signal.SIGINT, lambda *_: worker.stop.set())
    if args.once:
        try:
            return worker.run_once()
        except Exception as exc:  # public repository: the type only
            log.error("worker --once failed (%s)", type(exc).__name__)
            return 1
    worker.run_forever()
    return 0
