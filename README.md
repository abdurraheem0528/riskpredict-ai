# RiskPredict AI

Free, map-first AI platform that predicts supply-chain, market and cyber risk for a business.

It matches global risk feeds (news, markets, ports, hazards, cyber, sanctions) to a
business's own connected tools and shows the result on a map-first dashboard.

## Architecture

```
 public feeds ──► GitHub Actions (jobs/, every 15 min) ──► Neon Postgres: feeds schema (public data)
                                                                 │
 business tools ─► Northflank: api (FastAPI) + worker ──► Neon Postgres: biz schema (row-level security)
 (Google Sheets direct,                                          │
  others via Composio)                                           ▼
                                   Cloudflare Pages: web (React + MapLibre dashboard)
```

| Part | Where it runs | Status |
|---|---|---|
| `jobs/` – fetch and normalise public feeds | GitHub Actions (`.github/workflows/feeds.yml`) | **step 1** |
| `infra/migrations/` – SQL for the `feeds` schema | applied by `python -m jobs.migrate` | **step 1** |
| `api/` – FastAPI for business data | Northflank Sandbox | later |
| `worker/` – business data sync and analysis | Northflank Sandbox | later |
| `web/` – dashboard | Cloudflare Pages | later |

Ground rules: everything uses free tiers and free, open-source code; the platform never
contains made-up data (demo data lives separately); nothing runs on the owner's computer;
everything is portable (`docker-compose.yml`) and removable.

## Public sources (step 1, no API keys)

| Source | What is stored | Every |
|---|---|---|
| GDELT DOC 2.0 | news articles for watch terms (`feeds.watch_terms`, kind `gdelt_query`) | 15 min |
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

## Running

```bash
python3.12 -m venv .venv && . .venv/bin/activate
pip install -r jobs/requirements-dev.txt
python -m jobs.run --sources all --dry-run          # fetch and print counts, no database
python -m pytest                                    # parsers on saved real responses
```

With a database (`DATABASE_URL_FEEDS` set):

```bash
python -m jobs.migrate
python -m jobs.run --sources all                    # skips sources not due yet
python -m jobs.run --sources usgs,gdacs --force
```

Self-hosted, with Postgres 16 in Docker: `docker compose up -d --build`.

### GitHub Actions

* `feeds.yml` runs every 15 minutes (and on demand). It exits cleanly with a notice
  until the `DATABASE_URL_FEEDS` repository secret is set.
* `ci.yml` runs the tests (including a Postgres 16 service) and a Docker build on pull requests.

The repository is public, so job logs only ever show per-source status, counts and timings.

### Free-tier notes

* Neon bills compute while it is awake and suspends it after 5 idle minutes. Jobs use
  short-lived connections only (one to read state, one to write), but a 15-minute
  schedule still keeps a 0.25 CU compute awake roughly 6 of every 15 minutes:
  about 60–75 of the 100 free CU-hours a month, before the API and worker add theirs.
* Unchanged records are not rewritten (`IS DISTINCT FROM` upserts), so repeated runs
  add little write load or storage. One full run currently stores about 19k rows (~14 MB).
