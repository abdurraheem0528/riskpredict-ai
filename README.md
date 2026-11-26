# RiskPredict AI

Free, map-first AI platform that predicts supply-chain, market and cyber risk for a business.

It matches global risk feeds (news, markets, ports, hazards, cyber, sanctions) to a
business's own connected tools and shows the result on a map-first dashboard.

## Architecture

```
 public feeds ──► GitHub Actions (jobs/, every 30 min) ──► Neon Postgres: feeds schema (public data)
                      │ then, while Neon is awake                    │  read-only
                      ▼                                              │
                  GitHub Actions: worker --once ───────────────────► Neon Postgres: biz schema (row-level security,
                                                                     ▲   live-update events, rate limits)
 business tools ─► Vercel Hobby (serverless): api + engine/ ────────┘
 (Google Sheets direct,        ▲  polled: GET /v1/events every 15 s while the tab is visible
  others via Composio)         │
                     Cloudflare Pages: web (React + MapLibre dashboard)
```

| Part | Where it runs | Status |
|---|---|---|
| `jobs/` – fetch and normalise public feeds | GitHub Actions (`.github/workflows/feeds.yml`) | **step 1** |
| `jobs/analyse/` – news tagging (GLiNER2) and spike detection | GitHub Actions, after each fetch | **step 2** |
| `infra/migrations/` – SQL for the `feeds` and `biz` schemas, roles | applied by `python -m jobs.migrate` | **steps 1–3** |
| `docs/contracts/` – API, engine and feed contracts | – | **step 3** |
| `api/` – FastAPI for business data | Vercel Hobby, one Python function (`infra/vercel/`) | **session B** ([`api/README.md`](api/README.md)) |
| `worker/` – sync, watch renewal, scoring runs, advice | GitHub Actions, run once after each feeds run (`.github/workflows/worker.yml`); urgent work runs inside API requests | **session B** ([`worker/README.md`](worker/README.md)) |
| `engine/` – risk engine library | imported by worker and api | **step 3** |
| `web/` – dashboard | Cloudflare Pages | **session D** ([`web/README.md`](web/README.md)) |

Ground rules: everything uses free tiers and free, open-source code; the platform never
contains made-up data (demo data lives separately); nothing runs on the owner's computer;
everything is portable (`docker-compose.yml`) and removable.

## Deploy / Destroy

Two one-click GitHub Actions workflows, run on `main` (*Actions → deploy* or *destroy →
Run workflow*); nothing runs on your computer and nothing is paid.

* **deploy** applies the migrations, creates or updates the Cloudflare Pages project
  `riskpredict` and the Vercel Hobby project `riskpredict-api` (no card; limits pause,
  never bill), creates the `riskpredict_app` login once, deploys a clean bundle of the API
  (`infra/vercel/bundle.py`), checks `/healthz`, builds and publishes the dashboard, and
  prints both URLs. Re-running it updates in place; generated secrets stay in the Vercel
  project's environment and are never overwritten.
* **worker** runs by itself after every feeds run (no schedule of its own).
* **destroy** (type `destroy` to confirm) deletes the Vercel project and the Pages
  project. It never touches the Neon data; dropping the app login is an option.
* **db-roles** (optional) creates the `riskpredict_jobs` login for the feed jobs.

