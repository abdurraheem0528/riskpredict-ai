"""Run the analysis steps against the database, or in memory for dry runs.

Each step returns an :class:`AnalysisResult` with counts only; nothing here
prints or returns titles, entity names or series keys, because job logs are
public.
"""

from __future__ import annotations

import os
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Callable, Sequence

from jobs.analyse import anomalies, tagging
from jobs.analyse.tagging import TagInput
from jobs.models import Event, FetchResult, SeriesPoint

TAG_BUDGET_S = float(os.environ.get("TAG_BUDGET_S", "540"))  # model load + inference; fits a 15-min job
TAG_MAX_EVENTS = int(os.environ.get("TAG_MAX_EVENTS", "2000"))
TAG_BATCH_SIZE = int(os.environ.get("TAG_BATCH_SIZE", "16"))
ANOMALY_MIN_INTERVAL = timedelta(hours=6)
SCHEDULE_SLACK = timedelta(minutes=3)

STEPS = ("tagging", "anomalies")


@dataclass
class AnalysisResult:
    name: str
    status: str  # 'ok' | 'empty' | 'partial' | 'insufficient_history' | 'skipped' | 'error'
    inputs: int = 0
    outputs: int = 0
    duration_ms: int = 0
    error: str | None = None
    notes: dict[str, int] = field(default_factory=dict)


ModelLoader = Callable[[], "tagging.Model"]


def default_loader() -> tagging.Model:
    return tagging.Gliner2Model(batch_size=TAG_BATCH_SIZE, threads=os.cpu_count())


def is_due(last_success: datetime | None, now: datetime, interval: timedelta) -> bool:
    return last_success is None or now - last_success >= interval - SCHEDULE_SLACK


# --- tagging -----------------------------------------------------------------


def _ms(started: float) -> int:
    return int((time.monotonic() - started) * 1000)


def tag(
    load_model: ModelLoader,
    pending: Callable[[str, str], list[TagInput]],
    *,
    budget_s: float = TAG_BUDGET_S,
    clock: Callable[[], float] = time.monotonic,
) -> tuple[AnalysisResult, list[tagging.Tag], tuple[str, str] | None]:
    """Load the model, fetch pending inputs for its version, tag them.

    ``pending(model, version)`` returns the events to tag. Returns the result,
    the tags and ``(model, version)`` (``None`` if the model did not load).
    """
    started = time.monotonic()
    deadline = clock() + budget_s
    try:
        model = load_model()
    except tagging.ModelUnavailable as exc:
        msg = str(exc)
        status = "skipped" if "not installed" in msg else "error"
        return AnalysisResult("tagging", status, error=msg, duration_ms=_ms(started)), [], None
    version = tagging.model_version(model.revision)
    items = pending(model.name, version)
    if not items:
        return AnalysisResult("tagging", "empty", duration_ms=_ms(started)), [], (model.name, version)
    run = tagging.tag_inputs(model, items, budget_s=max(deadline - clock(), 0.0), clock=clock)
    if not run.tags:
        status = "error"
    elif run.stopped_early or run.notes.get("failed") or run.notes.get("unparsed"):
        status = "partial"
    else:
        status = "ok"
    error = run.error or ("time budget reached" if run.stopped_early else None)
    by_type: dict[str, int] = {}
    for t in run.tags:
        by_type[f"type.{t.event_type}"] = by_type.get(f"type.{t.event_type}", 0) + 1
    notes = {**run.notes, **by_type, "with_countries": sum(1 for t in run.tags if t.entities["countries"])}
    result = AnalysisResult("tagging", status, len(items), len(run.tags), _ms(started), error, notes)
    return result, run.tags, (model.name, version)


def taggable(events: Sequence[Event]) -> list[TagInput]:
    """In-memory equivalent of :data:`jobs.analyse.store.PENDING_TAGS` (dry runs)."""
    out = []
    for e in events:
        if e.source not in tagging.TAG_SOURCES or not e.title:
            continue
        if e.source == "gdelt" and (e.attrs.get("language") or "English") != "English":
            continue
        out.append(TagInput(e.source, e.raw_id, e.title))
    out.sort(key=lambda t: t.source != "gdelt")
    return out[:TAG_MAX_EVENTS]


# --- anomalies ---------------------------------------------------------------


def detect_all(
    series: dict[tuple[str, str], list[tuple[datetime, float]]],
    news_rows: Sequence[tuple[str, datetime, int]],
    news_terms: Sequence[str],
) -> tuple[AnalysisResult, list[anomalies.Detection]]:
    started = time.monotonic()
    detections: list[anomalies.Detection] = []
    skipped: set[str] = set()
    for (name, key), points in sorted(series.items()):
        if anomalies.rule_for(name).skip_reason:
            skipped.add(name)  # one status row for the whole series, not one per key
            continue
        detections.append(anomalies.detect(name, key, points))
    detections.extend(anomalies.detect(name, "*", []) for name in sorted(skipped))
    for term, points in anomalies.daily_counts(news_rows, news_terms).items():
        detections.append(anomalies.detect(anomalies.NEWS_SERIES, term, points))

    notes: dict[str, int] = {}
    for d in detections:
        for k in (f"{d.rule.name}.keys", f"{d.rule.name}.{d.status}"):
            notes[k] = notes.get(k, 0) + 1
        for f in d.flags:
            k = f"{d.rule.name}.flag.{f.method}"
            notes[k] = notes.get(k, 0) + 1
    flags = sum(len(d.flags) for d in detections)
    if not detections:
        status = "empty"
    elif all(d.status in ("insufficient_history", "skipped", "empty") for d in detections):
        status = "insufficient_history"
    else:
        status = "ok"
    result = AnalysisResult("anomalies", status, len(detections), flags, _ms(started), notes=notes)
    return result, detections


def series_from_results(results: Sequence[FetchResult]) -> dict[tuple[str, str], list[tuple[datetime, float]]]:
    out: dict[tuple[str, str], list[tuple[datetime, float]]] = {}
    points: list[SeriesPoint] = [p for r in results for p in r.series]
    for p in points:
        out.setdefault((p.series, p.key), []).append((p.ts, p.value))
    return out


def news_rows_from_events(events: Sequence[Event], now: datetime) -> list[tuple[str, datetime, int]]:
    today = now.replace(hour=0, minute=0, second=0, microsecond=0)
    counts: dict[tuple[str, datetime], int] = {}
    for e in events:
        q = e.attrs.get("query")
        if e.source != "gdelt" or not q or e.published_at is None or e.published_at >= today:
            continue
        day = e.published_at.replace(hour=0, minute=0, second=0, microsecond=0)
        counts[(q, day)] = counts.get((q, day), 0) + 1
    return [(q, d, n) for (q, d), n in counts.items()]
