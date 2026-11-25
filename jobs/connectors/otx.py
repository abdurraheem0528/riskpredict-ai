"""AlienVault / LevelBlue OTX — threat-intelligence pulses.

Docs: https://otx.alienvault.com/api (DirectConnect API v1) and the official
SDK https://github.com/AlienVault-OTX/OTX-Python-SDK
Key: ``OTX_API_KEY`` (free account), sent in the ``X-OTX-API-KEY`` header.

Limit (checked 2026-11-24, LevelBlue support answer "API requests limit"):
10,000 authenticated requests an hour per key, subject to change; no daily cap
is published. We poll ``/api/v1/pulses/subscribed`` once an hour for pulses
modified since the previous run (``modified_since``, kept as
``modified_since`` in this source's notes), at most ``MAX_PAGES`` pages of
``PAGE_SIZE``: <= 120 requests a day. A new account is subscribed to the
"AlienVault" user, which publishes the curated public pulses.

One event per pulse: type ``cyber.threat_pulse``, ``raw_id`` = pulse id,
``countries`` = ``targeted_countries`` mapped to ISO alpha-3 (unmatched names
counted, never guessed), ``software`` empty (OTX does not name products).
The indicator lists (IPs, hashes, URLs) are not stored, only their count and
any CVE ids among them (``attrs.cves``) so the risk engine can link a pulse to
``nvd``/``cisa_kev`` events. Only pulses marked TLP white/clear or green are
kept: this database is read by a public-facing dashboard. OTX publishes no
severity, so ``severity`` stays empty.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from urllib.parse import urlencode

import httpx

from jobs import codes
from jobs.connectors.base import Batch, KeyedConnector, RunContext, parse_iso, safe_build
from jobs.http import SourceError, get_json
from jobs.models import Event, Status

API = "https://otx.alienvault.com/api/v1/pulses/subscribed"
PAGE_SIZE = 20
MAX_PAGES = 5
DEFAULT_LOOKBACK = timedelta(days=2)
SHAREABLE_TLP = {"white", "clear", "green"}
MAX_LIST = 20  # cap on tags, references, CVEs kept per pulse


def _strings(values, limit: int = MAX_LIST, prefer_id: bool = False) -> list[str]:
    out = []
    for v in values or []:
        if isinstance(v, dict):  # industries / malware_families / attack_ids can be objects
            order = ("id", "display_name", "name") if prefer_id else ("display_name", "name", "id")
            v = next((v[k] for k in order if v.get(k)), None)
        if v is None:
            continue
        s = str(v).strip()
        if s and s not in out:
            out.append(s)
    return out[:limit]


def parse(payload: dict, notes: dict[str, int] | None = None) -> list[Event]:
    notes = notes if notes is not None else {}
    if "results" not in payload:
        raise SourceError("OTX response has no results")
    events = []
    for p in payload.get("results") or []:
        pid = p.get("id")
        if not pid:
            notes["dropped_malformed"] = notes.get("dropped_malformed", 0) + 1
            continue
        tlp = (p.get("TLP") or p.get("tlp") or "").strip().lower()
        if tlp not in SHAREABLE_TLP:
            notes["skipped_tlp"] = notes.get("skipped_tlp", 0) + 1
            continue
        countries = []
        for c in p.get("targeted_countries") or []:
            a3 = codes.country_alpha3(c)
            if a3:
                countries.append(a3)
            else:
                notes["unmatched_country_names"] = notes.get("unmatched_country_names", 0) + 1
        indicators = p.get("indicators") or []
        cves = [i.get("indicator") for i in indicators if (i.get("type") or "").upper() == "CVE" and i.get("indicator")]
        ev = safe_build(
            Event,
            notes,
            source="otx",
            raw_id=str(pid),
            type="cyber.threat_pulse",
            title=p.get("name") or f"OTX pulse {pid}",
            url=f"https://otx.alienvault.com/pulse/{pid}",
            published_at=parse_iso(p.get("created")),
            countries=countries,
            attrs={
                k: v
                for k, v in {
                    "modified": p.get("modified"),
                    "author": p.get("author_name"),
                    "tlp": tlp,
                    "adversary": p.get("adversary") or None,
                    "tags": _strings(p.get("tags")) or None,
                    "industries": _strings(p.get("industries")) or None,
                    "malware_families": _strings(p.get("malware_families")) or None,
                    "attack_ids": _strings(p.get("attack_ids"), prefer_id=True) or None,  # MITRE ATT&CK ids
                    "cves": _strings(cves) or None,
                    "references": _strings(p.get("references"), 5) or None,
                    "indicator_count": len(indicators) or None,
                    "revision": p.get("revision"),
                }.items()
                if v is not None
            },
        )
        if ev:
            events.append(ev)
    return events


class OtxConnector(KeyedConnector):
    name = "otx"
    min_interval = timedelta(hours=1)

    def since(self, ctx: RunContext) -> datetime:
        prev = (ctx.state.get(self.name) or {}).get("modified_since")
        if prev:
            ts = datetime.fromtimestamp(int(prev), tz=timezone.utc)
            if timedelta(0) <= ctx.now - ts <= timedelta(days=30):
                return ts
        return ctx.now - DEFAULT_LOOKBACK

    def fetch_with_key(self, client: httpx.Client, ctx: RunContext, key: str | None) -> Batch:
        batch = Batch()
        since = self.since(ctx)
        headers = {"X-OTX-API-KEY": key or ""}
        page, complete = 1, False
        try:
            while page <= MAX_PAGES:
                query = urlencode(
                    {"limit": PAGE_SIZE, "page": page, "modified_since": since.strftime("%Y-%m-%dT%H:%M:%S")}
                )
                payload = get_json(client, f"{API}?{query}", headers=headers, sleep=ctx.sleep, follow_redirects=False)
                batch.events.extend(parse(payload, batch.notes))
                if not payload.get("next"):
                    complete = True
                    break
                page += 1
        except SourceError as exc:
            if page == 1:
                raise
            batch.status, batch.error = Status.PARTIAL, str(exc)
        batch.notes["pages"] = min(page, MAX_PAGES)
        # Advance only after a complete read; otherwise re-read the same window.
        batch.notes["modified_since"] = int((ctx.now if complete else since).timestamp())
        if not complete and batch.status is None:
            batch.status = Status.PARTIAL
            batch.error = f"stopped after {MAX_PAGES} pages; the rest is fetched next run"
        return batch
