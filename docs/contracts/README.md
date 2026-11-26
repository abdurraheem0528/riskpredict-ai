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
   (`openai/gpt-oss-120b` for mapping, `openai/gpt-oss-20b` for advice by default; the
   free plan's models) does two things only: suggest column mappings (the user confirms
   them) and word the advice text. It never produces a number that is stored as a score.
   A commodity, HS-chapter or currency match alone never creates a risk; it only
   strengthens a match on a place, port, chokepoint, company or sanctions name
   (`risk-engine.md` §5.9). One feed item that reaches several entities is one *story*
   (`Risk.story`); the Overview and the map count stories, not copies.
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

## The What-if risk index (`ScenarioSide.portfolio_score`, engine 1.2.0)

```
per entity e:  worst_c(e) = the highest score of e's risks in category c (0 if none)
               E(e)       = 100 × (1 − Π_c (1 − worst_c(e) / 100))
index         = Σ_e w(e) · E(e) / Σ_e w(e)
                w = criticality weight: low 0.25, medium 0.5, high 0.75, critical 1.0, unset 0.5
```

Over every entity on that side (the scenario side after its overrides; an entity no risk
touches counts as 0), rounded to one decimal. Many risks of one kind do not add up (the
worst counts) while different kinds do, so the index does not reach 100 just because
there are many risks; a scenario that adds exposure anywhere raises it; and it is on the
same 0–100 scale as a risk score, so the dashboard's level (Low < 40 ≤ Medium < 60 ≤
High < 80 ≤ Critical) names the index it shows. The old formula,
`100 × (1 − Π(1 − score_i/100))` over every risk, gave 100 for 77 risks whose highest
score was 64. Details and tests: `risk-engine.md` §12, `tests/test_engine_sample.py`.

## Environment variables

See the main [README](../../README.md#how-the-pieces-fit-together) for the table of
which service needs which variable.
