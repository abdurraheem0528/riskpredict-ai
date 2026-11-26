"""Static reference tables shipped with the engine (risk-engine.md §5.3)."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

DATA = Path(__file__).resolve().parent / "data"


@dataclass(frozen=True, slots=True)
class Chokepoint:
    id: str
    name: str
    lat: float
    lon: float
    radius_km: float
    aliases: tuple[str, ...]


@lru_cache(maxsize=1)
def chokepoints() -> tuple[Chokepoint, ...]:
    """The chokepoint table, in file order (the API serves it on the disruptions layer)."""
    raw = json.loads((DATA / "chokepoints.json").read_text(encoding="utf-8"))["chokepoints"]
    out = tuple(
        Chokepoint(c["id"], c["name"], float(c["lat"]), float(c["lon"]), float(c["radius_km"]),
                   tuple(a.casefold() for a in c["aliases"]))
        for c in raw
    )  # fmt: skip
    if len({c.id for c in out}) != len(out):
        raise ValueError("duplicate chokepoint id")
    return out


@lru_cache(maxsize=1)
def _chokepoint_lookup() -> dict[str, str]:
    """casefold id / name / alias → id."""
    out: dict[str, str] = {}
    for c in chokepoints():
        for k in (c.id, c.name.casefold(), *c.aliases):
            out.setdefault(k.casefold(), c.id)
    return out


def chokepoint_id(value: str | None) -> str | None:
    """Resolve a chokepoint id, name or alias (casefold); ``None`` if unknown."""
    if not value:
        return None
    k = re.sub(r"\s+", " ", str(value)).strip().casefold()
    return _chokepoint_lookup().get(k) or _chokepoint_lookup().get(k.replace(" ", "_"))


def chokepoint_by_id(cid: str) -> Chokepoint | None:
    return next((c for c in chokepoints() if c.id == cid), None)


def chokepoint_by_alias(text: str) -> Chokepoint | None:
    """Exact (casefold, whitespace-collapsed) alias match, for tagged place names."""
    k = re.sub(r"\s+", " ", text).strip().casefold()
    for c in chokepoints():
        if k in c.aliases:
            return c
    return None


@dataclass(frozen=True, slots=True)
class SeaLane:
    id: str
    name: str
    countries: tuple[str, ...]
    chokepoints: tuple[str, ...]


@lru_cache(maxsize=1)
def sea_lanes() -> tuple[SeaLane, ...]:
    """The coarse sea-lane table (§5.9): origin region → chokepoints on its main lanes."""
    raw = json.loads((DATA / "sea_lanes.json").read_text(encoding="utf-8"))["regions"]
    known = {c.id for c in chokepoints()}
    out = tuple(SeaLane(r["id"], r["name"], tuple(r["countries"]), tuple(r["chokepoints"])) for r in raw)
    seen: set[str] = set()
    for lane in out:
        if set(lane.chokepoints) - known:
            raise ValueError(f"sea lane {lane.id}: unknown chokepoint")
        if seen & set(lane.countries):
            raise ValueError(f"sea lane {lane.id}: a country is in two regions")
        seen |= set(lane.countries)
    return out


@lru_cache(maxsize=512)
def lane_for_country(alpha3: str) -> SeaLane | None:
    return next((lane for lane in sea_lanes() if alpha3 in lane.countries), None)


@lru_cache(maxsize=1)
def hs_chapter_keywords() -> dict[str, tuple[str, ...]]:
    raw = json.loads((DATA / "hs_chapters.json").read_text(encoding="utf-8"))["chapters"]
    return {ch: tuple(k.casefold() for k in kws) for ch, kws in raw.items()}


def keyword_in(text: str, keyword: str) -> bool:
    """``keyword`` occurs in ``text`` as whole word(s), casefold."""
    return re.search(rf"(?<![\w]){re.escape(keyword)}(?![\w])", text.casefold()) is not None
