from __future__ import annotations

import os
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Callable, ClassVar

import httpx

from jobs.http import SourceError, Throttled, redact
from jobs.models import Event, FetchResult, SanctionEntry, SeriesPoint, Status


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
    sanctions: list[SanctionEntry] = field(default_factory=list)
    replace_datasets: list[str] = field(default_factory=list)
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
                status = Status.OK if (batch.events or batch.series or batch.sanctions) else Status.EMPTY
            result = FetchResult(
                self.name,
                status,
                events=batch.events,
                series=batch.series,
                error=batch.error,
                notes=batch.notes,
                sanctions=batch.sanctions,
                replace_datasets=tuple(batch.replace_datasets),
            )
        result.error = redact(result.error)
        result.duration_ms = int((time.monotonic() - started) * 1000)
        return result


NO_KEY = "no key"


class KeyedConnector(Connector):
    """A source that needs a free API key, read from ``<NAME>_API_KEY``.

    Without the key the source reports ``skipped`` ("no key") and makes no
    request, so the fetch job passes with no keys set. With
    ``key_optional = True`` it runs keyless (at the lower keyless rate) instead.
    """

    key_optional: ClassVar[bool] = False

    @classmethod
    def key_env(cls) -> str:
        return f"{cls.name.upper()}_API_KEY"

    @classmethod
    def api_key(cls) -> str | None:
        return (os.environ.get(cls.key_env()) or "").strip() or None

    def fetch(self, client: httpx.Client, ctx: RunContext) -> Batch:
        key = self.api_key()
        if key is None and not self.key_optional:
            return Batch(status=Status.SKIPPED, error=NO_KEY)
        return self.fetch_with_key(client, ctx, key)

    def fetch_with_key(self, client: httpx.Client, ctx: RunContext, key: str | None) -> Batch:  # pragma: no cover
        raise NotImplementedError


@dataclass
class DailyBudget:
    """Requests spent today (UTC), carried between runs in the source's notes.

    ``min_interval`` and the per-run caps already keep each source under its
    provider's daily quota; this is the backstop for manual ``--force`` runs.
    The count lives in ``feeds.source_health.notes`` (``budget_day`` as
    YYYYMMDD, ``budget_used``), so a dry run (no stored state) starts at 0.
    """

    limit: int
    day: int
    used: int = 0

    @classmethod
    def load(cls, ctx: RunContext, source: str, limit: int) -> "DailyBudget":
        today = int(ctx.now.strftime("%Y%m%d"))
        prev = ctx.state.get(source) or {}
        used = prev.get("budget_used", 0) if prev.get("budget_day") == today else 0
        return cls(limit=limit, day=today, used=int(used or 0))

    def take(self, n: int = 1) -> bool:
        if self.used + n > self.limit:
            return False
        self.used += n
        return True

    def save(self, notes: dict[str, int]) -> None:
        notes.update(budget_day=self.day, budget_used=self.used)


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