Secrets to add first, the order of the first run, and what each step does:
[`infra/README.md`](infra/README.md#deploy--destroy). The only cron-job.org job is the one
that starts `feeds.yml` every 30 minutes; no keep-awake ping is needed.

## How the pieces fit together

1. **Feeds.** Every 30 minutes the jobs (`rp_feeds`) write public events, series,
   tags and anomalies to `feeds` ([`feeds-schema.md`](docs/contracts/feeds-schema.md)).
2. **Connecting a tool.** A user signs up with the API (email + argon2id, no
   third-party auth) and connects a tool. Google Sheets is direct (OAuth `drive.file`
   + Google Picker); Airtable and others go through Composio. Picking a sheet or table
   creates a *source*.
3. **Column mapping.** Groq suggests how the source's columns map to entity fields.
   The user confirms on the **Confirm** screen, with a map preview. The API then
   syncs rows into `biz.entities` within that request, normalised to ISO country, UN/LOCODE, ISO 4217,
   HS code and purl/CPE keys. Drive push notifications and Composio triggers keep
   that up to date.
4. **Scoring.** After each source sync (in the request) and after each feed update (the
   worker job that follows `feeds.yml`), the risk engine runs per workspace ([`risk-engine.md`](docs/contracts/risk-engine.md)). It matches
   entities to feed events and anomalies and writes `biz.risks` with
   `score = 100 × likelihood × impact`. Groq only words the advice.
5. **Dashboard.** The dashboard reads everything through the API
   ([`openapi.yaml`](docs/contracts/openapi.yaml)): GeoJSON map layers, risks with
   their evidence, scenarios and alerts. Live updates are rows in `biz.events`, which the
   dashboard polls (`GET /v1/events`) about every 15 s, only while its tab is visible.
6. **Isolation.** Every `biz` row is isolated by forced row-level security keyed on
   `app.workspace_id`. Roles and login setup: [`docs/database-roles.md`](docs/database-roles.md).
   The decisions every part follows: [`docs/contracts/README.md`](docs/contracts/README.md).

### Environment variables per service

Secrets go in GitHub repository secrets (the Deploy workflow copies them to Vercel) or
are generated by Deploy and kept only in the Vercel project's environment, never in the
repository. The worker job reads its values from there at start. "public" marks values that are safe to
expose to the browser.

| Variable | jobs | api | worker | web | What it is |
|---|:-:|:-:|:-:|:-:|---|
| `DATABASE_URL_MIGRATE` | ✓ (migrate step) | | | | Owner connection string; runs `python -m jobs.migrate` and `infra/scripts/create_login_roles.py` |
| `DATABASE_URL_FEEDS` | ✓ | | | | Login role in `rp_feeds` (today still the owner; see `docs/database-roles.md`) |
| `DATABASE_URL_APP` | | ✓ | ✓ | | Login role in `rp_app`, Neon **pooled** host |
| `EPSS_MIN_SCORE` | optional | | | | Default 0.5 |
| `JWT_SIGNING_KEY` | | ✓ | | | ≥ 32 random bytes, base64 |
| `TOKEN_ENCRYPTION_KEYS` | | ✓ | ✓ | | `k1:<base64 32 bytes>[,k2:…]`; the first entry encrypts, every entry decrypts |
| `WEB_ORIGIN` | | ✓ | | | Dashboard origin, for CORS and OAuth redirects |
| `PUBLIC_API_URL` | | ✓ | ✓ | | The API's public HTTPS base; OAuth callback and Drive webhook address (Drive push needs `https://`) |
| `CLIENT_IP_HEADER` | | optional | | | Header holding the client address for rate limits, set by the platform's edge (Deploy sets `x-real-ip` on Vercel); empty: the socket peer |
| `GOOGLE_OAUTH_CLIENT_ID` / `GOOGLE_OAUTH_CLIENT_SECRET` | | ✓ | ✓ | | Google Cloud OAuth client (Drive API, Sheets API, Picker API enabled) |
| `GOOGLE_PICKER_API_KEY` | | ✓ (public) | | | Browser key, HTTP-referrer restricted to `WEB_ORIGIN` |
| `GOOGLE_CLOUD_PROJECT_NUMBER` | | ✓ (public) | | | Picker `app_id` |
| `COMPOSIO_API_KEY` | | ✓ | ✓ | | Composio Hobby (free); without it only Google Sheets is offered |
| `COMPOSIO_WEBHOOK_SECRET` | | ✓ | | | Verifies Composio webhooks |
| `COMPOSIO_AUTH_CONFIGS` | | ✓ | ✓ | | `<app>:<auth config id>` per tool, e.g. `airtable:ac_…` (Composio dashboard) |
| `COMPOSIO_TRIGGER_SLUGS` | | | optional | | `<app>:<trigger slug>` to subscribe to on connect |
| `GROQ_API_KEY` | | ✓ | ✓ | | Groq free tier |
| `GROQ_MAPPING_MODEL` / `GROQ_ADVICE_MODEL` | | ✓ | ✓ | | Llama model ids; configurable because Groq renames models |
| `WORKER_DB_INTERVAL_SECONDS` / `SOURCE_RECHECK_SECONDS` | | | optional | | The loop's database pass (default 1800) and re-read (default 900), docker compose only |
| `WORKER_ONCE_BUDGET_SECONDS` | | | optional | | Time budget of `python -m worker --once` (default 480; the job's timeout is 10 min) |
| `INLINE_BUDGET_SECONDS` / `WEBHOOK_BUDGET_SECONDS` | | optional | | | Work done inside a request: sync and scoring (default 240, under Vercel's 300 s) and webhook processing (default 20) |
| `ADVICE_PER_RUN` | | | optional | | Groq advice calls per engine run (default 20) |
| `EXPECTED_SCHEMA_VERSION` / `DB_MAX_CONNECTIONS` | | optional | | | Lazy schema check on an instance's first database use (default `050_serverless_state`); pool size (default 5) |
| `VITE_API_URL` | | | | ✓ (public) | The API base URL baked into the dashboard build (the older name `VITE_API_BASE_URL` also works) |

## Public sources (step 1, no API keys)

| Source | What is stored | Every |
|---|---|---|
| GDELT 2.0 | news articles matching watch terms (`feeds.watch_terms`, kind `gdelt_query`: phrases and GKG `theme:` codes, ~15 articles per 15-minute file with the defaults), from the 15-minute GKG files; one DOC API call as fallback | 15 min |
| CISA KEV | known exploited vulnerabilities | 1 h |
| FIRST EPSS | daily exploit-probability scores ≥ `EPSS_MIN_SCORE` (default 0.5) | 6 h |
| OSV.dev | vulnerabilities for watched packages (kind `osv_package`) | 6 h |
| IMF PortWatch | port disruptions; daily port calls / import / export per country | 6 h |
| GDACS | current disaster alerts | 15 min |
| USGS | M4.5+ earthquakes, past day | 15 min |
| NASA EONET | open natural events, last 30 days | 30 min |
| Frankfurter | ECB reference FX rates, base EUR | 3 h |
| World Bank | 6 macro / trade / logistics indicators per country | 24 h |
| OFAC SLS | latest sanctions list changes (delta file) | 1 h |
| OFAC SLS, full lists | every entity on the SDN and consolidated lists, with aliases, countries and programmes (`feeds.sanctions_entries`, replaced on each read) | 24 h |

## Sources with a free API key

Each reads its key from the GitHub secret of the same name (passed to the fetch step in
`feeds.yml`). A missing key is not an error: the source reports `skipped` ("no key") and
the job passes with no keys at all. Keys go in a request header wherever the provider
allows it; FRED and EIA only take a query parameter, so URLs are never printed and
`jobs/http.py` redacts keys and `key=`/`token=` parameters from every reported message.

| Source | Secret | Free-tier limit (checked 2026-11-24) | How often we call it | What is stored |
|---|---|---|---|---|
| FRED | `FRED_API_KEY` | 120 requests/min | 8 requests every 6 h (32/day) | `fred.<id>` series: fed funds, 10y–2y, HY spread, VIX, broad USD, CPI, unemployment, industrial production |
| NVD CVE 2.0 | `NVD_API_KEY` (optional) | 5 req/30 s keyless, 50 with key; poll ≤ every 2 h | every 2 h, 1–5 pages, 6.5 s apart keyless (~12–60/day) | CVEs modified since the last run with CVSS ≥ 7.0: severity, CPE vendor:product, purls, KEV flag |
| EIA v2 | `EIA_API_KEY` | unpublished; stay < ~9,000/h and < 5/s; 5,000 rows/response | 2 requests every 6 h (8/day) | `eia.spot_price`: WTI, Brent, Henry Hub |
| UN Comtrade | `COMTRADE_API_KEY` | 500 calls/day, 1/s, 100k records/call | 1 call/day (cap 400/day), World totals only (≤ ~9,600 records) | `comtrade.import_value` / `export_value` by country and HS code (12 critical codes), trade with the World, last 2 years |
| AlienVault OTX | `OTX_API_KEY` | 10,000 requests/h | every 1 h, ≤ 5 pages (≤ 120/day) | `cyber.threat_pulse` events (TLP white/green only): targeted countries, ATT&CK ids, CVEs, malware families |
| Tiingo | `TIINGO_API_KEY` | 50/h, 1,000/day, 500 symbols/month, 1 GB/month | 20 requests every 12 h (40/day, 20 symbols) | `tiingo.close` for 12 market ETFs (incl. BDRY freight), `tiingo.fx_close` for 8 non-ECB currencies |
| NewsData.io | `NEWSDATA_API_KEY` | 200 credits/day, 30 per 15 min, 10 articles/call, 12 h delay | ≤ 8 queries every 2 h (≤ 96/day, cap 160) | `news` events for the watch terms |
| Currents | `CURRENTS_API_KEY` | 250 requests/day, 20 results/request | ≤ 8 queries every 2 h (≤ 96/day, cap 200) | `news` events for the watch terms |

Daily caps are tracked in each source's `feeds.source_health.notes` (`budget_used`), so
manual `--force` runs cannot exhaust a quota either. Details and field mappings:
[`feeds-schema.md`](docs/contracts/feeds-schema.md). Migrations run with the separate
`DATABASE_URL_OWNER` secret (falling back to `DATABASE_URL_FEEDS`); see
[`database-roles.md`](docs/database-roles.md#switching-the-jobs-to-the-rp_feeds-login-order-matters).

Every record is either an **event** (`feeds.events`: type, title, url, published_at,
ISO 3166-1 alpha-3 countries, UN/LOCODE ports, commodities, ISO 4217 currencies,
software, severity 0–1, position, source, raw id) or a **series point**
(`feeds.series`). Fields a source does not publish stay empty. Each run updates
`feeds.source_health` with the source's status (`ok`, `empty`, `partial`,
`throttled`, `error`, `skipped`), last success and last error.

## Analysis (step 2)

`python -m jobs.run --analyse` reads what is stored, analyses it and writes the
results to their own tables. It never changes a field in `feeds.events` or
`feeds.series`.

**News tagging** (`feeds.event_tags`). [GLiNER2](https://github.com/fastino-ai/GLiNER2)
(`fastino/gliner2-base-v1`, Apache-2.0) runs on CPU. One pass per title extracts
companies, places and commodities, and picks one event type from a fixed set:
`conflict`, `protest`, `sanctions`, `trade_policy`, `port_shipping_disruption`,
`natural_hazard`, `cyber_incident`, `labour_action`, `financial_stress`, `other`.

* **What gets tagged:** English GDELT titles first, then GDACS, EONET and OFAC titles.
* **Event type:** GDACS and EONET keep `natural_hazard` and OFAC keeps `sanctions`,
  because the source already says so (`type_source = 'source'`). The model's own guess
  is still stored as `entities.type_guess`. A GDELT title gets the model's type when its
  confidence is at least 0.4, otherwise `other`.
* **Places:** a place becomes an ISO 3166-1 alpha-3 code only when the whole name
  exactly matches a country name or alias in `jobs/analyse/data/countries.tsv`.
  "Texas", "Red Sea", "Georgia" and "Congo" stay unmatched. There is no geocoding service.
* **What gets re-tagged:** an event is tagged again only when its title changes, or when
  `model_version` changes. `model_version` is the model revision plus a hash of the
  label schema.
* **Limits per run:** at most 2,000 events and a 540-second budget, so the job fits
  in 15 minutes.

**Spike detection** (`feeds.anomalies`). For every stored series key, and for daily
GDELT article counts per watch term, each recent point gets a **robust z-score**: its
distance from the median of the trailing window, divided by 1.4826 × MAD. A point is
flagged at |z| ≥ 3.5. An **IsolationForest** gives a second opinion when there is
enough history. Each method writes its own rows.

| Series | Window | Min. points | Scored on | Direction |
|---|---|---|---|---|
| `fx.rate` | 90 days | 40 (forest: 55) | daily log returns | both |
| `portwatch.*` | 90 days | 30 (forest: 60) | level | both |
| `gdelt.articles` (per watch term) | 30 days | 14 (forest: 25) | daily count | rises only |
| `wb.*` (annual) | 10 years | 8 | level | both |
| `epss.score` | – | – | not scored | – |

* **Too little history:** keys with too few points are never flagged. They are
  recorded as `insufficient_history` in `feeds.analysis_status`, which works like
  `source_health` (one row per job and one per key).
* **EPSS:** scores are only stored when they are ≥ `EPSS_MIN_SCORE`, so the
  history is censored and EPSS is not scored.
* **GDELT gaps:** a day with no stored GDELT article at all counts as an outage, not
  as zero. Note that the counts are articles *stored*, which is a proxy for news volume.
* **How often:** spike detection runs at most every 6 hours (`--force` overrides);
  tagging runs every time.

```bash
pip install -r jobs/requirements-analyse.txt          # spike detection only
pip install --index-url https://download.pytorch.org/whl/cpu torch==2.15.0
pip install -r jobs/requirements-model.txt            # + the tagging model stack
python -m jobs.run --analyse --dry-run                # fetch live, analyse in memory, store nothing
python -m jobs.run --analyse --analysis anomalies     # one step only
python -m jobs.analyse.bench                          # model size, peak RAM, time per 100 events
```

Without the model libraries, tagging reports `skipped` and spike detection still runs.

## Risk engine (step 3)

`engine/` is the Python library that turns a workspace's entities and the public feeds
into scored risks. The worker calls `engine.run_workspace(conn, workspace_id)` inside a
transaction that already has `app.workspace_id` set, and the API calls
`engine.run_scenario(...)` for what-if runs. It is deterministic: exact code matches
(country, port, UN/LOCODE, currency, HS chapter, purl/CPE), fuzzy matching for names
only (sanctions, company news), and `score = 100 × likelihood × impact` from fixed
constants. There is no LLM and no randomness.

```bash
pip install -r jobs/requirements-dev.txt -r engine/requirements.txt
python -m pytest tests/test_engine_*.py                     # DB tests need TEST_DATABASE_URL
TEST_DATABASE_URL=postgresql://... python tests/bench_engine.py   # throwaway DB only: timing
```

Every rule, with an example, the known gaps and the runtime per workspace (about 2–6 s
for a typical workspace): [`engine/README.md`](engine/README.md). The interface:
[`docs/contracts/risk-engine.md`](docs/contracts/risk-engine.md).

## Running

```bash
python3.12 -m venv .venv && . .venv/bin/activate
pip install -r jobs/requirements-dev.txt
python -m jobs.run --sources all --dry-run          # fetch and print counts, no database
python -m pytest                                    # parsers on saved real responses
```

With a database (`DATABASE_URL_FEEDS` set; migrations need the owner role):

```bash
python -m jobs.migrate
python -m jobs.run --sources all                    # skips sources not due yet
python -m jobs.run --sources usgs,gdacs --force
python -m jobs.prune --dry-run                      # what retention would delete
```

**Retention** (`python -m jobs.prune`, run by `feeds.yml` after each fetch, deleting at
most once a day) keeps the database inside Neon's free 1 GB: events whose event time
*and* first-stored time are older than `RETAIN_EVENTS_DAYS` (90), with their tags;
series observations older than `RETAIN_SERIES_DAYS` (400) except the newest
`RETAIN_SERIES_MIN_POINTS` (60) of each series key, so annual World Bank series keep
their history; and anomaly flags whose point is gone. It prints rows deleted and
`pg_database_size`.

Self-hosted, with Postgres 16 in Docker: `docker compose up -d --build`. The
container runs spike detection after every fetch. To include tagging, build with
`--build-arg WITH_MODEL=true`, which adds CPU torch and the model libraries.

### GitHub Actions

* `feeds.yml` runs every 15 minutes (and on demand). It exits cleanly with a notice
  until the `DATABASE_URL_FEEDS` repository secret is set. Its `analyse` job runs after
  the fetch:
  * It installs CPU-only torch and restores the model from `actions/cache`.
  * It downloads the model from Hugging Face only on a cache miss.
  * It then runs with `HF_HUB_OFFLINE=1`.
* `model-bench.yml` (manual only) runs `jobs.analyse.bench` on GitHub's runner. It
  prints only the model size, peak RAM, seconds per 100 events and tags per type.
* `ci.yml` runs the tests (including a Postgres 16 service) and a Docker build on pull
  requests. The tagging model is mocked there, so nothing is downloaded.
* `ci-api.yml` runs the API and worker tests (`tests/apiservice`, Postgres 16, Google,
  Composio and Groq replaced inside the tests) and builds the `api` and `worker` images
  (docker compose). Its `vercel-bundle` job builds the Vercel function as Deploy does and
  reports its size, the cold-import time and an ASGI smoke test; its `workflows` job lints
  the deploy and worker workflows with actionlint.
* `worker.yml` runs `python -m worker --once` after each `feeds.yml` run.
* `deploy.yml`, `destroy.yml` and `db-roles.yml` (manual only): see
  [Deploy / Destroy](#deploy--destroy).

The repository is public, so job logs only ever show per-source status, counts and timings.

### Free-tier notes

* Neon bills compute while it is awake and suspends it after 5 idle minutes. Jobs use
  short-lived connections only (one to read state, one to write). The budget, at
  0.25 CU, out of the 100 free CU-hours a month (estimate; see
  [`infra/README.md`](infra/README.md#neon-compute-budget)):

  | What wakes or keeps Neon awake | Roughly |
  |---|---|
  | `feeds.yml` every 30 min (fetch + analyse, ~6 min awake per run including the 5-min tail) | 36 CU-h |
  | `worker.yml` right after it (extends the tail by ~1–1.5 min per run, never a wake of its own) | 6–9 CU-h |
  | a visible dashboard tab (polls every 15 s), e.g. 1 h a day | ~8 CU-h |
  | requests outside those windows (sign-in, confirm, webhooks), e.g. 10 a day | ~7 CU-h |
  | **total** | **~55–60 CU-h** |
* The `analyse` job uses the same short connections, right after the fetch, while
  the compute is still awake. Tags add about one row per tagged event, and anomaly
  rows exist only for flagged points.
* Unchanged records are not rewritten (`IS DISTINCT FROM` upserts), so repeated runs
  add little write load or storage. One full run currently stores about 19k rows (~14 MB).
