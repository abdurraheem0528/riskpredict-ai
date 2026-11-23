"""Frankfurter — daily reference exchange rates (ECB data), base EUR.

Docs: https://frankfurter.dev
EUR is the ECB's own base, so no cross rates are computed here. The last
10 days are requested so a missed run fills itself in.
"""

from __future__ import annotations

from datetime import timedelta

import httpx

from jobs import codes
from jobs.connectors.base import Batch, Connector, RunContext, parse_date, safe_build
from jobs.http import get_json
from jobs.models import SeriesPoint

API = "https://api.frankfurter.dev/v1"
BASE = "EUR"
LOOKBACK_DAYS = 10


def parse(payload: dict, notes: dict[str, int] | None = None) -> list[SeriesPoint]:
    """Accept both the ``/latest`` and the time-series (``/start..``) shapes."""
    notes = notes if notes is not None else {}
    base = payload.get("base")
    rates = payload.get("rates") or {}
    if payload.get("date") and rates and not isinstance(next(iter(rates.values())), dict):
        rates = {payload["date"]: rates}
    points = []
    if not base or not codes.is_currency(base):
        return points
    for day, day_rates in rates.items():
        ts = parse_date(day)
        for quote, value in (day_rates or {}).items():
            if not codes.is_currency(quote):
                continue
            p = safe_build(
                SeriesPoint,
                notes,
                source="frankfurter",
                series="fx.rate",
                key=f"{base}/{quote}",
                ts=ts,
                value=value,
                unit=f"{quote} per {base}",
            )
            if p:
                points.append(p)
    return points


class FrankfurterConnector(Connector):
    name = "frankfurter"
    min_interval = timedelta(hours=3)

    def fetch(self, client: httpx.Client, ctx: RunContext) -> Batch:
        start = (ctx.now - timedelta(days=LOOKBACK_DAYS)).strftime("%Y-%m-%d")
        batch = Batch()
        batch.series = parse(get_json(client, f"{API}/{start}..?base={BASE}", sleep=ctx.sleep), batch.notes)
        return batch
