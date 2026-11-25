"""FRED (Federal Reserve Bank of St. Louis) — US macro and financial series.

Docs: https://fred.stlouisfed.org/docs/api/fred/series_observations.html
Key: ``FRED_API_KEY`` (free). FRED only accepts it as the ``api_key`` query
parameter, so the request URL is never printed (see :mod:`jobs.http`).

Free-tier limit (checked 2026-11-24, https://fred.stlouisfed.org/docs/api/fred/errors.html):
up to 120 requests per minute, then HTTP 429; no daily cap is published.
We make one request per series every 6 hours: 8 series -> 32 requests a day.

Each observation becomes ``feeds.series`` row ``fred.<SERIES_ID>`` with key
``USA`` (all default series are US measures). FRED marks a missing value with
``"."``; those are skipped, never filled. The last ``LOOKBACK_DAYS`` are
requested each time, so revisions and missed runs fill themselves in.
"""

from __future__ import annotations

from datetime import timedelta
from urllib.parse import urlencode

import httpx

from jobs.connectors.base import Batch, KeyedConnector, RunContext, parse_date, safe_build
from jobs.http import SourceError, get_json
from jobs.models import SeriesPoint, Status

API = "https://api.stlouisfed.org/fred/series/observations"
LOOKBACK_DAYS = 400  # matches the series retention window (RETAIN_SERIES_DAYS)

# series id -> (key, unit). Public, generic macro / market-stress indicators.
SERIES: dict[str, tuple[str, str]] = {
    "DFF": ("USA", "% (effective federal funds rate)"),
    "T10Y2Y": ("USA", "percentage points (10y minus 2y Treasury)"),
    "BAMLH0A0HYM2": ("USA", "% (high-yield option-adjusted spread)"),
    "VIXCLS": ("USA", "index (CBOE VIX)"),
    "DTWEXBGS": ("USA", "index Jan 2006=100 (broad trade-weighted USD)"),
    "CPIAUCSL": ("USA", "index 1982-84=100 (CPI, all urban, SA)"),
    "UNRATE": ("USA", "% (unemployment rate, SA)"),
    "INDPRO": ("USA", "index 2017=100 (industrial production, SA)"),
}


def parse(payload: dict, series_id: str, notes: dict[str, int] | None = None) -> list[SeriesPoint]:
    notes = notes if notes is not None else {}
    if "observations" not in payload:
        raise SourceError("FRED response has no observations")
    key, unit = SERIES.get(series_id, ("USA", None))
    points = []
    for obs in payload.get("observations") or []:
        raw = obs.get("value")
        if raw in (None, "", "."):
            notes["missing_values"] = notes.get("missing_values", 0) + 1
            continue
        p = safe_build(
            SeriesPoint,
            notes,
            source="fred",
            series=f"fred.{series_id}",
            key=key,
            ts=parse_date(obs.get("date")),
            value=float(raw),
            unit=unit,
        )
        if p:
            points.append(p)
    return points


class FredConnector(KeyedConnector):
    name = "fred"
    min_interval = timedelta(hours=6)

    def fetch_with_key(self, client: httpx.Client, ctx: RunContext, key: str | None) -> Batch:
        batch = Batch()
        start = (ctx.now - timedelta(days=LOOKBACK_DAYS)).strftime("%Y-%m-%d")
        failed, last_error = 0, None
        for series_id in SERIES:
            query = urlencode(
                {"series_id": series_id, "api_key": key, "file_type": "json", "observation_start": start}
            )
            try:
                payload = get_json(client, f"{API}?{query}", sleep=ctx.sleep, follow_redirects=False)
                batch.series.extend(parse(payload, series_id, batch.notes))
            except SourceError as exc:
                failed += 1
                last_error = str(exc)
        batch.notes.update(series_requested=len(SERIES), series_failed=failed)
        if failed:
            batch.status = Status.ERROR if failed == len(SERIES) else Status.PARTIAL
            batch.error = last_error
        return batch
