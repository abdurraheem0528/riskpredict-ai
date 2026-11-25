"""Shared pieces of the keyed news connectors (NewsData.io, Currents).

Both ask about the same watch terms as GDELT (``feeds.watch_terms`` kind
``gdelt_query``). Those are ``("phrase" OR "phrase")`` lists, which both APIs
accept as boolean queries. Terms using GDELT-only operators (``theme:``,
``sourcecountry:``...) are skipped and counted.

Free quotas are small, so each run sends at most ``per_run`` queries and the
next run continues where this one stopped (``term_offset`` in the source's
notes), cycling through all terms over a few runs.

Articles are stored like GDELT's: type ``news``, ``raw_id`` = sha256(url)[:32]
(so one URL is one row per source), ``countries`` EMPTY (the APIs give the
publisher's country, kept as ``attrs.publisher_country``, not where the
event is), ``attrs.query`` = the watch term.
"""

from __future__ import annotations

import hashlib
import re

from jobs.connectors.base import RunContext

_UNSUPPORTED = re.compile(r"\b[a-z]+:", re.I)
MAX_QUERY_CHARS = 500  # NewsData allows 512


def url_id(url: str) -> str:
    return hashlib.sha256(url.encode("utf-8")).hexdigest()[:32]


def supported(term: str) -> bool:
    return not _UNSUPPORTED.search(term) and len(term) <= MAX_QUERY_CHARS


class Rotation:
    """The watch terms for this run, starting where the previous run stopped."""

    def __init__(self, ctx: RunContext, source: str, per_run: int):
        terms = [t.term for t in ctx.terms("gdelt_query")]
        usable = [t for t in terms if supported(t)]
        self.unsupported = len(terms) - len(usable)
        self.total = len(usable)
        self.start = int((ctx.state.get(source) or {}).get("term_offset") or 0) % max(self.total, 1)
        n = min(per_run, self.total)
        self.terms = [usable[(self.start + i) % self.total] for i in range(n)]

    def next_offset(self, sent: int) -> int:
        """Offset for the next run after ``sent`` of this run's terms were tried."""
        return (self.start + sent) % max(self.total, 1)


def domain_of(url: str | None) -> str | None:
    m = re.match(r"https?://([^/:?#]+)", url or "")
    return m.group(1).lower() if m else None
