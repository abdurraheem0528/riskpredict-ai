from __future__ import annotations

import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Callable, ClassVar

import httpx

from jobs.http import SourceError, Throttled
from jobs.models import Event, FetchResult, SeriesPoint, Status


@dataclass
class WatchTerm:
    kind: str  # 'gdelt_query' | 'osv_package'
    term: str
    scope: str = ""  # e.g. the OSV ecosystem


def _sleep(seconds: float) -> None:
    time.sleep(seconds)  # looked up at call time, so tests can stub it


@dataclass
class RunContext:
    now: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    watch_terms: list[WatchTerm] = field(default_factory=list)
    sleep: Callable[[float], None] = _sleep
    # Each source's notes from its previous run (feeds.source_health.notes), e.g.
    # the last GDELT file window stored. Empty on a dry run.
    state: dict[str, dict] = field(default_factory=dict)

    def terms(self, kind: str) -> list[WatchTerm]:
        return [t for t in self.watch_terms if t.kind == kind]


@dataclass
class Batch:
    events: list[Event] = field(default_factory=list)
    series: list[SeriesPoint] = field(default_factory=list)
    status: Status | None = None  # override (e.g. PARTIAL); None = derive
    error: str | None = None
    notes: dict[str, int] = field(default_factory=dict)


class Connector:
    """One public source. Subclasses implement :meth:`fetch`.

    ``fetch`` returns only records parsed from the source; on failure it
    raises :class:`SourceError`, which :meth:`run` turns into a status.
    """

    name: ClassVar[str]
    # Minimum time between successful runs when a database is available.
    min_interval: ClassVar[timedelta] = timedelta(minutes=15)

    def fetch(self, client: httpx.Client, ctx: RunContext) -> Batch:  # pragma: no cover
        raise NotImplementedError

    def run(self, client: httpx.Client, ctx: RunContext) -> FetchResult:
        started = time.monotonic()
        try:
            batch = self.fetch(client, ctx)
        except Throttled as exc:
            result = FetchResult(self.name, Status.THROTTLED, error=str(exc))
        except SourceError as exc:
            result = FetchResult(self.name, Status.ERROR, error=str(exc))
        except Exception as exc:  # parser bug or unexpected payload shape
            # Only the exception class is reported: messages can echo payload data.
            result = FetchResult(self.name, Status.ERROR, error=f"unexpected {type(exc).__name__}")
        else:
            status = batch.status
            if status is None:
                status = Status.OK if (batch.events or batch.series) else Status.EMPTY
            result = FetchResult(
                self.name,
                status,
                events=batch.events,
                series=batch.series,
                error=batch.error,
                notes=batch.notes,
            )
        result.duration_ms = int((time.monotonic() - started) * 1000)
        return result


def safe_build(builder, notes: dict[str, int], *args, **kwargs):
    """Build one record; count and drop it if the source data is malformed."""
    try:
        return builder(*args, **kwargs)
    except (ValueError, TypeError, KeyError):
        notes["dropped_malformed"] = notes.get("dropped_malformed", 0) + 1
        return None


def utc_from_ms(ms) -> datetime | None:
    if ms is None:
        return None
    return datetime.fromtimestamp(int(ms) / 1000, tz=timezone.utc)


def parse_iso(value: str | None, *, assume_utc: bool = True) -> datetime | None:
    if not value:
        return None
    s = value.strip().replace("Z", "+00:00")
    dt = datetime.fromisoformat(s)
    if dt.tzinfo is None:
        if not assume_utc:
            return None
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def parse_date(value: str | None) -> datetime | None:
    """``YYYY-MM-DD`` -> midnight UTC."""
    if not value:
        return None
    return datetime.strptime(value.strip()[:10], "%Y-%m-%d").replace(tzinfo=timezone.utc)
