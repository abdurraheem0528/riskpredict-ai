# Risk engine contract (session C implements, the worker and API call it)

The engine turns a workspace's **entities** (`biz.entities`) plus the public **feeds**
(`feeds.events`, `feeds.event_tags`, `feeds.anomalies`, `feeds.series`) into rows in
`biz.risks`, `biz.risk_evidence` and `biz.risk_history`.

**Scoring is deterministic: `score = 100 × likelihood × impact`.** Every number comes
from the rules below. **Groq is never used for scoring or matching;** it only words
the advice text, after scoring (§8). The database enforces the formula: `score` is a
generated column, so the engine writes `likelihood` and `impact` only.

## 1. Package and dependencies

* **Location:** the package is `engine/` at the repository root (`import engine`). It
  is pure Python 3.12. It may import `jobs.codes` (ISO and LOCODE normalisation) and
  `jobs.analyse.anomalies.detect` (the same robust-z/IsolationForest code the jobs use),
  but nothing else from `jobs/`.
* **Allowed dependencies:** `numpy`, `scikit-learn` (already in
  `jobs/requirements-analyse.txt`), `rapidfuzz` (MIT) for name similarity, and
  `psycopg` for the DB layer. Nothing that calls the network, nothing paid.
* **Version:** `engine.ENGINE_VERSION: str` (semver). Bump it on any change that can
  alter a score; it is stored on every risk and history row.

## 2. Python interface

```python
from engine import (
    ENGINE_VERSION, EngineConfig, DEFAULT_CONFIG,
    Entity, FeedEvent, EventTag, Anomaly, FeedSnapshot, Evidence, Assessment,
    RunResult, RiskChange, ScenarioInputs,
    assess, load_entities, load_feeds, write_risks, run_workspace, run_scenario,
    watch_terms_for, advice_messages, template_advice, needs_new_advice,
    # also exported (engine 1.0.0):
    HazardShock, FxShock, SanctionsShock, EntityOverride,   # the parsed ScenarioInputs parts
    ScenarioError,          # ValueError; bad scenario inputs, message safe as a Problem `detail` (422)
    EngineContextError,     # app.workspace_id missing or not the workspace passed in
    scenario_results,       # the pure part of run_scenario (no I/O)
    chokepoints, chokepoint_id,  # §5.3 table for the API's disruptions layer; id/name/alias → id
)

# --- pure core: no I/O, deterministic for the same inputs -----------------------
def assess(entities: Sequence[Entity], feeds: FeedSnapshot, *,
           base_currency: str | None, config: EngineConfig = DEFAULT_CONFIG) -> list[Assessment]: ...

# --- DB layer: `conn` is a psycopg 3 connection logged in as an rp_app member, with
# app.workspace_id already set *for the current transaction* by the caller. The
# engine never commits, never sets app.* itself and never opens connections. ------
def load_entities(conn) -> list[Entity]: ...
def load_feeds(conn, entities: Sequence[Entity], *, now: datetime,
               config: EngineConfig = DEFAULT_CONFIG) -> FeedSnapshot: ...
def write_risks(conn, workspace_id: UUID, assessments: Sequence[Assessment], *,
                now: datetime, config: EngineConfig = DEFAULT_CONFIG) -> RunResult: ...

# --- what the worker calls --------------------------------------------------------
def run_workspace(conn, workspace_id: UUID, *, now: datetime | None = None,
                  config: EngineConfig = DEFAULT_CONFIG) -> RunResult:
    """load_entities → load_feeds → assess → write_risks, inside the caller's transaction."""

# --- what the API calls for POST /v1/scenarios (writes nothing) --------------------
def run_scenario(conn, workspace_id: UUID, inputs: ScenarioInputs | Mapping, *,
                 now: datetime | None = None,
                 config: EngineConfig = DEFAULT_CONFIG) -> dict:  # ScenarioResults (openapi.yaml), §12
    """`inputs` may be the request JSON as a dict; it is validated (ScenarioError)."""

# --- helpers for the worker ---------------------------------------------------------
def watch_terms_for(entities: Sequence[Entity]) -> list[tuple[str, str, str]]: ...  # (kind, term, scope), §7
def advice_messages(risk: Assessment, entity: Entity) -> list[dict]: ...  # Groq chat messages, §8
def template_advice(risk: Assessment, entity: Entity) -> str: ...
def needs_new_advice(old_score: float | None, new_score: float, old_evidence: set[str], new_evidence: set[str]) -> bool: ...
```

