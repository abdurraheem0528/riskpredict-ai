"""The engine's data classes (docs/contracts/risk-engine.md §2). All frozen, with slots."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal
from types import MappingProxyType
from typing import Any, Mapping
from uuid import UUID

EMPTY: Mapping[str, Any] = MappingProxyType({})


def rfc3339(ts: datetime | None) -> str | None:
    """``2026-11-20T03:12:00Z`` (UTC, seconds; microseconds kept only when present)."""
    if ts is None:
        return None
    ts = ts.astimezone(timezone.utc)
    fmt = "%Y-%m-%dT%H:%M:%S.%fZ" if ts.microsecond else "%Y-%m-%dT%H:%M:%SZ"
    return ts.strftime(fmt)


@dataclass(frozen=True, slots=True)
class Entity:
    """Exactly the ``biz.entities`` columns of the same name."""

    id: UUID
    kind: str
    name: str
    countries: tuple[str, ...] = ()
    ports: tuple[str, ...] = ()
    currencies: tuple[str, ...] = ()
    hs_codes: tuple[str, ...] = ()
    commodities: tuple[str, ...] = ()
    software: tuple[str, ...] = ()
    lat: float | None = None
    lon: float | None = None
    annual_spend: Decimal | None = None
    spend_currency: str | None = None
    revenue_share: Decimal | None = None
    criticality: str | None = None
    attrs: Mapping[str, Any] = EMPTY
    city: str | None = None  # only words the advice prompt's Place: line (risk-engine.md §8); never matched on

    @property
    def has_position(self) -> bool:
        return self.lat is not None and self.lon is not None


@dataclass(frozen=True, slots=True)
class EventTag:
    event_type: str
    type_confidence: float | None
    type_source: str
    entities: Mapping[str, Any] = EMPTY


@dataclass(frozen=True, slots=True)
class FeedEvent:
    source: str
    raw_id: str
    type: str
    title: str
    url: str | None = None
    published_at: datetime | None = None
    first_seen_at: datetime | None = None
    countries: tuple[str, ...] = ()
    ports: tuple[str, ...] = ()
    commodities: tuple[str, ...] = ()
    currencies: tuple[str, ...] = ()
    software: tuple[str, ...] = ()
    severity: float | None = None
    lat: float | None = None
    lon: float | None = None
    attrs: Mapping[str, Any] = EMPTY
    tag: EventTag | None = None  # newest tag row, if any

    @property
    def key(self) -> str:
        return f"event:{self.source}:{self.raw_id}"

    @property
    def has_position(self) -> bool:
        return self.lat is not None and self.lon is not None


@dataclass(frozen=True, slots=True)
class Anomaly:
    series: str
    key: str
    ts: datetime
    method: str
    value: float
    baseline: float
    score: float
    attrs: Mapping[str, Any] = EMPTY

    @property
    def evidence_key(self) -> str:
        return f"anomaly:{self.series}|{self.key}|{rfc3339(self.ts)}|{self.method}"


@dataclass(frozen=True, slots=True)
class FeedSnapshot:
    now: datetime
    events: tuple[FeedEvent, ...] = ()
    anomalies: tuple[Anomaly, ...] = ()
    # ('fx.rate', 'EUR/<CCY>') → ((ts, value), ...) ascending, for conversion and cross
    # rates; ('epss.score', '<CVE>') → the latest point only.
    series: Mapping[tuple[str, str], tuple[tuple[datetime, float], ...]] = EMPTY
    source_health: Mapping[str, datetime | None] = EMPTY  # source → last_success_at


@dataclass(frozen=True, slots=True)
class Evidence:
    kind: str  # 'event' | 'anomaly'
    match_type: str
    match_value: str
    match_confidence: float
    signal: float
    recency: float
    contribution: float
    snapshot: Mapping[str, Any] = EMPTY
    event_source: str | None = None
    event_raw_id: str | None = None
    anomaly_series: str | None = None
    anomaly_key: str | None = None
    anomaly_ts: datetime | None = None
    anomaly_method: str | None = None

    @property
    def evidence_key(self) -> str:
        if self.kind == "event":
            return f"event:{self.event_source}:{self.event_raw_id}"
        return f"anomaly:{self.anomaly_series}|{self.anomaly_key}|{rfc3339(self.anomaly_ts)}|{self.anomaly_method}"


@dataclass(frozen=True, slots=True)
class Assessment:
    fingerprint: str
    entity_id: UUID
    category: str
    title: str
    explanation: str
    likelihood: float
    impact: float
    factors: Mapping[str, Any]
    countries: tuple[str, ...] = ()
    ports: tuple[str, ...] = ()
    lat: float | None = None
    lon: float | None = None
    evidence: tuple[Evidence, ...] = ()

    @property
    def score(self) -> float:
        return round(100 * self.likelihood * self.impact, 1)

    @property
    def evidence_keys(self) -> tuple[str, ...]:
        return tuple(e.evidence_key for e in self.evidence)


@dataclass(frozen=True, slots=True)
class RiskChange:
    risk_id: UUID
    fingerprint: str
    change: str  # 'created' | 'rescored' | 'resolved' | 'reopened'
    old_score: float | None
    new_score: float | None
    category: str


@dataclass(frozen=True, slots=True)
class RunResult:
    engine_version: str
    scored_entities: int
    changes: tuple[RiskChange, ...] = ()
    counts: Mapping[str, int] = EMPTY  # created, rescored, unchanged, resolved, reopened
    # Filled by run_workspace (empty from write_risks), so the worker can word advice and
    # pick watch terms without loading or scoring again: the assessments written, and
    # the entities they were scored from.
    assessments: tuple[Assessment, ...] = ()
    entities: tuple[Entity, ...] = ()
