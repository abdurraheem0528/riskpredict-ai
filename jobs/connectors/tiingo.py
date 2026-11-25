"""Tiingo — end-of-day market prices (ETFs as market proxies) and FX rates.

Docs: https://www.tiingo.com/documentation/end-of-day and
https://www.tiingo.com/documentation/forex
Key: ``TIINGO_API_KEY`` (free "Starter" plan), sent as
``Authorization: Token <key>``.

Free-tier limits (checked 2026-11-24, tiingo.com/about/pricing and
llms-full.txt): 50 requests an hour, 1,000 a day (reset at midnight US
Eastern), 500 unique symbols a month, 1 GB a month; internal/personal use
only. We make one request per symbol (``len(TICKERS) + len(FX_PAIRS)`` = 20)
every 12 hours: 40 requests a day, 20 unique symbols a month, a few hundred
KB of bandwidth. ``DAILY_LIMIT`` stops early if manual ``--force`` runs would
push past 800 a day.

* ETFs -> ``feeds.series`` ``tiingo.close`` (key = ticker, unit ``USD``; the
  adjusted close and volume in attrs). They stand in for markets the platform
  watches: broad equity (SPY, EEM), dry-bulk shipping rates (BDRY),
  semiconductors (SOXX), oil, gas, copper, grains and gold.
* FX -> ``tiingo.fx_close`` (key ``USD/<CCY>``, unit ``<CCY> per USD``, daily
  close). These are currencies the ECB (Frankfurter, base EUR) does not
  publish, so they are kept in their own series rather than mixed into
  ``fx.rate``.
"""

from __future__ import annotations

from datetime import timedelta
from urllib.parse import urlencode

import httpx

from jobs import codes
from jobs.connectors.base import Batch, DailyBudget, KeyedConnector, RunContext, parse_iso, safe_build
from jobs.http import SourceError, Throttled, get_json
from jobs.models import SeriesPoint, Status

API = "https://api.tiingo.com/tiingo"
LOOKBACK_DAYS = 120
DAILY_LIMIT = 800  # of 1,000
TICKERS = ("SPY", "EEM", "VGK", "EWJ", "FXI", "SOXX", "BDRY", "USO", "UNG", "CPER", "DBA", "GLD")
# Currencies without an ECB reference rate (so not in fx.rate).
FX_PAIRS = ("usdrub", "usdars", "usdngn", "usdegp", "usdpkr", "usdvnd", "usdkzt", "usduah")


def parse_eod(payload, ticker: str, notes: dict[str, int] | None = None) -> list[SeriesPoint]:
    notes = notes if notes is not None else {}
    if not isinstance(payload, list):
        raise SourceError("Tiingo EOD response is not a list")
    points = []
    for row in payload:
        close = row.get("close")
        if close is None:
            notes["missing_values"] = notes.get("missing_values", 0) + 1
            continue
        p = safe_build(
            SeriesPoint,
            notes,
            source="tiingo",
            series="tiingo.close",
            key=ticker.upper(),
            ts=parse_iso(row.get("date")),
            value=close,
            unit="USD",
            attrs={k: row[k] for k in ("adjClose", "volume", "divCash", "splitFactor") if row.get(k) is not None},
        )
        if p:
            points.append(p)
    return points


def parse_fx(payload, pair: str, notes: dict[str, int] | None = None) -> list[SeriesPoint]:
    notes = notes if notes is not None else {}
    if not isinstance(payload, list):
        raise SourceError("Tiingo FX response is not a list")
    base, quote = pair[:3].upper(), pair[3:].upper()
    if not (codes.is_currency(base) and codes.is_currency(quote)):
        raise SourceError("Tiingo FX pair is not two ISO 4217 codes")
    points = []
    for row in payload:
        close = row.get("close")
        if close is None:
            notes["missing_values"] = notes.get("missing_values", 0) + 1
            continue
        ts = parse_iso(row.get("date"))
        p = safe_build(
            SeriesPoint,
            notes,
            source="tiingo",
            series="tiingo.fx_close",
            key=f"{base}/{quote}",
            ts=ts.replace(hour=0, minute=0, second=0, microsecond=0) if ts else None,
            value=close,
            unit=f"{quote} per {base}",
        )
        if p:
            points.append(p)
    return points


class TiingoConnector(KeyedConnector):
    name = "tiingo"
    min_interval = timedelta(hours=12)

    def fetch_with_key(self, client: httpx.Client, ctx: RunContext, key: str | None) -> Batch:
        batch = Batch()
        budget = DailyBudget.load(ctx, self.name, DAILY_LIMIT)
        headers = {"Authorization": f"Token {key}"}
        start = (ctx.now - timedelta(days=LOOKBACK_DAYS)).strftime("%Y-%m-%d")
        jobs = [("eod", t) for t in TICKERS] + [("fx", p) for p in FX_PAIRS]
        failed, unavailable, last_error = 0, 0, None
        for kind, symbol in jobs:
            if not budget.take():
                batch.notes["budget_stopped"] = 1
                break
            if kind == "eod":
                url = f"{API}/daily/{symbol.lower()}/prices?{urlencode({'startDate': start})}"
            else:
                url = f"{API}/fx/{symbol}/prices?{urlencode({'startDate': start, 'resampleFreq': '1day'})}"
            try:
                payload = get_json(client, url, headers=headers, sleep=ctx.sleep, follow_redirects=False)
                parser = parse_eod if kind == "eod" else parse_fx
                batch.series.extend(parser(payload, symbol, batch.notes))
            except Throttled as exc:  # hourly or daily cap reached: stop at once
                failed += 1
                last_error = str(exc)
                break
            except SourceError as exc:
                if "HTTP 404" in str(exc):
                    unavailable += 1  # symbol not covered on this plan
                else:
                    failed += 1
                last_error = str(exc)
        budget.save(batch.notes)
        batch.notes.update(symbols=len(jobs), symbols_failed=failed, symbols_unavailable=unavailable)
        # A symbol the plan does not cover (404) is counted, not an error.
        if failed or batch.notes.get("budget_stopped"):
            batch.status = Status.ERROR if not batch.series else Status.PARTIAL
            batch.error = last_error or "daily request budget used"
        return batch