### Data classes (frozen dataclasses, `slots=True`)

```python
Entity:       id: UUID; kind: str; name: str; countries/ports/currencies/hs_codes/commodities/software: tuple[str, ...]
              lat/lon: float | None; annual_spend: Decimal | None; spend_currency: str | None
              revenue_share: Decimal | None; criticality: str | None; attrs: Mapping[str, Any]
              # exactly the biz.entities columns of the same name

EventTag:     event_type: str; type_confidence: float | None; type_source: str; entities: Mapping[str, Any]
FeedEvent:    source, raw_id, type, title: str; url: str | None; published_at: datetime | None; first_seen_at: datetime
              countries/ports/commodities/currencies/software: tuple[str, ...]; severity: float | None
              lat/lon: float | None; attrs: Mapping[str, Any]; tag: EventTag | None   # newest tag row, if any
Anomaly:      series, key: str; ts: datetime; method: str; value, baseline, score: float; attrs: Mapping[str, Any]
FeedSnapshot: now: datetime; events: tuple[FeedEvent, ...]; anomalies: tuple[Anomaly, ...]
              series: Mapping[tuple[str, str], tuple[tuple[datetime, float], ...]]   # only fx.rate, for conversion/cross rates
              source_health: Mapping[str, datetime | None]                          # source → last_success_at

Evidence:     kind: 'event' | 'anomaly'; event_source/event_raw_id | anomaly_series/anomaly_key/anomaly_ts/anomaly_method
              match_type: str; match_value: str; match_confidence, signal, recency, contribution: float
              snapshot: Mapping[str, Any]          # EvidenceSnapshot below
Assessment:   fingerprint: str; entity_id: UUID; category: str; title: str; explanation: str
              likelihood: float; impact: float     # 0–1, rounded to 3 places
              factors: Mapping[str, Any]           # RiskFactors below
              countries: tuple[str, ...]; ports: tuple[str, ...]; lat/lon: float | None
              evidence: tuple[Evidence, ...]       # sorted by contribution desc, at most config.max_evidence (50)
              score -> float                       # property: round(100 * likelihood * impact, 1)

RiskChange:   risk_id: UUID; fingerprint: str; change: 'created' | 'rescored' | 'resolved' | 'reopened'
              old_score: float | None; new_score: float | None; category: str
RunResult:    engine_version: str; scored_entities: int; changes: tuple[RiskChange, ...]
              counts: Mapping[str, int]            # created, rescored, unchanged, resolved, reopened
```

`ScenarioInputs` is the `ScenarioInputs` schema in `openapi.yaml`, parsed into
dataclasses.

`load_feeds` reads only what can match: events whose keys overlap the entities' keys,
or which have a position (for radius matching), within the lookback windows (§5). It
must stay cheap on the Neon free tier: one query per table, using the GIN indexes,
and no full scans of `feeds.series`.

## 3. Output rows

`write_risks` upserts by `(workspace_id, fingerprint)`:

| Column | Value |
|---|---|
| `fingerprint` | `"<entity_id>\|<category>\|<driver>"`; driver is `<match_type>:<match_value>` of the strongest evidence, except geo matches, which use `geo_radius:near` so that several quakes near one site are one risk |
| `category` | §4 |
| `title` | template, e.g. `"Earthquake near Osaka plant"`, `"Sanctions designation may match Acme Trading LLC"`, `"Exploited vulnerability in Microsoft Exchange"` (≤ 300 chars) |
| `explanation` | deterministic text: what matched, on which key, and the factors (no AI) |
| `likelihood`, `impact` | §5, §6 |
| `factors` | `RiskFactors`, below |
| `countries`, `ports`, `lat`, `lon` | for the map: the entity's position if it has one, else the matched country/port |
| `status` | lifecycle below |
| `engine_version` | `ENGINE_VERSION` |

`RiskFactors` (stored as jsonb, returned by the API as is):

```json
{
  "likelihood": {"method": "noisy_or", "value": 0.62,
                 "items": [{"evidence_key": "event:usgs:us6000abcd", "signal": 0.71, "recency": 0.93,
                            "match_confidence": 0.88, "contribution": 0.58}]},
  "impact": {"value": 0.68, "criticality": "high", "criticality_weight": 0.75,
             "exposure_share": 0.12, "exposure_norm": 0.48, "category_multiplier": 0.9},
  "engine_version": "1.0.0"
}
```

