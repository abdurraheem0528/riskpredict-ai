"""Exact place-name → ISO 3166-1 alpha-3 matching, from a vendored table.

The table (``data/countries.tsv``) holds ISO 3166-1 English names plus a short
list of everyday aliases ("Russia", "UAE", "U.S."). A place matches only when
the *whole* extracted string equals one of those names after normalisation
(case, accents, full stops, a leading "the"). There is no fuzzy matching, no
substring search and no geocoding service: "Port of Rotterdam", "Gulf" or
"Texas" stay unmatched, which is the intended behaviour.
"""

from __future__ import annotations

import csv
import re
import unicodedata
from functools import lru_cache
from pathlib import Path

TABLE = Path(__file__).resolve().parent / "data" / "countries.tsv"

# Names that are exact ISO names but usually mean something else in news text.
AMBIGUOUS = frozenset({"georgia", "congo"})


def normalise(name: str) -> str:
    s = unicodedata.normalize("NFKD", name)
    s = "".join(ch for ch in s if not unicodedata.combining(ch))
    s = s.casefold().replace("’", "'").replace("`", "'")
    s = s.replace(".", "")  # "U.S." == "US", "St. Lucia" == "St Lucia"
    s = re.sub(r"[\s\-‐–—]+", " ", s).strip(" ,;:'\"()")
    if s.startswith("the "):
        s = s[4:]
    return s


@lru_cache(maxsize=1)
def table() -> dict[str, str]:
    """Normalised name → alpha-3. Raises if the vendored file is inconsistent."""
    out: dict[str, str] = {}
    with TABLE.open(encoding="utf-8", newline="") as f:
        rows = csv.DictReader((line for line in f if not line.startswith("#")), delimiter="\t")
        for row in rows:
            code, name = row["alpha3"].strip(), row["name"].strip()
            if not re.fullmatch(r"[A-Z]{3}", code) or not name:
                raise ValueError(f"bad row in {TABLE.name}: {row!r}")
            key = normalise(name)
            if out.get(key, code) != code:
                raise ValueError(f"{name!r} maps to two codes in {TABLE.name}")
            out[key] = code
    return out


def country_code(place: str | None) -> str | None:
    """Alpha-3 for an exact country name or alias, else ``None``."""
    if not place:
        return None
    key = normalise(place)
    if not key or key in AMBIGUOUS:
        return None
    return table().get(key)
