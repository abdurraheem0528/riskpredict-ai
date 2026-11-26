# infra

* `migrations/` holds plain SQL migrations, applied in filename order by
  `python -m jobs.migrate` (each file once, recorded in `feeds.schema_migrations`;
  gaps in the numbering are fine). `001`–`003` are the public `feeds` schema;
  `005`–`007` add the rp_* roles and the `biz` schema with row-level security.
  `010` lets `rp_app` delete handled `biz.webhook_events` rows (the worker prunes them
  after 30 days, per workspace under RLS). `050` holds what a serverless API cannot keep
  in memory: `biz.events` (live updates), `biz.worker_state` and the shared rate limits
  (`biz.rate_hit`).
* `scripts/create_login_roles.py` creates the login roles for `rp_feeds` / `rp_app`
  with passwords from env or stdin. See [`docs/database-roles.md`](../docs/database-roles.md).
* `scripts/deploy.py` is what the Deploy, Destroy and worker workflows run (Vercel and
  Cloudflare REST APIs, the pinned Vercel CLI, the app login). It runs in GitHub Actions only.
* `vercel/bundle.py` builds the API's Vercel function in a clean directory (only what the
  API imports) and checks it: size, cold import, an ASGI smoke test.

## Where it runs (all free, no card)

| Part | Where | Free-tier limits that matter |
|---|---|---|
| Feed jobs (`jobs/`) | GitHub Actions, `feeds.yml` every 30 min (started by cron-job.org) | public repository: free minutes |
| Worker (`worker/`) | GitHub Actions, `worker.yml`, once after every feeds run | 10-minute job timeout, 8-minute work budget |
| Database | Neon Postgres | 1 GB storage, 100 CU-hours a month; scales to zero after 5 idle minutes |
| API (`api/` + `engine/`) | **Vercel Hobby** project `riskpredict-api`: one Python function (Fluid compute) | no card; non-commercial use; 300 s per request; 2 GB memory; 500 MB bundle; 4 active-CPU-hours, 360 GB-hours of memory and 1 M invocations a month; one region. Over a limit it **pauses**, it never bills |
| Dashboard (`web/`) | Cloudflare Pages project `riskpredict` | static site, unlimited requests |

**Serverless rules the code follows** (contracts decision 10): each request may land on a
different short-lived instance, so nothing is shared in memory, nothing runs in the
background, and nothing waits on a long poll. The database pool opens on first use and
checks each connection before handing it out; the schema check runs lazily once per
instance. Rate limits, live-update events and worker bookkeeping are in the database.
Work the always-on worker used to be woken for runs **inside the request**: a confirmed
mapping or "sync now" syncs and scores within the request (up to 240 s), and a Drive or
Composio webhook is recorded, then processed for about 20 s. The rest (Drive watch
renewal, pending Composio accounts, scoring after the feeds moved, housekeeping, the
re-read of every source, and whatever a request left over) is the worker job's.

**The function bundle**, measured by CI (`ci-api` → `vercel-bundle`) and by every Deploy:
about **272 MB** uncompressed with its dependencies (largest: scipy 111 MB with its
libraries, scikit-learn 31 MB, numpy 53 MB, pycountry 21 MB), well under Vercel's 500 MB.
A cold `import app` takes about **0.5 s**; the first request that needs the engine adds
about 0.9 s (numpy, scikit-learn). Each run writes its own numbers to the job summary.

## Deploy / Destroy

Both are GitHub Actions workflows you start with one click (*Actions → deploy* or
*destroy → Run workflow*, on `main`). They share the `riskpredict-infra` concurrency group
with db-roles, so they never overlap.

### Secrets (repository *Settings → Secrets and variables → Actions*)

| Secret | Needed by | Notes |
|---|---|---|
| `VERCEL_TOKEN` | deploy, destroy, worker | Vercel → Account Settings → Tokens; scope: your personal account (Hobby). No card is asked for |
| `DATABASE_URL_OWNER` | deploy, db-roles, destroy (`drop_app_role`) | the `neondb_owner` connection string |
| `CLOUDFLARE_API_TOKEN` | deploy, destroy | a token with **Account → Cloudflare Pages → Edit** |
| `CLOUDFLARE_ACCOUNT_ID` | deploy, destroy | Cloudflare dashboard, account home |
| `GROQ_API_KEY` | deploy | Groq free tier |
| `GOOGLE_OAUTH_CLIENT_ID`, `GOOGLE_OAUTH_CLIENT_SECRET`, `GOOGLE_PICKER_API_KEY`, `GOOGLE_CLOUD_PROJECT_NUMBER` | deploy (optional) | skipped while empty; a later Deploy adds them |
| `COMPOSIO_API_KEY`, `COMPOSIO_WEBHOOK_SECRET`, `COMPOSIO_AUTH_CONFIGS`, `COMPOSIO_TRIGGER_SLUGS` | deploy (optional) | same |
| `RP_PASSWORD_RISKPREDICT_JOBS` | db-roles (optional) | see `docs/database-roles.md` |

