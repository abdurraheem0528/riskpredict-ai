# Map data

Built by `scripts/build-map-data.mjs`; committed so nothing is fetched at run time.

| File | Source | Licence |
|---|---|---|
| `countries.geojson` | Natural Earth 1:50m admin-0 countries, simplified to 12 % (Visvalingam), 0.01° precision; property `iso` (ISO 3166-1 alpha-3) | Public domain |
| `centroids.json` | Natural Earth `[LABEL_X, LABEL_Y, NAME]` per country, keyed by alpha-3 (and `ADM0_A3` where it differs) | Public domain |
| `lanes.json` | Eurostat [SeaRoute](https://github.com/eurostat/searoute) MARNET maritime network, 100 km resolution (`marnet_plus_100km.gpkg`), itself based on the Oak Ridge National Laboratory CTA Global Shipping Lane Network (2000) and AIS data. Simplified for drawing (approximate, not for navigation): the largest connected part, without the Arctic passages and the Southern Ocean south of 60°S, the ±180° meridian joined, a node added at each chokepoint of `engine/data/chokepoints.json`, then only the edges on a shortest path between two port ends or chokepoints; 1/100° precision. 4,023 nodes, 6,200 edges | © European Union, EUPL-1.2 (the licence text is `EUPL-1.2.txt` here, as Eurostat publishes it with SeaRoute). This derived file is redistributed under the same licence; `scripts/build-map-data.mjs` shows how it was made from the original. The dashboard's code only reads it as data |
| `ports.json` | NGA World Port Index (Pub 150), `UN/LOCODE → [lon, lat, name]`; a fourth element, the UN/LOCODE name, where it differs from the WPI name (UNECE UN/LOCODE code list, via the Frictionless Data `datasets/un-locode` CSV; a name that names an airport is not used). The dashboard shows a cleaned display name; the raw WPI name is kept. 18 standard codes of big container ports that the WPI files under its own codes (CNSHA, SGSIN, CNSZX, …) take a WPI row's position and name (`PORT_ALIASES` in `scripts/build-map-data.mjs`, each with its source), and 4 that no WPI row fits (CNNSA, CNDGG, VNCMT, AEKHL) the UN/LOCODE code list's own coordinates (`LOCODE_PLACED`) | WPI: US Government work, public domain. UN/LOCODE: UNECE, free to use |

Positions the API gives entities from their city (not a file here) come from [GeoNames](https://www.geonames.org/) `cities15000`, licensed CC BY 4.0, via the `geonamescache` package (see `engine/README.md`).
