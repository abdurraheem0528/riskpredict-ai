"""World Bank Indicators API (v2), most recent 5 values for every country.

Docs: https://datahelpdesk.worldbank.org/knowledgebase/articles/889392
Regional and income-group aggregates are skipped (they have no ISO code).
Annual values are stamped on 1 January of their year.
"""

from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone

import httpx

from jobs import codes
from jobs.connectors.base import Batch, Connector, RunContext, safe_build
from jobs.http import SourceError, get_json
from jobs.models import SeriesPoint, Status

API = "https://api.worldbank.org/v2/country/all/indicator"
INDICATORS = {
    "FP.CPI.TOTL.ZG": "% annual",  # Inflation, consumer prices
    "NY.GDP.MKTP.KD.ZG": "% annual",  # GDP growth
    "NE.TRD.GNFS.ZS": "% of GDP",  # Trade
    "PA.NUS.FCRF": "LCU per USD",  # Official exchange rate, period average
    "LP.LPI.OVRL.XQ": "score 1-5",  # Logistics Performance Index
    "NV.IND.MANF.ZS": "% of GDP",  # Manufacturing value added
}
PER_PAGE = 1000
MAX_PAGES = 20


def period_start(date: str) -> datetime | None:
    """``2024`` / ``2024Q3`` / ``2024M07`` -> start of that period (UTC)."""
    m = re.fullmatch(r"(\d{4})(?:([QM])(\d{1,2}))?", (date or "").strip())
    if not m:
        return None
    year, kind, n = int(m[1]), m[2], m[3]
    month = 1
    if kind == "Q":
        month = 3 * (int(n) - 1) + 1
    elif kind == "M":
        month = int(n)
    return datetime(year, month, 1, tzinfo=timezone.utc)


def parse(rows: list[dict], unit: str | None = None, notes: dict[str, int] | None = None) -> list[SeriesPoint]:
    notes = notes if notes is not None else {}
    points = []
    for r in rows or []:
        iso3 = r.get("countryiso3code")
        value = r.get("value")
        indicator = (r.get("indicator") or {}).get("id")
        if value is None or not indicator or not iso3 or not codes.is_country(iso3):
            continue
        ts = period_start(r.get("date"))
        if ts is None:
            continue
        p = safe_build(
            SeriesPoint,
            notes,
            source="worldbank",
            series=f"wb.{indicator}",
            key=iso3,
            ts=ts,
            value=value,
            unit=unit or r.get("unit") or None,
        )
        if p:
            points.append(p)
    return points


class WorldBankConnector(Connector):
    name = "worldbank"
    min_interval = timedelta(hours=24)

    def fetch_indicator(self, client: httpx.Client, ctx: RunContext, indicator: str) -> list[dict]:
        rows: list[dict] = []
        for page in range(1, MAX_PAGES + 1):
            url = f"{API}/{indicator}?format=json&mrv=5&per_page={PER_PAGE}&page={page}"
            payload = get_json(client, url, sleep=ctx.sleep)
            if not isinstance(payload, list) or len(payload) < 2:
                # The API answers errors as [{"message": [...]}] with HTTP 200.
                raise SourceError(f"World Bank API error for {indicator}")
            meta, data = payload[0], payload[1] or []
            rows.extend(data)
            if int(meta.get("page") or page) >= int(meta.get("pages") or 1):
                return rows
        raise SourceError("World Bank pagination did not finish")

    def fetch(self, client: httpx.Client, ctx: RunContext) -> Batch:
        batch = Batch()
        failed = []
        for indicator, unit in INDICATORS.items():
            try:
                rows = self.fetch_indicator(client, ctx, indicator)
            except SourceError as exc:
                failed.append(str(exc))
                continue
            batch.series.extend(parse(rows, unit, batch.notes))
        batch.notes.update(indicators=len(INDICATORS), indicators_failed=len(failed))
        if failed:
            batch.error = failed[-1]
            batch.status = Status.ERROR if len(failed) == len(INDICATORS) else Status.PARTIAL
        return batch
