"""USGS earthquake GeoJSON summary feed (M4.5+, past day).

Docs: https://earthquake.usgs.gov/earthquakes/feed/v1.0/geojson.php
Severity is the USGS significance number ``sig`` / 1000, clipped to 0–1
(USGS: "the larger the number, the more significant the event"; 600+ is
treated as significant by USGS itself). The country is read from the end of
USGS's ``place`` text only when it names a country or US state exactly.
"""

from __future__ import annotations

from datetime import timedelta

import httpx

from jobs import codes
from jobs.connectors.base import Batch, Connector, RunContext, safe_build, utc_from_ms
from jobs.http import get_json
from jobs.models import Event

URL = "https://earthquake.usgs.gov/earthquakes/feed/v1.0/summary/4.5_day.geojson"


def place_country(place: str | None) -> str | None:
    if not place or "," not in place:
        return None
    tail = place.rsplit(",", 1)[1].strip()
    if codes._norm(tail) in codes.US_STATES:
        return "USA"
    return codes.country_alpha3(tail)


def parse(payload: dict, notes: dict[str, int] | None = None) -> list[Event]:
    notes = notes if notes is not None else {}
    events = []
    for f in payload.get("features") or []:
        p = f.get("properties") or {}
        if not f.get("id"):
            continue
        coords = (f.get("geometry") or {}).get("coordinates") or []
        country = place_country(p.get("place"))
        sig = p.get("sig")
        ev = safe_build(
            Event,
            notes,
            source="usgs",
            raw_id=f["id"],
            type="hazard.earthquake",
            title=p.get("title") or p.get("place") or "",
            url=p.get("url"),
            published_at=utc_from_ms(p.get("time")),
            countries=[country] if country else [],
            severity=min(1.0, max(0.0, sig / 1000.0)) if isinstance(sig, (int, float)) else None,
            lat=coords[1] if len(coords) >= 2 else None,
            lon=coords[0] if len(coords) >= 2 else None,
            attrs={
                k: v
                for k, v in {
                    "magnitude": p.get("mag"),
                    "mag_type": p.get("magType"),
                    "depth_km": coords[2] if len(coords) >= 3 else None,
                    "place": p.get("place"),
                    "alert": p.get("alert"),
                    "tsunami": p.get("tsunami"),
                    "sig": sig,
                    "status": p.get("status"),
                    "updated": utc_from_ms(p.get("updated")).isoformat() if p.get("updated") else None,
                }.items()
                if v is not None
            },
        )
        if ev:
            events.append(ev)
    return events


class UsgsConnector(Connector):
    name = "usgs"
    min_interval = timedelta(minutes=15)

    def fetch(self, client: httpx.Client, ctx: RunContext) -> Batch:
        batch = Batch()
        batch.events = parse(get_json(client, URL, sleep=ctx.sleep), batch.notes)
        return batch
