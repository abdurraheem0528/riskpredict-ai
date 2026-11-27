"""One news report, many copies: when two evidence items are the same item (engine 1.3.3, 1.3.4).

GDELT and the RSS feeds carry a syndicated article once per site that ran it, each copy
under its own ``raw_id``. Within one risk such copies are one item: the strongest copy
is kept and the others neither count in the likelihood (risk-engine.md §5.6) nor show.
Two news items are the same item when their normalised headlines are equal, or their
canonical URLs are. Since 1.3.4 the headline is the title without a trailing outlet or section
name and without a leading wire tag (``engine.relevance.headline``), so "… | The Arkansas
Democrat-Gazette" and "… | Northwest Arkansas Democrat-Gazette" are one report, and so are
"Analysis-Trump's new tariffs …" and "Trump's new tariffs …".

Only news is merged. A quake, a CVE record or a sanctions listing is identified by its
own ``raw_id``; two of those with the same title are two things (two quakes of the same
size near the same town, two listed people with one name).

Pure functions, shared by the engine (scoring) and the API (showing stored evidence).
"""

from __future__ import annotations

import re
import unicodedata
from typing import Callable, Iterable, TypeVar
from urllib.parse import parse_qsl, urlencode, urlsplit

from engine.relevance import headline
from engine.scoring import event_class

T = TypeVar("T")

# A title this short ("Market update", "Breaking news") may head different articles.
TITLE_MIN_WORDS = 3
# Query parameters that say how a reader arrived, not which article it is.
TRACKING_PARAMS = frozenset({
    "fbclid", "gclid", "dclid", "gbraid", "wbraid", "msclkid", "mc_cid", "mc_eid", "ocid", "cmpid",
    "smid", "smtyp", "ref", "ref_src", "taid", "guccounter", "guce_referrer", "guce_referrer_sig",
    "at_medium", "at_campaign",
})  # fmt: skip
_SEPARATORS = re.compile(r"[\W_]+")


def title_key(title: str | None, url: str | None = None) -> str | None:
    """Casefolded words of a title's headline (outlet name and wire tag dropped, 1.3.4; ``url``
    helps tell the outlet), accents and punctuation dropped; ``None`` when too short."""
    if not isinstance(title, str):
        return None
    t = unicodedata.normalize("NFKD", headline(title, url))
    t = "".join(ch for ch in t if not unicodedata.combining(ch)).casefold()
    words = _SEPARATORS.sub(" ", t).split()
    return " ".join(words) if len(words) >= TITLE_MIN_WORDS else None


def url_key(url: str | None) -> str | None:
    """Host (without ``www.``), path and the query minus tracking parameters; scheme and
    fragment ignored. ``None`` for a non-web URL or a bare home page."""
    if not isinstance(url, str) or not url.strip():
        return None
    try:
        parts = urlsplit(url.strip())
        host = (parts.hostname or "").lower()
    except ValueError:
        return None
    if parts.scheme.lower() not in ("http", "https") or not host:
        return None
    host = host.removeprefix("www.")
    path = re.sub(r"/{2,}", "/", parts.path).rstrip("/")
    query = sorted((k, v) for k, v in parse_qsl(parts.query, keep_blank_values=True)
                   if not (k.lower().startswith("utm_") or k.lower() in TRACKING_PARAMS))  # fmt: skip
    if not path and not query:
        return None
    return host + path + ("?" + urlencode(query) if query else "")


def item_keys(source: str | None, type_: str | None, title: str | None, url: str | None) -> tuple[str, ...]:
    """The keys under which a news item is the same as another; ``()`` for anything else."""
    if not source or not type_ or event_class(source, type_) != "news":
        return ()
    out = []
    if (t := title_key(title, url)) is not None:
        out.append(f"title:{t}")
    if (u := url_key(url)) is not None:
        out.append(f"url:{u}")
    return tuple(out)


def first_of_each(items: Iterable[T], keys: Callable[[T], tuple[str, ...]]) -> list[T]:
    """``items`` (strongest first) without later copies of an earlier item.

    A copy's keys are remembered too, so a copy of a copy (same title as one, same URL as
    the next) is still the first item's copy.
    """
    seen: set[str] = set()
    out: list[T] = []
    for item in items:
        ks = keys(item)
        if not ks or seen.isdisjoint(ks):
            out.append(item)
        seen.update(ks)
    return out
