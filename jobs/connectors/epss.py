"""FIRST EPSS daily scores (gzipped CSV).

Docs: https://www.first.org/epss/data_stats
The full file holds ~400k CVEs per day. To stay within the Neon free tier only
CVEs whose score is at least ``EPSS_MIN_SCORE`` (default 0.5) are kept; the
threshold is a storage filter, it does not change any value.
"""

from __future__ import annotations

import csv
import gzip
import io
import os
import re
from datetime import timedelta

import httpx

from jobs.connectors.base import Batch, Connector, RunContext, parse_iso, safe_build
from jobs.http import SourceError, get
from jobs.models import SeriesPoint

URL = "https://epss.empiricalsecurity.com/epss_scores-current.csv.gz"
DEFAULT_MIN_SCORE = 0.5


def parse_csv(text: str, min_score: float, notes: dict[str, int] | None = None) -> list[SeriesPoint]:
    notes = notes if notes is not None else {}
    lines = text.splitlines()
    if not lines or not lines[0].startswith("#"):
        raise SourceError("EPSS file has no metadata line")
    meta = dict(re.findall(r"(\w+):([^,]+)", lines[0].lstrip("#")))
    ts = parse_iso(meta.get("score_date"))
    if ts is None:
        raise SourceError("EPSS file has no score_date")
    model = meta.get("model_version")
    reader = csv.DictReader(io.StringIO("\n".join(lines[1:])))
    points = []
    total = 0
    for row in reader:
        total += 1
        try:
            score = float(row["epss"])
        except (KeyError, TypeError, ValueError):
            notes["dropped_malformed"] = notes.get("dropped_malformed", 0) + 1
            continue
        if score < min_score:
            continue
        p = safe_build(
            SeriesPoint,
            notes,
            source="epss",
            series="epss.score",
            key=row["cve"],
            ts=ts,
            value=score,
            unit="probability",
            attrs={"percentile": float(row["percentile"]), "model_version": model},
        )
        if p:
            points.append(p)
    notes["rows_in_file"] = total
    return points


class EpssConnector(Connector):
    name = "epss"
    min_interval = timedelta(hours=6)

    def __init__(self, min_score: float | None = None):
        if min_score is None:
            min_score = float(os.environ.get("EPSS_MIN_SCORE") or DEFAULT_MIN_SCORE)
        self.min_score = min_score

    def fetch(self, client: httpx.Client, ctx: RunContext) -> Batch:
        resp = get(client, URL, sleep=ctx.sleep)
        raw = resp.content
        try:
            # httpx already inflates it if the server set Content-Encoding.
            text = (gzip.decompress(raw) if raw[:2] == b"\x1f\x8b" else raw).decode("utf-8")
        except (OSError, EOFError, UnicodeDecodeError) as exc:
            raise SourceError("EPSS file is not valid gzip/UTF-8") from exc
        batch = Batch()
        batch.series = parse_csv(text, self.min_score, batch.notes)
        return batch
