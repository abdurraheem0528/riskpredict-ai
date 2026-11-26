# engine

The risk engine. It reads a workspace's entities (`biz.entities`) and the public feeds
(`feeds.events`, `event_tags`, `anomalies`, `series`), and writes `biz.risks`,
`risk_evidence` and `risk_history`. The contract is
[`docs/contracts/risk-engine.md`](../docs/contracts/risk-engine.md). This page explains
each rule with an example and gives the runtime to expect.

**Deterministic.** `score = 100 × likelihood × impact`, where every number comes from
the fixed rules below (`engine/config.py` holds them all). There is no LLM, no
randomness and no network call, and the same inputs give the same assessments in the
same order. The database computes `score` as a generated column; the engine writes only
`likelihood` and `impact`.

## Using it

```python
import engine

# the worker, one transaction per workspace (decision 3)
with conn.transaction():
    conn.execute("SELECT set_config('app.workspace_id', %s, true)", (str(ws),))
    result = engine.run_workspace(conn, ws)        # RunResult: counts + changes → alerts
    for c in result.changes:                       # created / rescored / resolved / reopened
        ...

# the API, POST /v1/scenarios (reads only)
results = engine.run_scenario(conn, ws, request_json["inputs"])  # ScenarioResults dict
```

`engine.assess(entities, feeds, base_currency=...)` is the pure core, and is what the
tests use. `engine.watch_terms_for`, `advice_messages`, `template_advice` and
`needs_new_advice` are the worker's helpers (§7–§8). `engine.chokepoints()` is the
static table that the API serves on the disruptions layer.

Install with `pip install -r engine/requirements.txt`: the analysis requirements plus
`rapidfuzz` (MIT).

## The rules, with examples

Every example below is real engine output for test-only inputs (`tests/`).

### 1. Match: which feed item concerns which entity (contract §5.1–§5.2, §5.7)

Each entity kind is matched on its own keys only. A supplier is matched on its
position, countries, ports, name, HS codes and commodities, and currencies; a site on
its position, countries and ports; a software entity on purl or CPE only. Every
comparison is an exact code match except names, which are fuzzy. Each (entity, item)
pair counts once, with its best match.

**Anchors (engine 1.2.0, contract §5.9).** A commodity, HS-chapter or currency match
never makes a risk on its own for an entity with a place: it only strengthens a match of
the same item on a country, port, position, chokepoint, company name or sanctions name.
So a story about chip export rules and China reaches the supplier *in* China (and says
"chips like those it supplies"), not every supplier of HS chapter 85. Market-wide moves
(an `fx.rate` flag, a `market.*` event) may make a risk alone, capped at score 50.
Suppliers, sites and ports get chokepoints inferred from their port's country
(`engine/data/sea_lanes.json`) or position, at half a declared route's confidence. One
feed item that reaches several entities shares `factors.story`, so the API can show it
once.

