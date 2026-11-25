"""Common records produced by every connector.

Connectors only ever emit what a source actually published. Optional fields
stay empty (``None`` or an empty tuple) when the source does not provide them;
nothing is guessed or filled in.
"""

from __future__ import annotations

import enum
import math
import re
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from typing import Any

from jobs import codes

_LOCODE_RE = re.compile(r"^[A-Z]{2}[A-Z2-9]{3}$")


def _utc(value: datetime | None, name: str) -> datetime | None:
    if value is None:
        return None
    if not isinstance(value, datetime):
        raise TypeError(f"{name} must be a datetime")
    if value.tzinfo is None:
        raise ValueError(f"{name} must be timezone-aware")
    return value.astimezone(timezone.utc)


def _clean_tuple(values: Any) -> tuple[str, ...]:
    """Deduplicate while keeping order; drop empty strings."""
    if values is None:
        return ()
    if isinstance(values, str):
        values = (values,)
    seen: dict[str, None] = {}
    for v in values:
        if v is None:
            continue
        s = str(v).strip()
        if s:
            seen.setdefault(s, None)
    return tuple(seen)


@dataclass(frozen=True, slots=True)
class Event:
    """A discrete thing that happened (an article, an advisory, a hazard...).

    ``countries`` are ISO 3166-1 alpha-3 codes, ``ports`` UN/LOCODEs (5 chars,
    no space), ``currencies`` ISO 4217 codes. ``software`` entries are
    ``"<vendor or ecosystem>:<product or package>"`` exactly as the source
    names them. ``severity`` is 0–1 and only set when the source publishes a
    severity measure (each connector documents how it maps that measure).
    """

    source: str
    raw_id: str
    type: str
    title: str
    url: str | None = None
    published_at: datetime | None = None
    countries: tuple[str, ...] = ()
    ports: tuple[str, ...] = ()
    commodities: tuple[str, ...] = ()
    currencies: tuple[str, ...] = ()
    software: tuple[str, ...] = ()
    severity: float | None = None
    lat: float | None = None
    lon: float | None = None
    attrs: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        set_ = object.__setattr__
        if not self.source or not self.raw_id or not self.type:
            raise ValueError("source, raw_id and type are required")
        set_(self, "raw_id", str(self.raw_id))
        set_(self, "title", (self.title or "").strip())
        set_(self, "published_at", _utc(self.published_at, "published_at"))

        countries = _clean_tuple(self.countries)
        for c in countries:
            if not codes.is_country(c):
                raise ValueError(f"not an ISO 3166-1 alpha-3 code: {c!r}")
        set_(self, "countries", countries)

        ports = _clean_tuple(self.ports)
        for p in ports:
            if not _LOCODE_RE.match(p):
                raise ValueError(f"not a UN/LOCODE: {p!r}")
        set_(self, "ports", ports)

        currencies = _clean_tuple(self.currencies)
        for c in currencies:
            if not codes.is_currency(c):
                raise ValueError(f"not an ISO 4217 code: {c!r}")
        set_(self, "currencies", currencies)

        set_(self, "commodities", _clean_tuple(self.commodities))
        set_(self, "software", _clean_tuple(self.software))

        if self.severity is not None:
            sev = float(self.severity)
            if math.isnan(sev) or not 0.0 <= sev <= 1.0:
                raise ValueError(f"severity must be within 0..1, got {self.severity!r}")
            set_(self, "severity", sev)
        if self.lat is not None and not -90.0 <= float(self.lat) <= 90.0:
            raise ValueError("lat out of range")
        if self.lon is not None and not -180.0 <= float(self.lon) <= 180.0:
            raise ValueError("lon out of range")
        if (self.lat is None) != (self.lon is None):
            raise ValueError("lat and lon must be given together")


@dataclass(frozen=True, slots=True)
class SeriesPoint:
    """One observation of a numeric time series.

    ``series`` names the measure (e.g. ``fx.rate``), ``key`` the subject
    (e.g. ``EUR/USD``, an ISO3 country code or a CVE id).
    """

    source: str
    series: str
    key: str
    ts: datetime
    value: float
    unit: str | None = None
    attrs: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not self.source or not self.series or not self.key:
            raise ValueError("source, series and key are required")
        object.__setattr__(self, "ts", _utc(self.ts, "ts"))
        value = float(self.value)
        if math.isnan(value) or math.isinf(value):
            raise ValueError("value must be a finite number")
        object.__setattr__(self, "value", value)


@dataclass(frozen=True, slots=True)
class SanctionEntry:
    """One entity on a full sanctions list (``feeds.sanctions_entries``).

    Unlike an :class:`Event`, this is current state, not a happening: the
    whole list is replaced each time it is read in full. ``name`` is the
    primary name; ``aliases`` are the other names OFAC publishes (all scripts),
    ``weak_aliases`` the ones it flags as low quality. ``countries`` are
    ISO 3166-1 alpha-3, from addresses, nationality, citizenship and flag.
    """

    source: str
    dataset: str
    entity_id: str
    name: str
    entity_type: str | None = None
    aliases: tuple[str, ...] = ()
    weak_aliases: tuple[str, ...] = ()
    countries: tuple[str, ...] = ()
    programs: tuple[str, ...] = ()
    lists: tuple[str, ...] = ()
    listed_on: date | None = None

    def __post_init__(self) -> None:
        set_ = object.__setattr__
        if not self.source or not self.dataset or not self.entity_id:
            raise ValueError("source, dataset and entity_id are required")
        name = (self.name or "").strip()
        if not name:
            raise ValueError("name is required")
        set_(self, "name", name)
        set_(self, "entity_id", str(self.entity_id))
        aliases = tuple(a for a in _clean_tuple(self.aliases) if a != name)
        set_(self, "aliases", aliases)
        set_(self, "weak_aliases", tuple(a for a in _clean_tuple(self.weak_aliases) if a != name and a not in aliases))
        countries = _clean_tuple(self.countries)
        for c in countries:
            if not codes.is_country(c):
                raise ValueError(f"not an ISO 3166-1 alpha-3 code: {c!r}")
        set_(self, "countries", tuple(sorted(countries)))
        set_(self, "programs", _clean_tuple(self.programs))
        set_(self, "lists", _clean_tuple(self.lists))
        if self.listed_on is not None and not isinstance(self.listed_on, date):
            raise TypeError("listed_on must be a date")


class Status(str, enum.Enum):
    OK = "ok"  # fetched and got records
    EMPTY = "empty"  # fetched fine, source had nothing for us
    PARTIAL = "partial"  # some sub-requests failed, some records kept
    THROTTLED = "throttled"  # source rate-limited us; nothing fetched
    ERROR = "error"  # failed; no records
    SKIPPED = "skipped"  # not due yet (min interval) or nothing to query


@dataclass(slots=True)
class FetchResult:
    """What one connector run produced. ``error`` never holds record contents."""

    source: str
    status: Status
    events: list[Event] = field(default_factory=list)
    series: list[SeriesPoint] = field(default_factory=list)
    error: str | None = None
    duration_ms: int = 0
    notes: dict[str, int] = field(default_factory=dict)
    sanctions: list[SanctionEntry] = field(default_factory=list)
    # Sanctions datasets read completely this run: their stored rows are
    # replaced (entries no longer listed are deleted). Others are only upserted.
    replace_datasets: tuple[str, ...] = ()

    @property
    def record_count(self) -> int:
        return len(self.events) + len(self.series) + len(self.sanctions)
