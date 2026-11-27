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
    city_position, port_name, place_label,  # §5.11: the mapper's city positions, display names
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
              # since engine 1.1.0, filled by run_workspace only (empty from write_risks):
              assessments: tuple[Assessment, ...]  # the assessments written (score ≥ min_score)
              entities: tuple[Entity, ...]         # the entities they were scored from
```

The worker calls `run_workspace` (§9) and takes the assessments and entities for the
advice and watch-term steps from its `RunResult`, so it never loads or scores twice and
always runs under the per-workspace lock (§13).

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
| `explanation` | deterministic text (no AI): one plain sentence saying what reached the entity and through what, e.g. "3 recent news reports describe sanctions aimed at China, and one mentions circuit board, which is in the same product group (HS 8534) as the printed circuit boards Pearl River PCB Works supplies." The numbers are in `factors.explain` (engine 1.2.0) |
| `likelihood`, `impact` | §5, §6 |
| `factors` | `RiskFactors`, below |
| `countries`, `ports`, `lat`, `lon` | for the map: the entity's position if it has one, else the matched country/port |
| `status` | lifecycle below |
| `engine_version` | `ENGINE_VERSION` |

`RiskFactors` (stored as jsonb, returned by the API as is):

```json
{
  "likelihood": {"method": "noisy_or", "value": 0.62, "decay": 0.5,
                 "items": [{"evidence_key": "event:usgs:us6000abcd", "signal": 0.71, "recency": 0.93,
                            "match_confidence": 0.88, "contribution": 0.58, "weight": 1.0}]},
  "impact": {"value": 0.68, "criticality": "high", "criticality_weight": 0.75,
             "exposure_share": 0.12, "exposure_norm": 0.48, "category_multiplier": 0.9},
  "engine_version": "1.3.2",
  "story": "natural_hazard|event:usgs:us6000abcd",
  "explain": {"matched": {"match_type": "geo_radius", "match_value": "12 km", "label": "distance: 12 km",
                          "matched_text": null, "inferred_from": null, "supported_by": null},
              "strongest": {"evidence_key": "event:usgs:us6000abcd", "title": "M 6.1 …", "source": "usgs",
                            "published_at": "2026-11-20T03:12:00Z"},
              "item_count": 1,
              "steps": [{"label": "Strongest signal", "formula": "signal 0.710 × recency 0.930 × match 0.880 = 0.581", "value": 0.581},
                        {"label": "Likelihood", "formula": "noisy-OR of 1 item: 1 − (1 − 0.581) = 0.581", "value": 0.581},
                        {"label": "Impact", "formula": "0.9 (natural hazard) × …", "value": 0.68},
                        {"label": "Score", "formula": "100 × 0.581 × 0.680 = 39.5", "value": 39.5}]}
}
```

`story` and `explain` are new in engine 1.2.0 (§5.9); `likelihood.decay` and each item's
`weight` in 1.3.0 (§5.6); `likelihood.capped_from` appears only on a capped market-only risk.
With more than one item the "Likelihood" step reads e.g. `noisy-OR of 3 items, each further
item counting 0.5× the one before: 1 − (1 − 0.351) × (1 − 0.5 × 0.330) × (1 − 0.25 × 0.200) = 0.485`.
A risk written by an older engine has none of these until it is re-scored, which the
worker does on its next run (§9).

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
generated (e.g. `"EUR/TRY moved 4.1 robust z from its 90-day norm"`). Since engine 1.3.0
a news item's `country` evidence also has `central`: `"title"` (the country is named in
the title or its tag) or `"first_place"` (the first place located in the article), §5.10.

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
`biz.alerts` rows and live-update events (§9).

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
| `supplier` | position (radius), countries, ports, name (sanctions + company news), hs_codes/commodities (support only, §5.9), currencies (market moves only, §5.9), chokepoints (inferred, §5.9) |
| `site` | position, countries, ports, chokepoints (inferred, §5.9) |
| `customer` | countries, name (sanctions), currencies (market moves only, §5.9) |
| `route` | ports (ordered origin → destination), countries, `attrs.chokepoints` (ids, §5.3) |
| `port` | ports, position, countries, chokepoints (inferred, §5.9) |
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
| `country` | alpha-3 in `event.countries`, or (tagged news) in `tag.entities.countries`. For news (1.3.0) only a country **central** to the item, and for sanctions and cyber news only under the extra conditions of §5.10 | site 0.8, supplier 0.6, customer 0.5, route/port 0.5; × 0.6 when it came from a news tag |
| `chokepoint` | the route lists the chokepoint (or, for a supplier, site or port, it is inferred, §5.9), and the event is within its radius, or a tagged place's text equals one of its aliases (casefold) | geo: `1 − d/R`; text: 0.6 × place confidence; × 0.5 when inferred |
| `currency` | an `fx.rate` anomaly on the entity's currency against the workspace `base_currency`. With base EUR this is the stored pair `EUR/<CCY>`; otherwise the engine builds the cross rate from the two EUR pairs and runs `jobs.analyse.anomalies.detect` on it in memory (cross anomalies are not stored in `feeds`). Without a `base_currency`, `EUR/<CCY>` is used | direct 1.0, cross 0.9 |
| `commodity` | a keyword of the entity's HS code is in a `tag.entities.commodities[].text` or `event.commodities` value (whole words, casefold): since 1.3.0 the keywords of its **4-digit heading** for a 4- or 6-digit code, and of its 2-digit chapter only for a code given as 2 digits (§5.3); or `entity.commodities` contains (casefold) that text. Support only (§5.9): never a risk on its own for an entity with a place | 0.6 × tag confidence (HS keyword); 0.8 × tag confidence (exact text) |
| `software` | purl `pkg:<type>/<namespace>/<name>@v` → `<OSV ecosystem>:<name>`, with pypi→`PyPI` (PEP 503-normalised name), npm→`npm`, maven→`Maven` (`group:artifact`), golang→`Go`, cargo→`crates.io`, nuget→`NuGet`, gem→`RubyGems`, composer→`Packagist`; exact match with `event.software`. CPE `cpe:2.3:a:<vendor>:<product>:…` → `<vendor>:<product>`, compared casefold with `_` and spaces removed against KEV's `vendorProject:product` | OSV 1.0; KEV 0.9. Versions are not compared (the feeds hold no affected ranges) |
| `sanctions_name` | suppliers and customers only. Normalise both names (NFKD, strip accents, casefold, `&`→`and`, drop punctuation, drop legal-form tokens: ltd, limited, llc, inc, corp, corporation, co, company, gmbh, ag, sa, sas, srl, spa, bv, nv, plc, jsc, pjsc, ooo, oao, zao, llp, lp, pte, pvt, kk, as, ab, oy), then `sim = rapidfuzz.fuzz.token_sort_ratio(a, b) / 100` against `attrs.name` (and `attrs.aliases`) of `sanction.designation`/`sanction.update` events, and against `name` and `aliases` of every `feeds.sanctions_entries` row (§5.8). Match if `sim ≥ 0.92` | `0.7 + 0.3 × (sim − 0.92)/0.08`; then +0.1 if the countries overlap, −0.2 if both have countries and they are disjoint; clamp to 0–1, and drop if below 0.5 |
| `company_name` | suppliers and customers only: same normaliser, `sim ≥ 0.95` against `tag.entities.companies[].text` of tagged news; or a `gdelt.articles` anomaly whose key equals `watch_terms_for` output for this entity | 0.7 × the company's tag confidence; anomaly 0.8 |

Name matching is the only fuzzy match. Every other key is an exact code comparison.

### 5.3 Static reference tables (data files inside `engine/`, public geography)

* **Chokepoints:** `id`, name, aliases, centre and radius (km). Initial set: `suez`,
  `panama`, `hormuz`, `bab_el_mandeb`, `malacca`, `singapore`, `bosporus`,
  `gibraltar`, `dover`, `taiwan_strait`, `cape_of_good_hope`. The API serves this
  table on the `disruptions` map layer.
* **HS → commodity keywords** (`engine/data/hs_chapters.json`). `headings` (4 digits,
  engine 1.3.0) are what a 4- or 6-digit code is matched on, e.g. 8534 → printed circuit
  board(s), circuit board(s), pcb; 8542 → chip(s), semiconductor(s), integrated
  circuit(s), microcontroller(s), processor(s), memory chip(s); 8504 → transformer(s),
  power supply/supplies, power adapter(s), charger(s), inverter(s); 8544 → cable(s),
  wire(s), harness(es); 9031 → sensor(s), measuring instrument(s); 7410 → copper, copper
  foil; 2709 → crude, crude oil, oil. A heading not in the table matches only the
  entity's own product names. `chapters` (2 digits) are used only for an entity that
  gives a 2-digit code alone: 27 → oil, crude, gas, lng, coal; 72 → steel, iron; 74 →
  copper; 76 → aluminium, aluminum; 85 → semiconductor, chip, electronics; 10 → wheat,
  rice, maize, corn, grain; 12 → soy, soybean; 31 → fertiliser, fertilizer, potash; 40 →
  rubber; 52 → cotton; 09 → coffee, tea; 18 → cocoa; 17 → sugar. (Engine ≤ 1.2.0 matched
  every code on its chapter, so "AI chips" strengthened a printed-circuit-board maker,
  HS 8534, and the sentence read "AI chips like those … supplies (HS 8534)".)
* **Country names in titles** (`engine/data/country_terms.json`, 1.3.0): the ISO names
  from pycountry plus everyday aliases, adjectives ("Chinese") and capitals used for the
  government ("Beijing"), and phrases that are not the country ("South China Sea"); §5.10.

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
fingerprint are combined by a noisy-OR in which each further item counts less (engine
1.3.0). With the contributions sorted strongest first, `p_1 ≥ p_2 ≥ …`:

```
likelihood = min(0.99, 1 − Π_k (1 − w_k × p_k)),   w_k = decay^(k − 1),   decay = 0.5
```

The strongest item counts in full, so a risk with one item scores exactly as in 1.2.0;
the second counts half, the third a quarter, and so on. Because `L ≤ Σ w_k p_k` and the
weights sum to less than `1 / (1 − decay) = 2`, the likelihood is always **below twice
the strongest contribution**: many weak items corroborate, but cannot add up to
"likely". Engine ≤ 1.2.0 used the plain noisy-OR (`decay = 1`), under which seven weak
country matches of about 0.3 each read "92% likely"; now they give 0.49 (live test of
27 Nov: "89% likely" for a China sanctions risk built from loosely related articles).
`factors.likelihood` keeps `method: "noisy_or"`, and adds `decay` and each item's
`weight`. `EngineConfig.likelihood_decay` (`engine/config.py`); 1.0 restores the plain
noisy-OR.

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
  article's locations). These count like `tag.entities.countries`: × 0.6 — since 1.3.0
  only when the country is central to the article (§5.10).
* **`match_value` per type:** `geo_radius` → `"<d> km"` (driver `geo_radius:near`);
  `currency` → the pair, e.g. `EUR/TRY` or `USD/TRY`; `commodity` → `HS <heading>` (`HS <chapter>` for a 2-digit code, 1.3.0) or
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

### 5.8 Full sanctions lists (engine 1.1.0)

`feeds.sanctions_entries` (migration 030, `feeds-schema.md`) holds every entity
currently on OFAC's SDN and consolidated lists. The engine screens suppliers and
customers against it exactly like `sanction.*` events (§5.2 `sanctions_name`, same
threshold, confidence and country adjustment), as if each row were an event:

| Event field | From the row |
|---|---|
| `source`, `raw_id` | `source` (`ofac_sdn`), `<dataset>:<entity_id>`, e.g. evidence key `event:ofac_sdn:sdn:36001` |
| `type` | `sanction.listed`: a standing listing, `s = 1.0` and `r = 1` while the row exists (§5.5) |
| `title` | `OFAC SDN list: <name> [<programs>]` (`OFAC consolidated list: …` for that dataset) |
| `published_at`, `first_seen_at` | `NULL`, the row's `first_seen_at` |
| `countries` | `countries` |
| `attrs` | `entity_id`, `entity_type`, `name`, `aliases`, `programs`, `lists`, `dataset`, `listed_on`, `full_list: true` |

* **Names compared:** `name` and every entry of `aliases` (all scripts and
  translations). `weak_aliases` are not read: OFAC flags them as too low quality to
  screen on.
* **Removals:** a `sanction.removal` delta event for the same `entity_id` published after
  the row's `first_seen_at` cancels it (the delta arrives hourly, the full list daily).
  The next complete read deletes the row; if the entity is listed again, the new row's
  `first_seen_at` is later than the removal and it counts again.
* **One risk per listed name:** the driver is the normalised primary name, so a delta
  designation and the full-list row of the same entity are two evidence items of one
  risk. When a delisted row is deleted its evidence goes and the risk is resolved;
  `still_in_feeds` in the API then reads false.
* **Load bound and caching:** at most `max_sanctions_entries` = 50,000 rows are read
  (the lists are ~18k), in `(source, dataset, entity_id)` order. The rows are read once
  per worker process and re-read only when `(count, max(updated_at))` of either the
  `sanction.*` events or `sanctions_entries` changes (one stamp query per run). The name
  index built from them is reused while the same rows are in use. Both caches give
  results identical to a fresh read.

### 5.9 Anchors, market moves, inferred chokepoints and stories (engine 1.2.0)

The first live end-to-end test (11 suppliers, 77 risks, top score 64) showed matching
that was too broad: a news story about chip export rules and China became a High
*sanctions* risk for a Malaysian supplier because the story mentioned chips (HS chapter
85), and the same story was a separate risk for every supplier in that chapter.

* **Anchors and support.** A `commodity` (HS chapter or text) or `currency` match is
  *support*. For an entity with a place (supplier, customer, site, route, port) it never
  creates a risk on its own. It only strengthens an *anchor* match of the **same item**
  to the same entity: country, port, position (`geo_radius`), chokepoint, company name or
  sanctions name. Then `c = 1 − (1 − c_anchor)(1 − w × c_support)` with
  `w = support_weight = 0.5`, and the evidence snapshot keeps `supported_by`.
  A `commodity` or `currency` entity has no other key: for it that match is the anchor.
* **Market-wide moves.** An `fx.rate` anomaly, or an event whose type starts with
  `market.` (a commodity price move from a markets feed; no connector emits one yet), is
  global for that currency or commodity, so it may create a risk on its own. If *every*
  item of a risk is such a move, the risk is capped below High: its likelihood is lowered
  (rounded down to 3 places) until `100 × L × I ≤ market_only_score_cap = 50`, and
  `factors.likelihood.capped_from` keeps the value before the cap.
* **Inferred chokepoints.** A supplier, site or port with no `attrs.chokepoints` gets
  chokepoints inferred (declared ones always win; routes are never inferred):
  * from the **main sea lanes** of its first port's country (from the LOCODE's first two
    letters), or of its first country when it has no port, in
    `engine/data/sea_lanes.json`: East Asia (CHN, TWN, HKG, MAC, KOR, PRK, JPN) → Taiwan
    Strait, Singapore Strait, Malacca, Bab el-Mandeb, Suez, Panama; Southeast Asia (VNM,
    THA, KHM, PHL, MYS, SGP, IDN, BRN) → the same without the Taiwan Strait; South Asia
    (IND, PAK, BGD, LKA, MDV, MMR) → Bab el-Mandeb, Suez; Persian Gulf (ARE, QAT, KWT,
    BHR, IRQ, IRN) → Hormuz, Bab el-Mandeb, Suez; west coast of South America (CHL, PER,
    ECU) → Panama. These are the passages on the usual lanes from that region to Europe
    and the Americas. The table is coarse on purpose and lists nothing for Europe, North
    America or any country with coasts on two oceans (USA, CAN, MEX, COL, SAU, EGY, TUR,
    RUS, ZAF, …): without port coordinates the engine cannot tell the coast, so it does
    not guess;
  * and from its **position**: any chokepoint whose radius holds the entity's `lat`/`lon`.

  An inferred chokepoint matches like a declared one (radius, tagged place name, scenario
  shock) at `inferred_chokepoint_factor = 0.5` of the confidence, and the evidence
  snapshot and `factors.explain.matched` carry `inferred_from` (`port TWKHH`,
  `country VNM` or `position`), so the dashboard says "inferred from the port".
* **Stories.** `factors.story = "<category>|<evidence key of the strongest item>"`. One
  news report (or hazard, or listing) that reaches several entities gives each of their
  risks the same key. Risks stay one per entity (the fingerprint is unchanged), and the
  API groups them: `Risk.story`, `GET /v1/risks?group=story`, `RiskSummary.story_count`
  and `stories_by_category`, and `story_count` on the map's risk places.

All of these constants live in `EngineConfig` (`engine/config.py`, the "anchoring"
block): `support_match_types`, `support_weight`, `market_event_prefix`,
`market_only_score_cap`, `inferred_chokepoint_factor`.

### 5.10 A news country must be what the article is about (engine 1.3.0)

The live test of 27 Nov (11 suppliers, 41 open stories) still let in loosely related
articles that only mention a country somewhere in their text: "Sanctions news in China
affecting Pearl River PCB Works" (score 49, "89% likely") cited an opinion piece on
European leaders and an Ethiopian ministry page, each as "GDELT · same country: China";
a FIFA data breach and a global ransomware report were "Cyber incident news in Germany"
for a German sensor maker. GDELT's `countries` are every place it located in the full
article, so a passing mention counted like a headline. Implemented in `engine/relevance.py`.

* **Central country.** A news item (`gdelt`, `newsdata`, `currents`: any tagged text
  that is not a hazard, port disruption, vulnerability or sanctions listing) matches a
  country only when the country is central to it:
  * it is **named in the title** — by an ISO name or alias (`country_terms.json`;
    all-capital aliases such as `US`, `UK` match only in capitals), its adjective
    ("Chinese chipmakers", "German sensor maker") or its capital standing for the
    government ("Beijing protests…"); or it is in `tag.entities.countries` (the tag is the
    model's reading of the same title). A country named only by its adjective becomes a
    candidate even when the feed row has no `countries` (DOC API, NewsData, Currents); or
  * it is the **first place** GDELT located in the article (`event.countries[0]`; the
    feed keeps the locations first mention first). The stored rows have no mention
    counts, so "most-mentioned" cannot be told apart and the first place stands for it.

  Any other country of `event.countries` is a passing mention and does not count. Events
  that carry their own location (GDACS, USGS, PortWatch, scenario shocks) are unchanged:
  their `countries` are where the event is. The country evidence's snapshot says which
  rule applied: `central: "title" | "first_place"`.
* **Sanctions news** (tag `sanctions`) additionally needs the country to be the
  **target** of the measure, read from the title within `sanctions_target_window = 5`
  words that never cross `; : | ? !`:
  `<sanctions word> [on|against|targeting …] <country>` ("sanctions on Chinese
  chipmakers", "export curbs on China"; a verb form may take it directly: "US sanctions
  Chinese firms"); `<country> … to <sanctions word>` with a placing verb (add, put,
  place, move) before the country or right before "to", or the sanctions word straight
  after "to" (articles and the list's owner aside): "adds Chinese circuit board makers to
  export blacklist", "China added to US entity list" (1.3.1: never an infinitive, so in
  "The Netherlands and China plan to coordinate export controls" neither is a target, and
  in "China to blacklist US defence firms" China is not); `<country> hit with / faces /
  protests … <sanctions word>`
  ("China hit with EU sanctions", "Beijing protests new US sanctions"; a country between
  the two is the imposer); `<country> [one or two words] <sanctions word>` ("Iran oil
  sanctions", "North Korea sanctions evasion"); `<country> sanctioned / blacklisted`.
  A country followed by an imposing verb ("US imposes", "Washington adds") or preceded by
  "under" / "by" is the actor, not the target; since 1.3.1 so is every country of a list
  joined by "and", "or" or commas before such a verb ("The Netherlands and China plan …"),
  and a country after "on", "against", "of", "to" … is never read as the subject of the
  verb after it ("Sanctions on Russia widen" targets Russia). So "EU agrees new sanctions package
  against Russia" reaches a Russian supplier and not a Chinese one, even when GDELT
  located China in the text. Sanctions **name** screening (`sanctions_name`, §5.8) is a
  separate rule and is unchanged.
* **Cyber news** (tag `cyber_incident`) matched on a country additionally needs either a
  **national-scale** incident in the title (government, ministry, military, critical
  infrastructure, power grid, energy, rail, airport, port, hospital, telecom, nationwide,
  outage, blackout, state-sponsored, …) or the **supplier's own sector**: a content word
  of its products / commodities, of its HS heading's keywords, or of its notes (generic
  words such as "parts", "industrial", "supplier" are left out; plurals folded). "Germany
  hit by nationwide cyberattack on rail network" reaches every German supplier;
  "Ransomware attack halts production at German sensor maker" reaches the sensor maker
  only; a FIFA breach, a global ransomware statistic or a retailer's data leak reaches
  none. Company-name and software matches are unchanged.

Every other category needs only a central country. The numbers did not change
(`country_confidence`, `news_country_factor`); only which countries count. Measured on
the constructed copy of the 27 Nov sheet (`tests/fixtures/engine_live_nov27_feeds.json`,
`tests/test_engine_relevance.py`): 12 risks → 10, highest likelihood 0.957 → 0.458, risks
at 0.8 or more 1 → 0, and none of the 13 passing-mention articles reaches a supplier,
while every positive control does.

### 5.11 Places: city positions and port names

* **Position precedence** for an entity: the row's own `lat`/`lon`; else its **city**,
  from `engine/data/cities15000.tsv.gz` (GeoNames `cities15000`, CC BY 4.0, 34,006 cities);
  else (on the map only) its first port, else its first country. The mapper
  (`api/core/mapping.py`) applies it when it reads a row, so `biz.entities.lat/lon` hold the
  city and the engine's radius matching (§5.2 `geo_radius`, "position beats country") and
  inferred chokepoints (§5.9) use it. Ports are unchanged: port, chokepoint and lane matching
  still use the entity's `ports`.
* `city_position(city, countries) -> City | None`: the city is matched **only within the
  given countries** (alpha-3 or alpha-2; the row's countries, or its ports' countries when it
  names none), in order, on its GeoNames name or a Latin alternate name, compared as match
  keys (accents stripped, casefold, punctuation as spaces). Also tried: the text before a
  comma or bracket, and without a trailing "City"/"-si"/"-shi". A city's own name beats
  another city's alternate name; then the larger population. A name not found in those
  countries gives `None` (it is never looked up elsewhere).
* `port_name(locode) -> str | None`: the dashboard's display name (`web/src/map/geo.ts`):
  the UN/LOCODE name, else the World Port Index name, cleaned (parenthesised names, a region
  after a comma, and the suffixes Ko/Kou/Hsien/Pt dropped): `TWKHH` → Kaohsiung, `MYPEN` →
  Penang, `AEJEA` → Jebel Ali, `CLSAI` → San Antonio, `VNHPH` → Haiphong
  (`engine/data/ports.tsv`, built from `web/public/map/ports.json`). Titles and sentences
  name ports this way ("at the port of Kaohsiung"); `match_value` and `inferred_from` keep
  the code. Since 1.3.2 the table also has the standard codes of the busiest ports that the
  WPI files under its own codes (`CNSHA` → Shanghai, `SGSIN` → Singapore, `CNSZX` →
  Shenzhen; `PORT_ALIASES` in `web/scripts/build-map-data.mjs`), so their texts name the
  port, not the code. A name the code list gives an airport ("… International Apt") is
  never used.
* `place_label(city, countries, ports) -> str | None`: `Hsinchu, Taiwan` with a city, else
  the first port's name and up to two countries (`Kaohsiung, Taiwan`). The API serves it
  as `Entity.place` and `Risk.place`.

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
2. The worker calls Groq (`GROQ_ADVICE_MODEL`, default `openai/gpt-oss-20b`; a reasoning
   model, called with `reasoning_effort = "low"`, `include_reasoning = false` and a
   1,000-token completion budget so the reasoning cannot use up the answer), then
   validates the reply: ≤ 1,200 characters, no URLs, and no digits other than those in
   the input. An empty reply is a fallback with reason `empty reply (finish_reason=…)`.
3. If it passes, the worker sets `advice`, `advice_source = 'groq'`,
   `advice_model` and `advice_generated_at`.
4. If it fails, or Groq is unavailable or rate-limited, the worker stores
   `template_advice(...)` (deterministic per category × entity kind: a supplier or
   customer is asked, a site, route, port, currency, commodity or software gets advice
   for that kind of thing, and only software is told to patch; a sanctions risk driven
   by a list name match gets the compliance-review text) with
   `advice_source = 'template'`.

Groq output never changes `likelihood`, `impact`, `score`, `status` or the evidence.

## 9. When it runs (the worker)

* After each feed update: the worker polls `max(feeds.source_health.last_run_at)`
  every 10 minutes, only while it is awake anyway (no long-lived DB connections, so
  Neon can scale to zero).
* After a source sync changed entities, and on demand from the API.
* When a workspace has an open, acknowledged or mitigating risk whose `engine_version`
  differs from the running engine's: a new `ENGINE_VERSION` re-scores every workspace on
  the worker's next run, without waiting for new feed rows.
* Per workspace, as listed by `biz.worker_workspace_ids()`. Each workspace runs in its
  own transaction: `set_config('app.workspace_id', …, true)`, then `run_workspace`,
  then commit. Advice and watch terms use `RunResult.assessments` / `.entities` (§2).
* `RunResult.changes` → alerts. `created` with score ≥ `workspaces.alert_min_score` →
  `risk_new`. A rescore that crosses the threshold upwards → `risk_escalated`. A
  `created` sanctions risk whose driver is a sanctions-list **name match** (fingerprint
  `<entity>|sanctions|sanctions_name:…`) → `sanctions_match` (critical, "Possible
  sanctions match"); sanctions *news* about a country only gets `risk_new` /
  `risk_escalated` like any risk. Each also becomes a live-update `risk.*` /
  `alert.created` event (see `openapi.yaml`).
* After the alerts, every pass marks read the workspace's unread alerts whose risk is now
  `resolved` or `dismissed`, and `sanctions_match` alerts whose risk is not a name match
  (raised by the earlier rule). `Alert.risk_status` carries the risk's current status.

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
* `portfolio_score` is the **risk index**, computed for each side over that side's
  entities (the scenario side after `entity_overrides`), rounded to 0.1:

  ```
  per entity e, category c:  s_1 ≥ s_2 ≥ … ≥ s_n = scores of e's risks in c
                             C_c(e) = 100 × (1 − Π_k (1 − s_k/100)^(2^−(k−1)))     (0 if none)
  exposure                   E(e)   = 100 × (1 − Π_c (1 − C_c(e)/100))
  index                      = Σ_e w(e) · E(e) / Σ_e w(e)    over all entities; w = criticality weight (§6; NULL 0.5)
  ```

  Within a category the highest risk counts in full, the next at half weight, the next at
  a quarter, and so on: more risks of one kind add, but all of them together add less
  than one more copy of the highest (`C_c < 100 × (1 − (1 − s_1/100)²)`), so the index
  does not saturate with many risks. Different kinds combine by noisy-OR. Every entity
  counts (0 when no risk touches it). Consequences:
  * **A scenario that adds or raises any risk, and lowers none, strictly raises the index**
    (before rounding; unless that entity is already at 100). In `−ln(1 − s/100)` terms
    `C_c` is a sum with positive weights that shrink by rank: raising a score raises it,
    and inserting a score `x > 0` at rank `k` of `n` adds at least `x × w_(n+1) > 0`
    (the risks pushed down a rank lose `Σ (w_i − w_(i+1)) x_i ≤ x (w_k − w_(n+1))`).
    Engine 1.2.0 took the worst score per category, so a new shipping risk below an
    existing one added nothing ("Suez Canal closes: 62 → 62").
  * **The index is never above the most exposed entity's exposure.** Each side also
    reports `most_exposed` (`entity_id`, `entity_name`, `exposure` = max `E`, its
    `risk_count`), and the What-if panel shows it next to the index: the index is in a
    level band because an entity's risks *combined* are, even when no single risk is
    (several Medium risks of different kinds give a High exposure).
  * With one entity and one risk it equals that risk's score.

  Each side also has `story_count`: distinct `factors.story` keys (a risk without one is
  its own story), the same grouping as `Risk.story.key`. Headline counts are stories;
  `risk_count` (one per entity × story) is secondary. Results stored before these
  fields were added lack them. Engine 1.1.0 used `100 × (1 − Π(1 − score_i/100))` over
  all risks, which saturated at 100 with many risks (77 risks → "100 → 100").

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

* Done in engine 1.1.0: the full OFAC lists arrived as their own table,
  `feeds.sanctions_entries`, rather than as `sanction.listed` events, and are screened
  as described in §5.8. Entity types, birth dates and identifiers are still not compared,
  so a match is a lead for compliance to review, not a verdict.
* PortWatch's per-chokepoint daily layer needs an ArcGIS token, so chokepoints are
  matched by radius and tagged text only, until E finds a keyless source.
