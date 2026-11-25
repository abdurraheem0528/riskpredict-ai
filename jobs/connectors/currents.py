"""Currents API — news articles for the watch terms (``/v1/search``).

Docs: https://currentsapi.services/en/docs/search, /en/docs/latest_news and
the OpenAPI spec https://currentsapi.services/json/swagger.json
Key: ``CURRENTS_API_KEY`` (free plan), sent as ``Authorization: Bearer <key>``.

Free-plan limits (checked 2026-11-24, currentsapi.services/en/product/price):
**250 requests a day**, reset at 00:00 UTC (then HTTP 429 with Retry-After,
no overage billing); at most **20 results per request**; search looks back 30
days with a maximum **7-day span** per request; article text is a 300-character
preview (we store only titles and links anyway).

Our use: every 2 hours (``min_interval``), at most ``PER_RUN`` = 8 searches
(one page each) over the last ``WINDOW`` (6 h, so a late or missed run still
overlaps): <= 96 requests a day with the 8 default terms. ``DAILY_LIMIT``
(200) stops early if manual ``--force`` runs would go further. Storage
follows :mod:`jobs.connectors.news`.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from urllib.parse import urlencode

import httpx

from jobs.connectors import news
from jobs.connectors.base import Batch, DailyBudget, KeyedConnector, RunContext, safe_build
from jobs.http import SourceError, Throttled, get_json
from jobs.models import Event, Status

API = "https://api.currentsapi.services/v1/search"
PER_RUN = 8
DAILY_LIMIT = 200  # of 250
PAGE_SIZE = 20  # free-plan maximum
WINDOW = timedelta(hours=6)


def parse_published(value: str | None) -> datetime | None:
    """``2026-09-07 14:22:08 +0000`` (format documented in swagger.json)."""
    if not value:
        return None
    return datetime.strptime(value.strip(), "%Y-%m-%d %H:%M:%S %z")


def parse(payload: dict, query: str, notes: dict[str, int] | None = None) -> list[Event]:
    notes = notes if notes is not None else {}
    if payload.get("status") != "ok":
        raise SourceError("Currents returned an error status")
    events = []
    for art in payload.get("news") or []:
        url = art.get("url")
        if not url:
            notes["skipped_no_url"] = notes.get("skipped_no_url", 0) + 1
            continue
        ev = safe_build(
            Event,
            notes,
            source="currents",
            raw_id=news.url_id(url),
            type="news",
            title=art.get("title") or "",
            url=url,
            published_at=parse_published(art.get("published")),
            attrs={
                k: v
                for k, v in {
                    "query": query,
                    "article_id": art.get("id"),
                    "domain": news.domain_of(url),
                    "language": art.get("language"),
                    "category": art.get("category") or None,
                }.items()
                if v is not None
            },
        )
        if ev:
            events.append(ev)
    return events


def rfc3339(ts: datetime) -> str:
    return ts.strftime("%Y-%m-%dT%H:%M:%SZ")


class CurrentsConnector(KeyedConnector):
    name = "currents"
    min_interval = timedelta(hours=2)

    def fetch_with_key(self, client: httpx.Client, ctx: RunContext, key: str | None) -> Batch:
        batch = Batch()
        rot = news.Rotation(ctx, self.name, PER_RUN)
        batch.notes["terms_unsupported"] = rot.unsupported
        if not rot.terms:
            return Batch(status=Status.SKIPPED, error="no usable gdelt_query watch terms", notes=batch.notes)
        budget = DailyBudget.load(ctx, self.name, DAILY_LIMIT)
        headers = {"Authorization": f"Bearer {key}"}
        window = {"start_date": rfc3339(ctx.now - WINDOW), "end_date": rfc3339(ctx.now)}
        done = failed = 0
        last_error = None
        for term in rot.terms:
            if not budget.take():
                batch.notes["budget_stopped"] = 1
                break
            q = urlencode({"query": term, "language": "en", "page_size": PAGE_SIZE, **window})
            try:
                payload = get_json(client, f"{API}?{q}", headers=headers, sleep=ctx.sleep, follow_redirects=False)
                batch.events.extend(parse(payload, term, batch.notes))
                done += 1
            except Throttled as exc:
                failed += 1
                last_error = str(exc)
                break
            except SourceError as exc:
                failed += 1
                last_error = str(exc)
        budget.save(batch.notes)
        batch.notes.update(queries=done, queries_failed=failed, term_offset=rot.next_offset(done + failed))
        if failed or batch.notes.get("budget_stopped"):
            batch.status = Status.ERROR if done == 0 else Status.PARTIAL
            batch.error = last_error or "daily request budget used"
        return batch
