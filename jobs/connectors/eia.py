"""U.S. EIA Open Data API v2 — daily energy spot prices.

Docs: https://www.eia.gov/opendata/documentation.php and /opendata/faqs.php
Key: ``EIA_API_KEY`` (free). EIA's documented way to send it is the
``api_key`` query parameter, so the URL is never printed (see :mod:`jobs.http`).

Limits (checked 2026-11-24, FAQ "API Throttles"): EIA does not publish exact
throttles; keep the sustained rate under ~9,000 requests an hour and bursts
under 5 a second. A JSON response holds at most 5,000 rows. We send
``len(ROUTES)`` = 2 requests every 6 hours (8 a day), each for the newest
``LENGTH`` rows only.

Rows become ``feeds.series`` ``eia.spot_price`` with key = the EIA series id
(``RWTC`` WTI Cushing, ``RBRTE`` Brent, ``RNGWHHD`` Henry Hub natural gas),
``unit`` as EIA gives it (``$/BBL``, ``$/MMBTU``) and the description in attrs.
"""

from __future__ import annotations

from datetime import timedelta
from urllib.parse import urlencode

import httpx

from jobs.connectors.base import Batch, KeyedConnector, RunContext, parse_date, safe_build
from jobs.http import SourceError, get_json
from jobs.models import SeriesPoint, Status

API = "https://api.eia.gov/v2/"
LENGTH = 60  # newest rows per series; ~3 months of business days
# route -> series ids (facets[series][]); all daily.
ROUTES: dict[str, tuple[str, ...]] = {
    "petroleum/pri/spt": ("RWTC", "RBRTE"),
    "natural-gas/pri/fut": ("RNGWHHD",),
}


def parse(payload: dict, notes: dict[str, int] | None = None) -> list[SeriesPoint]:
    notes = notes if notes is not None else {}
    resp = payload.get("response")
    if not isinstance(resp, dict) or "data" not in resp:
        raise SourceError("EIA response has no data")
    points = []
    for row in resp.get("data") or []:
        value = row.get("value")
        if value in (None, ""):
            notes["missing_values"] = notes.get("missing_values", 0) + 1
            continue
        p = safe_build(
            SeriesPoint,
            notes,
            source="eia",
            series="eia.spot_price",
            key=row.get("series"),
            ts=parse_date(row.get("period")),
            value=float(value),
            unit=row.get("units"),
            attrs={
                k: v
                for k, v in {
                    "description": row.get("series-description"),
                    "product": row.get("product-name"),
                    "area": row.get("area-name") if row.get("area-name") not in (None, "NA") else None,
                }.items()
                if v
            },
        )
        if p:
            points.append(p)
    return points


def query(route: str, series: tuple[str, ...], key: str | None) -> str:
    params: list[tuple[str, str]] = [("api_key", key or ""), ("frequency", "daily"), ("data[0]", "value")]
    params += [("facets[series][]", s) for s in series]
    params += [("sort[0][column]", "period"), ("sort[0][direction]", "desc"), ("length", str(LENGTH * len(series)))]
    return f"{API}{route}/data/?{urlencode(params)}"


class EiaConnector(KeyedConnector):
    name = "eia"
    min_interval = timedelta(hours=6)

    def fetch_with_key(self, client: httpx.Client, ctx: RunContext, key: str | None) -> Batch:
        batch = Batch()
        failed, last_error = 0, None
        for route, series in ROUTES.items():
            try:
                payload = get_json(client, query(route, series, key), sleep=ctx.sleep, follow_redirects=False)
                batch.series.extend(parse(payload, batch.notes))
            except SourceError as exc:
                failed += 1
                last_error = str(exc)
        batch.notes.update(routes=len(ROUTES), routes_failed=failed)
        if failed:
            batch.status = Status.ERROR if failed == len(ROUTES) else Status.PARTIAL
            batch.error = last_error
        return batch