`RENDER_API_KEY` is no longer used and can be deleted.

Deploy creates its own Vercel project, `riskpredict-api`, deployed only from Actions. A
project you may have imported from GitHub in the Vercel dashboard (e.g. `riskpredict-ai`,
which builds every push as a preview) is not used by anything here; you can delete it or
disconnect its Git repository so it does not use your Hobby build minutes.

Optional repository **variables** (not secrets): `VERCEL_TEAM_ID` (only to use a Vercel
team other than the token's default one; a Hobby account's default team is its own,
"<name>'s projects", and that is what Deploy uses), `GROQ_MAPPING_MODEL`, `GROQ_ADVICE_MODEL`
(Groq model ids; without them column mapping is manual and advice uses the engine's
templates).

### Where the API's secrets live, and how the worker gets them

Only in the Vercel project's environment (production). Deploy writes them there; nothing
is copied by hand. The worker job reads the ones it needs (`DATABASE_URL_APP`,
`TOKEN_ENCRYPTION_KEYS`, `PUBLIC_API_URL`, the Google, Composio and Groq values) from the
Vercel REST API at its start with the same `VERCEL_TOKEN`, prints `::add-mask::` for each
before anything else, and hands them to `python -m worker --once` as its environment, in
memory (no `$GITHUB_ENV`, no file).

**The trade-off:** Vercel's `sensitive` variables cannot be read back by anyone, so the
worker could not get them. Deploy therefore stores every variable as type `encrypted`
(encrypted at rest, readable through the API by a token with access to the project). Anyone
holding `VERCEL_TOKEN` can read them; but such a token can also deploy code that prints
them, so `sensitive` would not protect them from it. Keep `VERCEL_TOKEN` as secret as
`DATABASE_URL_OWNER`.

### What Deploy does (`.github/workflows/deploy.yml`)

Every step is idempotent: running it again updates in place.

1. **Migrations:** `python -m jobs.migrate` as the owner.
2. **Pages project** `riskpredict`: created if missing; its real `*.pages.dev` address is
   read from Cloudflare's answer (a taken name gets a suffix).
3. **Vercel project** `riskpredict-api`: found by name or created (`POST /v11/projects`),
   framework preset FastAPI, no build overrides. **Deployment Protection is switched off
   on every run** (`ssoProtection` and `passwordProtection` null): the dashboard, Google
   Drive and Composio call the API without Vercel credentials. Deploy fails if Vercel
   keeps it on, and the final `/healthz` check fails on a 401/403.
4. **App login:** if `riskpredict_app` does not exist, a password is generated in the job,
   masked at once, and the role is created with
   `create_login_roles.py --dsn-env DATABASE_URL_OWNER --role riskpredict_app:rp_app --password-stdin`.
   Later runs keep the role and the Vercel value and run `--check`. The input
   `rotate_app_password` sets a new password and updates Vercel. **A role that exists while
   the Vercel project has no `DATABASE_URL_APP` gets a new password too**, since the old one
   is not stored anywhere: that is the case on the first Vercel deploy, because the failed
   Render run already created the role.
5. **`DATABASE_URL_APP`** uses the **pooled** Neon host. The rule: take the host of
   `DATABASE_URL_OWNER` and add `-pooler` to its first label (the endpoint id), e.g.
   `ep-cool-dew-123456.us-east-2.aws.neon.tech` → `ep-cool-dew-123456-pooler.us-east-2.aws.neon.tech`
   (a host that already ends in `-pooler` is kept). Same database, `sslmode` and
   `channel_binding` as the owner string. It is stored **only** in the Vercel project.
6. **Env vars** (production, type `encrypted`, `POST …/env?upsert=true` one by one):
   `DATABASE_URL_APP`, `GROQ_API_KEY`, `WEB_ORIGIN`, `CLIENT_IP_HEADER=x-real-ip` (Vercel's
   edge sets it; a client cannot), `PUBLIC_API_URL` and the optional ones that are set.
   `JWT_SIGNING_KEY` and `TOKEN_ENCRYPTION_KEYS` (`k1:<base64 32 bytes>`) are generated
   when missing and **never overwritten**: a new `TOKEN_ENCRYPTION_KEYS` would make the
   stored Google tokens unreadable. (`INTERNAL_TOKEN` is gone with the worker → API
   endpoints.)
