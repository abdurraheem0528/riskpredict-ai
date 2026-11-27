"""The only door to the risk engine (``engine/``).

The engine is imported lazily and used only through the interface in
``docs/contracts/risk-engine.md``. If the package is missing from an install,
``load()`` returns ``None`` and callers degrade (no scoring runs, scenarios fail
with a clear error, the disruptions layer shows port disruptions only). The API
and worker images both ship it (CI checks the import). Nothing here scores anything.
"""

from __future__ import annotations

import importlib
import logging
from dataclasses import dataclass
from typing import Any

log = logging.getLogger("riskpredict.engine")

_missing_logged = False


def load() -> Any | None:
    global _missing_logged
    try:
        return importlib.import_module("engine")
    except ModuleNotFoundError as exc:
        if exc.name != "engine":
            raise
        if not _missing_logged:
            log.warning("risk engine package not installed; scoring is skipped")
            _missing_logged = True
        return None


@dataclass(frozen=True)
class Chokepoint:
    id: str
    name: str
    aliases: tuple[str, ...]
    lat: float | None
    lon: float | None
    radius_km: float | None


# Ids and common names of the initial set in risk-engine.md §5.3, used only to
# resolve a mapped "via chokepoints" value to an id when the engine's table is
# not available. Geometry always comes from the engine.
_FALLBACK_NAMES = {
    "suez": ("Suez Canal", "Suez"),
    "panama": ("Panama Canal", "Panama"),
    "hormuz": ("Strait of Hormuz", "Hormuz"),
    "bab_el_mandeb": ("Bab-el-Mandeb", "Bab el-Mandeb", "Bab al-Mandab"),
    "malacca": ("Strait of Malacca", "Malacca Strait", "Malacca"),
    "singapore": ("Singapore Strait", "Strait of Singapore"),
    "bosporus": ("Bosporus", "Bosphorus", "Istanbul Strait"),
    "gibraltar": ("Strait of Gibraltar", "Gibraltar"),
    "dover": ("Strait of Dover", "Dover Strait", "Dover"),
    "taiwan_strait": ("Taiwan Strait", "Formosa Strait"),
    "cape_of_good_hope": ("Cape of Good Hope", "Good Hope"),
}


def _get(item: Any, *names: str) -> Any:
    for n in names:
        if isinstance(item, dict) and n in item:
            return item[n]
        if hasattr(item, n):
            return getattr(item, n)
    return None


def chokepoints() -> list[Chokepoint]:
    """The engine's static chokepoint table (``engine.CHOKEPOINTS`` or
    ``engine.chokepoints()``), or names only when the engine is absent."""
    eng = load()
    table = None
    if eng is not None:
        table = getattr(eng, "CHOKEPOINTS", None)
        if table is None and callable(getattr(eng, "chokepoints", None)):
            table = eng.chokepoints()
        if isinstance(table, dict):
            table = [{"id": k, **(v if isinstance(v, dict) else {})} for k, v in table.items()]
    if table:
        out = []
        for item in table:
            cid = _get(item, "id")
            if not cid:
                continue
            centre = _get(item, "centre", "center")
            lat, lon = _get(item, "lat"), _get(item, "lon")
            if lat is None and centre is not None:
                try:
                    lat, lon = (centre["lat"], centre["lon"]) if isinstance(centre, dict) else (centre[0], centre[1])
                except (KeyError, IndexError, TypeError):
                    lat = lon = None
            out.append(
                Chokepoint(
                    id=str(cid),
                    name=str(_get(item, "name") or cid),
                    aliases=tuple(str(a) for a in (_get(item, "aliases") or ())),
                    lat=float(lat) if lat is not None else None,
                    lon=float(lon) if lon is not None else None,
                    radius_km=float(r) if (r := _get(item, "radius_km", "radius")) is not None else None,
                )
            )
        return out
    return [Chokepoint(k, v[0], v[1:], None, None, None) for k, v in _FALLBACK_NAMES.items()]


# --- places (risk-engine.md §5.11) ------------------------------------------------------------


_places_unavailable = False


def _place_fn(name: str):
    """``engine.<name>``, or ``None`` without the engine (or one of its dependencies): sync
    and serialisation then go on without city positions and port names."""
    global _places_unavailable
    if _places_unavailable:
        return None
    try:
        eng = load()
    except ImportError:
        _places_unavailable = True
        return None
    return getattr(eng, name, None) if eng is not None else None


def city_position(city: str | None, countries: list[str]) -> tuple[float, float] | None:
    """``(lat, lon)`` of ``city`` within ``countries`` from the engine's gazetteer, or ``None``
    (no such city there, or no engine: the entity is then placed by its port or country)."""
    fn = _place_fn("city_position") if city else None
    hit = fn(city, countries) if fn is not None else None
    return (hit.lat, hit.lon) if hit is not None else None


def port_name(locode: str) -> str:
    """The port's display name (``"TWKHH"`` → ``"Kaohsiung"``), else the code."""
    fn = _place_fn("port_name")
    return (fn(locode) if fn is not None else None) or locode


def place_label(city: str | None, countries: list[str], ports: list[str]) -> str | None:
    """``"Hsinchu, Taiwan"`` / ``"Kaohsiung, Taiwan"`` / ``"Taiwan and Japan"``, or ``None``
    (``None`` too without the engine: the dashboard then writes the line itself)."""
    fn = _place_fn("place_label")
    return fn(city, countries, ports) if fn is not None else None


def headline(title: str | None, url: str | None = None) -> str:
    """The feed title as the engine reads it (``engine.headline``: a trailing outlet segment and a
    leading wire tag dropped), else the title as stored (no engine)."""
    fn = _place_fn("headline")
    return fn(title, url) if fn is not None else (title or "")


def country_name(code: str) -> str:
    """``"TWN"`` → ``"Taiwan"`` (``engine.country_name``: pycountry's common name), else the code."""
    fn = _place_fn("country_name")
    return (fn(code) if fn is not None else None) or code


def copy_keys(source: str | None, type_: str | None, title: str | None, url: str | None) -> tuple[str, ...]:
    """The keys under which a stored news item is a copy of another (same normalised title or
    canonical URL, engine 1.3.3), or ``()``: not news, or no engine (then nothing is merged)."""
    fn = _place_fn("copy_keys")
    return tuple(fn(source, type_, title, url)) if fn is not None else ()