`evidence_key` is `event:<source>:<raw_id>` or
`anomaly:<series>|<key>|<ts as RFC 3339 UTC>|<method>`.

`EvidenceSnapshot` (in `biz.risk_evidence.snapshot`, because feed retention may delete
the original):

```json
{"title": "...", "url": "...", "type": "hazard.earthquake", "published_at": "2026-11-20T03:12:00Z",
 "severity": 0.71, "lat": 34.6, "lon": 135.5, "countries": ["JPN"], "ports": [],
 "value": null, "baseline": null, "score": null, "unit": null}
```

For anomalies, `value`, `baseline`, `score` and `unit` are filled and `title` is
generated (e.g. `"EUR/TRY moved 4.1 robust z from its 90-day norm"`).

**Lifecycle**, applied by `write_risks` (one transaction):

1. **New fingerprint:** insert with `status = 'open'`, add a `risk_history` row with
   reason `created`.
2. **Existing risk:** update the numbers, texts, `factors`, `last_seen_at` and
   `last_scored_at`. Add history reason `rescored` only when the score moves by
   ≥ 1.0. User-set statuses (`acknowledged`, `mitigating`) are kept.
3. **Dismissed by a user:** stays dismissed unless an evidence key appears that was
   never linked to it before. Then it gets `status = 'open'` and history `reopened`.
4. **Resolved by the engine but found again:** `open`, history `reopened`.
5. **Not assessed this run, or `score < config.min_score` (5.0):** an `open`,
   `acknowledged` or `mitigating` risk becomes `resolved` with
   `status_changed_by = NULL` and history `resolved_by_engine`. Risks are never
   deleted by the engine.
6. **Evidence:** upserted on its unique key, with `last_linked_at = now`. At most 50
   rows are kept per risk; the lowest contributions are deleted first.

Alerts are **not** written by the engine. The worker turns `RunResult.changes` into
`biz.alerts` rows and SSE events (§9).

## 4. Categories

| `category` | From events | From anomalies |
|---|---|---|
| `natural_hazard` | `hazard.*`; tag `natural_hazard` | – |
| `logistics` | `port.disruption`; tag `port_shipping_disruption`; chokepoint matches | `portwatch.*` |
| `geopolitical` | tags `conflict`, `protest` | – |
| `sanctions` | `sanction.designation`, `sanction.update`; tag `sanctions` | – |
| `trade_policy` | tag `trade_policy` | – |
| `cyber` | `cyber.exploited_vulnerability`, `cyber.vulnerability`; tag `cyber_incident` | – |
| `financial` | tag `financial_stress` | `fx.rate`, `wb.*` |
| `labour` | tag `labour_action` | – |
| `other` | tag `other` (only via a `company_name` match) | `gdelt.articles` for a workspace's own term |

`sanction.removal` is never positive evidence. It resolves open sanctions risks whose
evidence has the same OFAC `attrs.entity_id`.

## 5. Matching and likelihood

### 5.1 Which keys each entity kind is matched on

| `kind` | Keys |
|---|---|
| `supplier` | position (radius), countries, ports, name (sanctions + company news), hs_codes/commodities, currencies |
| `site` | position, countries, ports |
| `customer` | countries, name (sanctions), currencies |
| `route` | ports (ordered origin → destination), countries, `attrs.chokepoints` (ids, §5.3) |
| `port` | ports, position, countries |
| `currency` | currencies |
| `commodity` | hs_codes, commodities |
| `software` | software (purl / CPE) |

**Position beats country.** For `natural_hazard` and `logistics`, an entity with
`lat`/`lon` is matched by radius only, not by country. Without a position, the
country match is used.

### 5.2 Match rules and confidence `c`

