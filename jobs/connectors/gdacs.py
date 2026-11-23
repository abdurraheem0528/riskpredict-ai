"""GDACS current disaster alerts (GeoJSON event list).

Docs: https://www.gdacs.org/Knowledge/models.aspx
Severity is GDACS ``alertscore`` / 3 (GDACS scores 1 = Green, 2 = Orange,
3 = Red), clipped to 0–1.
"""

from __future__ import annotations

from datetime import timedelta

import httpx

from jobs import codes
from jobs.connectors.base import Batch, Connector, RunContext, parse_iso, safe_build
from jobs.http import get_json
from jobs.models import Event

URL = "https://www.gdacs.org/gdacsapi/api/events/geteventlist/EVENTS4APP"
HAZARDS = {
    "EQ": "earthquake",
    "TC": "tropical_cyclone",
    "FL": "flood",
    "VO": "volcano",
    "DR": "drought",
    "WF": "wildfire",
    "TS": "tsunami",
}


def _severity(score) -> float | None:
    if score is None:
        return None
    try:
        return max(0.0, min(1.0, float(score) / 3.0))
    except (TypeError, ValueError):
        return None


def parse(payload: dict, notes: dict[str, int] | None = None) -> list[Event]:
    notes = notes if notes is not None else {}
    events = []
    for f in payload.get("features") or []:
        p = f.get("properties") or {}
        etype, eid = p.get("eventtype"), p.get("eventid")
        if not etype or eid is None:
            continue
        coords = (f.get("geometry") or {}).get("coordinates") or []
        lon, lat = (coords[0], coords[1]) if len(coords) >= 2 and f["geometry"].get("type") == "Point" else (None, None)
        countries = [codes.country_alpha3(c.get("iso3")) for c in p.get("affectedcountries") or []]
        if not countries and p.get("iso3"):
            countries = [codes.country_alpha3(p["iso3"])]
        severity_data = p.get("severitydata") or {}
        ev = safe_build(
            Event,
            notes,
            source="gdacs",
            raw_id=f"{etype}-{eid}-{p.get('episodeid')}",
            type=f"hazard.{HAZARDS.get(etype, etype.lower())}",
            title=p.get("name") or p.get("description") or "",
            url=(p.get("url") or {}).get("report"),
            published_at=parse_iso(p.get("fromdate")),
            countries=[c for c in countries if c],
            severity=_severity(p.get("alertscore")),
            lat=lat,
            lon=lon,
            attrs={
                k: v
                for k, v in {
                    "alert_level": p.get("alertlevel"),
                    "alert_score": p.get("alertscore"),
                    "episode_alert_level": p.get("episodealertlevel"),
                    "to_date": p.get("todate"),
                    "modified": p.get("datemodified"),
                    "is_current": p.get("iscurrent"),
                    "severity_text": (severity_data.get("severitytext") or "").strip() or None,
                    "glide": p.get("glide"),
                }.items()
                if v not in (None, "")
            },
        )
        if ev:
            events.append(ev)
    return events


class GdacsConnector(Connector):
    name = "gdacs"
    min_interval = timedelta(minutes=15)

    def fetch(self, client: httpx.Client, ctx: RunContext) -> Batch:
        batch = Batch()
        batch.events = parse(get_json(client, URL, sleep=ctx.sleep), batch.notes)
        return batch