| Rule | Example | `c` |
|---|---|---|
| `geo_radius` | Earthquake M6.5 (R = 50 × 2^1.5 = 141 km) 15 km from *Osaka plant* | 1 − 15/141 = **0.892** |
| `country` | GDACS flood in TUR, site without a position in TUR | **0.8** (supplier 0.6, customer 0.5, route/port 0.5; × 0.6 from news) |
| position beats country | M6 quake in Hokkaido, plant near Tokyo with lat/lon: 1,000 km away | no match |
| `port` | PortWatch disruption listing NLRTM, route CNSHA → NLRTM | **1.0** |
| `chokepoint` | Cyclone inside the Strait of Malacca radius, route via `malacca`; news place "Suez Canal" (conf 0.8) | 1 − d/R; 0.6 × 0.8 = **0.48** |
| `chokepoint` (inferred) | supplier with port TWKHH (East Asia lane: Taiwan Strait, Singapore, Malacca, Bab el-Mandeb, Suez, Panama), news place "Red Sea" (0.9) | 0.6 × 0.9 × 0.5 = **0.27** |
| `currency` | `EUR/TRY` robust-z flag, workspace base EUR; base USD → in-memory `USD/TRY` from the two EUR pairs | **1.0**; cross **0.9** |
| `commodity` (support) | supplier in CHL with HS 7403, news on Chile (country 0.6 × 0.6 = 0.36) tagged "refined copper" (0.6 × 0.9 = 0.54) | 1 − (1 − 0.36)(1 − 0.5 × 0.54) = **0.533**; without the Chile mention: **no risk** |
| `software` | `pkg:pypi/Requests@2.31.0` → `PyPI:requests` = OSV row; CPE `microsoft:exchange_server` = KEV `Microsoft:Exchange Server` | **1.0**; **0.9** |
| `sanctions_name` | supplier "Acme Trading LLC" vs OFAC "ACME TRADING L.L.C." (both → `acme trading`, sim 1.0) | 0.7 + 0.3 × (1 − 0.92)/0.08 = **1.0** |
| `company_name` | news company "Nordwind Logistik" (0.8) vs supplier "Nordwind Logistik GmbH" (sim 1.0 ≥ 0.95) | 0.7 × 0.8 = **0.56** |
| `company_name` (volume) | `gdelt.articles` anomaly on the watch term `"nordwind logistik"` | **0.8** |
| macro / port activity | `wb.FP.CPI.TOTL.ZG` or `portwatch.port_calls` flag on ARG, site in ARG | 0.8 (by kind) |

**Sanctions screening in detail.** Both names are normalised: NFKD with accents
stripped, casefold, `&` → `and`, dots and apostrophes removed inside words, other
punctuation turned into spaces, and legal forms (`ltd`, `llc`, `gmbh`, `sa`, `ooo`, …)
dropped. They are then compared with `rapidfuzz.fuzz.token_sort_ratio`, against the
primary name and every alias of every `sanction.*` row present, whatever its source,
and of every row of the full OFAC lists in `feeds.sanctions_entries` (contract §5.8;
weak a.k.a.s are not screened).
A pair matches at `sim ≥ 0.92`. Confidence is then adjusted by +0.1 when the countries
overlap and −0.2 when both sides list countries and they are disjoint, and a match
below 0.5 is dropped. What that means in practice:

| Ours | Listed | sim | Result |
|---|---|---|---|
| Acme Trading LLC | ACME TRADING L.L.C. | 1.000 | match, c 1.0 |
| Trading Acme | Acme Trading | 1.000 | match (word order is ignored) |
| Acme Trading | Acme Tradin | 0.957 | near miss, matches with c 0.83 |
| Acme Trading | Acma Trading | 0.917 | no match (one letter in a short name) |
| Global Shipping Ltd | Global Shipping Lines | 0.833 | no match |
| Blue Lagoon Maritime | *alias of* Ocean Pearl Shipping: "BLUE LAGOON MARITIME" | 1.000 | match on the alias |
| AB Ltd | AB | – | never screened (normalised name under 3 characters) |

A `sanction.removal` for the same OFAC `entity_id` cancels the designations dated
before it. Full-list rows (`sanction.listed` or `attrs.full_list: true`, and every
`sanctions_entries` row) do not decay. A `sanctions_entries` row counts from its
`first_seen_at`, so a removal delta published after it cancels it until the daily
full read deletes the row; the title then reads "Sanctions listing may match …".

### 2. Signal `s`: how severe the item is (§5.4)

The table below gives `s` per source.

| Source | `s` |
|---|---|
| GDACS, USGS, PortWatch | severity, or 0.5 if it is NULL |
| EONET | 0.5 for wildfire, volcano, storm and flood; 0.3 otherwise |
| KEV | max(0.9, EPSS) |
| OSV | max(CVSS/10, EPSS); withdrawn advisories are ignored |
| OFAC | designation 1.0; update 0.8 |
| News | 0.25 + 0.5 × tag confidence (tag `other`: 0.15) |
| robust z | 3.5 → 0.5, linear up to 8 → 1.0 |
| IsolationForest | 0.8 × score |

