# `feeds` schema (as on `main`, migrations 001–003)

The public-data side, written only by the GitHub Actions jobs (`rp_feeds`) and read by
the API, worker and risk engine (`rp_app`, SELECT only). Nothing here belongs to a
business, and nothing here has a `workspace_id`. This page describes what is
actually stored, so B, C and D don't need to read the SQL or the connectors.

General rules:

* **Codes.** `countries` are ISO 3166-1 **alpha-3** (`JPN`), `ports` are UN/LOCODE
  without a space (`NLRTM`), and `currencies` are ISO 4217 (`EUR`).
* **Missing values.** A field a source does not publish stays empty (`{}` / NULL).
  Nothing is guessed: e.g. GDELT articles have **no** `countries`, and EONET has no
  country or severity.
* **Times.** All times are `timestamptz` in UTC.
* **Writes.** Rows are upserted, and unchanged records are not rewritten. A later
  retention migration (expected `004`) may **delete** old rows. Never hold a foreign
  key to `feeds`: copy what you need (see `biz.risk_evidence.snapshot`).
* **Schedule.** The fetch runs every 30 minutes (`feeds.yml`). Each source also has a
  minimum interval (below), and analysis follows each fetch.

## `feeds.events` — discrete happenings

Primary key `(source, raw_id)`.

| Column | Type | Notes |
|---|---|---|
| `source` | text | connector name (table below) |
| `raw_id` | text | id as given by the source (format per source below) |
| `type` | text | dotted type, below |
| `title` | text | `''` if none |
| `url` | text | nullable |
| `published_at` | timestamptz | nullable; when the source says it happened / was published |
| `countries` | text[] | ISO 3166-1 alpha-3 |
| `ports` | text[] | UN/LOCODE |
| `commodities` | text[] | free text; **no connector fills this yet** (use `event_tags`) |
| `currencies` | text[] | ISO 4217; **no connector fills this yet** |
| `software` | text[] | `"<vendor or ecosystem>:<product or package>"` exactly as the source names them |
| `severity` | real 0–1 | only when the source publishes a measure (mapping per source below) |
| `lat`, `lon` | double | both or neither |
| `attrs` | jsonb | source-specific extras (keys per source below; absent keys are omitted, not null) |
| `first_seen_at` | timestamptz | first insert |
| `updated_at` | timestamptz | last change of any column |

Indexes: `published_at DESC`; `(type, published_at DESC)`; GIN on `countries`, `ports`,
`software`, `currencies` and `commodities`.

### Per source

| `source` | Every | `type` | `raw_id` | Keys filled | `severity` | `attrs` keys |
|---|---|---|---|---|---|---|
| `gdelt` | 15 min | `news` | sha256(url)[:32] | none (publisher country is in attrs, **not** `countries`) | – | `query` (the watch term), `domain`, `language`, `sourcecountry` |
| `gdacs` | 15 min | `hazard.earthquake` / `tropical_cyclone` / `flood` / `volcano` / `drought` / `wildfire` / `tsunami` (else the lower-cased GDACS code) | `<TYPE>-<eventid>-<episodeid>` | countries, lat/lon | GDACS alert score / 3 (Green ⅓, Orange ⅔, Red 1) | `alert_level`, `alert_score`, `episode_alert_level`, `to_date`, `modified`, `is_current`, `severity_text`, `glide` |
| `usgs` | 15 min | `hazard.earthquake` (M4.5+, past day) | USGS id | countries (only if the place text ends in a country or US state), lat/lon | USGS `sig` / 1000, clipped | `magnitude`, `mag_type`, `depth_km`, `place`, `alert`, `tsunami`, `sig`, `status`, `updated` |
| `eonet` | 30 min | `hazard.<category>`: wildfire, volcano, severe_storm, flood, earthquake, landslide, temperature_extreme, sea_lake_ice, dust_haze, drought, snow, water_color, manmade, other | EONET id | lat/lon (latest point) only | – | `categories`, `sources`, `last_observed`, `magnitude`, `magnitude_unit`, `observations` |
| `portwatch` | 6 h | `port.disruption` | `<eventtype>-<eventid>` | countries, ports, lat/lon | alert level Green ⅓, Orange ⅔, Red 1 | `hazard_type`, `alert_level`, `severity_text`, `description`, `to_date`, `affected_ports`, `affected_population` |
| `cisa_kev` | 1 h | `cyber.exploited_vulnerability` | CVE id | software `"<vendorProject>:<product>"` (e.g. `Microsoft:Windows`) | – | `cve`, `due_date`, `known_ransomware_use`, `cwes`, `catalog_version` |
| `osv` | 6 h | `cyber.vulnerability` | OSV id (GHSA-…, PYSEC-…) | software `"<OSV ecosystem>:<package>"` (e.g. `PyPI:requests`, `Maven:org.apache.logging.log4j:log4j-core`) — only for packages in `watch_terms` | CVSS v3 (else v4) base score / 10 | `aliases` (CVE ids), `modified`, `withdrawn`, `severity_label` |
| `ofac` | 1 h | `sanction.designation` / `sanction.update` / `sanction.removal` | `<entity id>:<action>:<date>` | countries (addresses, nationality, citizenship, vessel flag) | – | `entity_id`, `action`, `entity_type`, `name` (primary full name), `programs`, `lists`, `publication_type` |

