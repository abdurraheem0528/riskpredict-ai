# Contracts

These are the shared rules for the four parallel pieces of work. Each file is the
source of truth for its boundary. If something here is wrong, change the contract in
its own PR first, then the code.

| File | Boundary | Implemented by | Consumed by |
|---|---|---|---|
| [`openapi.yaml`](openapi.yaml) | HTTP API (live updates polled from `GET /v1/events`) + inbound webhooks | B (`api/`) | D (`web/`) |
| [`risk-engine.md`](risk-engine.md) | Python interface, scoring and matching rules | C (`engine/`) | B (worker and API) |
| [`feeds-schema.md`](feeds-schema.md) | the `feeds` tables as they are on `main` | jobs / E (`jobs/`) | B, C, D |
| [`../database-roles.md`](../database-roles.md) | roles, RLS session context, login roles | this PR | B, C, E |
| `infra/migrations/005–007` | the `biz` schema | this PR | B, C |

CI checks the contract on every PR. `tests/test_contracts.py` validates the spec and
compares its enums with the SQL CHECK constraints. The `contracts` job lints it with
Redocly, generates TypeScript types with `openapi-typescript`, and type-checks them.

## Decisions every session follows

1. **No made-up data in the platform.** Migrations seed nothing in `biz`. Empty lists
   render empty states. A demo company lives in its own external Google Sheet or
   Airtable base and is connected like any customer's. Test rows exist only inside
   `tests/`.
2. **Two schemas, three group roles.** `feeds` is public data, written by
   `rp_feeds` (jobs) and read by `rp_app`. `biz` is business data, read and written
   by `rp_app` under forced RLS. Login roles are created with
   `infra/scripts/create_login_roles.py`, never in the Neon Console. `rp_app` never
   owns tables and never has `BYPASSRLS`.
3. **Session context per transaction:** `set_config('app.workspace_id', $1, true)`
   and `set_config('app.user_id', $2, true)`, set after the API has checked
   membership, never with a session-level `SET`. Without them, `biz` is empty.
4. **Cross-workspace access goes only through the six `SECURITY DEFINER` functions**
   (`database-roles.md`: five lookups from 007, and `biz.rate_hit` from 050). Don't add
   a seventh without updating that file and the tests.
5. **The workspace is chosen by the `X-Workspace-Id` header**, except for
   `/v1/workspaces/{id}…`, which uses its path. Not being a member gives 404.
6. **Auth is our own:** argon2id (m=19456 KiB, t=2, p=1); 15-minute HS256 access JWT;
   30-day opaque refresh token, stored as sha256, rotated on every use, with reuse
   detection per family. No cookies, because the API and dashboard are on different
   sites. Live updates are polled with the same bearer token (`GET /v1/events`).
7. **Google Sheets is direct**, with the `drive.file` scope and the Google Picker. The
   refresh token is AES-256-GCM encrypted by the app (`TOKEN_ENCRYPTION_KEYS`,
   versioned by `refresh_token_key_id`). One Drive `changes.watch` channel per
   connection; the worker renews it when `watch_renew_after` passes (expiry − 24 h).
   **Other tools go through Composio**, and we store only its connected-account id.
   Composio's user id is `ws_<workspace id>`.
8. **Webhooks are idempotent:** resolve the workspace with
   `biz.route_drive_channel` / `biz.route_composio_account`, then
   `INSERT … ON CONFLICT DO NOTHING` into `biz.webhook_events`, then process it in the
   same request for about 20 s (`WEBHOOK_BUDGET_SECONDS`) under a per-workspace lease
   (`biz.worker_state.lease_until`), so a burst of notifications runs the pass once.
   What the budget leaves stays `received` for the next worker run.
9. **Scoring is deterministic:** `score = 100 × L × I` is a generated column. Groq
   (Llama) does two things only: suggest column mappings (the user confirms them) and
   word the advice text. It never produces a number that is stored as a score.
10. **Serverless API on Vercel Hobby, Neon free tier.** Each request may run on a
    different short-lived instance: no shared memory, no background loop, no stream,
    no long poll, and startup hooks may not run (the pool opens on first use; the
    schema check runs once per instance, lazily). Anything that must be shared lives
    in the database (migration `050_serverless_state`): live-update events in
    `biz.events` (24 h, pruned by the worker), rate limits through
    `biz.rate_hit()`, worker bookkeeping in `biz.worker_state`. OAuth state was
    already in `biz.connections` (hashed) and the PKCE verifier is derived.
    Live updates are **polled**: `GET /v1/events?after=<id>` about every 15 s, only
    while the dashboard's tab is visible, so nothing touches the database while no
    dashboard is open. Work the worker used to be woken for runs **in the request**
    (mapping confirmed, sync now, re-score: up to `INLINE_BUDGET_SECONDS` = 240 s of
    Vercel's 300 s). The worker is a **run-once GitHub Actions job**
    (`python -m worker --once`) that follows each `feeds` run, while Neon is already
    awake, and adds no wake-up of its own. No long-lived DB connections (no
    LISTEN/NOTIFY, no idle pool): pool `min_size=0`, idle connections closed after
    60 s, each connection checked before use. Never hold a foreign key to `feeds`,
    because feed retention deletes rows.
11. **Codes everywhere:** ISO 3166-1 alpha-3, UN/LOCODE without a space, ISO 4217,
    HS digits only, software as purl or CPE 2.3. Normalise with `jobs/codes.py`; a
    value that cannot be normalised is reported, never guessed.
12. **Public repository:** logs show counts and statuses only, never row contents,
    tokens or connection strings. Problem `detail` texts are safe to show.

## Environment variables

See the main [README](../../README.md#how-the-pieces-fit-together) for the table of
which service needs which variable.
