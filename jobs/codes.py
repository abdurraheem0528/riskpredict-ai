"""Normalisation to ISO 3166-1 alpha-3, ISO 4217 and UN/LOCODE.

Lookups use the ISO data shipped with ``pycountry`` (offline). A name that
cannot be matched with certainty returns ``None``: connectors then leave the
field empty rather than guess.
"""

from __future__ import annotations

import re
import unicodedata
from functools import lru_cache

import pycountry


def _norm(name: str) -> str:
    s = unicodedata.normalize("NFKD", name)
    s = "".join(ch for ch in s if not unicodedata.combining(ch))
    s = s.casefold().replace("’", "'").replace("`", "'")
    s = re.sub(r"[.\s]+", " ", s)
    return s.strip(" ,")


# Names seen in the live feeds (OFAC, GDACS, PortWatch, USGS, World Bank) that
# pycountry does not resolve on its own. Only unambiguous mappings are listed.
_ALIASES: dict[str, str] = {
    _norm(k): v
    for k, v in {
        "Russia": "RUS",
        "Korea, North": "PRK",
        "Korea, South": "KOR",
        "Burma": "MMR",
        "Turkey": "TUR",
        "Turkiye": "TUR",
        "Bahamas, The": "BHS",
        "The Bahamas": "BHS",
        "Gambia, The": "GMB",
        "The Gambia": "GMB",
        "Curacao": "CUW",
        "Congo, Democratic Republic of the": "COD",
        "Democratic Republic of the Congo": "COD",
        "DR Congo": "COD",
        "Congo, Republic of the": "COG",
        "Republic of Congo": "COG",
        "Republic of the Congo": "COG",
        "Cote d Ivoire": "CIV",
        "Cote d'Ivoire": "CIV",
        "Ivory Coast": "CIV",
        "Macau": "MAC",
        "Macao": "MAC",
        "North Macedonia, The Republic of": "MKD",
        "Macedonia": "MKD",
        "Man, Isle of": "IMN",
        "Timor Leste": "TLS",
        "East Timor": "TLS",
        "Taiwan Province of China": "TWN",
        "St Vincent and the Grenadines": "VCT",
        "St Kitts and Nevis": "KNA",
        "St Lucia": "LCA",
        "Micronesia": "FSM",
        "Brunei": "BRN",
        "Swaziland": "SWZ",
        "Cape Verde": "CPV",
        "Sao Tome and Principe": "STP",
        "West Bank": "PSE",
        "Gaza": "PSE",
        "Region: Gaza": "PSE",
        "Palestinian": "PSE",
        "Palestinian Territories": "PSE",
        "Region: Northern Mali": "MLI",
        "UK": "GBR",
        "Great Britain": "GBR",
        "US": "USA",
        "United States of America": "USA",
        "Czechia": "CZE",
        "Laos": "LAO",
        "Vatican": "VAT",
        "Vatican City": "VAT",
        "Falkland Islands": "FLK",
        "Fiji Islands": "FJI",
    }.items()
}

# USGS place strings end in a US state for US earthquakes.
US_STATES = {
    _norm(s)
    for s in (
        "Alabama Alaska Arizona Arkansas California Colorado Connecticut Delaware Florida "
        "Georgia Hawaii Idaho Illinois Indiana Iowa Kansas Kentucky Louisiana Maine Maryland "
        "Massachusetts Michigan Minnesota Mississippi Missouri Montana Nebraska Nevada Ohio "
        "Oklahoma Oregon Pennsylvania Tennessee Texas Utah Vermont Virginia Washington Wisconsin "
        "Wyoming"
    ).split()
} | {
    _norm(s)
    for s in (
        "New Hampshire",
        "New Jersey",
        "New Mexico",
        "New York",
        "North Carolina",
        "North Dakota",
        "Rhode Island",
        "South Carolina",
        "South Dakota",
        "West Virginia",
        "CA",
        "AK",
        "HI",
        "NV",
    )
}


@lru_cache(maxsize=4096)
def country_alpha3(name_or_code: str | None) -> str | None:
    """Return the ISO 3166-1 alpha-3 code for a name or alpha-2/alpha-3 code."""
    if not name_or_code:
        return None
    raw = name_or_code.strip()
    if not raw:
        return None
    key = _norm(raw)
    if key in _ALIASES:
        return _ALIASES[key]
    if len(raw) in (2, 3) and raw.isalpha():
        c = pycountry.countries.get(**{"alpha_2" if len(raw) == 2 else "alpha_3": raw.upper()})
        return c.alpha_3 if c else None
    try:
        return pycountry.countries.lookup(raw).alpha_3
    except LookupError:
        pass
    # Retry without diacritics / punctuation differences against every name form.
    for c in pycountry.countries:
        for attr in ("name", "official_name", "common_name"):
            v = getattr(c, attr, None)
            if v and _norm(v) == key:
                return c.alpha_3
    return None


@lru_cache(maxsize=512)
def is_country(code: str) -> bool:
    return len(code) == 3 and code.isupper() and pycountry.countries.get(alpha_3=code) is not None


@lru_cache(maxsize=512)
def is_currency(code: str) -> bool:
    return len(code) == 3 and code.isupper() and pycountry.currencies.get(alpha_3=code) is not None


def locode(value: str | None) -> str | None:
    """Normalise ``"JP TRG"`` / ``"jptrg"`` to ``"JPTRG"``; ``None`` if malformed."""
    if not value:
        return None
    s = re.sub(r"[\s-]+", "", value).upper()
    if re.fullmatch(r"[A-Z]{2}[A-Z2-9]{3}", s) and pycountry.countries.get(alpha_2=s[:2]):
        return s
    return None