| `match_type` | Rule | `c` |
|---|---|---|
| `geo_radius` | haversine distance `d` ≤ `R`. `R` (km): earthquake `clamp(50 × 2^(M−5), 25, 400)` with `M = attrs.magnitude` (100 if missing); tropical cyclone 300; tsunami 300; drought 400; flood 100; volcano 100; wildfire 50; other hazard 100; `port.disruption` 50 | `1 − d/R` (0.05 at the edge at least) |
| `port` | exact UN/LOCODE in `event.ports` | 1.0 |
| `country` | alpha-3 in `event.countries`, or (tagged news) in `tag.entities.countries` | site 0.8, supplier 0.6, customer 0.5, route/port 0.5; × 0.6 when it came from a news tag |
| `chokepoint` | the route lists the chokepoint, and the event is within its radius, or a tagged place's text equals one of its aliases (casefold) | geo: `1 − d/R`; text: 0.6 × place confidence |
| `currency` | an `fx.rate` anomaly on the entity's currency against the workspace `base_currency`. With base EUR this is the stored pair `EUR/<CCY>`; otherwise the engine builds the cross rate from the two EUR pairs and runs `jobs.analyse.anomalies.detect` on it in memory (cross anomalies are not stored in `feeds`). Without a `base_currency`, `EUR/<CCY>` is used | direct 1.0, cross 0.9 |
| `commodity` | the HS code's chapter (first 2 digits) is in the engine's chapter → keyword table, or `entity.commodities` contains (casefold) a `tag.entities.commodities[].text` or `event.commodities` value | 0.6 × tag confidence (chapter); 0.8 × tag confidence (exact text) |
| `software` | purl `pkg:<type>/<namespace>/<name>@v` → `<OSV ecosystem>:<name>`, with pypi→`PyPI` (PEP 503-normalised name), npm→`npm`, maven→`Maven` (`group:artifact`), golang→`Go`, cargo→`crates.io`, nuget→`NuGet`, gem→`RubyGems`, composer→`Packagist`; exact match with `event.software`. CPE `cpe:2.3:a:<vendor>:<product>:…` → `<vendor>:<product>`, compared casefold with `_` and spaces removed against KEV's `vendorProject:product` | OSV 1.0; KEV 0.9. Versions are not compared (the feeds hold no affected ranges) |
| `sanctions_name` | suppliers and customers only. Normalise both names (NFKD, strip accents, casefold, `&`→`and`, drop punctuation, drop legal-form tokens: ltd, limited, llc, inc, corp, corporation, co, company, gmbh, ag, sa, sas, srl, spa, bv, nv, plc, jsc, pjsc, ooo, oao, zao, llp, lp, pte, pvt, kk, as, ab, oy), then `sim = rapidfuzz.fuzz.token_sort_ratio(a, b) / 100` against `attrs.name` of `sanction.designation`/`sanction.update` events. Match if `sim ≥ 0.92` | `0.7 + 0.3 × (sim − 0.92)/0.08`; then +0.1 if the countries overlap, −0.2 if both have countries and they are disjoint; clamp to 0–1, and drop if below 0.5 |
| `company_name` | suppliers and customers only: same normaliser, `sim ≥ 0.95` against `tag.entities.companies[].text` of tagged news; or a `gdelt.articles` anomaly whose key equals `watch_terms_for` output for this entity | 0.7 × the company's tag confidence; anomaly 0.8 |

Name matching is the only fuzzy match. Every other key is an exact code comparison.

### 5.3 Static reference tables (data files inside `engine/`, public geography)

* **Chokepoints:** `id`, name, aliases, centre and radius (km). Initial set: `suez`,
  `panama`, `hormuz`, `bab_el_mandeb`, `malacca`, `singapore`, `bosporus`,
  `gibraltar`, `dover`, `taiwan_strait`, `cape_of_good_hope`. The API serves this
  table on the `disruptions` map layer.
* **HS chapter → commodity keywords**, e.g. 27 → oil, crude, gas, lng, coal; 72 →
  steel, iron; 74 → copper; 76 → aluminium, aluminum; 85 → semiconductor, chip,
  electronics; 10 → wheat, rice, maize, corn, grain; 12 → soy, soybean; 31 →
  fertiliser, fertilizer, potash; 40 → rubber; 52 → cotton; 09 → coffee, tea;
  18 → cocoa; 17 → sugar.

### 5.4 Signal `s` (0–1) per evidence item

| Evidence | `s` |
|---|---|
| `gdacs`, `usgs`, `portwatch` | `event.severity` (0.5 if NULL) |
| `eonet` | 0.5 for wildfire, volcano, severe_storm, flood; else 0.3 |
| `cisa_kev` | `max(0.9, epss)` |
| `osv` (and `nvd`, when E adds it) | `max(severity or 0.5, epss)`; ignored if `attrs.withdrawn` is set |
| `ofac` | `sanction.designation` 1.0; `sanction.update` 0.8 |
| tagged news (`gdelt`) | `0.25 + 0.5 × type_confidence` (type `other`: 0.15) |
| `robust_z` anomaly | `clamp(0.5 + (\|z\| − 3.5) / 9, 0.5, 1.0)` (3.5 → 0.5, 8 → 1.0) |
| `isolation_forest` anomaly | `0.8 × clamp(score, 0, 1)` |
| `scenario` (§12) | the shock's `severity` |
| any other source | `event.severity` (0.5 if NULL); tagged text events as news |

