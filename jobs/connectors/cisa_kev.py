"""CISA Known Exploited Vulnerabilities catalog (JSON).

Docs: https://www.cisa.gov/known-exploited-vulnerabilities-catalog
KEV publishes no severity score, so ``severity`` stays empty.
"""

from __future__ import annotations

from datetime import timedelta

import httpx

from jobs.connectors.base import Batch, Connector, RunContext, parse_date, safe_build
from jobs.http import get_json
from jobs.models import Event

URL = "https://www.cisa.gov/sites/default/files/feeds/known_exploited_vulnerabilities.json"


def parse(payload: dict, notes: dict[str, int] | None = None) -> list[Event]:
    notes = notes if notes is not None else {}
    version = payload.get("catalogVersion")
    events = []
    for v in payload.get("vulnerabilities") or []:
        cve = (v.get("cveID") or "").strip()
        if not cve:
            continue
        vendor, product = v.get("vendorProject"), v.get("product")
        ev = safe_build(
            Event,
            notes,
            source="cisa_kev",
            raw_id=cve,
            type="cyber.exploited_vulnerability",
            title=v.get("vulnerabilityName") or cve,
            url=f"https://nvd.nist.gov/vuln/detail/{cve}",
            published_at=parse_date(v.get("dateAdded")),
            software=[f"{vendor}:{product}"] if vendor and product else [],
            attrs={
                k: val
                for k, val in {
                    "cve": cve,
                    "due_date": v.get("dueDate"),
                    "known_ransomware_use": v.get("knownRansomwareCampaignUse"),
                    "cwes": v.get("cwes") or None,
                    "catalog_version": version,
                }.items()
                if val
            },
        )
        if ev:
            events.append(ev)
    return events


class CisaKevConnector(Connector):
    name = "cisa_kev"
    min_interval = timedelta(hours=1)

    def fetch(self, client: httpx.Client, ctx: RunContext) -> Batch:
        batch = Batch()
        batch.events = parse(get_json(client, URL, sleep=ctx.sleep), batch.notes)
        return batch
