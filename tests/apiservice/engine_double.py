"""A test double for ``engine`` (session C), used only in tests until C merges.

It follows the interface in docs/contracts/risk-engine.md just enough for the
worker and API tests: every entity whose countries overlap a feed event's
countries gets one risk with fixed likelihood and impact. It is **not** the
scoring model; the real numbers come from session C's engine.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any
from uuid import UUID

from psycopg.types.json import Jsonb

ENGINE_VERSION = "0.0.0-test-double"
LIKELIHOOD = 0.9
IMPACT = 0.8
CHOKEPOINTS = [{"id": "suez", "name": "Suez Canal", "aliases": ["Suez"], "lat": 30.6, "lon": 32.35, "radius_km": 150}]
calls: list[str] = []


def reset() -> None:
    global LIKELIHOOD
    LIKELIHOOD = 0.9
    calls.clear()


@dataclass(frozen=True, slots=True)
class Entity:
    id: UUID
    kind: str
    name: str
    countries: tuple[str, ...]
    lat: float | None
    lon: float | None


@dataclass(frozen=True, slots=True)
class Evidence:
    kind: str
    event_source: str
    event_raw_id: str
    match_type: str
    match_value: str
    match_confidence: float
    signal: float
    recency: float
    contribution: float
    snapshot: dict


@dataclass(frozen=True, slots=True)
class Assessment:
    fingerprint: str
    entity_id: UUID
    category: str
    title: str
    explanation: str
    likelihood: float
    impact: float
    factors: dict
    countries: tuple[str, ...]
    ports: tuple[str, ...]
    lat: float | None
    lon: float | None
    evidence: tuple[Evidence, ...]

    @property
    def score(self) -> float:
        return round(100 * self.likelihood * self.impact, 1)


@dataclass(frozen=True, slots=True)
class RiskChange:
    risk_id: UUID
    fingerprint: str
    change: str
    old_score: float | None
    new_score: float | None
    category: str


@dataclass(frozen=True, slots=True)
class RunResult:
    engine_version: str
    scored_entities: int
    changes: tuple[RiskChange, ...]
    counts: dict = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class FeedSnapshot:
    now: datetime
    events: tuple[dict, ...]


class ScenarioInputs:
    def __init__(self, data: dict):
        self.data = data

    @classmethod
    def from_dict(cls, data: dict) -> ScenarioInputs:
        return cls(data)


def load_entities(conn) -> list[Entity]:
    calls.append("load_entities")
    rows = conn.execute("SELECT id, kind, name, countries, lat, lon FROM biz.entities ORDER BY id").fetchall()
    return [Entity(r["id"], r["kind"], r["name"], tuple(r["countries"]), r["lat"], r["lon"]) for r in rows]


def load_feeds(conn, entities, *, now, config=None) -> FeedSnapshot:
    calls.append("load_feeds")
    rows = conn.execute("SELECT source, raw_id, title, countries FROM feeds.events ORDER BY source, raw_id").fetchall()
    return FeedSnapshot(now, tuple(rows))


def assess(entities, feeds, *, base_currency, config=None) -> list[Assessment]:
    calls.append("assess")
    out = []
    for e in entities:
        for ev in feeds.events:
            common = sorted(set(e.countries) & set(ev["countries"]))
            if not common:
                continue
            c = common[0]
            evid = Evidence("event", ev["source"], ev["raw_id"], "country", c, 1.0, 1.0, 1.0, LIKELIHOOD,
                            {"title": ev["title"], "countries": list(ev["countries"])})  # fmt: skip
            out.append(Assessment(f"{e.id}|natural_hazard|country:{c}", e.id, "natural_hazard", f"Hazard affecting {e.name}",
                                  "test double", LIKELIHOOD, IMPACT, {"engine_version": ENGINE_VERSION}, (c,), (),
                                  e.lat, e.lon, (evid,)))  # fmt: skip
            break
    return out


def write_risks(conn, workspace_id, assessments, *, now) -> RunResult:
    calls.append("write_risks")
    changes = []
    seen = set()
    for a in assessments:
        old = conn.execute("SELECT id, score FROM biz.risks WHERE fingerprint = %s", (a.fingerprint,)).fetchone()
        row = conn.execute(
            "INSERT INTO biz.risks (workspace_id, entity_id, fingerprint, category, title, explanation, likelihood, impact,"
            " factors, countries, lat, lon, engine_version) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)"
            " ON CONFLICT (workspace_id, fingerprint) DO UPDATE SET likelihood = EXCLUDED.likelihood,"
            " last_seen_at = now(), last_scored_at = now() RETURNING id, score",
            (workspace_id, a.entity_id, a.fingerprint, a.category, a.title, a.explanation, a.likelihood, a.impact,
             Jsonb(a.factors), list(a.countries), a.lat, a.lon, ENGINE_VERSION),
        ).fetchone()  # fmt: skip
        seen.add(row["id"])
        for ev in a.evidence:
            conn.execute(
                "INSERT INTO biz.risk_evidence (workspace_id, risk_id, kind, event_source, event_raw_id, match_type,"
                " match_value, match_confidence, signal, recency, contribution, snapshot)"
                " VALUES (%s,%s,'event',%s,%s,%s,%s,%s,%s,%s,%s,%s) ON CONFLICT DO NOTHING",
                (workspace_id, row["id"], ev.event_source, ev.event_raw_id, ev.match_type, ev.match_value,
                 ev.match_confidence, ev.signal, ev.recency, ev.contribution, Jsonb(ev.snapshot)),
            )  # fmt: skip
        if old is None:
            conn.execute(
                "INSERT INTO biz.risk_history (workspace_id, risk_id, likelihood, impact, status, reason, engine_version)"
                " VALUES (%s, %s, %s, %s, 'open', 'created', %s)",
                (workspace_id, row["id"], a.likelihood, a.impact, ENGINE_VERSION),
            )
            changes.append(RiskChange(row["id"], a.fingerprint, "created", None, row["score"], a.category))
        elif abs(old["score"] - row["score"]) >= 1:
            changes.append(RiskChange(row["id"], a.fingerprint, "rescored", old["score"], row["score"], a.category))
    for r in conn.execute("SELECT id, fingerprint, score, category FROM biz.risks WHERE status IN ('open','acknowledged','mitigating')").fetchall():
        if r["id"] not in seen:
            conn.execute("UPDATE biz.risks SET status = 'resolved', status_changed_at = now() WHERE id = %s", (r["id"],))
            changes.append(RiskChange(r["id"], r["fingerprint"], "resolved", r["score"], None, r["category"]))
    counts = {k: sum(1 for c in changes if c.change == k) for k in ("created", "rescored", "resolved", "reopened")}
    return RunResult(ENGINE_VERSION, len({a.entity_id for a in assessments}), tuple(changes), counts)


def run_workspace(conn, workspace_id, *, now=None, config=None) -> RunResult:
    ents = load_entities(conn)
    return write_risks(conn, workspace_id, assess(ents, load_feeds(conn, ents, now=now), base_currency=None), now=now)


def run_scenario(conn, workspace_id, inputs, *, now=None) -> dict:
    calls.append("run_scenario")
    assert isinstance(inputs, ScenarioInputs)
    side = {"risk_count": 0, "max_score": None, "portfolio_score": 0.0, "by_category": {}}
    return {"baseline": side, "scenario": side, "changes": [], "engine_version": ENGINE_VERSION,
            "as_of": "2026-11-24T12:00:00Z"}  # fmt: skip


def watch_terms_for(entities) -> list[tuple[str, str, str]]:
    return [("osv_package", "requests", "PyPI")] if entities else []


def advice_messages(risk: Assessment, entity: Any) -> list[dict]:
    return [
        {"role": "system", "content": "Plain text, at most 120 words."},
        {"role": "user", "content": f"{risk.title}. Score {risk.score}. Likelihood {risk.likelihood}."},
    ]


def template_advice(risk: Assessment, entity: Any) -> str:
    return "Check the supplier's continuity plan and identify an alternative source."


def needs_new_advice(old_score, new_score, old_evidence, new_evidence) -> bool:
    return old_score is None or int(old_score // 20) != int(new_score // 20) or set(old_evidence) != set(new_evidence)