`epss` is the latest `feeds.series` value of `epss.score` for the event's CVE (the KEV
`raw_id`, or a `CVE-` id in OSV's `attrs.aliases`), 0 if absent. `load_feeds` puts it in
`FeedSnapshot.series` under `('epss.score', <CVE>)` (latest point only), next to the
`fx.rate` history.

### 5.5 Recency `r`

`r = 0.5 ^ (age_hours / half_life_hours)`. `age` is measured from `published_at` (else
`first_seen_at`); for anomalies, from `ts`. An event whose `attrs.to_date` is in the
future counts as age 0 (it is ongoing). Items older than 4 half-lives (at most 365
days) are ignored.

**Standing sanctions listings do not decay.** A full-list entry (type
`sanction.listed`, or a `sanction.designation`/`sanction.update` with
`attrs.full_list: true`) has `r = 1` while its row exists: a listing is in force until it
is removed, and the full-list connector deletes or replaces rows that leave the list.
Delta-file events keep the 720 h half-life.

| Category | Half-life |
|---|---|
| natural_hazard | 72 h |
| logistics | 168 h (7 d) |
| geopolitical, labour, trade_policy, other | 120 h (5 d) |
| sanctions | 720 h (30 d) |
| cyber | KEV and tagged news 720 h; OSV (and NVD) advisories 2,160 h (90 d) |
| financial | fx 120 h; `wb.*` 4,380 h (≈ 6 months) |

### 5.6 Likelihood

Each item contributes `p = s × r × c`, rounded to 3 places. Items for the same
fingerprint are combined by noisy-OR:

```
likelihood = min(0.99, 1 − Π (1 − p_i))
```

The two anomaly methods on the same point are separate items, so agreement raises L.
The items are the (at most) `config.max_evidence` = 50 strongest of the fingerprint, by
`p` then evidence key; L is computed from exactly these, so the stored evidence and
`factors.likelihood.items` reproduce it.

### 5.7 Clarifications (engine 1.0.0)

These make §5.1–§5.6 precise where they left a choice open. None changes a constant.

