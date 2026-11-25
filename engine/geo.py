"""Distances and hazard radii (risk-engine.md §5.2 ``geo_radius``)."""

from __future__ import annotations

import math

from engine.config import EngineConfig
from engine.models import FeedEvent

EARTH_RADIUS_KM = 6371.0088


def haversine_km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp, dl = p2 - p1, math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * EARTH_RADIUS_KM * math.asin(min(1.0, math.sqrt(a)))


def _number(v) -> float | None:
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return f if math.isfinite(f) else None


def radius_km(event: FeedEvent, config: EngineConfig) -> float:
    """``R`` for an event with a position."""
    if event.source == "scenario":
        r = _number(event.attrs.get("radius_km"))
        if r is not None:
            return r
    if event.type == "port.disruption":
        return float(config.radius_km["port.disruption"])
    hazard = event.type.removeprefix("hazard.") if event.type.startswith("hazard.") else "other"
    if hazard == "earthquake":
        m = _number(event.attrs.get("magnitude"))
        if m is None:
            return config.quake_default_km
        r = config.quake_base_km * 2 ** (m - config.quake_ref_magnitude)
        return min(config.quake_max_km, max(config.quake_min_km, r))
    return float(config.radius_km.get(hazard, config.radius_km["other"]))


def geo_confidence(distance_km: float, radius: float, config: EngineConfig) -> float | None:
    """``1 − d/R`` (at least 0.05 at the edge), or ``None`` outside the radius."""
    if radius <= 0 or distance_km > radius:
        return None
    return max(config.geo_min_confidence, 1.0 - distance_km / radius)
