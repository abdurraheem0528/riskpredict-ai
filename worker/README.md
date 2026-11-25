# worker

Background worker (runs on Northflank): source syncs, Drive watch renewal, risk-engine
runs per workspace ([`docs/contracts/risk-engine.md`](../docs/contracts/risk-engine.md)),
Groq advice wording, alerts, and SSE events pushed to the API. Connects as an `rp_app`
login. One process, no Redis, no web server.

```bash
pip install -r worker/requirements.txt
python -m worker
```

It imports `api.core` (shared code, no FastAPI) and, lazily, `engine` (session C). If the
package is missing from an image, the worker still syncs sources and logs once that scoring
is skipped.

## What wakes it

| Trigger | Database? | Then |
|---|---|---|
| Hint from the API (`GET /internal/work`, a 25 s long poll) | no, while waiting | one pass for that workspace |
| Every `WORKER_DB_INTERVAL_SECONDS` (default 1800) | yes | a pass for every workspace (`biz.worker_workspace_ids()`), re-scoring all of them when `max(feeds.source_health.last_run_at)` advanced, plus `feeds.updated` |
| Every `SOURCE_RECHECK_SECONDS` (default 900) | only on change | re-reads each active source from its tool and syncs it only if its content hash changed (the fallback for missed Drive / Composio notifications) |

A workspace pass, each step in its own short transaction with `app.workspace_id` set:

1. **Webhooks.** `webhook_events` rows with status `received`: Drive → `changes.list`
   from the stored page token, then mark the changed spreadsheets' sources; Composio
   trigger → mark the connection's sources; Composio lifecycle event → re-check the
   account status.
2. **Connections.** Pending Composio accounts are re-checked; Google connections get a
   Drive `changes.watch` channel (needs an HTTPS `PUBLIC_API_URL`), renewed once
   `watch_renew_after` (expiry − 24 h) passes; the old channel is stopped.
3. **Sources** with a confirmed mapping and `last_change_at > last_synced_at` are read,
   mapped and normalised (`api/core/mapping.py`, the rules of `jobs/codes.py`). Only rows
   whose hash or mapping changed are written; rows that disappeared are deleted with
   their risks. SSE `source.sync_started` / `source.synced` / `source.sync_failed`.
4. **Scoring** when entities changed, the feeds moved on or the API asked:
   `load_entities → load_feeds → assess → write_risks` in one transaction (the steps of
   `engine.run_workspace`, called one by one to keep the assessments for advice). Then
   `RunResult.changes` → alerts (`risk_new`, `risk_escalated`, `sanctions_match`) and SSE
   `risk.*` / `alert.created`; opted-in watch terms go to `feeds.watch_terms`.
5. **Advice** when `engine.needs_new_advice` says so: `engine.advice_messages` → Groq
   (`GROQ_ADVICE_MODEL`), accepted only if ≤ 1,200 characters, without links and without
   digits that were not in the prompt; otherwise `engine.template_advice`. At most
   `ADVICE_PER_RUN` (20) Groq calls per run.

SSE events are collected during a step and pushed after its commit
(`POST /internal/events`). A failed step is logged by exception type only, and the
other steps and workspaces carry on.

## Neon compute

With the defaults, the worker wakes the database every 30 minutes (close to the feed
jobs' own schedule, so often while it is already awake), plus when there is real work.
Waiting for hints, re-reading sheets and calling Groq hold no database connection, and
the pool closes idle connections after 60 s. `risk-engine.md` §9 suggests checking the
feeds every 10 minutes; that is `WORKER_DB_INTERVAL_SECONDS=600` if the compute budget
allows it.
