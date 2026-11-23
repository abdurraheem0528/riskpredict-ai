"""OSV.dev — known vulnerabilities for packages in watch terms of kind
``osv_package`` (``scope`` = OSV ecosystem, ``term`` = package name).

Docs: https://google.github.io/osv.dev/post-v1-query/
Severity is the CVSS base score / 10, computed from the CVSS vector OSV
publishes (v3 preferred, then v4). Without a vector it stays empty.
"""

from __future__ import annotations

from datetime import timedelta

import httpx
from cvss import CVSS3, CVSS4

from jobs.connectors.base import Batch, Connector, RunContext, parse_iso, safe_build
from jobs.http import SourceError, request
from jobs.models import Event, Status

URL = "https://api.osv.dev/v1/query"
MAX_PAGES = 10


def cvss_severity(entries: list[dict] | None) -> float | None:
    by_type = {e.get("type"): e.get("score") for e in entries or [] if e.get("score")}
    for kind, cls in (("CVSS_V3", CVSS3), ("CVSS_V4", CVSS4)):
        vector = by_type.get(kind)
        if not vector:
            continue
        try:
            score = cls(vector).base_score
        except Exception:  # malformed vector in the advisory
            continue
        return round(float(score) / 10.0, 3)
    return None


def parse(payload: dict, ecosystem: str, package: str, notes: dict[str, int] | None = None) -> list[Event]:
    notes = notes if notes is not None else {}
    events = []
    for v in payload.get("vulns") or []:
        vid = v.get("id")
        if not vid:
            continue
        label = (v.get("database_specific") or {}).get("severity")
        ev = safe_build(
            Event,
            notes,
            source="osv",
            raw_id=vid,
            type="cyber.vulnerability",
            title=v.get("summary") or vid,
            url=f"https://osv.dev/vulnerability/{vid}",
            published_at=parse_iso(v.get("published")),
            software=[f"{ecosystem}:{package}"],
            severity=cvss_severity(v.get("severity")),
            attrs={
                k: val
                for k, val in {
                    "aliases": v.get("aliases") or None,
                    "modified": v.get("modified"),
                    "withdrawn": v.get("withdrawn"),
                    "severity_label": label,
                }.items()
                if val
            },
        )
        if ev:
            events.append(ev)
    return events


class OsvConnector(Connector):
    name = "osv"
    min_interval = timedelta(hours=6)

    def fetch(self, client: httpx.Client, ctx: RunContext) -> Batch:
        terms = ctx.terms("osv_package")
        if not terms:
            return Batch(status=Status.SKIPPED, error="no osv_package watch terms")
        batch = Batch()
        failed = 0
        last_error = None
        for t in terms:
            body: dict = {"package": {"name": t.term, "ecosystem": t.scope}}
            try:
                for _ in range(MAX_PAGES):
                    resp = request(client, "POST", URL, json=body, sleep=ctx.sleep)
                    try:
                        payload = resp.json()
                    except ValueError as exc:
                        raise SourceError("invalid JSON from api.osv.dev") from exc
                    batch.events.extend(parse(payload, t.scope, t.term, batch.notes))
                    token = payload.get("next_page_token")
                    if not token:
                        break
                    body = {**body, "page_token": token}
            except SourceError as exc:
                failed += 1
                last_error = str(exc)
        batch.notes.update(packages=len(terms), packages_failed=failed)
        if failed:
            batch.status = Status.ERROR if failed == len(terms) else Status.PARTIAL
            batch.error = last_error
        return batch
