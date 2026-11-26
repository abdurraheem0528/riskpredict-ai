# infra

* `migrations/` holds plain SQL migrations, applied in filename order by
  `python -m jobs.migrate` (each file once, recorded in `feeds.schema_migrations`;
  gaps in the numbering are fine). `001`–`003` are the public `feeds` schema;
  `005`–`007` add the rp_* roles and the `biz` schema with row-level security.
  `010` lets `rp_app` delete handled `biz.webhook_events` rows (the worker prunes them
  after 30 days, per workspace under RLS).
* `scripts/create_login_roles.py` creates the login roles for `rp_feeds` / `rp_app`
  with passwords from env or stdin. See [`docs/database-roles.md`](../docs/database-roles.md).
* `scripts/deploy.py` is what the Deploy and Destroy workflows run (Render and Cloudflare
  REST APIs, the app login). It runs in GitHub Actions only.
* `render/` is the one-container image for Render: `Dockerfile`, `start.sh` (worker in
  the background, uvicorn exec'd on `$PORT`) and `smoke.py`, the CI check at Render's
  free limits.

## Where it runs (all free, no card)

| Part | Where | Free-tier limits that matter |
|---|---|---|
| Feed jobs (`jobs/`) | GitHub Actions, `feeds.yml` every 30 min | public repository: free minutes |
| Database | Neon Postgres | 1 GB storage, 100 CU-hours a month; scales to zero after 5 idle minutes |
| API **and** worker | **one** Render free web service, `riskpredict-api` | 512 MB, 0.1 CPU, 750 instance-hours a month; sleeps after 15 idle minutes; if the hours run out it is suspended until the next month, never charged |
| Dashboard (`web/`) | Cloudflare Pages project `riskpredict` | static site, unlimited requests |

Render's background workers and cron jobs are paid, so the worker runs **inside the API's
container** (`infra/render/start.sh`). It reaches the API at `http://127.0.0.1:$PORT`, and
`/internal/*` answers only that loopback address (`INTERNAL_LOOPBACK_ONLY=true`), because
the container has one public port. If either process stops, the container exits and Render
restarts it.

Memory, measured by CI (`ci-api` → `render-image`) at `--memory 512m --cpus 0.1`: about
**85 MiB idle** and **105–110 MiB peak** while a what-if run (the engine in the API) and a
worker re-score (the engine in the worker) happen, so well inside 512 MB. Startup takes
about 30 s at 0.1 CPU. Each run writes its own numbers to the job summary.

## Deploy / Destroy

Both are GitHub Actions workflows you start with one click (*Actions → deploy* or
*destroy → Run workflow*, on `main`). They share a `concurrency` group, so they never
overlap.

### Secrets (repository *Settings → Secrets and variables → Actions*)

| Secret | Needed by | Notes |
|---|---|---|
| `DATABASE_URL_OWNER` | deploy, db-roles, destroy (`drop_app_role`) | the `neondb_owner` connection string |
| `RENDER_API_KEY` | deploy, destroy | Render → Account settings → API keys |
| `CLOUDFLARE_API_TOKEN` | deploy, destroy | a token with **Account → Cloudflare Pages → Edit** |
| `CLOUDFLARE_ACCOUNT_ID` | deploy, destroy | Cloudflare dashboard, account home |
| `GROQ_API_KEY` | deploy | Groq free tier |
| `GOOGLE_OAUTH_CLIENT_ID`, `GOOGLE_OAUTH_CLIENT_SECRET`, `GOOGLE_PICKER_API_KEY`, `GOOGLE_CLOUD_PROJECT_NUMBER` | deploy (optional) | skipped while empty; a later Deploy adds them |
| `COMPOSIO_API_KEY`, `COMPOSIO_WEBHOOK_SECRET`, `COMPOSIO_AUTH_CONFIGS`, `COMPOSIO_TRIGGER_SLUGS` | deploy (optional) | same |
| `RP_PASSWORD_RISKPREDICT_JOBS` | db-roles (optional) | see `docs/database-roles.md` |

Optional repository **variables** (not secrets): `RENDER_OWNER_ID` (only if the API key
sees more than one Render workspace), `GROQ_MAPPING_MODEL`, `GROQ_ADVICE_MODEL` (Groq
model ids; without them column mapping is manual and advice uses the engine's templates),
`WORKER_DB_INTERVAL_SECONDS`.

### What Deploy does (`.github/workflows/deploy.yml`)

Every step is idempotent: running it again updates in place.

1. **Migrations:** `python -m jobs.migrate` as the owner.
2. **Pages project** `riskpredict`: created if missing; its real `*.pages.dev` address is
   read from Cloudflare's answer (a taken name gets a suffix).
3. **App login:** if `riskpredict_app` does not exist, a password is generated in the job,
   masked at once, and the role is created with
   `create_login_roles.py --dsn-env DATABASE_URL_OWNER --role riskpredict_app:rp_app --password-stdin`.
   Later runs keep the role and the Render value and run `--check`. The input
   `rotate_app_password` sets a new password and updates Render. A role that exists while
   Render has no `DATABASE_URL_APP` (for example after Destroy) gets a new password too,
   since the old one is not stored anywhere.
4. **`DATABASE_URL_APP`** uses the **pooled** Neon host. The rule: take the host of
   `DATABASE_URL_OWNER` and add `-pooler` to its first label (the endpoint id), e.g.
   `ep-cool-dew-123456.us-east-2.aws.neon.tech` → `ep-cool-dew-123456-pooler.us-east-2.aws.neon.tech`
   (a host that already ends in `-pooler` is kept). Same database, `sslmode` and
   `channel_binding` as the owner string. It is stored **only** as a Render env var.
5. **Render service** `riskpredict-api`: found by name or created (free plan, Docker,
   `infra/render/Dockerfile`, this public repository's `main`, auto-deploy off, health
   check `/healthz`). The region is the one closest to the Neon region in the owner host
   (`us-east-1` → virginia, `us-east-2` → ohio, `us-west-2` → oregon, `eu-*` → frankfurt,
   `ap-*` → singapore; anything else → ohio), or the `render_region` input. A service that
   is not on the free plan is never touched.
6. **Env vars:** `DATABASE_URL_APP`, `GROQ_API_KEY`, `WEB_ORIGIN`, `PUBLIC_API_URL` (the
   real `*.onrender.com` URL), `API_INTERNAL_URL`, `PORT`, `INTERNAL_LOOPBACK_ONLY`,
   `FORWARDED_ALLOW_IPS` and the optional ones that are set. `JWT_SIGNING_KEY`,
   `TOKEN_ENCRYPTION_KEYS` (`k1:<base64 32 bytes>`) and `INTERNAL_TOKEN` are generated on
   first create and **never overwritten**: they live only on Render, and a new
   `TOKEN_ENCRYPTION_KEYS` would make the stored Google tokens unreadable. Each value is set
   one by one (`PUT …/env-vars/{key}`), so nothing else on the service is replaced.
7. **Deploy** of the workflow's commit; it waits until Render reports `live`, then until
   `GET <api>/healthz` answers `{"status":"ok"}`.
8. **Dashboard:** `web/` built with `VITE_API_URL=<api>` and deployed with
   `npx wrangler pages deploy` (branch `main` = production). Then `WEB_ORIGIN` is compared
   with the Pages URL again and the API redeployed only if it changed.
9. **Summary:** the dashboard and API URLs and the two cron-job.org jobs to create (no
   secret values).

`FORWARDED_ALLOW_IPS` lists private ranges (Render's load balancer) and Cloudflare's
published ranges (read at deploy time, with a built-in fallback), never `*`: Render keeps
any `X-Forwarded-For` the client sent and appends to it, so uvicorn must walk it from the
right to find the real client for the rate limits.

### Keeping it awake

Render's free service sleeps after 15 idle minutes, and the worker's own long poll stays
inside the container, so it does not count as traffic. Create a free
[cron-job.org](https://cron-job.org) job that calls `GET <api>/healthz` every 10 minutes:
744 hours a month, under the 750 free hours. `/healthz` touches neither the database nor
any secret, so it does not wake Neon. A second job can dispatch `feeds.yml` every 30
minutes (GitHub's own schedule is often late); the Deploy summary lists both.

### What Destroy does (`.github/workflows/destroy.yml`)

Type `destroy` in the `confirm` box. It deletes the Render service and the Pages project
(either may already be gone) and **never touches Neon data**. `drop_app_role` (default
off) also drops the `riskpredict_app` login, never a table or row. The secrets that lived
only on Render go with the service: after a new Deploy everyone signs in again and Google
Sheets connections must be reconnected.

### Self-hosting

`docker compose` is unchanged: `api/Dockerfile` and `worker/Dockerfile` stay two
containers on a private network (see the root README and `docker-compose.yml`).
