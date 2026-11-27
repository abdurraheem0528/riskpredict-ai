# Test fixtures

Every file here is a real response from the public source, fetched live on
2026-11-23 while building step 1 (or on 2026-11-24 for step 2, marked below). Nothing is hand-written. Where the API could
not return a small response, the real response was **trimmed**: whole records
were dropped, and the records that remain are byte-for-byte unchanged.

| File | Request | Trimmed? |
|---|---|---|
| `gdelt_artlist.json` | `api.gdeltproject.org/api/v2/doc/doc?query="supply chain" disruption&mode=artlist&format=json&maxrecords=10&timespan=1d` | no (GDELT returned 1 article) |
| `gdelt_throttle.txt` | GDELT DOC 2.0, second request inside 5 s (HTTP 429, plain text) | no |
| `gdelt_lastupdate.txt` (step 3) | `data.gdeltproject.org/gdeltv2/lastupdate.txt` as published for the 2026-11-16 16:30 UTC window (read through a web-fetch service, because this sandbox cannot reach data.gdeltproject.org) | no |
| `gdelt_gkg_trimmed.csv` (step 3) | GKG 2.1 file `20190301001500.gkg.csv` from GDELT's mirror in the AWS Open Data bucket `gdelt-open-data` (`v2/gkg/`), same format as `data.gdeltproject.org/gdeltv2/*.gkg.csv.zip`; fetched 2026-11-24. The mirror ends in 2019, so these rows predate the `<PAGE_TITLE>` block (added Sept 2019) | yes: 5 of 2,656 rows (ids `-0`, `-201`, `-910`, `-1825`, `-2133`); tests zip it in memory |
| `gdelt_gkg_themes.csv` (step 4) | GKG 2.1 files `20190410120000`, `20190410121500`, `20190410180000` and `20190411030000.gkg.csv` from the same AWS mirror (`v2/gkg/`, which ends in April 2019), fetched 2026-11-25; rows chosen as the shortest that match each default theme term, plus a phrase hit and a miss | yes: 7 of 10,064 rows (ids `20190410180000-1225`, `-2132`, `20190410121500-1822`, `20190410120000-1801`, `-1257`, `20190411030000-215`, `20190410121500-148`) |
| `gdelt_gkg_themes_used.txt` (step 4) | `data.gdeltproject.org/api/v2/guides/LOOKUP-GKGTHEMES.TXT` (GDELT's published GKG theme list, `CODE<TAB>article count`; read through a web-fetch service, 59,279 codes), fetched 2026-11-25 | yes: the 35 lines for the codes the default theme terms use |
| `cisa_kev_trimmed.json` | `www.cisa.gov/sites/default/files/feeds/known_exploited_vulnerabilities.json` | yes: 5 of 1,790 entries (2 oldest, 3 newest); header fields kept |
| `epss_scores_trimmed.csv` | `epss.empiricalsecurity.com/epss_scores-current.csv.gz` (decompressed) | yes: metadata + header + first 4 rows + first 3 rows scoring ≥ 0.5 |
| `osv_jinja2_trimmed.json` | `POST api.osv.dev/v1/query {"package":{"name":"jinja2","ecosystem":"PyPI"}}` | yes: 4 of 20 vulns |
| `portwatch_ports_page1.json`, `portwatch_ports_page2.json` | `PortWatch_ports_database` query, `portid IN (...)`, 2 records per page | no (two real pages) |
| `portwatch_disruptions.json` | `portwatch_disruptions_database` query, `affectedports LIKE '%port896%'` | no |
| `portwatch_trade.json` | `Daily_Trade_Data_REG` query, `date = '2026-11-13' AND ISO3 IN ('MOZ','COM','WLD','400')` | no |
| `portwatch_token_required.json` | `Daily_Ports_Data` query (layer now needs a token) | no |
| `gdacs_events4app_trimmed.json` | `www.gdacs.org/gdacsapi/api/events/geteventlist/EVENTS4APP` | yes: 4 of 100 features |
| `usgs_4.5_day.geojson` | `earthquake.usgs.gov/earthquakes/feed/v1.0/summary/4.5_day.geojson` | no |
| `eonet_events.json` | `eonet.gsfc.nasa.gov/api/v3/events?status=open&days=30&limit=5` | no |
| `frankfurter_latest.json` | `api.frankfurter.dev/v1/latest?base=EUR` | no |
| `frankfurter_history.json` (step 2) | `api.frankfurter.dev/v1/2026-05-01..2026-11-20?base=EUR&symbols=USD,TRY,JPY,CHF` (146 business days, for spike detection) | no |
| `frankfurter_range.json` | `api.frankfurter.dev/v1/2026-11-19..2026-11-20?base=EUR` | no |
| `worldbank_page1.json`, `worldbank_last_page.json` | `api.worldbank.org/v2/country/all/indicator/FP.CPI.TOTL.ZG?format=json&mrv=1&per_page=4&page=1` / `page=67` | no |
| `worldbank_error.json` | same API with an unknown indicator code | no |
| `ofac_delta_trimmed.xml` | `sanctionslistservice.ofac.treas.gov/changes/latest` (2026-11-23 delta) | yes: 3 of 30 entities |

## Keyed sources and the full OFAC lists (round 2)

The sandbox these were built in could not reach the providers (its proxy refused
every provider host, including treasury.gov), and no API keys were available.
So each fixture below is marked by where it came from:

* **real**: a real response, fetched on 2026-11-24 through a web-fetch service
  that returned the provider's JSON unchanged (it re-serialises whitespace only).
* **documented**: the example response printed in the provider's own docs,
  copied as published.
* **constructed**: written for the tests from the provider's documented field
  list or schema, because neither a keyless call nor a documented example exists.
  Content is placeholder text (`example.com`, "Example pulse", fictional names
  and ids). Nothing in these files describes a real company, person or threat.

| File | Origin | Request / source |
|---|---|---|
| `nvd_cve_log4shell.json` | real (keyless) | `services.nvd.nist.gov/rest/json/cves/2.0?cveId=CVE-2021-44228` (has CVSS, CPE configurations, KEV fields) |
| `nvd_cves_pub_window.json` | real (keyless) | `…/cves/2.0?pubStartDate=2026-11-17T00:00:00.000Z&pubEndDate=2026-11-18T00:00:00.000Z&resultsPerPage=3` (CNA scores, CVSS v4, a purl, no CPEs yet) |
| `eia_spot_prices.json` | real (EIA's public `DEMO_KEY`) | `api.eia.gov/v2/petroleum/pri/spt/data/?frequency=daily&data[0]=value&facets[series][]=RWTC&sort[0][column]=period&sort[0][direction]=desc&length=3` |
| `comtrade_preview.json` | real (keyless preview API) | `comtradeapi.un.org/public/v1/preview/C/A/HS?reporterCode=392,699&period=2024&partnerCode=156&cmdCode=8542,2709&flowCode=M&includeDesc=true`; the preview returns the same record layout as the keyed `data/v1/get` |
| `comtrade_world_classic.json` | real (keyless preview API, fetched 2026-11-25 through a web-fetch service) | `comtradeapi.un.org/public/v1/preview/C/A/HS?reporterCode=392,699&period=2024&partnerCode=0&partner2Code=0&customsCode=C00&motCode=0&breakdownMode=classic&cmdCode=8542,2709&flowCode=M&includeDesc=true`: the World totals the connector now asks for (`comtrade_preview.json` is the same query with partner China, kept to show bilateral rows are dropped) |
| `fred_observations_doc.json` | documented, trimmed | the JSON example on `fred.stlouisfed.org/docs/api/fred/series_observations.html` (series GNPCA); its first 5 of 84 observations, header unchanged |
| `tiingo_eod_doc.json` | documented | the EOD prices example in `tiingo.com/llms-full.txt` ("REST - End-of-Day Prices") |
| `currents_search_doc.json` | documented | the `/search` 200 example in `currentsapi.services/json/swagger.json` |
| `newsdata_latest_shape.json` | constructed | field names and types from `newsdata.io/openapi.json` (`ArticleListExample`) and `/documentation/response-object`, including the plan-gated "ONLY AVAILABLE IN PAID PLANS" strings |
| `otx_pulses_shape.json` | constructed | `/api/v1/pulses/subscribed` envelope (`count`, `next`, `results`) and pulse fields from `otx.alienvault.com/api` and the OTX-Python-SDK notebook |
| `engine_sample_feeds.json` | constructed | the 11-supplier sheet of the first live end-to-end test (fictional companies) and a recorded set of feed rows in the exact shape the feed jobs store (`feeds.events` + newest `feeds.event_tags`, `feeds.anomalies`), modelled on the story types that test matched; titles are paraphrased story types, not quotes. Used by `tests/engine_sample.py` / `tests/test_engine_sample.py` for the engine 1.1.0 → 1.2.0 before/after |
| `engine_live_nov27_feeds.json` | constructed | the fictional 11-supplier sheet of the 27 Nov live test (Pearl River PCB Works, Rhein Sensorik GmbH, …) and feed rows in the same shape: `x-*` rows modelled on the loosely related articles that test cited (a country mentioned in passing), `k-*` rows positive controls. Titles paraphrase story types, they are not quotes. Used by `tests/test_engine_relevance.py` for the engine 1.2.0 → 1.3.0 before/after |
| `../engine_nov27_headlines.py` | real (quoted headlines) + constructed (suppliers) | the headlines, tag categories and GDELT `countries` of the news evidence the live API returned on 27 Nov 2026 (engine 1.3.3), quoted; the suppliers they are tested against are fictional (three carry the live workspace's fictional names). Used by `tests/test_engine_headlines.py` for the engine 1.3.3 → 1.3.4 before/after |
| `ofac_enhanced_shape.xml` | constructed | element layout from `sanctionslistservice.ofac.treas.gov/api/PublicationPreview/exports/ENHANCED_XML.xsd`; fictional names. The full-list parser is **also** tested on the real `ofac_delta_trimmed.xml`, which uses the same enhanced layout |

Tiingo's FX prices endpoint is tested with an inline row in
`tests/test_keyed_sources.py`: the docs list its fields but print no example values.

## Reference lists

| File | Source |
|---|---|
| `top50_container_ports.json` | The World Shipping Council's [Top 50 Container Ports](https://www.worldshipping.org/top-50-container-ports), 2024 statistics: rank and port name, as two full reproductions of the WSC table give them (Goliath Africa Logistics, 12 Feb 2026; PortNews24, 30 Jul 2026; they agree row for row), because the WSC page draws its table with a script and this sandbox could not read it. Fetched 2026-11-27. Each port's UN/LOCODEs (standard code first, then other codes in common use) were added from the UNECE code list and the World Port Index. `tests/test_places.py` and `web/tests/unit/lib.test.ts` check that every code resolves in `ports.json` and `ports.tsv` |

When the keys are in place, replace the constructed and documented files with real
responses (keep them small, never commit a URL or header that contains a key) and
update the expected values in `tests/test_keyed_sources.py`.

To refresh a fixture, fetch it again the same way and update the expected
values in `tests/`.
