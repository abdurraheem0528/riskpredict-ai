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

To refresh a fixture, fetch it again the same way and update the expected
values in `tests/`.