For example, an OSV advisory with CVSS 7.5 whose CVE has EPSS 0.95 gets `s` = 0.95.

### 3. Recency `r`: how fresh it is (§5.5)

`r = 0.5^(age / half-life)`. Items older than four half-lives (at most 365 days) are
dropped, which is what closes old risks. The half-lives are: hazards 72 h, logistics
7 d, news 5 d, sanctions 30 d, KEV 30 d, OSV 90 d, FX 5 d and World Bank about 6 months.
For example, a quake 72 h old has r = 0.5; at 288 h it still counts with r = 1/16, and
after that it is gone. An event whose `to_date` is in the future (it is still ongoing)
has r = 1.

### 4. Likelihood (§5.6)

Each item gives `p = s × r × c`. The items of one risk (same entity, category and
driver) combine by noisy-OR: `L = min(0.99, 1 − Π(1 − p))`. The strongest 50 are kept,
stored as evidence, and are exactly the items L is computed from. For example, an
`EUR/TRY` robust-z flag (p 0.697) and an IsolationForest flag on the same day (p 0.488)
combine to L = 1 − 0.303 × 0.512 = **0.845**.

### 5. Impact (§6)

`impact = clamp(m × base, 0.05, 1)`, where:

* `base = w` (criticality weight: low 0.25, medium 0.5, high 0.75, critical 1, unset 0.5)
  when exposure is unknown;
* otherwise `base = 0.5 w + 0.5 × min(1, share / 0.25)`, where share is a supplier's
  part of total spend (converted at the latest ECB rates) or a customer's revenue share;
* `m` is the category multiplier: sanctions 1.0, hazard and cyber 0.9, logistics 0.8,
  geopolitical 0.7, trade, financial and labour 0.6, other 0.4.

For example, take a critical supplier with 100 of 400 EUR total spend (25 %) and a
sanctions match: impact = 1.0 × (0.5 + 0.5) = **1.0**. With the match above at
L = 0.977, the score is **97.7**.

### 6. End to end: the scores from the example above

| Risk | L | I | Score |
|---|---|---|---|
| Sanctions designation may match Acme Trading LLC | 0.977 | 1.000 | 97.7 |
| Earthquake near Osaka plant | 0.567 | 0.675 | 38.3 |
| Vulnerability in PyPI requests | 0.726 | 0.450 | 32.7 |
| Port disruption at port NLRTM affecting Shanghai-Rotterdam | 0.660 | 0.400 | 26.4 |
| TRY moved sharply against EUR (Turkish lira) | 0.845 | 0.300 | 25.3 |
| Shipping disruption near Suez Canal on route Shanghai-Rotterdam | 0.370 | 0.400 | 14.8 |

Each risk's `explanation` spells out the same arithmetic, for example: *"1 item(s)
matched site "Osaka plant" on geo_radius 15 km. Strongest: … signal 0.700 × recency
0.908 × match 0.892 = 0.567. Likelihood 0.567 is the noisy-OR of 1 item(s). Impact
0.675 = 0.9 (natural_hazard) × criticality weight 0.75 (high; exposure unknown)."*

### 7. Lifecycle (§3, §13)

`write_risks` applies these rules in one transaction, as `rp_app` under RLS:

* A new fingerprint becomes an `open` risk, with history `created`.
* A risk found again is updated, with history `rescored` only when the score moves by
  ≥ 1.0. `acknowledged` and `mitigating` are kept.
* A `dismissed` risk stays dismissed until evidence appears that was never linked to
  it; then it reopens.
* A risk that is not assessed any more (its evidence aged out of the window, or retention
  deleted it), or that scores below 5, is `resolved` by the engine with history
  `resolved_by_engine`. It reopens if it is found again.
* A re-run with the same data at the same `now` writes nothing.

Evidence keeps a snapshot of the feed row, because feed retention deletes rows and
nothing in `biz` holds a foreign key to `feeds`.

