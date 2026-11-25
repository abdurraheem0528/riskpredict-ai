"""UN Comtrade — annual goods trade by HS code and reporting country.

Docs: https://comtradeplus.un.org/en/api-docs (Data API ``data/v1/get``) and
https://uncomtrade.org/docs/subscriptions/
Key: ``COMTRADE_API_KEY`` (free "Basic Individual" subscription key), sent in
the ``Ocp-Apim-Subscription-Key`` header.

Free-tier limits (checked 2026-11-24, comtradeplus.un.org/en/subscriptions):
500 calls a day, 1 call a second, at most 100,000 records per call (more are
silently truncated), at most 12 periods per call; the key "may be renewed from
time to time". We make ONE call a day (``min_interval`` 24 h) for the last two
complete years, every reporter, imports and exports, and the HS codes in
:data:`HS_CODES`, with every breakdown pinned to its total:

* ``partnerCode=0`` — trade with the World, not one row per partner;
* ``breakdownMode=classic`` plus ``partner2Code=0``, ``customsCode=C00`` and
  ``motCode=0`` — without them the API answers in "plus" mode, one row per
  second partner, customs procedure and mode of transport, which is what filled
  the 100,000-record cap before (about ten rows per reporter, code and year).

That is at most (reporters) x 12 codes x 2 flows x 2 years: about 200 x 48 =
9,600 records, a tenth of the cap. A response that still reaches the cap is
reported ``partial``, never stored silently as complete.

Each record becomes ``feeds.series`` ``comtrade.import_value`` or
``comtrade.export_value`` with key ``<ISO3>:<HS>`` (e.g. ``JPN:8542``; trade
with the whole world); ``ts`` = 1 January of the year, unit ``USD`` (the
reporter's primary value: CIF for imports, FOB for exports). Reporters without
an ISO 3166-1 code (regional aggregates such as "EU" or "Other Asia, nes") are
dropped and counted, and so is any row that is not a World total (a partner,
second partner, customs procedure or transport mode breakdown), so a reply in
an unexpected mode can never overwrite a total with one of its parts.
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
MAX_RECORDS = 100_000  # free-tier cap per call; the totals-only query needs < 10,000
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
# The totals we ask for, field by field; anything else in a reply is a breakdown.
# A field the reply leaves out counts as the total (older record layouts).
TOTALS = {"partnerCode": "0", "partner2Code": "0", "customsCode": "C00", "motCode": "0"}


def is_world_total(row: dict) -> bool:
    """True for a reporter's trade with the World, all customs procedures and transport modes."""
    return all(row.get(k) is None or str(row.get(k)).strip().upper() == v for k, v in TOTALS.items())


def parse(payload: dict, notes: dict[str, int] | None = None) -> list[SeriesPoint]:
    notes = notes if notes is not None else {}
    if payload.get("error"):
        raise SourceError("Comtrade returned an error message")
    if "data" not in payload:
        raise SourceError("Comtrade response has no data")
    points = []
    seen: set[tuple] = set()
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
        if not is_world_total(row):
            notes["dropped_breakdown"] = notes.get("dropped_breakdown", 0) + 1
            continue
        value = row.get("primaryValue")
        if value is None:
            notes["missing_values"] = notes.get("missing_values", 0) + 1
            continue
        ts = datetime(int(row["refYear"]), 1, 1, tzinfo=timezone.utc) if row.get("refYear") else None
        key = f"{iso}:{hs}"
        if (series, key, ts) in seen:  # one total per key and year; never let a later row overwrite it
            notes["duplicate_keys"] = notes.get("duplicate_keys", 0) + 1
            continue
        seen.add((series, key, ts))
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
            "cmdCode": ",".join(HS_CODES),
            "flowCode": ",".join(FLOWS),
            **TOTALS,
            "breakdownMode": "classic",
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
        if count >= MAX_RECORDS:  # only if the totals-only query somehow grows past the cap
            batch.status = Status.PARTIAL
            dropped = batch.notes.get("dropped_breakdown", 0)
            batch.error = (
                f"Comtrade returned the {MAX_RECORDS} record maximum ({dropped} were not World totals); "
                "result may be truncated"
            )
        return batch
