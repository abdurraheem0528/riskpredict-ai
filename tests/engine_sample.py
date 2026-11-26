"""The 11-supplier sample (tests/fixtures/engine_sample_feeds.json) through the mapper and the engine.

Test data only. Used by tests/test_engine_sample.py, and runnable on its own to print the
risk count, the score spread and every risk, e.g. to compare two engine versions:

    python tests/engine_sample.py                       # this checkout
    PYTHONPATH=<other checkout> python tests/engine_sample.py   # another one
    python tests/engine_sample.py tests/fixtures/engine_live_nov27_feeds.json   # another fixture

Every function takes an optional ``fixture`` path; the default is engine_sample_feeds.json.
LIVE_NOV27 (the 27 Nov live sheet, 1.2.0 → 1.3.0) has the same shape.
"""

from __future__ import annotations

import csv
import json
import sys
import uuid
from collections import Counter
from datetime import timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:  # pragma: no cover - when run as a script
    sys.path.append(str(ROOT))

from engine import Anomaly, Entity, EventTag, FeedEvent, FeedSnapshot, assess
from engine.db import clean_entity

FIXTURE = Path(__file__).resolve().parent / "fixtures" / "engine_sample_feeds.json"
LIVE_NOV27 = FIXTURE.with_name("engine_live_nov27_feeds.json")


def _now():
    from engine_fixtures import NOW

    return NOW


def data(fixture: Path | None = None) -> dict:
    return json.loads((fixture or FIXTURE).read_text(encoding="utf-8"))


def suppliers(fixture: Path | None = None) -> list[Entity]:
    """The sheet, mapped with the header heuristics (what the live test got) and applied."""
    from api.core import mapping

    rows = list(csv.reader(data(fixture)["suppliers_csv"]))
    header, body = rows[0], rows[1:]
    columns = [{"name": h, "samples": [r[i] for r in body[:5]]} for i, h in enumerate(header)]
    spec = mapping.heuristic_suggest(columns)
    raw = [mapping.RawRow(i + 2, None, dict(zip(header, r))) for i, r in enumerate(body)]
    result = mapping.apply(spec, raw, chokepoints=lambda v: None)
    out = []
    for e in result.entities:
        ent = Entity(
            id=uuid.uuid5(uuid.NAMESPACE_URL, f"riskpredict-test:sample:{e.row_key}"), kind=e.kind, name=e.name,
            countries=tuple(e.countries), ports=tuple(e.ports), currencies=tuple(e.currencies),
            hs_codes=tuple(e.hs_codes), commodities=tuple(e.commodities), software=tuple(e.software),
            lat=e.lat, lon=e.lon, annual_spend=e.annual_spend, spend_currency=e.spend_currency,
            revenue_share=e.revenue_share, criticality=e.criticality, attrs=e.attrs,
        )  # fmt: skip
        out.append(clean_entity(ent)[0])
    return out


def feeds(extra_events=(), fixture: Path | None = None) -> FeedSnapshot:
    d, now = data(fixture), _now()
    events = []
    for r in d["events"]:
        t = r.get("tag")
        tag = None
        if t is not None:
            tag = EventTag(
                t["event_type"], t.get("confidence"), t.get("source", "model"),
                {"companies": [{"text": x, "confidence": c} for x, c in t["companies"]],
                 "places": [{"text": x, "confidence": c, "country": None} for x, c in t["places"]],
                 "commodities": [{"text": x, "confidence": c} for x, c in t["commodities"]],
                 "countries": t["countries"], "type_guess": {"type": t["event_type"], "confidence": t.get("confidence")}},
            )  # fmt: skip
        at = now - timedelta(hours=r["age_h"])
        events.append(FeedEvent(
            source=r["source"], raw_id=r["raw_id"], type=r["type"], title=r["title"], published_at=at,
            first_seen_at=at, countries=tuple(r.get("countries", ())), ports=tuple(r.get("ports", ())),
            severity=r.get("severity"), lat=r.get("lat"), lon=r.get("lon"), attrs=r.get("attrs", {}), tag=tag,
        ))  # fmt: skip
    anomalies = [
        Anomaly(a["series"], a["key"], now - timedelta(hours=a["age_h"]), a["method"], a["value"], a["baseline"],
                a["score"], {"window_days": 90})
        for a in d["anomalies"]
    ]
    return FeedSnapshot(now=now, events=(*events, *extra_events), anomalies=tuple(anomalies))


def run(fixture: Path | None = None):
    ents = suppliers(fixture)
    return ents, assess(ents, feeds(fixture=fixture), base_currency=None)


def summary(ents, out) -> dict:
    names = {e.id: e.name for e in ents}
    scores = sorted((a.score for a in out), reverse=True)
    stories = {(a.factors.get("story") or a.fingerprint) for a in out}

    def band(s):
        return "critical" if s >= 80 else "high" if s >= 60 else "medium" if s >= 40 else "low"

    return {
        "risks": len(out),
        "stories": len(stories),
        "max": scores[0] if scores else None,
        "median": scores[len(scores) // 2] if scores else None,
        "bands": {b: sum(1 for s in scores if band(s) == b) for b in ("critical", "high", "medium", "low")},
        "by_category": dict(sorted(Counter(a.category for a in out).items())),
        "max_likelihood": max((a.likelihood for a in out), default=None),
        "likely": sum(1 for a in out if a.likelihood >= 0.8),  # risks reading "80% likely" or more
        "items": [
            {"entity": names[a.entity_id], "category": a.category, "score": a.score,
             "driver": a.fingerprint.split("|", 2)[2], "title": a.title}
            for a in out
        ],
    }


if __name__ == "__main__":  # pragma: no cover
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    print(json.dumps(summary(*run(Path(sys.argv[1]) if len(sys.argv) > 1 else None)), indent=1, ensure_ascii=False))