**OFAC caveat:** `ofac` stores only the **latest change set** (the delta file). It is
not the full SDN list, so sanctions matching against it only sees entities designated,
updated or removed recently. A full-list connector is needed for complete screening;
see the open items in `risk-engine.md`.

## `feeds.series` — numeric time series

Primary key `(series, key, ts, source)`. Columns: `value` (double), `unit`, `attrs`
(jsonb), `updated_at`.

| `series` | `key` | `source` | `ts` | `unit` | Notes |
|---|---|---|---|---|---|
| `fx.rate` | `EUR/<CCY>` | `frankfurter` | ECB business day, 00:00 UTC | `<CCY> per EUR` | base is always EUR; derive cross rates yourself |
| `portwatch.port_calls` | ISO3 | `portwatch` | day | `calls` | country level; lags ~10 days |
| `portwatch.import_volume` | ISO3 | `portwatch` | day | `metric tonnes` | |
| `portwatch.export_volume` | ISO3 | `portwatch` | day | `metric tonnes` | |
| `wb.FP.CPI.TOTL.ZG` | ISO3 | `worldbank` | 1 Jan of the year | `% annual` | inflation |
| `wb.NY.GDP.MKTP.KD.ZG` | ISO3 | `worldbank` | 1 Jan | `% annual` | GDP growth |
| `wb.NE.TRD.GNFS.ZS` | ISO3 | `worldbank` | 1 Jan | `% of GDP` | trade |
| `wb.PA.NUS.FCRF` | ISO3 | `worldbank` | 1 Jan | `LCU per USD` | official FX, period average |
| `wb.LP.LPI.OVRL.XQ` | ISO3 | `worldbank` | 1 Jan | `score 1-5` | logistics performance |
| `wb.NV.IND.MANF.ZS` | ISO3 | `worldbank` | 1 Jan | `% of GDP` | manufacturing |
| `epss.score` | CVE id | `epss` | score date | `probability` | only scores ≥ `EPSS_MIN_SCORE` (0.5) are stored; `attrs.percentile`, `attrs.model_version` |

Index: `(source, ts DESC)`.

## `feeds.event_tags` — GLiNER2 tags for text events

Primary key `(source, raw_id, model)`, FK to `feeds.events` (cascade). One row per
tagged event per model. Sources tagged: `gdelt` (English titles), `gdacs`, `eonet`,
`ofac`.

| Column | Notes |
|---|---|
| `model`, `model_version` | e.g. `fastino/gliner2-base-v1`; version = revision + label-schema hash. Re-tagged when either or the title changes. |
| `input_md5` | md5 of the tagged title |
| `event_type` | one of `conflict`, `protest`, `sanctions`, `trade_policy`, `port_shipping_disruption`, `natural_hazard`, `cyber_incident`, `labour_action`, `financial_stress`, `other` |
| `type_confidence` | 0–1, or NULL when `type_source = 'source'` or the guess was below 0.4 (then `other`) |
| `type_source` | `source` (gdacs/eonet → natural_hazard, ofac → sanctions) or `model` |
| `entities` | see below |
| `tagged_at` | |

