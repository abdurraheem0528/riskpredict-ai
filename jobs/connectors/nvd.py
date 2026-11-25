"""NVD CVE API 2.0 — CVEs with CVSS scores and affected products (CPE).

Docs: https://nvd.nist.gov/developers/vulnerabilities and
https://nvd.nist.gov/developers/start-here
Key: ``NVD_API_KEY`` (free, optional). It is sent in the ``apiKey`` header. NVD
also works without a key at a lower rate, so this source runs keyless too.

Limits (checked 2026-11-24, developers/start-here): 5 requests per rolling
30 s without a key, 50 with one; NVD asks clients to sleep ~6 s between
requests and to poll for changes "no more than once every two hours".
We run every 2 hours with one request per page (2,000 CVEs a page, at most
``MAX_PAGES``), sleeping between pages: ~12-30 requests a day.

Each run asks for CVEs *modified* since the previous run's window end (kept as
``window_end`` in this source's ``feeds.source_health.notes``; first run or
lost state: the last ``DEFAULT_LOOKBACK``). Only CVEs whose CVSS base score is
at least ``NVD_MIN_CVSS`` (default 7.0, i.e. High and Critical) are stored,
to stay inside the Neon free tier; the threshold filters, it changes no value.

Mapping: ``severity`` = CVSS base score / 10, preferring NVD's own (Primary)
score and v3.1 > v3.0 > v4.0 > v2. ``software`` = ``"<vendor>:<product>"``
from the vulnerable CPE 2.3 match criteria (``cpe:2.3:a:apache:log4j:...`` ->
``apache:log4j``; applications before OS and hardware entries) after the CNA's
``affected`` vendor/product as published, at most ``MAX_SOFTWARE``;
``attrs.cpes`` keeps the version-less CPE prefixes and ``attrs.purls`` the
package URLs the CNA gave.
"""

from __future__ import annotations

import os
import re
from datetime import datetime, timedelta, timezone
from urllib.parse import urlencode

import httpx

from jobs.connectors.base import Batch, KeyedConnector, RunContext, parse_iso, safe_build
from jobs.http import SourceError, get_json
from jobs.models import Event, Status

API = "https://services.nvd.nist.gov/rest/json/cves/2.0"
DEFAULT_MIN_CVSS = 7.0
DEFAULT_LOOKBACK = timedelta(days=1)
MAX_WINDOW = timedelta(days=120)  # NVD rejects longer lastMod ranges
MAX_STEP = timedelta(days=1)  # catch up one day per run after an outage
MAX_PAGES = 5
PAGE_SLEEP_KEYLESS = 6.5  # 5 requests / 30 s
PAGE_SLEEP_KEYED = 1.0  # 50 requests / 30 s
MAX_SOFTWARE = 40
MAX_PURLS = 10
_CPE_SPLIT = re.compile(r"(?<!\\):")
_METRIC_ORDER = ("cvssMetricV31", "cvssMetricV30", "cvssMetricV40", "cvssMetricV2")


def nvd_time(ts: datetime) -> str:
    return ts.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z")


def cpe_parts(criteria: str) -> list[str] | None:
    """Split a CPE 2.3 formatted string on unescaped colons (``\\:`` stays)."""
    parts = _CPE_SPLIT.split(criteria or "")
    if len(parts) < 5 or parts[0] != "cpe" or parts[1] != "2.3":
        return None
    return [p.replace("\\", "") for p in parts]


def best_metric(metrics: dict | None) -> tuple[dict, str] | None:
    """NVD's own (Primary) score first, then by CVSS version."""
    candidates = []
    for rank, name in enumerate(_METRIC_ORDER):
        for m in (metrics or {}).get(name) or []:
            data = m.get("cvssData") or {}
            if data.get("baseScore") is None:
                continue
            candidates.append((m.get("type") != "Primary", rank, m, name))
    if not candidates:
        return None
    _, _, m, name = min(candidates, key=lambda c: (c[0], c[1]))
    return m, name


def _english(descriptions) -> str:
    for d in descriptions or []:
        if d.get("lang") == "en" and d.get("value"):
            return " ".join(d["value"].split())
    return ""


