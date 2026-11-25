# Map data

Built by `scripts/build-map-data.mjs`; committed so nothing is fetched at run time.

| File | Source | Licence |
|---|---|---|
| `countries.geojson` | Natural Earth 1:50m admin-0 countries, simplified to 12 % (Visvalingam), 0.01° precision; property `iso` (ISO 3166-1 alpha-3) | Public domain |
| `centroids.json` | Natural Earth `[LABEL_X, LABEL_Y, NAME]` per country, keyed by alpha-3 (and `ADM0_A3` where it differs) | Public domain |
| `ports.json` | NGA World Port Index (Pub 150), `UN/LOCODE → [lon, lat, name]` | US Government work, public domain |
