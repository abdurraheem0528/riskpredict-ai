# worker

Source syncs, Drive watch renewal, risk-engine runs per workspace
([`docs/contracts/risk-engine.md`](../docs/contracts/risk-engine.md)), Groq advice wording,
alerts, and the live-update events (`biz.events`). Connects as an `rp_app` login. No web
server, no Redis, no long-lived database connection.

**Where it runs:** as a **run-once GitHub Actions job**, `.github/workflows/worker.yml`,
right after every `feeds.yml` run (`workflow_run`), so the Neon database is already awake
and the worker adds no wake-up of its own. Its settings come from the Vercel project's
environment (`infra/scripts/deploy.py worker-run`, see
[`infra/README.md`](../infra/README.md#where-the-apis-secrets-live-and-how-the-worker-gets-them)).
With `docker compose` it is its own container running the same passes in a loop.

```bash
pip install -r worker/requirements.txt
python -m worker --once   # one database pass and one re-read, then exit
python -m worker          # the same on a timer (docker compose)
```

It imports `api.core` and `api.events` (shared code, no FastAPI) and, lazily, `engine`. If
the engine is missing from an image, the worker still syncs sources and logs once that
scoring is skipped.

## Who does what (serverless API)

The API has no background loop, so the urgent part runs **inside API requests**
(`api/work.py`, using `worker/passes.py`):

| Trigger | Where | Then |
|---|---|---|
| Mapping confirmed, "sync now", a source resumed or its header row changed | the API request | sync the due sources and score, within `INLINE_BUDGET_SECONDS` (240 s; Vercel stops at 300 s) |
| A source or connection deleted | the API request | score |
| A Google connection became active | the OAuth callback request | open its Drive watch channel |
| A Drive or Composio webhook | the webhook request, after recording it | its pass for about 20 s (`WEBHOOK_BUDGET_SECONDS`) under the workspace's lease in `biz.worker_state`, so a burst of notifications runs once |
| After each feeds run | `python -m worker --once` | everything below, for every workspace |

Whatever a budget cuts short stays in the database (webhook rows still `received`, sources
still due, `biz.worker_state.score_pending`) and the next worker run finishes it.

## A run (`--once`)

1. **The database pass**, for every workspace (`biz.worker_workspace_ids()`), each step in
   its own short transaction with `app.workspace_id` set and isolated from the others:
   * **Webhooks.** `webhook_events` rows with status `received`: Drive → `changes.list`
     from the stored page token, then mark the changed spreadsheets' sources; Composio
     trigger → mark the connection's sources; Composio lifecycle event → re-check the
     account status.
   * **Connections.** Pending Composio accounts are re-checked; Google connections get a
     Drive `changes.watch` channel (needs an HTTPS `PUBLIC_API_URL`), renewed once
     `watch_renew_after` (expiry − 24 h) passes; the old channel is stopped.
   * **Housekeeping.** Handled webhook rows after 30 days, unfinished Google sign-ins after
     a day, and live-update events after 24 hours (the highest id removed is kept in
     `biz.worker_state.events_pruned_through`, so an older cursor gets `resync`).
   * **Sources** with a confirmed mapping and `last_change_at > last_synced_at` are read,
     mapped and normalised (`api/core/mapping.py`, the rules of `jobs/codes.py`). Only rows
     whose hash or mapping changed are written; rows that disappeared are deleted with
     their risks. Events `source.sync_started` / `source.synced` / `source.sync_failed`.
   * **Scoring** when entities changed, when the feeds moved on since this workspace was
     last scored (`max(feeds.source_health.last_run_at)` against
     `biz.worker_state.feeds_seen_at`, kept in the database because every run is a fresh
     process), when a request left `score_pending`, or when an open risk was scored by an
     older `ENGINE_VERSION` (so a new engine cleans up every workspace on the next run):
     `engine.run_workspace` in one
     transaction (it takes the engine's per-workspace lock, so two passes over one workspace
     queue). `RunResult.changes` → alerts (`risk_new`, `risk_escalated`, `sanctions_match`)
     and events `risk.*` / `alert.created`; opted-in watch terms go to `feeds.watch_terms`;
     then `feeds.updated`.
   * **Advice** when `engine.needs_new_advice` says so: `engine.advice_messages` → Groq
     (`GROQ_ADVICE_MODEL`, default `openai/gpt-oss-20b`, low reasoning effort), accepted only
     if ≤ 1,200 characters, without links and without digits that were not in the prompt;
     otherwise `engine.template_advice`. At most `ADVICE_PER_RUN` (20) Groq calls per run;
     the calls follow Groq's rate-limit headers (waiting for the per-minute token reset, and
     stopping near the day's request limit). The reason for a fallback is logged as a short
     code only.
2. **The re-read:** every active source is read again from its tool *without* touching the
   database; only a changed content hash leads to a sync and a re-score (the fallback for
   missed Drive / Composio notifications). Each row's digest includes the mapper's
   `APPLY_RULES_VERSION` (`api/core/mapping.py`), so when a release reads rows differently
   (version 3: a row with a city is placed at the city) the next re-read re-syncs and
   re-scores every source once, with nothing for the owner to do.

The run stops starting new workspaces once `WORKER_ONCE_BUDGET_SECONDS` (480) have passed;
the job's own timeout is 10 minutes. Events are collected during a step and written after
its commit. A failed step is logged by exception type only (public repository), and the
other steps and workspaces carry on.

## Neon compute

The worker job starts within the 5-minute tail the feed jobs leave, so it extends that
tail by about a minute instead of waking the database: roughly 6–9 of the 100 free
CU-hours a month. The full estimate is in
[`infra/README.md`](../infra/README.md#neon-compute-budget). Re-reading sheets and calling
Groq hold no database connection.