* **One match per (entity, item).** An item that matches an entity on several keys
  (e.g. a port disruption at the supplier's port *and* within its radius) counts once,
  with its best match: highest `p`, then `c`, then the order port, geo_radius,
  chokepoint, software, sanctions_name, company_name, currency, commodity, country.
  The fingerprint then follows that match.
* **Position beats country only when the event has a position.** For `natural_hazard`
  and `logistics`, a positioned entity is not country-matched to an event that has
  `lat`/`lon`. Country-keyed items without a position (a GDACS row without
  coordinates, `portwatch.*` and `wb.*` anomalies) still match it by country.
* **Which items can match on which keys.** Sanctions rows match only by
  `sanctions_name`, vulnerabilities only by `software`: never by country.
  Hazards and port disruptions match by port, radius, chokepoint and country. Tagged
  text (news) matches by country, company name, commodity and chokepoint place name;
  tag `other` only by company name. An untagged news row is never evidence.
* **News countries.** GDELT file articles now carry `event.countries` (from the
  article's locations). These count like `tag.entities.countries`: × 0.6.
* **`match_value` per type:** `geo_radius` → `"<d> km"` (driver `geo_radius:near`);
  `currency` → the pair, e.g. `EUR/TRY` or `USD/TRY`; `commodity` → `HS <chapter>` or
  the matched text; `software` → the normalised comparable key (`PyPI:jinja2`,
  `microsoft:exchangeserver`); `sanctions_name` → the listed primary name (driver uses
  its normalised form, so `ACME TRADING L.L.C.` and `Acme Trading LLC` are one risk);
  `company_name` → the entity's normalised name.
* **Name normaliser:** after NFKD, `æ ø œ ł đ ð þ ı` are transliterated; apostrophes
  and dots are removed inside words (`S.A.` → `sa`), other punctuation becomes a space;
  if every token is a legal form they are all kept. Names shorter than 3 characters
  after normalisation (spaces ignored) are never fuzzy-matched. Aliases are read from
  `attrs.aliases` as a list of strings or of objects with `name`/`full_name`/`text`.
* **Removals:** a `sanction.removal` with `attrs.entity_id` X drops every positive
  sanctions event of X dated at or before it; a later re-designation counts again.
* **Software:** a KEV/NVD row is compared only as CPE `vendor:product`, an OSV row
  only as `<ecosystem>:<package>` (PyPI names PEP 503-normalised on both sides).
* **Commodity keywords** match the tagged text as whole word(s), casefold; the
  table adds plurals (`soybeans`, `semiconductors`, `chips`).
* **Routes' `attrs.chokepoints`** may hold ids, names or aliases; they are resolved
  with `chokepoint_id`. `bab_el_mandeb` has the alias `red sea`, the name news uses for
  that passage.
* **Supplier spend shares** are computed with EUR as the pivot currency. A share does
  not depend on which common currency is used, so this equals converting to
  `base_currency`, and also works when the workspace has none.
* **Map fields:** `countries`/`ports` are the matched codes when the risk has country
  or port evidence, otherwise the entity's own.

## 6. Impact

```
criticality_weight w = {low: 0.25, medium: 0.5, high: 0.75, critical: 1.0, NULL: 0.5}
exposure_share x     = supplier: annual_spend / Σ annual_spend of the workspace's suppliers
                        (converted to base_currency with the latest fx.rate; if conversion is
                         impossible, only when all suppliers share one spend_currency, else unknown)
                       customer: revenue_share
                       other kinds: unknown
exposure_norm n      = min(1, x / 0.25)          # 25 %+ of spend or revenue is maximal exposure
base                 = w                 if x unknown
                       0.5·w + 0.5·n     otherwise
category_multiplier m = sanctions 1.0, natural_hazard 0.9, cyber 0.9, logistics 0.8,
                        geopolitical 0.7, trade_policy 0.6, financial 0.6, labour 0.6, other 0.4
impact               = clamp(m × base, 0.05, 1.0), rounded to 3 places
```

All constants live in `EngineConfig` (a frozen dataclass). `DEFAULT_CONFIG` holds
exactly the values in this document. A change to any of them is an
`ENGINE_VERSION` bump plus an update to this file.

## 7. Watch terms (the worker, only if `biz.workspaces.share_watch_terms`)

`watch_terms_for(entities)` returns at most:

* **OSV:** one `osv_package` term per distinct purl in a supported ecosystem,
  `(kind='osv_package', term=<package name as OSV expects it>, scope=<ecosystem>)`.
* **GDELT:** up to 5 `gdelt_query` terms for suppliers with criticality `high` or
  `critical` and a normalised name of at least 4 characters: `term = '"<name>"'`,
  `scope = ''`.

The worker inserts them with `origin = 'workspace'` and
`ON CONFLICT (kind, scope, term) DO NOTHING`. It never updates or deletes them (it has
no privilege to). Nothing ties a term to a workspace. GDELT allows about 40 queries
per run in total, which is why only 5 per workspace are allowed.

## 8. Advice (Groq, wording only)

After `write_risks`, the worker asks for advice when `needs_new_advice(...)` is true.
That is when there is no advice yet, the score moved into another 20-point band
(0–20, 20–40, …), or the set of top-5 evidence keys changed. The steps:

1. `advice_messages(risk, entity)` builds the chat messages. They contain the entity
   kind and name, the category, the title and explanation, up to 5 evidence titles
   and dates, and L, I and score as given numbers. Nothing else from the workspace.
   The system prompt says: plain text, at most 120 words, 2–3 concrete mitigation
   steps, no new numbers or probabilities, no links, and do not restate the score.
2. The worker calls Groq (`GROQ_ADVICE_MODEL`), then validates the reply: ≤ 1,200
   characters, no URLs, and no digits other than those in the input.
3. If it passes, the worker sets `advice`, `advice_source = 'groq'`,
   `advice_model` and `advice_generated_at`.
4. If it fails, or Groq is unavailable or rate-limited, the worker stores
   `template_advice(...)` (deterministic per category) with
   `advice_source = 'template'`.

Groq output never changes `likelihood`, `impact`, `score`, `status` or the evidence.

## 9. When it runs (the worker)

* After each feed update: the worker polls `max(feeds.source_health.last_run_at)`
  every 10 minutes, only while it is awake anyway (no long-lived DB connections, so
  Neon can scale to zero).
* After a source sync changed entities, and on demand from the API.
* Per workspace, as listed by `biz.worker_workspace_ids()`. Each workspace runs in its
  own transaction: `set_config('app.workspace_id', …, true)`, then `run_workspace`,
  then commit.
* `RunResult.changes` → alerts. `created` with score ≥ `workspaces.alert_min_score` →
  `risk_new`. A rescore that crosses the threshold upwards → `risk_escalated`. Any
  `created` sanctions risk → `sanctions_match` (critical). Each also becomes an SSE
  `risk.*` / `alert.created` event (see `openapi.yaml`).

## 10. Tests C must ship

* **Unit tests on fixtures:** one per match rule, the noisy-OR, recency decay, impact
  formula, and the fingerprint's stability.
* **A DB test against the throwaway Postgres** (like `tests/test_biz_db.py`): insert
  test entities and feed rows, run `run_workspace` as an rp_app login, and assert the
  rows written. Also check that it finds nothing for another workspace.
* **Determinism:** the same inputs give the same assessments, in the same order.

## 12. Scenarios (`run_scenario`)

A scenario runs the same `assess` twice on one feed snapshot: as is (baseline) and with
the inputs applied. It writes nothing; the API stores the result in `biz.scenarios`.

* `HazardShock` → a hypothetical event (`source = 'scenario'`, `type =
  'scenario.<category>'`, published `now`, signal = `severity`). It matches by port,
  country, chokepoint ids (`c = 1`) and position (`R = radius_km`, default 100 km); as
  for real events, a positioned entity skips the country match only when the shock
  itself has `lat`/`lon`. "Port NLRTM closed" is
  `{"type": "event", "category": "logistics", "severity": 1, "ports": ["NLRTM"]}`.
* `FxShock` → a hypothetical `robust_z` anomaly on `<base>/<currency>` at `now`,
  `z = ln(1 + pct/100) / spread`, where `spread` is the robust spread
  (1.4826 × MAD) of the pair's daily log returns over the last 90 days, the way stored
  anomalies are scored. `|z| < 3.5` adds nothing. No recent history → `ScenarioError`.
* `SanctionsShock` → a hypothetical `sanction.designation` with the entity's own name
  and countries, screened like any OFAC row.
* `entity_overrides` change or drop entities before scoring; dropping a supplier also
  changes the other suppliers' spend shares.
* `changes` lists fingerprints whose score differs (by at least 0.1), largest absolute
  delta first, at most 200, with `risk_id` when a stored risk has that fingerprint.
  `portfolio_score = 100 × (1 − Π(1 − score_i/100))` over each side's assessments.

## 13. Write details (engine 1.0.0)

* Risk ids are `uuid5(<fixed namespace>, "<workspace_id>|<fingerprint>")`, so a
  `RiskChange.risk_id` is known before the insert.
* `run_workspace` takes `pg_advisory_xact_lock` on the workspace, so two runs of one
  workspace queue rather than race. It also checks that `app.workspace_id` equals the
  `workspace_id` argument (`EngineContextError` otherwise).
* A `resolved` change has `new_score = None`. A dismissed risk that stays dismissed is
  still rescored (numbers, `last_seen_at`, history `rescored` on a move of ≥ 1.0).
* The evidence trim keeps the rows linked in this run first, then the highest
  contributions. Rows that are no longer linked stay (with their old `last_linked_at`)
  until the trim removes them, so "never linked before" (§3, rule 3) has a memory.
* Updates are skipped when nothing differs, so a re-run with the same data and the same
  `now` changes no row. `RunResult.counts` also has `invalid_keys`: entity key values
  `jobs.codes` could not normalise (dropped, not guessed).

## 11. Open items

* `ofac` stores only the latest delta file. Complete sanctions screening needs a
  full-list connector for session E (OFAC SDN + consolidated list, with aliases). It
  should store one event per listed entity, with `attrs.name`, `attrs.aliases` and
  `attrs.entity_id`, and type `sanction.listed` (or `attrs.full_list: true`) so it does
  not decay (§5.5). The matcher already reads every `sanction.*` row of any source and
  compares aliases, so nothing in the engine changes when those rows arrive.
* PortWatch's per-chokepoint daily layer needs an ArcGIS token, so chokepoints are
  matched by radius and tagged text only, until E finds a keyless source.
