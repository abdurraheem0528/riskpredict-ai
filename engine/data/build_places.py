"""Build the engine's place tables (engine/places.py) from their sources. Run by hand when a
source changes; the outputs are committed, so nothing is fetched at run time.

    python engine/data/build_places.py --cities path/to/cities15000.json [--ports web/public/map/ports.json]

* ``cities15000.tsv.gz``: GeoNames ``cities15000`` (every city with 15,000+ people; CC BY 4.0,
  https://www.geonames.org/), from the JSON copy the PyPI package ``geonamescache`` ships
  (``geonamescache/data/cities15000.json``, its fields geonameid, name, latitude, longitude,
  countrycode, population, alternatenames). Trimmed to one line per city:
  ``country  lat  lon  population  name  keys`` where ``keys`` are the match keys
  (``engine.places.norm``) of the name and then of its Latin alternate names, ``|``-separated.
  Alternate names are kept only if they are Latin script, have a capital letter, no digits,
  and are not an all-capital code of up to 4 letters (airport codes such as "KHH"): that
  drops GeoNames' lower-case machine transliterations of other scripts.
* ``ports.tsv``: ``locode  WPI name  UN/LOCODE name`` from the dashboard's
  ``web/public/map/ports.json`` (built by ``web/scripts/build-map-data.mjs``), so the API
  names ports exactly as the map does (tests/test_places.py checks they agree).

The input files are read as data only.
"""

from __future__ import annotations

import argparse
import gzip
import io
import json
import re
import sys
import unicodedata
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from engine.places import norm  # noqa: E402

CITIES_HEADER = """\
# GeoNames cities15000 (https://www.geonames.org/), licensed CC BY 4.0
# (https://creativecommons.org/licenses/by/4.0/); via the geonamescache package's copy.
# Trimmed by engine/data/build_places.py: country (ISO 3166-1 alpha-2), lat, lon (4 dp),
# population, name, match keys of the name and its Latin alternate names (| separated).
"""
PORTS_HEADER = """\
# UN/LOCODE -> World Port Index name, UN/LOCODE name (empty when the same), from
# web/public/map/ports.json (NGA World Port Index, public domain; UNECE UN/LOCODE, free to
# use). Built by engine/data/build_places.py; engine.places.port_name cleans the names.
"""


def _latin_alternate(s: str) -> bool:
    letters = [ch for ch in s if ch.isalpha()]
    if not letters or re.search(r"\d", s):
        return False
    if not all(ch.isascii() or "LATIN" in unicodedata.name(ch, "") for ch in letters):
        return False
    if not any(ch.isupper() for ch in letters):
        return False
    return not (s.isupper() and len(s) <= 4)


def build_cities(src: Path, out: Path) -> int:
    data = json.loads(src.read_text(encoding="utf-8"))
    cities = sorted(data.values(), key=lambda c: (c["countrycode"], int(c["geonameid"])))
    buf = io.StringIO()
    buf.write(CITIES_HEADER)
    n = 0
    for c in cities:
        cc, name = c["countrycode"], " ".join(str(c["name"]).split())
        primary = norm(name)
        if not re.fullmatch(r"[A-Z]{2}", cc or "") or not primary:
            continue
        keys = [primary]
        for alt in c.get("alternatenames") or ():
            k = norm(alt)
            if k and len(k) >= 3 and k not in keys and _latin_alternate(alt):
                keys.append(k)
        keys = keys[:1] + sorted(keys[1:])
        lat, lon = round(float(c["latitude"]), 4), round(float(c["longitude"]), 4)
        buf.write(f"{cc}\t{lat!r}\t{lon!r}\t{int(c['population'] or 0)}\t{name}\t{'|'.join(keys)}\n")
        n += 1
    with open(out, "wb") as fh, gzip.GzipFile(fileobj=fh, mode="wb", compresslevel=9, mtime=0, filename="") as gz:
        gz.write(buf.getvalue().encode("utf-8"))
    return n


def build_ports(src: Path, out: Path) -> int:
    ports = json.loads(src.read_text(encoding="utf-8"))
    lines = [PORTS_HEADER.rstrip("\n")]
    for code in sorted(ports):
        p = ports[code]
        wpi = " ".join(str(p[2]).split())
        unl = " ".join(str(p[3]).split()) if len(p) > 3 and p[3] else ""
        lines.append(f"{code}\t{wpi}\t{unl if unl != wpi else ''}")
    out.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return len(ports)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--cities", type=Path, help="geonamescache's cities15000.json")
    ap.add_argument("--ports", type=Path, default=ROOT / "web/public/map/ports.json")
    args = ap.parse_args()
    if args.cities:
        n = build_cities(args.cities, HERE / "cities15000.tsv.gz")
        print(f"cities15000.tsv.gz: {n} cities, {(HERE / 'cities15000.tsv.gz').stat().st_size} bytes")
    n = build_ports(args.ports, HERE / "ports.tsv")
    print(f"ports.tsv: {n} ports, {(HERE / 'ports.tsv').stat().st_size} bytes")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
