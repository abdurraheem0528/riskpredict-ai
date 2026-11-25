"""The worker loop: one process, no Redis, no long-lived database connection.

    python -m worker

What wakes it:

* **Hints from the API** (long poll on ``GET /internal/work``, no database):
  a webhook arrived, a mapping was confirmed, a sync or a re-score was asked for.
  The worker then does one pass for that workspace.
* **A database pass** every ``WORKER_DB_INTERVAL_SECONDS`` (default 30 min): for
  every workspace (``biz.worker_workspace_ids()``) it processes queued webhooks,
  renews Drive channels whose ``watch_renew_after`` passed, checks pending
  Composio accounts, syncs changed sources, and re-scores when the feeds moved
  on (``max(feeds.source_health.last_run_at)`` advanced). Hints are only an
  optimisation; this pass recovers anything a lost hint missed.
* **The re-read fallback** every ``SOURCE_RECHECK_SECONDS`` (default 15 min):
  each active source is re-read from its tool *without* touching the database;
  only when its content hash changed does a sync (and a re-score) follow.

Between those the pool is empty, so Neon can scale to zero.
"""

from __future__ import annotations

import logging
import signal
import threading
import time
from datetime import datetime
from uuid import UUID

from api.core.config import Settings
from api.core.db import Database
from api.core.providers import CONNECTION_COLUMNS
from worker import scoring, sync
from worker.api_client import ApiClient

log = logging.getLogger("riskpredict.worker")
LONG_POLL = 25.0


class Worker:
    def __init__(self, settings: Settings, db: Database | None = None, api: ApiClient | None = None):
        self.settings = settings
        self.db = db or Database(settings.database_url, max_size=2, max_idle=settings.db_max_idle_seconds,
                                 application_name="riskpredict-worker")  # fmt: skip
        self.api = api or ApiClient(settings)
        self.ctx = sync.Ctx(settings, self.db, self.api)
        self.stop = threading.Event()
        self.feeds_seen: datetime | None = None
        # source id → (workspace id, source row, connection row): for the re-read fallback
        self.cache: dict[UUID, tuple[UUID, dict, dict]] = {}

    # --- units of work -------------------------------------------------------------

    def workspace_pass(self, ws: UUID, reasons: set[str] | frozenset[str] = frozenset(), *, score: bool = False) -> None:
        ctx = self.ctx
        if "deleted" in reasons:
            self._forget(ws)
            return
        try:
            with self.db.workspace_tx(ws) as conn:
                if conn.execute("SELECT 1 FROM biz.workspaces WHERE id = %s", (ws,)).fetchone() is None:
                    self._forget(ws)
                    return
            # Each step is isolated: a Drive or Composio problem must not stop the syncs.
            self._step("webhooks", sync.process_webhooks, ctx, ws)
            conns = sync._connections(ctx, ws)
            self._step("composio status", sync.check_pending_composio, ctx, ws, conns)
            self._step("drive watch", sync.ensure_watches, ctx, ws, conns)
            self._step("housekeeping", sync.housekeeping, ctx, ws)
            changed = False
            for src in sync.due_sources(ctx, ws):
                result = self._step("source sync", sync.sync_source, ctx, ws, src["id"])
                changed |= bool(result and result.changed)
                ctx.flush()
            if changed or score or "engine" in reasons:
                self._step("engine run", scoring.run_workspace, ctx, ws)
            self._refresh_cache(ws)
        except Exception as exc:  # one workspace must not stop the others
            log.warning("workspace pass failed (%s)", type(exc).__name__)
        finally:
            ctx.flush()

    @staticmethod
    def _step(name: str, fn, *args):
        try:
            return fn(*args)
        except Exception as exc:  # logged by type only: messages can carry row values
            log.warning("%s failed (%s)", name, type(exc).__name__)
            return None

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

    def db_pass(self) -> None:
        with self.db.tx() as conn:
            latest = conn.execute("SELECT max(last_run_at) AS t FROM feeds.source_health").fetchone()["t"]
            advanced = [r["source"] for r in conn.execute(
                "SELECT source FROM feeds.source_health WHERE %s::timestamptz IS NULL OR last_run_at > %s",
                (self.feeds_seen, self.feeds_seen)).fetchall()]  # fmt: skip
            workspaces = [r["workspace_id"] for r in conn.execute("SELECT workspace_id FROM biz.worker_workspace_ids()").fetchall()]
        feeds_moved = latest is not None and (self.feeds_seen is None or latest > self.feeds_seen)
        for ws in workspaces:
            if self.stop.is_set():
                return
            self.workspace_pass(ws, score=feeds_moved)
            if feeds_moved and self.feeds_seen is not None:
                self.ctx.outbox.add(ws, "feeds.updated", {"as_of": latest.isoformat().replace("+00:00", "Z"),
                                                          "sources": sorted(advanced)})  # fmt: skip
        self.ctx.flush()
        if latest is not None:
            self.feeds_seen = latest
        log.info("database pass: %d workspaces, feeds %s", len(workspaces), "moved" if feeds_moved else "unchanged")

    def recheck(self) -> None:
        """Re-read every cached source from its tool; sync only those that changed."""
        by_ws: dict[UUID, bool] = {}
        for sid, (ws, src, crow) in list(self.cache.items()):
            if self.stop.is_set():
                return
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
                self.ctx.flush()
            self._refresh_cache(ws)

    # --- the loop -----------------------------------------------------------------------

    def run_forever(self) -> None:
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
            wait = max(1.0, min(LONG_POLL, next_db - time.monotonic(), next_recheck - time.monotonic()))
            hints = self.api.wait_for_work(wait)
            if hints is None:
                self.stop.wait(wait)
                continue
            for ws, reasons in hints.items():
                try:
                    self.workspace_pass(UUID(ws), set(reasons))
                except ValueError:
                    continue
        self.db.close()


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    logging.getLogger("httpx").setLevel(logging.WARNING)  # its INFO lines include URLs
    settings = Settings.from_env()
    if not settings.database_url:
        log.error("DATABASE_URL_APP is not set")
        return 2
    worker = Worker(settings)
    signal.signal(signal.SIGTERM, lambda *_: worker.stop.set())
    signal.signal(signal.SIGINT, lambda *_: worker.stop.set())
    worker.run_forever()
    return 0