`entities` shape (always these keys):

```json
{
  "companies":   [{"text": "Maersk", "confidence": 0.91}],
  "places":      [{"text": "Rotterdam", "confidence": 0.88, "country": null},
                  {"text": "Japan", "confidence": 0.95, "country": "JPN"}],
  "commodities": [{"text": "copper", "confidence": 0.77}],
  "countries":   ["JPN"],
  "type_guess":  {"type": "port_shipping_disruption", "confidence": 0.62}
}
```

`places[].country` is set only on an **exact** country-name match (no geocoding:
"Texas", "Red Sea" and "Port of Rotterdam" stay `null`). `countries` is the sorted
set of those codes. Indexes: `(event_type, tagged_at DESC)`, GIN `jsonb_path_ops` on
`entities` (so `entities @> '{"countries":["JPN"]}'` is indexed).

## `feeds.anomalies` — flagged points

Primary key `(series, key, ts, method)`.

| Column | Notes |
|---|---|
| `series`, `key` | a `feeds.series` pair, **or** `series = 'gdelt.articles'` with `key` = the GDELT watch term (daily stored-article counts) |
| `ts` | the flagged observation (00:00 UTC for daily data) |
| `method` | `robust_z` or `isolation_forest` (each method writes its own row; agreement = both rows exist) |
| `value` | observed value (for `fx.rate`: the rate, although scoring used log returns) |
| `baseline` | what the trailing window expected, same unit as `value` |
| `score` | `robust_z`: signed robust z (flag at \|z\| ≥ 3.5; one-sided, rises only, for `gdelt.articles`). `isolation_forest`: 0–1 anomaly score, higher = more isolated |
| `attrs` | `rule`, `window_days`, `transform` (`level`/`log_return`), `points`, and `threshold` or `contamination` |
| `detected_at` | |

Rules: `fx.rate` 90 d window, ≥ 40 points; `portwatch.*` 90 d, ≥ 30; `gdelt.articles`
30 d, ≥ 14; `wb.*` 10 y, ≥ 8 (so annual indicators report insufficient history for
now); `epss.score` is never scored. Spike detection runs at most every 6 h.
Index: `ts DESC`.

## `feeds.watch_terms` — what query-driven sources ask about

| Column | Notes |
|---|---|
| `id` | identity |
| `kind` | `gdelt_query` (a GDELT DOC query string) or `osv_package` (`term` = package, `scope` = OSV ecosystem: `PyPI`, `npm`, `Maven`, `Go`, `crates.io`, `NuGet`, `RubyGems`, `Packagist`…) |
| `term`, `scope` | unique together with `kind` |
| `enabled` | |
| `origin` | `default` (migration 002) or, from the worker, `workspace` |
| `created_at` | |

`rp_app` may INSERT `(kind, term, scope, origin)` but never update or delete, and a
row never names a workspace. GDELT is slow: about 40 queries fit in one 4-minute run,
so the worker must keep `gdelt_query` additions few (see `risk-engine.md`).

## `feeds.source_health` and `feeds.analysis_status` — freshness

`source_health`: one row per `source`, with `status`, `last_run_at`,
`last_success_at`, `last_error` (safe text), `consecutive_failures`,
`records_last_run`, `records_changed`, `duration_ms` and `notes` (jsonb counts).
`status` is one of `ok`, `empty`, `partial`, `throttled`, `error` or `skipped`.

`analysis_status`: primary key `(analysis, subject)`. `analysis` is `tagging` or
`anomalies`. `subject` is `*` for the job itself, or `'<series>|<key>'` for each
checked series. `status` is one of `ok`, `empty`, `partial`, `insufficient_history`,
`skipped` or `error`. It also has `history_points`, `needed_points`, `last_run_at`,
`last_success_at`, `last_error` and `notes`.

Use these for the dashboard's "data as of" indicator and to tell "no risk" apart
from "no data". The worker watches `max(source_health.last_run_at)` to know when to
re-score.

## `feeds.schema_migrations`

`version` (file stem, e.g. `006_biz_schema`), `applied_at`. Readable by `rp_app`, so the
API can refuse to start if it expects a migration that is not applied.