def parse(payload: dict, min_cvss: float, notes: dict[str, int] | None = None) -> list[Event]:
    notes = notes if notes is not None else {}
    if "vulnerabilities" not in payload:
        raise SourceError("NVD response has no vulnerabilities list")
    events = []
    for item in payload.get("vulnerabilities") or []:
        cve = item.get("cve") or {}
        cid = cve.get("id")
        if not cid:
            notes["dropped_malformed"] = notes.get("dropped_malformed", 0) + 1
            continue
        if cve.get("vulnStatus") == "Rejected":
            notes["rejected"] = notes.get("rejected", 0) + 1
            continue
        best = best_metric(cve.get("metrics"))
        if best is None:
            notes["no_cvss"] = notes.get("no_cvss", 0) + 1
            continue
        metric, metric_name = best
        data = metric.get("cvssData") or {}
        score = float(data["baseScore"])
        if score < min_cvss:
            notes["below_threshold"] = notes.get("below_threshold", 0) + 1
            continue

        apps: list[str] = []  # CPE part "a" first: what software matching looks for
        others: list[str] = []
        cpes: list[str] = []
        for conf in cve.get("configurations") or []:
            for node in conf.get("nodes") or []:
                for match in node.get("cpeMatch") or []:
                    if not match.get("vulnerable"):
                        continue
                    parts = cpe_parts(match.get("criteria", ""))
                    if not parts or parts[3] in ("*", "-") or parts[4] in ("*", "-"):
                        continue
                    (apps if parts[2] == "a" else others).append(f"{parts[3]}:{parts[4]}")
                    cpes.append(":".join(["cpe", "2.3", parts[2], parts[3], parts[4]]))
        cna: list[str] = []
        purls: list[str] = []
        for aff in cve.get("affected") or []:
            for a in aff.get("affectedData") or []:
                vendor, product = (a.get("vendor") or "").strip(), (a.get("product") or "").strip()
                if vendor and product and vendor.lower() != "n/a" and product.lower() != "n/a":
                    cna.append(f"{vendor}:{product}")
                if a.get("packageURL"):
                    purls.append(a["packageURL"])
        software = list(dict.fromkeys(cna + apps + others))  # the CNA names only a few
        cpes = sorted(dict.fromkeys(cpes), key=lambda c: c.split(":")[2] != "a")
        purls = list(dict.fromkeys(purls))
        if len(software) > MAX_SOFTWARE:
            notes["software_truncated"] = notes.get("software_truncated", 0) + 1

        desc = _english(cve.get("descriptions"))
        title = f"{cid}: {desc}" if desc else cid
        if len(title) > 300:
            title = title[:297].rstrip() + "..."
        cwes = [
            d["value"]
            for w in cve.get("weaknesses") or []
            for d in w.get("description") or []
            if d.get("value", "").startswith("CWE-")
        ]
        ev = safe_build(
            Event,
            notes,
            source="nvd",
            raw_id=cid,
            type="cyber.vulnerability",
            title=title,
            url=f"https://nvd.nist.gov/vuln/detail/{cid}",
            published_at=parse_iso(cve.get("published")),
            software=software[:MAX_SOFTWARE],
            severity=round(score / 10.0, 3),
            attrs={
                k: v
                for k, v in {
                    "cvss_score": score,
                    "cvss_version": data.get("version"),
                    "cvss_vector": data.get("vectorString"),
                    "cvss_source": "nvd" if metric.get("type") == "Primary" else "cna",
                    "base_severity": data.get("baseSeverity") or metric.get("baseSeverity"),
                    "vuln_status": cve.get("vulnStatus"),
                    "last_modified": cve.get("lastModified"),
                    "cwes": list(dict.fromkeys(cwes)) or None,
                    "cpes": cpes[:MAX_SOFTWARE] or None,
                    "purls": purls[:MAX_PURLS] or None,
                    "cisa_kev": True if cve.get("cisaExploitAdd") else None,
                    "metric": metric_name,
                }.items()
                if v is not None
            },
        )
        if ev:
            events.append(ev)
    return events


class NvdConnector(KeyedConnector):
    name = "nvd"
    min_interval = timedelta(hours=2)
    key_optional = True  # keyless works at 5 requests / 30 s

    def __init__(self, min_cvss: float | None = None):
        if min_cvss is None:
            min_cvss = float(os.environ.get("NVD_MIN_CVSS") or DEFAULT_MIN_CVSS)
        self.min_cvss = min_cvss

    def window(self, ctx: RunContext) -> tuple[datetime, datetime, int]:
        """(start, end, start index). Resumes an unfinished window exactly;
        otherwise continues from the last window end, at most one day at a time."""
        now = ctx.now.replace(microsecond=0)
        prev = ctx.state.get(self.name) or {}
        if prev.get("next_index") and prev.get("pending_start") and prev.get("pending_end"):
            start = datetime.fromtimestamp(int(prev["pending_start"]), tz=timezone.utc)
            end = datetime.fromtimestamp(int(prev["pending_end"]), tz=timezone.utc)
            if timedelta(0) < end - start <= MAX_WINDOW:
                return start, end, int(prev["next_index"])
        last = prev.get("window_end")
        start = datetime.fromtimestamp(int(last), tz=timezone.utc) if last else now - DEFAULT_LOOKBACK
        if not timedelta(0) < now - start <= MAX_WINDOW:
            start = now - DEFAULT_LOOKBACK
        return start, min(now, start + MAX_STEP), 0

    def fetch_with_key(self, client: httpx.Client, ctx: RunContext, key: str | None) -> Batch:
        batch = Batch()
        start, end, index = self.window(ctx)
        headers = {"apiKey": key} if key else {}
        pause = PAGE_SLEEP_KEYED if key else PAGE_SLEEP_KEYLESS
        total, pages, complete = None, 0, False
        try:
            while pages < MAX_PAGES:
                if pages:
                    ctx.sleep(pause)
                query = urlencode(
                    {"lastModStartDate": nvd_time(start), "lastModEndDate": nvd_time(end), "startIndex": index}
                )
                payload = get_json(
                    client, f"{API}?{query}", headers=headers, sleep=ctx.sleep, follow_redirects=False
                )
                batch.events.extend(parse(payload, self.min_cvss, batch.notes))
                pages += 1
                total = int(payload.get("totalResults") or 0)
                per_page = int(payload.get("resultsPerPage") or 0)
                index += per_page
                if index >= total or per_page == 0:
                    complete = True
                    break
        except SourceError as exc:
            if pages == 0:
                raise
            batch.status, batch.error = Status.PARTIAL, str(exc)
        batch.notes.update(pages=pages, cves_in_window=total or 0, keyed=1 if key else 0)
        if complete:
            batch.notes["window_end"] = int(end.timestamp())
        else:
            # Resume this exact window from `next_index` next run.
            batch.notes.update(
                window_end=int(start.timestamp()),
                pending_start=int(start.timestamp()),
                pending_end=int(end.timestamp()),
                next_index=index,
            )
            if batch.status is None:
                batch.status = Status.PARTIAL
                batch.error = f"stopped after {MAX_PAGES} pages; the rest is fetched next run"
        return batch
