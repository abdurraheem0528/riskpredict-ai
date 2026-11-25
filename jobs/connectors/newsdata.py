"""NewsData.io — news articles for the watch terms (``/api/1/latest``).

Docs: https://newsdata.io/documentation and https://newsdata.io/llms.txt
Key: ``NEWSDATA_API_KEY`` (free plan), sent in the ``X-ACCESS-KEY`` header.

Free-plan limits (checked 2026-11-24, newsdata.io/pricing, /documentation/
api-credit-limits and /documentation/api-rate-limit): **200 API credits a
day**; 1 credit per ``/latest`` call whatever it returns (errors cost
nothing); at most **10 articles per call** on Free; **30 credits per 15
minutes**, then HTTP 429; free articles are **delayed 12 hours** and
``/latest`` covers the past 48 hours (no ``timeframe`` filter on Free).

Our use: every 2 hours (``min_interval``), at most ``PER_RUN`` = 8 queries
(one page each, no pagination): <= 96 credits a day with the 8 default
terms, and never more than 8 in any 15 minutes. ``DAILY_LIMIT`` (160) stops a
run early if manual ``--force`` runs would go further. See
:mod:`jobs.connectors.news` for how terms rotate and how articles are stored.
NewsData's ``sentiment`` and ``ai_tag`` (on every plan) are kept in attrs.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from urllib.parse import urlencode

import httpx

from jobs.connectors import news
from jobs.connectors.base import Batch, DailyBudget, KeyedConnector, RunContext, safe_build
from jobs.http import SourceError, Throttled, get_json
from jobs.models import Event, Status

API = "https://newsdata.io/api/1/latest"
PER_RUN = 8
DAILY_LIMIT = 160  # of 200 credits
SIZE = 10  # free-plan maximum


def parse_pubdate(value: str | None, tz: str | None) -> datetime | None:
    if not value:
        return None
    if tz and tz.upper() != "UTC":
        return None  # only UTC is documented; don't guess an offset
    return datetime.strptime(value.strip()[:19], "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)


def _plain(value):
    """Plan-gated fields hold a notice string ("ONLY AVAILABLE IN PAID PLANS")."""
    if isinstance(value, str) and value.upper().startswith("ONLY AVAILABLE"):
        return None
    return value or None


def parse(payload: dict, query: str, notes: dict[str, int] | None = None) -> list[Event]:
    notes = notes if notes is not None else {}
    if payload.get("status") != "success":
        raise SourceError("NewsData returned an error status")
    events = []
    for art in payload.get("results") or []:
        url = art.get("link")
        if not url or art.get("duplicate") is True:
            notes["skipped_duplicate_or_no_url"] = notes.get("skipped_duplicate_or_no_url", 0) + 1
            continue
        countries = art.get("country") or []
        ev = safe_build(
            Event,
            notes,
            source="newsdata",
            raw_id=news.url_id(url),
            type="news",
            title=art.get("title") or "",
            url=url,
            published_at=parse_pubdate(art.get("pubDate"), art.get("pubDateTZ")),
            attrs={
                k: v
                for k, v in {
                    "query": query,
                    "article_id": art.get("article_id"),
                    "domain": news.domain_of(url),
                    "source_id": art.get("source_id"),
                    "language": art.get("language"),
                    "publisher_country": countries if isinstance(countries, list) and countries else None,
                    "category": art.get("category") or None,
                    "sentiment": _plain(art.get("sentiment")),
                    "ai_tag": _plain(art.get("ai_tag")),
                }.items()
                if v is not None
            },
        )
        if ev:
            events.append(ev)
    return events


class NewsDataConnector(KeyedConnector):
    name = "newsdata"
    min_interval = timedelta(hours=2)

    def fetch_with_key(self, client: httpx.Client, ctx: RunContext, key: str | None) -> Batch:
        batch = Batch()
        rot = news.Rotation(ctx, self.name, PER_RUN)
        batch.notes["terms_unsupported"] = rot.unsupported
        if not rot.terms:
            return Batch(status=Status.SKIPPED, error="no usable gdelt_query watch terms", notes=batch.notes)
        budget = DailyBudget.load(ctx, self.name, DAILY_LIMIT)
        headers = {"X-ACCESS-KEY": key or ""}
        done = failed = 0
        last_error = None
        for term in rot.terms:
            if not budget.take():
                batch.notes["budget_stopped"] = 1
                break
            q = urlencode({"q": term, "language": "en", "size": SIZE, "removeduplicate": 1})
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
            batch.error = last_error or "daily credit budget used"
        return batch
