# api

FastAPI service for business data (serverless on Vercel Hobby, one Python function built
by `infra/vercel/bundle.py`; uvicorn with docker compose: [`infra/README.md`](../infra/README.md)). Implements
[`docs/contracts/openapi.yaml`](../docs/contracts/openapi.yaml) against the `biz` schema
as an `rp_app` login ([`docs/database-roles.md`](../docs/database-roles.md)).

```bash
pip install -r api/requirements.txt
export DATABASE_URL_APP=... JWT_SIGNING_KEY=...    # see .env.example
uvicorn api.main:app --port 8000 --no-access-log
```

## Layout

| Path | What |
|---|---|
| `main.py` | App factory: problem+json errors, CORS for `WEB_ORIGIN`, route-template access log, lazy schema check on an instance's first database use |
| `context.py` | Who is calling and which workspace; the transaction that sets `app.user_id` / `app.workspace_id` |
| `auth.py` | argon2id, the access JWT, refresh tokens |
| `events.py` | Live updates in `biz.events`: write (`publish`, `Outbox`), poll (`GET /v1/events`), 24 h retention; no FastAPI (the worker uses it) |
| `ratelimit.py` | Rate limits shared by every instance, through `biz.rate_hit` (keys HMAC-hashed) |
| `work.py` | Work done inside a request: sync and score after a confirm or "sync now", webhooks for ~20 s under a lease (uses `worker/passes.py`) |
| `routes/` | One module per area of the contract |
| `core/` | Shared with the worker, no FastAPI: config, DB pool, AES-GCM, Google / Composio / Groq clients, column mapping, row readers, serialisers, the engine door |

## How the decisions in `docs/contracts/README.md` are met

* **Session context (3, 5).** Every workspace request opens one transaction, sets
  `app.user_id`, checks the token's session and the membership, and only then sets
  `app.workspace_id`, all with `set_config(…, true)`. Not a member: `404`. The workspace
  comes from `X-Workspace-Id`, or from the path on `/v1/workspaces/{id}…`.
* **Cross-workspace access (4).** Only the six `SECURITY DEFINER` functions: the rate
  limits (`rate_hit`), and the five lookups: sign-in and
  add-member (`auth_user_by_email`), refresh and sign-out (`auth_session_by_refresh_hash`),
  the Drive webhook (`route_drive_channel`), the Composio webhook
  (`route_composio_account`) and the worker loop (`worker_workspace_ids`). The Google
  OAuth callback needs no lookup: the `state` carries the workspace and connection ids,
  and it is checked against `oauth_state_hash` inside that workspace. The PKCE verifier
  is derived from the state with the server key, so nothing else is stored.
* **Auth (6).** argon2id m=19456 KiB, t=2, p=1. Access JWT HS256, 15 minutes; each
  request also checks that its session row is not revoked. Refresh tokens: 32 random
  bytes, sha256 stored, rotated on every use; presenting a rotated one revokes every
  session of that family (`401 refresh_reused`). Rotated rows are kept 7 days for reuse
  detection, then deleted. Sign-in: same answer and timing for a wrong email or password;
  10 failures lock the account for 15 minutes; rate limits per IP and account, shared
  by every instance in the database (the IP comes from `CLIENT_IP_HEADER`, `x-real-ip`
  on Vercel). Live updates need no ticket: `GET /v1/events` uses the bearer token.
* **Google (7).** `drive.file` + PKCE + Picker. Refresh tokens are AES-256-GCM encrypted
  with the first `TOKEN_ENCRYPTION_KEYS` key (the id goes in `refresh_token_key_id`; every
  key decrypts, so keys can be rotated), with the workspace and connection ids as
  associated data. Access tokens are cached in memory only.
* **Composio (7).** Connect link from `POST /connected_accounts/link`; we store only the
  connected-account id; Composio's user id is `ws_<workspace id>`. The webhook signature
  was checked against Composio's current docs; see the `composioTrigger` operation.
* **Webhooks (8).** Route with the lookup, `INSERT … ON CONFLICT DO NOTHING` into
  `webhook_events`, then process it in the same request for about 20 s under the
  workspace's lease (`work.after_webhook`), and answer `200`. A duplicate delivery does no
  work. Unknown channels, wrong tokens and unknown accounts also get `200`.
* **Groq (9).** Only the column-mapping suggestion (headers + ≤ 5 samples per column) and
  the advice wording (in the worker). If Groq is down or out of quota, mapping falls back
  to header heuristics (`model` = `heuristic`) and advice to the engine's template.
* **Serverless, Neon (10).** Nothing in memory is relied on across requests. The pool
  (`min_size=0`, idle connections closed after 60 s, each checked before use) opens on
  first use; the schema check runs on an instance's first connection and answers `503`
  while the expected migration is missing. No LISTEN/NOTIFY, no stream, no long poll:
  events are written to `biz.events` and polled. `/v1/health` and `GET /healthz` read
  nothing, so they never wake Neon. A mapping suggestion is made before
  `POST /v1/sources` answers (no background task outlives a serverless response).
* **Logs (12).** Method, route template, status and time only. Uvicorn's access log is off
  because URLs can carry the OAuth code and state.

## Tests

`tests/apiservice/` (pytest, real Postgres 16, `rp_app` login created by the test):

```bash
pip install -r api/requirements-dev.txt
TEST_DATABASE_URL=postgresql://test:test@localhost:5432/api_test python -m pytest tests/apiservice
```

Google, Composio and Groq are replaced only inside the tests (`api.core.http.transport`);
`test_no_mocks_outside_tests` checks that nothing under `api/` or `worker/` mocks them.
Every test runs against the real `engine/` (there is no engine double):
`test_end_to_end.py` goes from a workspace and its entities, through feed rows (including the
full sanctions list) and the worker's database pass, to risks, evidence, alerts and GeoJSON
read back through the API; `test_api_real_engine.py` covers the worker, scenarios and the
chokepoint layer.
