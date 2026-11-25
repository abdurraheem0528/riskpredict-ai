# RiskPredict AI

Free, map-first AI platform that predicts supply-chain, market and cyber risk for a business.

It matches global risk feeds (news, markets, ports, hazards, cyber, sanctions) to a
business's own connected tools and shows the result on a map-first dashboard.

## Architecture

```
 public feeds ──► GitHub Actions (jobs/, every 30 min) ──► Neon Postgres: feeds schema (public data)
                                                                 │  read-only
 business tools ─► Northflank: api (FastAPI) + worker ──► Neon Postgres: biz schema (row-level security)
 (Google Sheets direct,          │          ▲                    │
  others via Composio)           │ SSE      │ engine/ (risk scoring, a library)
                                 ▼          │
                                   Cloudflare Pages: web (React + MapLibre dashboard)
```

| Part | Where it runs | Status |
|---|---|---|
| `jobs/` – fetch and normalise public feeds | GitHub Actions (`.github/workflows/feeds.yml`) | **step 1** |
| `jobs/analyse/` – news tagging (GLiNER2) and spike detection | GitHub Actions, after each fetch | **step 2** |
| `infra/migrations/` – SQL for the `feeds` and `biz` schemas, roles | applied by `python -m jobs.migrate` | **steps 1–3** |
| `docs/contracts/` – API, engine and feed contracts | – | **step 3** |
| `api/` – FastAPI for business data | Northflank Sandbox | **session B** ([`api/README.md`](api/README.md)) |
| `worker/` – sync, watch renewal, scoring runs, advice | Northflank Sandbox | **session B** ([`worker/README.md`](worker/README.md)) |
| `engine/` – risk engine library | imported by worker and api | **step 3** |
| `web/` – dashboard | Cloudflare Pages | next (session D) |

Ground rules: everything uses free tiers and free, open-source code; the platform never
contains made-up data (demo data lives separately); nothing runs on the owner's computer;
everything is portable (`docker-compose.yml`) and removable.

## How the pieces fit together

1. **Feeds.** Every 30 minutes the jobs (`rp_feeds`) write public events, series,
   tags and anomalies to `feeds` ([`feeds-schema.md`](docs/contracts/feeds-schema.md)).
2. **Connecting a tool.** A user signs up with the API (email + argon2id, no
   third-party auth) and connects a tool. Google Sheets is direct (OAuth `drive.file`
   + Google Picker); Airtable and others go through Composio. Picking a sheet or table
   creates a *source*.
3. **Column mapping.** Groq suggests how the source's columns map to entity fields.
   The user confirms on the **Confirm** screen, with a map preview. The worker then
   syncs rows into `biz.entities`, normalised to ISO country, UN/LOCODE, ISO 4217,
   HS code and purl/CPE keys. Drive push notifications and Composio triggers keep
   that up to date.
4. **Scoring.** After each feed update or source sync, the worker runs the risk engine
   per workspace ([`risk-engine.md`](docs/contracts/risk-engine.md)). It matches
   entities to feed events and anomalies and writes `biz.risks` with
   `score = 100 × likelihood × impact`. Groq only words the advice.
5. **Dashboard.** The dashboard reads everything through the API
   ([`openapi.yaml`](docs/contracts/openapi.yaml)): GeoJSON map layers, risks with
   their evidence, scenarios and alerts. Live updates come over SSE, which the worker
   feeds through the API without holding a database connection open.
6. **Isolation.** Every `biz` row is isolated by forced row-level security keyed on
   `app.workspace_id`. Roles and login setup: [`docs/database-roles.md`](docs/database-roles.md).
   The decisions every part follows: [`docs/contracts/README.md`](docs/contracts/README.md).

### Environment variables per service

Secrets go in GitHub repository secrets, Northflank secret groups or Cloudflare
Pages settings, never in the repository. "public" marks values that are safe to
expose to the browser.