7. **Bundle and deploy:** `infra/vercel/bundle.py` builds a clean directory with only what
   the API needs (`api/`, `engine/`, the shared part of `worker/`, three `jobs/` modules,
   under `pkg/`), an `app.py` entrypoint, a `pyproject.toml` with the runtime dependencies
   (Python 3.12) and a `vercel.json` with the function region and `maxDuration: 300`. The
   region is the one closest to the Neon host (`us-east-1` → `iad1`, `us-east-2` → `cle1`,
   `us-west-2` → `pdx1`, `eu-central-1` → `fra1`, `eu-west-2` → `lhr1`, `ap-southeast-1` →
   `sin1`, …; anything else → `iad1`), or the `vercel_region` input. The bundle is
   installed once in the runner to check its size (Deploy stops above 400 MB) and to run
   the ASGI smoke test, then `npx vercel@65.14.0 deploy --prod` uploads it and Vercel
   builds it; Deploy waits until the deployment is `READY`.
8. **`PUBLIC_API_URL`** is the production address (`https://riskpredict-api.vercel.app`,
   or the alias Vercel gave if that name is taken). On the first run it is only known
   after the first deploy, so Deploy sets it and deploys once more. Then
   `GET <api>/healthz` must answer `{"status":"ok"}` without credentials.
9. **Dashboard:** `web/` built with `VITE_API_URL=<api>` and deployed with
   `npx wrangler pages deploy` (branch `main` = production). Then `WEB_ORIGIN` is compared
   with the Pages URL again and the API redeployed only if it changed.
10. **Summary:** the URLs, the bundle size and the one cron-job.org job (no secret values).

### cron-job.org

Keep only the job that dispatches `feeds.yml` every 30 minutes (GitHub's own schedule is
often late); the Deploy summary shows it. The worker needs no cron job: it follows each
feeds run. The API needs **no keep-awake ping**: delete the `/healthz` job made for Render.

### What Destroy does (`.github/workflows/destroy.yml`)

Type `destroy` in the `confirm` box. It deletes the Vercel project (its deployments and
environment) and the Pages project (either may already be gone) and **never touches Neon
data**. `drop_app_role` (default off) also drops the `riskpredict_app` login, never a
table or row. The secrets that lived only in the Vercel project go with it: after a new
Deploy everyone signs in again and Google Sheets connections must be reconnected. The
worker job skips itself until the next Deploy.

## Neon compute budget

Neon's free plan gives 100 CU-hours a month. At this load the compute stays at its 0.25 CU
minimum, so 1 CU-hour = 4 hours awake. It wakes on any query and stays up 5 minutes after
the last one. Estimate per month (30 days):

| Source | Awake time | CU-hours |
|---|---|---|
| `feeds.yml`, 48 runs a day: fetch, prune, analyse, ~6 min awake per run including the 5-min tail (the earlier README measured ~6 min per 15-minute run) | 48 × 6 min = 4.8 h/day | **36** |
| `worker.yml` after each run: starts ~0.5–1 min after the analyse job, inside the 5-min tail, so it never wakes Neon itself; it only moves the tail by its start delay plus its pass (~1–1.5 min) | 48 × 1–1.5 min | **6–9** |
| A visible dashboard tab polls every 15 s, so Neon stays awake while someone looks: e.g. 1 h a day, plus one tail | 30 × 1.1 h | **~8** |
| Requests outside those windows (sign-in, confirm, a Drive or Composio webhook), each ~5.5 min with the tail: e.g. 10 a day | 30 × 55 min | **~7** |
| **Total** | | **~57 (of 100)** |

What it does not do: no 15-minute database pass, no keep-awake ping, no polling while no
dashboard is visible (a hidden tab stops), and `/healthz` and `/v1/health` never query the
database. The numbers that move it most are how long dashboards stay open and how often
sheets change (Drive notifications); a sheet edited all day could add up to ~2 CU-hours a
day. The feeds schedule itself is unchanged.

### Self-hosting

`docker compose --profile app up` runs `api/Dockerfile` (uvicorn) and `worker/Dockerfile`
(`python -m worker`, the same passes in a loop every `WORKER_DB_INTERVAL_SECONDS`). They
share nothing but the database; see the root README and `docker-compose.yml`.
