"""Builders for risk-engine tests. Every entity, event and anomaly here is test data only."""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone
from decimal import Decimal

from engine import Anomaly, Entity, EventTag, FeedEvent, FeedSnapshot

NOW = datetime(2026, 11, 24, 12, tzinfo=timezone.utc)


def uid(label: str) -> uuid.UUID:
    """Stable ids, so fingerprints are the same on every run."""
    return uuid.uuid5(uuid.NAMESPACE_URL, f"riskpredict-test:{label}")


def ent(label: str, kind: str = "supplier", name: str | None = None, **kw) -> Entity:
    for k in ("countries", "ports", "currencies", "hs_codes", "commodities", "software"):
        if k in kw:
            kw[k] = tuple(kw[k])
    if "annual_spend" in kw and kw["annual_spend"] is not None:
        kw["annual_spend"] = Decimal(str(kw["annual_spend"]))
        kw.setdefault("spend_currency", "EUR")
    if "revenue_share" in kw and kw["revenue_share"] is not None:
        kw["revenue_share"] = Decimal(str(kw["revenue_share"]))
    return Entity(id=uid(label), kind=kind, name=name or label, **kw)


def tag(event_type: str, confidence: float | None = 0.8, *, source: str = "model", companies=(), places=(),
        commodities=(), countries=()) -> EventTag:  # fmt: skip
    return EventTag(
        event_type,
        confidence,
        source,
        {
            "companies": [{"text": t, "confidence": c} for t, c in companies],
            "places": [{"text": t, "confidence": c, "country": None} for t, c in places],
            "commodities": [{"text": t, "confidence": c} for t, c in commodities],
            "countries": list(countries),
            "type_guess": {"type": event_type, "confidence": confidence},
        },
    )


def event(source: str, raw_id: str, type_: str, *, age_h: float = 0.0, title: str | None = None, **kw) -> FeedEvent:
    for k in ("countries", "ports", "commodities", "currencies", "software"):
        if k in kw:
            kw[k] = tuple(kw[k])
    published = NOW - timedelta(hours=age_h)
    kw.setdefault("first_seen_at", published)
    return FeedEvent(source=source, raw_id=raw_id, type=type_, title=title or f"{source} {raw_id}",
                     published_at=published, **kw)  # fmt: skip


def quake(raw_id: str, lat: float, lon: float, *, magnitude: float = 6.0, severity: float = 0.6, age_h: float = 0,
          countries=()) -> FeedEvent:  # fmt: skip
    return event("usgs", raw_id, "hazard.earthquake", age_h=age_h, lat=lat, lon=lon, severity=severity,
                 countries=countries, attrs={"magnitude": magnitude})  # fmt: skip


# Engine 1.3.4 (risk-engine.md §5.10 C): a news country match needs a cue word of the tag's category
# in the headline. A test item without a title of its own gets one that has such a word, so tests
# about something else (confidence, impact, scenarios) keep testing what they did.
CUE_TITLES = {
    "conflict": "Military clash reported", "protest": "Protesters rally in the capital",
    "trade_policy": "New tariffs announced", "port_shipping_disruption": "Port congestion delays cargo",
    "natural_hazard": "Floods hit the region", "labour_action": "Workers strike over pay",
    "financial_stress": "Currency crisis deepens",
}


def news(raw_id: str, t: EventTag, *, age_h: float = 0, countries=(), title: str | None = None,
         url: str | None = None) -> FeedEvent:  # fmt: skip
    if title is None and t is not None and t.event_type in CUE_TITLES:
        title = f"{CUE_TITLES[t.event_type]} (gdelt {raw_id})"
    return event("gdelt", raw_id, "news", age_h=age_h, tag=t, countries=countries, title=title, url=url)


def ofac(raw_id: str, name: str, *, type_: str = "sanction.designation", countries=(), aliases=None, age_h=24.0,
         entity_id: str | None = None, **attrs) -> FeedEvent:  # fmt: skip
    a = {"name": name, "entity_id": entity_id or raw_id, **attrs}
    if aliases is not None:
        a["aliases"] = aliases
    return event("ofac", raw_id, type_, age_h=age_h, countries=countries, attrs=a)


def anomaly(series: str, key: str, *, method: str = "robust_z", score: float = 5.0, age_h: float = 24.0,
            value: float = 1.0, baseline: float = 1.0, **attrs) -> Anomaly:  # fmt: skip
    return Anomaly(series, key, NOW - timedelta(hours=age_h), method, value, baseline, score,
                   {"window_days": 90, **attrs})  # fmt: skip


def snap(events=(), anomalies=(), series=None, now: datetime = NOW) -> FeedSnapshot:
    return FeedSnapshot(now=now, events=tuple(events), anomalies=tuple(anomalies), series=series or {})


def fx_history(ccy: str, days: int = 120, start: float = 1.0, *, jump_last: float | None = None,
               now: datetime = NOW) -> dict:  # fmt: skip
    """A EUR/<ccy> series with ~0.5 % daily moves (deterministic), optionally with a last-day jump."""
    import math

    pts = []
    v = start
    day0 = (now - timedelta(days=days)).replace(hour=0, minute=0, second=0, microsecond=0)
    for i in range(days):
        v *= math.exp(0.005 * math.sin(i * 2.3 + len(ccy)))
        pts.append((day0 + timedelta(days=i), round(v, 6)))
    if jump_last is not None:
        ts, last = pts[-1]
        pts[-1] = (ts, round(pts[-2][1] * (1 + jump_last), 6))
    return {("fx.rate", f"EUR/{ccy}"): tuple(pts)}
