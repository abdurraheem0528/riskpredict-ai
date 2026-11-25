"""UN Comtrade — annual goods trade by HS code and reporting country.

Docs: https://comtradeplus.un.org/en/api-docs (Data API ``data/v1/get``) and
https://uncomtrade.org/docs/subscriptions/
Key: ``COMTRADE_API_KEY`` (free "Basic Individual" subscription key), sent in
the ``Ocp-Apim-Subscription-Key`` header.

Free-tier limits (checked 2026-11-24, comtradeplus.un.org/en/subscriptions):
500 calls a day, 1 call a second, at most 100,000 records per call (more are
silently truncated), at most 12 periods per call; the key "may be renewed from
time to time". We make ONE call a day (``min_interval`` 24 h) for the last two
complete years, every reporter, partner = World, imports and exports, and the
HS codes in :data:`HS_CODES`: roughly 200 x 2 x 2 x 12 = ~10,000 records.

Each record becomes ``feeds.series`` ``comtrade.import_value`` or
``comtrade.export_value`` with key ``<ISO3>:<HS>`` (e.g. ``JPN:8542``; trade
with the whole world) or, for a bilateral row, ``<ISO3>:<HS>:<partner ISO3>``; ``ts`` =
1 January of the year, unit ``USD`` (the reporter's primary value: CIF for
imports, FOB for exports). Reporters without an ISO 3166-1 code (regional
aggregates such as "EU" or "Other Asia, nes") are dropped and counted.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from urllib.parse import urlencode

import httpx

from jobs import codes
from jobs.connectors.base import Batch, DailyBudget, KeyedConnector, RunContext, safe_build
from jobs.http import SourceError, get_json
from jobs.models import SeriesPoint, Status

API = "https://comtradeapi.un.org/data/v1/get/C/A/HS"
MAX_RECORDS = 100_000  # free-tier cap per call
DAILY_LIMIT = 400  # of the 500 free calls a day; leaves room for manual use of the key
# HS codes (digits only) for supply-chain-critical goods. Chapters and headings.
HS_CODES = (
    "27",  # mineral fuels, oil
    "2709",  # crude petroleum
    "2711",  # petroleum gases (LNG, LPG)
    "10",  # cereals
    "31",  # fertilisers
    "30",  # pharmaceuticals
    "72",  # iron and steel
    "74",  # copper
    "76",  # aluminium
    "2805",  # rare-earth metals, scandium, yttrium
    "8542",  # electronic integrated circuits
    "8507",  # electric accumulators (batteries)
)
FLOWS = {"M": "comtrade.import_value", "X": "comtrade.export_value"}


def parse(payload: dict, notes: dict[str, int] | None = None) -> list[SeriesPoint]:
    notes = notes if notes is not None else {}
    if payload.get("error"):
        raise SourceError("Comtrade returned an error message")
    if "data" not in payload:
        raise SourceError("Comtrade response has no data")
    points = []
    for row in payload.get("data") or []:
        iso = (row.get("reporterISO") or "").strip().upper()
        series = FLOWS.get(row.get("flowCode"))
        hs = str(row.get("cmdCode") or "")
        if series is None or not hs.isdigit():
            notes["dropped_other"] = notes.get("dropped_other", 0) + 1
            continue
        if not codes.is_country(iso):
            notes["dropped_aggregate_reporter"] = notes.get("dropped_aggregate_reporter", 0) + 1
            continue
        value = row.get("primaryValue")
        if value is None:
            notes["missing_values"] = notes.get("missing_values", 0) + 1
            continue
        ts = datetime(int(row["refYear"]), 1, 1, tzinfo=timezone.utc) if row.get("refYear") else None
        key = f"{iso}:{hs}"
        if row.get("partnerCode") != 0:  # a bilateral flow: name the partner too
            partner = (row.get("partnerISO") or "").strip().upper()
            if not codes.is_country(partner):
                notes["dropped_aggregate_partner"] = notes.get("dropped_aggregate_partner", 0) + 1
                continue
            key = f"{key}:{partner}"
        p = safe_build(
            SeriesPoint,
            notes,
            source="comtrade",
            series=series,
            key=key,
            ts=ts,
            value=value,
            unit="USD",
            attrs={
                k: v
                for k, v in {
                    "classification": row.get("classificationCode"),
                    "description": row.get("cmdDesc"),
                    "net_weight_kg": row.get("netWgt"),
                    "weight_estimated": True if row.get("isNetWgtEstimated") else None,
                    "reported": row.get("isReported"),
                }.items()
                if v is not None
            },
        )
        if p:
            points.append(p)
    return points


def periods(ctx: RunContext) -> list[str]:
    year = ctx.now.year
    return [str(year - 1), str(year - 2)]


def query(ctx: RunContext) -> str:
    return API + "?" + urlencode(
        {
            "period": ",".join(periods(ctx)),
            "partnerCode": "0",
            "cmdCode": ",".join(HS_CODES),
            "flowCode": ",".join(FLOWS),
            "maxRecords": str(MAX_RECORDS),
            "includeDesc": "true",
        }
    )


class ComtradeConnector(KeyedConnector):
    name = "comtrade"
    min_interval = timedelta(hours=24)

    def fetch_with_key(self, client: httpx.Client, ctx: RunContext, key: str | None) -> Batch:
        batch = Batch()
        budget = DailyBudget.load(ctx, self.name, DAILY_LIMIT)
        if not budget.take():
            budget.save(batch.notes)
            return Batch(status=Status.SKIPPED, error="daily call budget used", notes=batch.notes)
        # A failed call still counts against the quota, but its notes are lost
        # with the error; DAILY_LIMIT leaves ample margin for that.
        payload = get_json(
            client,
            query(ctx),
            headers={"Ocp-Apim-Subscription-Key": key or ""},
            sleep=ctx.sleep,
            follow_redirects=False,
        )
        budget.save(batch.notes)
        batch.series = parse(payload, batch.notes)
        count = int(payload.get("count") or 0)
        batch.notes["records_in_response"] = count
        if count >= MAX_RECORDS:
            batch.status = Status.PARTIAL
            batch.error = f"Comtrade returned the {MAX_RECORDS} record maximum; result may be truncated"
        return batch
