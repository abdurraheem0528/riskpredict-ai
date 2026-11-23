"""GDELT DOC 2.0 API — news articles matching watch terms of kind ``gdelt_query``.

GDELT asks for at most one request every 5 seconds. When it throttles it
answers HTTP 429 (or sometimes HTTP 200) with a plain-text "Please limit
requests..." message instead of JSON; both are treated as throttling and
retried with exponential back-off.

Docs: https://blog.gdeltproject.org/gdelt-doc-2-0-api-debuts/
"""

from __future__ import annotations

import hashlib
import json
import time
from datetime import datetime, timedelta, timezone
from urllib.parse import urlencode

import httpx

from jobs.connectors.base import Batch, Connector, RunContext, safe_build
from jobs.http import SourceError, Throttled, get
from jobs.models import Event, Status

API = "https://api.gdeltproject.org/api/v2/doc/doc"
MIN_GAP_S = 5.5  # a little over GDELT's 1 request / 5 s
BACKOFF_S = (15.0, 30.0, 60.0)  # waits after a throttle reply
BUDGET_S = 240.0  # stop starting new queries after this long
MAX_CONSECUTIVE_FAILURES = 2  # then stop: GDELT is down or refusing us
REQUEST_TIMEOUT = httpx.Timeout(30.0, connect=15.0)
TIMESPAN = "2h"  # runs every 15 min; overlap absorbs late schedules
MAX_RECORDS = 75


def is_throttle_text(body: str) -> bool:
    head = body.lstrip()[:200].lower()
    return head.startswith("please limit requests") or "limit requests to one every" in head


def parse_seendate(value: str) -> datetime:
    return datetime.strptime(value, "%Y%m%dT%H%M%SZ").replace(tzinfo=timezone.utc)


def parse_articles(payload: dict, query: str, notes: dict[str, int] | None = None) -> list[Event]:
    """Turn a DOC 2.0 ``mode=artlist`` JSON payload into events.

    GDELT gives no event location: ``sourcecountry`` is where the publisher is,
    so it is kept in ``attrs`` and *not* used for ``countries``.
    """
    notes = notes if notes is not None else {}
    events: list[Event] = []
    for art in payload.get("articles") or []:
        url = (art.get("url") or "").strip()
        if not url:
            continue

        def build(art=art, url=url):
            return Event(
                source="gdelt",
                raw_id=hashlib.sha256(url.encode()).hexdigest()[:32],
                type="news",
                title=art.get("title") or "",
                url=url,
                published_at=parse_seendate(art["seendate"]) if art.get("seendate") else None,
                attrs={
                    k: v
                    for k, v in {
                        "query": query,
                        "domain": art.get("domain"),
                        "language": art.get("language"),
                        "sourcecountry": art.get("sourcecountry"),
                    }.items()
                    if v
                },
            )

        ev = safe_build(build, notes)
        if ev:
            events.append(ev)
    return events


def decode(body: str) -> dict:
    if is_throttle_text(body):
        raise Throttled("GDELT throttle message")
    if not body.strip():
        return {}
    try:
        # GDELT occasionally emits control characters inside strings.
        return json.loads(body, strict=False)
    except ValueError as exc:
        raise SourceError("invalid JSON from api.gdeltproject.org") from exc


class GdeltConnector(Connector):
    name = "gdelt"
    min_interval = timedelta(minutes=15)

    def __init__(
        self,
        min_gap: float = MIN_GAP_S,
        backoff: tuple[float, ...] = BACKOFF_S,
        budget: float = BUDGET_S,
        clock=time.monotonic,
    ):
        self.min_gap = min_gap
        self.backoff = backoff
        self.budget = budget
        self.clock = clock
        self._last_call: float | None = None
        self._deadline = 0.0

    def _pace(self, ctx: RunContext) -> None:
        if self._last_call is not None:
            wait = self._last_call + self.min_gap - self.clock()
            if wait > 0:
                ctx.sleep(wait)
        self._last_call = self.clock()

    def query(self, client: httpx.Client, ctx: RunContext, q: str) -> dict:
        params = {
            "query": q,
            "mode": "artlist",
            "format": "json",
            "maxrecords": MAX_RECORDS,
            "timespan": TIMESPAN,
            "sort": "datedesc",
        }
        url = f"{API}?{urlencode(params)}"
        for attempt in range(len(self.backoff) + 1):
            self._pace(ctx)
            try:
                # One attempt per call: retrying connection errors only adds load.
                resp = get(client, url, attempts=1, timeout=REQUEST_TIMEOUT)
                return decode(resp.text)
            except Throttled:
                if attempt == len(self.backoff):
                    raise
                wait = self.backoff[attempt]
                if self.clock() + wait > self._deadline:
                    raise
                ctx.sleep(wait)
        raise Throttled("GDELT throttle")  # pragma: no cover

    def fetch(self, client: httpx.Client, ctx: RunContext) -> Batch:
        terms = ctx.terms("gdelt_query")
        if not terms:
            return Batch(status=Status.SKIPPED, error="no gdelt_query watch terms")
        batch = Batch()
        failures = throttled = not_run = streak = 0
        last_error = None
        self._deadline = self.clock() + self.budget
        for t in terms:
            if streak >= MAX_CONSECUTIVE_FAILURES or self.clock() > self._deadline:
                not_run += 1
                continue
            try:
                payload = self.query(client, ctx, t.term)
            except Throttled as exc:
                throttled += 1
                streak += 1
                last_error = str(exc)
                continue
            except SourceError as exc:
                failures += 1
                streak += 1
                last_error = str(exc)
                continue
            streak = 0
            batch.events.extend(parse_articles(payload, t.term, batch.notes))
        batch.notes.update(
            queries=len(terms), queries_throttled=throttled, queries_failed=failures, queries_not_run=not_run
        )
        if not_run and not (throttled or failures):
            batch.status, batch.error = Status.PARTIAL, "time budget reached"
        elif throttled + failures + not_run == len(terms):
            batch.status = Status.THROTTLED if throttled else Status.ERROR
            batch.error = last_error
        elif throttled or failures:
            batch.status = Status.PARTIAL
            batch.error = last_error
        return batch
