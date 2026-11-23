"""NASA EONET v3 — open natural events from the last 30 days.

Docs: https://eonet.gsfc.nasa.gov/docs/v3
EONET gives no country or severity, so those stay empty. The position is the
most recent geometry point.
"""

from __future__ import annotations

import re
from datetime import timedelta

import httpx

from jobs.connectors.base import Batch, Connector, RunContext, parse_iso, safe_build
from jobs.http import get_json
from jobs.models import Event

URL = "https://eonet.gsfc.nasa.gov/api/v3/events?status=open&days=30"
CATEGORIES = {
    "wildfires": "wildfire",
    "volcanoes": "volcano",
    "severeStorms": "severe_storm",
    "floods": "flood",
    "earthquakes": "earthquake",
    "landslides": "landslide",
    "tempExtremes": "temperature_extreme",
    "seaLakeIce": "sea_lake_ice",
    "dustHaze": "dust_haze",
    "drought": "drought",
    "snow": "snow",
    "waterColor": "water_color",
    "manmade": "manmade",
}


def _category(cid: str) -> str:
    return CATEGORIES.get(cid) or re.sub(r"(?<!^)(?=[A-Z])", "_", cid).lower()


def parse(payload: dict, notes: dict[str, int] | None = None) -> list[Event]:
    notes = notes if notes is not None else {}
    events = []
    for e in payload.get("events") or []:
        if not e.get("id"):
            continue
        cats = [c.get("id") for c in e.get("categories") or [] if c.get("id")]
        points = [g for g in e.get("geometry") or [] if g.get("type") == "Point" and g.get("date")]
        points.sort(key=lambda g: g["date"])
        first, last = (points[0], points[-1]) if points else (None, None)
        coords = (last or {}).get("coordinates") or []
        ev = safe_build(
            Event,
            notes,
            source="eonet",
            raw_id=e["id"],
            type=f"hazard.{_category(cats[0])}" if cats else "hazard.other",
            title=e.get("title") or "",
            url=e.get("link"),
            published_at=parse_iso(first["date"]) if first else None,
            lat=coords[1] if len(coords) >= 2 else None,
            lon=coords[0] if len(coords) >= 2 else None,
            attrs={
                k: v
                for k, v in {
                    "categories": cats or None,
                    "sources": [s.get("id") for s in e.get("sources") or [] if s.get("id")] or None,
                    "last_observed": last["date"] if last else None,
                    "magnitude": (last or {}).get("magnitudeValue"),
                    "magnitude_unit": (last or {}).get("magnitudeUnit"),
                    "observations": len(points) or None,
                }.items()
                if v is not None
            },
        )
        if ev:
            events.append(ev)
    return events


class EonetConnector(Connector):
    name = "eonet"
    min_interval = timedelta(minutes=30)

    def fetch(self, client: httpx.Client, ctx: RunContext) -> Batch:
        batch = Batch()
        payload = get_json(client, URL, sleep=ctx.sleep, headers={"Accept": "application/json"})
        batch.events = parse(payload, batch.notes)
        return batch
