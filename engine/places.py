"""Where an entity is, and what a port is called (risk-engine.md §5.10).

* ``city_position(city, countries)``: the city's position from the GeoNames
  ``cities15000`` gazetteer (``engine/data/cities15000.tsv.gz``, CC BY 4.0), matched only
  within the given countries. The mapper uses it at sync time for a row with a city and
  no latitude/longitude, so the engine's radius matching sees the city, not the port.
* ``port_name(locode)``: the display name of a port, by the dashboard's rule
  (``web/src/map/geo.ts``): the UN/LOCODE name, else the World Port Index name, cleaned
  by ``clean_port_name`` (``engine/data/ports.tsv``, built from ``web/public/map/ports.json``).
* ``place_label(city, countries, ports)``: ``"Hsinchu, Taiwan"``, ``"Kaohsiung, Taiwan"``
  or ``"Taiwan and Japan"``: the place line the API serves with entities and risks.

Both tables are read lazily from files shipped with the engine; nothing is fetched.
"""

from __future__ import annotations

import gzip
import re
import unicodedata
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Iterable

DATA = Path(__file__).resolve().parent / "data"
CITIES_FILE = DATA / "cities15000.tsv.gz"
PORTS_FILE = DATA / "ports.tsv"


def norm(name: str) -> str:
    """Match key of a place name: accents stripped, casefolded, punctuation as single spaces.

    ``"Bayan-Lepas"``, ``"bayan lepas"`` and ``"Báyan Lepas"`` all give ``"bayan lepas"``.
    """
    s = unicodedata.normalize("NFKD", name)
    s = "".join(ch for ch in s if not unicodedata.combining(ch)).casefold()
    s = s.replace("ø", "o").replace("đ", "d").replace("ł", "l").replace("ı", "i").replace("æ", "ae").replace("œ", "oe")
    return " ".join(re.sub(r"[^a-z0-9]+", " ", s).split())


# --- cities ---------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class City:
    name: str  # GeoNames name, e.g. "Hsinchu"
    country: str  # ISO 3166-1 alpha-2
    lat: float
    lon: float
    population: int


@lru_cache(maxsize=1)
def _rows_by_country() -> dict[str, list[str]]:
    """Gazetteer lines by alpha-2 country, unparsed (a country is indexed when first asked)."""
    out: dict[str, list[str]] = {}
    with gzip.open(CITIES_FILE, "rt", encoding="utf-8") as fh:
        for line in fh:
            if not line.startswith("#") and line.strip():
                out.setdefault(line[:2], []).append(line.rstrip("\n"))
    return out


@lru_cache(maxsize=64)
def _country_index(cc: str) -> dict[str, City]:
    """match key → city within one country: a city's own name beats another city's
    alternate name; then the larger population; then file order (GeoNames id)."""
    best: dict[str, tuple[tuple[int, int], City]] = {}
    for line in _rows_by_country().get(cc, ()):
        country, lat, lon, pop, name, keys = line.split("\t")
        city = City(name, country, float(lat), float(lon), int(pop))
        for i, key in enumerate(keys.split("|")):
            rank = (0 if i == 0 else 1, -city.population)
            if key and (key not in best or rank < best[key][0]):
                best[key] = (rank, city)
    return {k: c for k, (_, c) in best.items()}


_SUFFIX = re.compile(r" (city|shi|si|gun|municipality)$")


def _variants(city: str) -> list[str]:
    """``"Hsinchu City, Hsinchu"`` → ``["hsinchu city hsinchu", "hsinchu city", "hsinchu"]``."""
    out: list[str] = []
    for raw in (city, re.split(r"[,(/;]", city, maxsplit=1)[0]):
        k = norm(raw)
        for v in (k, _SUFFIX.sub("", k), re.sub(r"^city of ", "", k)):
            if v and v not in out:
                out.append(v)
    return out


def _alpha2(code: str) -> str | None:
    code = code.strip().upper()
    if len(code) == 2 and code.isalpha():
        return code
    if len(code) == 3:
        import pycountry

        c = pycountry.countries.get(alpha_3=code)
        return c.alpha_2 if c else None
    return None


def city_position(city: str | None, countries: Iterable[str]) -> City | None:
    """The gazetteer city called ``city`` in the first of ``countries`` (alpha-3 or alpha-2)
    that has one, matched on its name or a Latin alternate name; ``None`` when no city of
    that name is in those countries (a name is never looked up in another country)."""
    if not city or not city.strip():
        return None
    keys = _variants(city)
    for code in dict.fromkeys(countries):
        cc = _alpha2(code) if code else None
        if not cc:
            continue
        index = _country_index(cc)
        for k in keys:
            if k in index:
                return index[k]
    return None


# --- ports ----------------------------------------------------------------------------------


def clean_port_name(raw: str) -> str:
    """The dashboard's ``cleanPortName`` (web/src/map/geo.ts), rule for rule: drops
    parenthesised former or local names ("Chennai (ex Madras)"), a region after a comma
    ("Tegal, Java"), and the WPI's harbour suffixes ("Tokyo Ko", "Hualien Hsien") and the
    UN/LOCODE's "Pt"."""
    s = re.sub(r"\s*\([^)]*\)", "", raw)
    s = re.sub(r",.*", "", s)
    s = re.sub(r"[\s-]+(?:Ko|Kou|Hsien|Pt)$", "", s, flags=re.IGNORECASE)
    s = re.sub(r"\s+", " ", s).strip()
    return s or raw.strip()


@lru_cache(maxsize=1)
def _ports() -> dict[str, str]:
    out: dict[str, str] = {}
    for line in PORTS_FILE.read_text(encoding="utf-8").splitlines():
        if not line or line.startswith("#"):
            continue
        code, wpi, unlocode = (line.split("\t") + [""])[:3]
        out[code] = clean_port_name(unlocode or wpi)
    return out


def port_name(locode: str) -> str | None:
    """``"TWKHH"`` → ``"Kaohsiung"``; ``None`` for a code the port table does not have."""
    return _ports().get(locode)


def port_label(locode: str) -> str:
    """The port's display name, or its code when the table does not have it."""
    return port_name(locode) or locode


# --- the place line -------------------------------------------------------------------------


def place_label(city: str | None, countries: Iterable[str] = (), ports: Iterable[str] = ()) -> str | None:
    """``"Hsinchu, Taiwan"`` for an entity with a city; else the dashboard's line from the
    first port and up to two countries (``"Kaohsiung, Taiwan"``, ``"Japan and China +1"``);
    ``None`` when there is nothing to say."""
    from engine.texts import country_name

    countries, ports = list(countries), list(ports)
    if city and city.strip():
        cc = countries[0] if countries else (_alpha3(ports[0][:2]) if ports else None)
        return f"{city.strip()}, {country_name(cc)}" if cc else city.strip()
    parts: list[str] = []
    if ports:
        parts.append(port_label(ports[0]))
    if countries:
        names = " and ".join(country_name(c) for c in countries[:2])
        parts.append(names + (f" +{len(countries) - 2}" if len(countries) > 2 else ""))
    return ", ".join(parts) or None


def _alpha3(cc: str) -> str | None:
    import pycountry

    c = pycountry.countries.get(alpha_2=cc)
    return c.alpha_3 if c else None
