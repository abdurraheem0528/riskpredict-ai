# Map data

Built by `scripts/build-map-data.mjs`; committed so nothing is fetched at run time.

| File | Source | Licence |
|---|---|---|
| `countries.geojson` | Natural Earth 1:50m admin-0 countries, simplified to 12 % (Visvalingam), 0.01° precision; property `iso` (ISO 3166-1 alpha-3) | Public domain |
| `centroids.json` | Natural Earth `[LABEL_X, LABEL_Y, NAME]` per country, keyed by alpha-3 (and `ADM0_A3` where it differs) | Public domain |
| `ports.json` | NGA World Port Index (Pub 150), `UN/LOCODE → [lon, lat, name]`; a fourth element, the UN/LOCODE name, where it differs from the WPI name (UNECE UN/LOCODE code list, via the Frictionless Data `datasets/un-locode` CSV). The dashboard shows a cleaned display name; the raw WPI name is kept | WPI: US Government work, public domain. UN/LOCODE: UNECE, free to use |

Positions the API gives entities from their city (not a file here) come from [GeoNames](https://www.geonames.org/) `cities15000`, licensed CC BY 4.0, via the `geonamescache` package (see `engine/README.md`).