| Variable | jobs | api | worker | web | What it is |
|---|:-:|:-:|:-:|:-:|---|
| `DATABASE_URL_MIGRATE` | ✓ (migrate step) | | | | Owner connection string; runs `python -m jobs.migrate` and `infra/scripts/create_login_roles.py` |
| `DATABASE_URL_FEEDS` | ✓ | | | | Login role in `rp_feeds` (today still the owner; see `docs/database-roles.md`) |
| `DATABASE_URL_APP` | | ✓ | ✓ | | Login role in `rp_app`, Neon **pooled** host |
| `EPSS_MIN_SCORE` | optional | | | | Default 0.5 |
| `JWT_SIGNING_KEY` | | ✓ | | | ≥ 32 random bytes, base64 |
| `TOKEN_ENCRYPTION_KEYS` | | ✓ | ✓ | | `k1:<base64 32 bytes>[,k2:…]`; the first entry encrypts, every entry decrypts |
| `INTERNAL_TOKEN` | | ✓ | ✓ | | Shared secret for `POST /internal/events` and `GET /internal/work` |
| `WEB_ORIGIN` | | ✓ | | | Dashboard origin, for CORS and OAuth redirects |
| `PUBLIC_API_URL` | | ✓ | ✓ | | The API's public HTTPS base; OAuth callback and Drive webhook address (Drive push needs `https://`) |
| `API_INTERNAL_URL` | | | ✓ | | API on the private network, e.g. `http://api:8000` |
| `GOOGLE_OAUTH_CLIENT_ID` / `GOOGLE_OAUTH_CLIENT_SECRET` | | ✓ | ✓ | | Google Cloud OAuth client (Drive API, Sheets API, Picker API enabled) |
| `GOOGLE_PICKER_API_KEY` | | ✓ (public) | | | Browser key, HTTP-referrer restricted to `WEB_ORIGIN` |
| `GOOGLE_CLOUD_PROJECT_NUMBER` | | ✓ (public) | | | Picker `app_id` |
| `COMPOSIO_API_KEY` | | ✓ | ✓ | | Composio Hobby (free); without it only Google Sheets is offered |
| `COMPOSIO_WEBHOOK_SECRET` | | ✓ | | | Verifies Composio webhooks |
| `COMPOSIO_AUTH_CONFIGS` | | ✓ | ✓ | | `<app>:<auth config id>` per tool, e.g. `airtable:ac_…` (Composio dashboard) |
| `COMPOSIO_TRIGGER_SLUGS` | | | optional | | `<app>:<trigger slug>` to subscribe to on connect |
| `GROQ_API_KEY` | | ✓ | ✓ | | Groq free tier |
| `GROQ_MAPPING_MODEL` / `GROQ_ADVICE_MODEL` | | ✓ | ✓ | | Llama model ids; configurable because Groq renames models |
| `WORKER_DB_INTERVAL_SECONDS` / `SOURCE_RECHECK_SECONDS` | | | optional | | Database pass (default 1800) and the tool re-read fallback (default 900) |
| `ADVICE_PER_RUN` | | | optional | | Groq advice calls per engine run (default 20) |
| `EXPECTED_SCHEMA_VERSION` / `DB_MAX_CONNECTIONS` | | optional | | | Startup schema check (default `007_biz_lookups`); pool size (default 5) |
| `PORT` / `FORWARDED_ALLOW_IPS` | | optional | | | Listen port (8000); proxies whose `X-Forwarded-For` is trusted for rate limits |
| `VITE_API_BASE_URL` | | | | ✓ (public) | The API base URL baked into the dashboard build |

## Public sources (step 1, no API keys)

| Source | What is stored | Every |
|---|---|---|
| GDELT 2.0 | news articles matching watch terms (`feeds.watch_terms`, kind `gdelt_query`), from the 15-minute GKG files; one DOC API call as fallback | 15 min |
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
  Composio and Groq replaced inside the tests) and builds the `api` and `worker` images.

The repository is public, so job logs only ever show per-source status, counts and timings.

### Free-tier notes

* Neon bills compute while it is awake and suspends it after 5 idle minutes. Jobs use
  short-lived connections only (one to read state, one to write), but a 15-minute
  schedule still keeps a 0.25 CU compute awake roughly 6 of every 15 minutes:
  about 60–75 of the 100 free CU-hours a month, before the API and worker add theirs.
* The `analyse` job uses the same short connections, right after the fetch, while
  the compute is still awake. Tags add about one row per tagged event, and anomaly
  rows exist only for flagged points.
* Unchanged records are not rewritten (`IS DISTINCT FROM` upserts), so repeated runs
  add little write load or storage. One full run currently stores about 19k rows (~14 MB).