### 8. Scenarios (§12)

A scenario runs the same `assess` twice, before and after the shocks and overrides. For
example, "Rotterdam closed" (`{"type":"event","category":"logistics","severity":1,
"ports":["NLRTM"]}`) gives a critical route through NLRTM L = 0.99 and I = 0.8, so a
score of **79.2**. A 25 % lira drop is sized against the pair's own 90-day volatility,
so a 0.1 % move adds nothing.

## Runtime per workspace

This was measured with `tests/bench_engine.py` (synthetic test data) on a 4-vCPU
2.1 GHz container, with Postgres 16 on the same machine and Python 3.12. "Cold" is the
first run in a worker process, which reads the sanctions list; "warm" runs reuse it
while the list is unchanged.

| Workspace | Feed rows read | Risks / evidence | Cold | Warm |
|---|---|---|---|---|
| Typical: 100 entities | 24,600 (incl. 20,000 sanctions rows) | 180 / 8,600 | 4.6 s | **2.4 s** |
| Heavy: 400 entities, every news item naming a company | 30,000 | 736 / 34,900 | 11.1 s | 9.1 s |

The warm typical run splits as: load 0.13 s, assess 0.8 s, write 1.4 s.

**On Neon free tier, expect about 3–6 s per typical workspace**, because Neon's
smaller compute and the network round trips add to the local numbers. Each step makes
only a few round trips: reads are 5–7 queries, and writes are pipelined `executemany`
batches of 200 rows. That is small next to the 5 minutes the compute stays awake after
each feed update, so the engine adds little to the 100 CU-hours a month.

How the load stays bounded:

* At most `max_events` = 20,000 event rows and 5,000 anomalies are read per run.
* Only rows that can match are read: the lookback windows are applied in SQL, and the
  filters use the workspace's countries, ports and keys.
* FX history is read only for the workspace's currencies, and EPSS only for matched CVEs.
* The sanctions rows (`sanction.*` events and up to 50,000 `sanctions_entries` rows)
  are read once per worker process and re-read only when `(count, max(updated_at))` of
  either changes; the name index built from them is reused while they are unchanged.
* Cross-rate detection and name normalisation are cached per process. Both are pure,
  so caching gives identical results.
* There are no long-lived connections: everything runs inside the caller's transaction.

A cost to know about: every run sets `last_seen_at` and `last_linked_at` (the contract
asks for it), so each risk and evidence row is rewritten once per run. That write
volume grows with the evidence count.

## Known gaps

* **Sanctions coverage.** The full SDN and consolidated lists (`feeds.sanctions_entries`)
  are screened, so a supplier designated years ago is found; a risk resolves when its
  entity leaves the list. Entity types (individual, vessel) are not used, and date of
  birth, addresses and identifiers are not compared, so a match is a lead for
  compliance to review, not a verdict.
* **No version ranges for CVEs.** The feeds carry no affected-version ranges, so an
  entity on a patched version still matches. NVD is not a source yet: if E adds it as
  `nvd`, it is matched like KEV (CPE) and scored like OSV.
* **Coarse country matches.** An item with no position matches every entity in its
  country that has no position of its own. A flood anywhere in the USA reaches every
  US supplier without coordinates (at c 0.6 × signal).
* **Chokepoints are circles,** not polygons, and the per-chokepoint PortWatch layer
  needs a token (contract §11).
* **News** is English titles only, and company names come from GLiNER2 tags. GDELT
  news volume only exists for watch terms, which `share_watch_terms` must allow.
* **Cross-rate anomalies** (base currency other than EUR) are computed in memory and
  never stored, so the API's `still_in_feeds` is always false for them.
* **Score rounding.** `RiskChange` scores are computed in Python. The database's
  generated column uses `real`, so the two can differ by 0.1 at a .x5 boundary.
* **Below `min_score` (5).** Weak signals are not stored at all: a risk scoring under
  5 is resolved, not shown as "low".
