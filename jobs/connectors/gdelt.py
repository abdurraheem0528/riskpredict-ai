"""GDELT news articles matching watch terms of kind ``gdelt_query``.

**Primary path: the 15-minute raw files.** ``data.gdeltproject.org/gdeltv2/
lastupdate.txt`` names the newest Global Knowledge Graph file
(``YYYYMMDDHHMMSS.gkg.csv.zip``; one tab-separated row per English-language
article, GKG 2.1 codebook). Each run downloads the newest windows not stored
yet (at most :data:`MAX_WINDOWS`, tracked as ``gkg_window`` in this source's
``feeds.source_health.notes``), parses them with the standard library and keeps
the articles that match a watch term. Earlier windows have predictable names,
so a late run still picks up the windows it missed (up to the cap).

GDELT sometimes skips a 15-minute file, so a computed earlier window can be a
404: that window is counted in ``notes.missing_windows`` and passed over (the
stored window moves past it). Only the file ``lastupdate.txt`` lists is sure to
exist; if it is a 404 anyway (not on the server yet), it is left for the next
run (``notes.latest_missing``) and the stored window does not move past it.

**Fallback: one DOC 2.0 API call.** Only if ``lastupdate.txt`` cannot be read
(or names no usable GKG file); never because a window file failed. Then all watch terms are ORed into a single query. That API is rate-limited (one
request per 5 s per IP, shared by everyone on a GitHub runner), so on a 429 or
its throttle text the run stops at once and reports ``throttled``.

Matching follows the DOC API query syntax our watch terms use: a space means
AND, ``( ... OR ... )`` means any of, ``"..."`` is an exact phrase, ``-`` negates
and ``theme:CODE`` matches a GKG theme. Phrases and words match whole words,
case-insensitively, in the article title, its URL, the organisation and
location names GDELT extracted, and the theme names (``ECON_CURRENCY_RESERVES``
reads as "econ currency reserves"). ``theme:CODE`` matches the exact GKG theme
code (any case), and can be combined like words: ``theme:A (theme:B OR
theme:C)`` needs A and one of B or C. Other DOC operators (``sourcecountry:``...)
cannot be checked against the files, so a term using them matches nothing there
and is counted in ``notes.terms_unsupported``.

An article is stored once, for the first term it matches (terms in
``feeds.watch_terms`` id order: the phrase defaults of migration 002 before the
theme defaults of 041). ``attrs.query`` is that term; for a term with ``theme:``
codes, ``attrs.themes`` lists the codes the article carried. The event type is
still ``news``: GLiNER2 types it from the title like any other article, so an
article whose title is off-topic is tagged ``other`` and is never evidence
except through a company name (``risk-engine.md``). The default theme terms
match ~15 articles per 15-minute file (see ``jobs/watch_defaults.py``), so
:data:`MAX_EVENTS` only bounds an exceptional news day. The DOC fallback can
only send terms that are one OR-group, so it skips the AND-ed theme terms
(``notes.doc_terms_skipped``).

The URL is the record id, so an article seen in two windows, or by both paths,
is one row. ``countries`` are the countries of the locations GDELT found in the
article text (FIPS 10-4 codes converted to ISO alpha-3); for DOC API articles
GDELT gives no event location, so they stay empty (``sourcecountry`` is where
the publisher is and is kept in ``attrs`` only).

Docs: http://data.gdeltproject.org/documentation/GDELT-Global_Knowledge_Graph_Codebook-V2.1.pdf
and https://blog.gdeltproject.org/gdelt-doc-2-0-api-debuts/
"""

from __future__ import annotations

import csv
import dataclasses
import hashlib
import html
import io
import json
import re
import time
import zipfile
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from urllib.parse import urlencode, urlsplit

import httpx
import pycountry

from jobs import codes
from jobs.connectors.base import Batch, Connector, RunContext, WatchTerm, safe_build
from jobs.http import SourceError, Throttled, get, host_of
from jobs.models import Event, Status

FILES = "http://data.gdeltproject.org/gdeltv2/"
LASTUPDATE = FILES + "lastupdate.txt"
API = "https://api.gdeltproject.org/api/v2/doc/doc"

WINDOW = timedelta(minutes=15)
MAX_WINDOWS = 3  # runs are every 30 min (2 windows); one more absorbs a late start
MAX_ZIP_BYTES = 40 * 2**20  # GKG zips are ~5-10 MB
MAX_CSV_BYTES = 200 * 2**20  # uncompressed (zip-bomb guard); ~25-40 MB in practice
MAX_FIELD_BYTES = 16 * 2**20  # csv's default 128 KiB field limit is too small for GCAM
MAX_EVENTS = 1500  # per run, after de-duplication
MAX_COUNTRIES = 10
FILES_BUDGET_S = 15.0  # no new window download after this; the DOC fallback stays inside ~30 s
LIST_TIMEOUT = httpx.Timeout(10.0, connect=5.0)
FILE_TIMEOUT = httpx.Timeout(5.0, connect=5.0)  # per read; the budget bounds the total
API_TIMEOUT = httpx.Timeout(8.0, connect=4.0)
TIMESPAN = "1h"  # DOC fallback only: two 15-min windows plus slack for a late run
MAX_RECORDS = 250  # DOC API maximum

# GKG 2.1 column positions (0-based), from the codebook.
GKG_ID, GKG_DATE, GKG_COLLECTION, GKG_DOMAIN, GKG_URL = 0, 1, 2, 3, 4
GKG_V1THEMES, GKG_V2THEMES, GKG_V1LOCATIONS, GKG_V2LOCATIONS = 7, 8, 9, 10
GKG_V1ORGS, GKG_V2ORGS, GKG_TONE, GKG_TRANSLATION, GKG_EXTRAS = 13, 14, 15, 25, 26
GKG_COLUMNS = 27
WEB_COLLECTION = "1"  # V2SOURCECOLLECTIONIDENTIFIER: 1 = WEB, the document id is a URL

_TITLE_RE = re.compile(r"<PAGE_TITLE>(.*?)</PAGE_TITLE>", re.S)
_TAG_RE = re.compile(r"<[^>]*>")
_NONWORD_RE = re.compile(r"[^0-9a-z|]+")
_WINDOW_RE = re.compile(r"/(\d{14})\.gkg\.csv\.zip$")


class MissingFile(SourceError):
    """HTTP 404 for a GKG window file: GDELT skipped it or has not published it yet."""


# --- DOC 2.0 API ---------------------------------------------------------------


def is_throttle_text(body: str) -> bool:
    head = body.lstrip()[:200].lower()
    return head.startswith("please limit requests") or "limit requests to one every" in head


def parse_seendate(value: str) -> datetime:
    return datetime.strptime(value, "%Y%m%dT%H%M%SZ").replace(tzinfo=timezone.utc)


def url_id(url: str) -> str:
    return hashlib.sha256(url.encode()).hexdigest()[:32]


def parse_articles(payload: dict, query: str | None, notes: dict[str, int] | None = None) -> list[Event]:
    """Turn a DOC 2.0 ``mode=artlist`` JSON payload into events.

    GDELT gives no event location: ``sourcecountry`` is where the publisher is,
    so it is kept in ``attrs`` and *not* used for ``countries``.
    """
    notes = notes if notes is not None else {}
    events: list[Event] = []
    for art in payload.get("articles") or []:
        url = (art.get("url") or "").strip()
        if not url:
            continue

        def build(art=art, url=url):
            return Event(
                source="gdelt",
                raw_id=url_id(url),
                type="news",
                title=art.get("title") or "",
                url=url,
                published_at=parse_seendate(art["seendate"]) if art.get("seendate") else None,
                attrs={
                    k: v
                    for k, v in {
                        "query": query,
                        "domain": art.get("domain"),
                        "language": art.get("language"),
                        "sourcecountry": art.get("sourcecountry"),
                        "feed": "doc",
                    }.items()
                    if v
                },
            )

        ev = safe_build(build, notes)
        if ev:
            events.append(ev)
    return events


def decode(body: str) -> dict:
    if is_throttle_text(body):
        raise Throttled("GDELT throttle message")
    if not body.strip():
        return {}
    try:
        # GDELT occasionally emits control characters inside strings.
        return json.loads(body, strict=False)
    except ValueError as exc:
        raise SourceError("invalid JSON from api.gdeltproject.org") from exc


# --- watch-term matching --------------------------------------------------------


def normalise(text: str) -> str:
    """Lower-case words separated by single spaces; ``|`` (field separator) kept."""
    return " " + " ".join(_NONWORD_RE.sub(" ", html.unescape(text).casefold()).split()) + " "


@dataclass(frozen=True)
class Token:
    kind: str  # "text" | "theme" | "unsupported"
    value: str
    negated: bool = False


@dataclass
class Matcher:
    """One watch term as an AND of OR-groups of tokens."""

    term: str
    groups: list[list[Token]] = field(default_factory=list)

    @property
    def supported(self) -> bool:
        return bool(self.groups) and all(t.kind != "unsupported" for g in self.groups for t in g)

    def matches(self, hay: str, themes: frozenset[str]) -> bool:
        if not self.supported:
            return False
        for group in self.groups:
            hit = any(_token_hit(t, hay, themes) for t in group)
            if group[0].negated:
                if hit:
                    return False
            elif not hit:
                return False
        return any(not g[0].negated for g in self.groups)


def _token_hit(t: Token, hay: str, themes: frozenset[str]) -> bool:
    if t.kind == "theme":
        return t.value in themes
    return t.value in hay


_QUERY_TOKEN_RE = re.compile(r'-?"[^"]*"|[()]|[^\s()"]+')


def parse_query(term: str) -> Matcher:
    """Parse a DOC-API style query (see the module docstring)."""
    m = Matcher(term)
    group: list[Token] | None = None
    for raw in _QUERY_TOKEN_RE.findall(term):
        if raw == "(":
            group = []
            continue
        if raw == ")":
            if group:
                m.groups.append(group)
            group = None
            continue
        if raw.upper() == "OR":
            continue
        negated = raw.startswith("-") and len(raw) > 1
        body = raw[1:] if negated else raw
        if body.startswith('"'):
            tok = Token("text", normalise(body.strip('"')), negated)
        elif body.lower().startswith("theme:"):
            tok = Token("theme", body[6:].upper(), negated)
        elif ":" in body or "<" in body or ">" in body:
            tok = Token("unsupported", body, negated)
        else:
            tok = Token("text", normalise(body), negated)
        if tok.kind == "text" and tok.value.strip() == "":
            continue
        if group is not None:
            group.append(tok)
        else:
            m.groups.append([tok])
    if group:
        m.groups.append(group)  # unclosed "(": be lenient
    return m


def doc_api_query(matchers: list[Matcher]) -> tuple[str | None, int]:
    """All terms ORed into one DOC query, and how many terms could not be folded in.

    The DOC API does not nest parentheses, so only terms that are a single
    OR-group of phrases, words and ``theme:`` codes (like our defaults) can be combined.
    """
    parts: list[str] = []
    skipped = 0
    for m in matchers:
        if len(m.groups) == 1 and all(t.kind in ("text", "theme") and not t.negated for t in m.groups[0]):
            for t in m.groups[0]:
                words = t.value.strip()
                if t.kind == "theme":
                    parts.append(f"theme:{words}")
                else:
                    parts.append(f'"{words}"' if " " in words else words)
        else:
            skipped += 1
    parts = list(dict.fromkeys(parts))
    if not parts:
        return None, skipped
    return (parts[0] if len(parts) == 1 else "(" + " OR ".join(parts) + ")"), skipped


def first_matcher(matchers: list[Matcher], hay: str, themes: frozenset[str] = frozenset()) -> Matcher | None:
    for m in matchers:
        if m.matches(hay, themes):
            return m
    return None


def first_match(matchers: list[Matcher], hay: str, themes: frozenset[str] = frozenset()) -> str | None:
    m = first_matcher(matchers, hay, themes)
    return m.term if m else None


def matched_themes(m: Matcher, themes: frozenset[str]) -> list[str]:
    """The term's (non-negated) theme codes that the article carries, in term order."""
    out = [t.value for g in m.groups for t in g if t.kind == "theme" and not t.negated and t.value in themes]
    return list(dict.fromkeys(out))


# --- GKG 2.1 files ----------------------------------------------------------------


def parse_lastupdate(text: str) -> dict[str, tuple[int, str]]:
    """``{"gkg": (size, url), "export": ..., "mentions": ...}`` from lastupdate.txt
    (lines of ``<bytes> <md5> <url>``)."""
    out: dict[str, tuple[int, str]] = {}
    for line in text.splitlines():
        parts = line.split()
        if len(parts) != 3 or not parts[0].isdigit():
            continue
        name = parts[2].rsplit("/", 1)[-1].lower()
        for kind in ("gkg", "export", "mentions"):
            if f".{kind}." in name:
                out[kind] = (int(parts[0]), parts[2])
    return out


def window_of(url: str) -> datetime | None:
    m = _WINDOW_RE.search(url)
    if not m:
        return None
    return datetime.strptime(m.group(1), "%Y%m%d%H%M%S").replace(tzinfo=timezone.utc)


def window_key(ts: datetime) -> int:
    return int(ts.strftime("%Y%m%d%H%M%S"))


def gkg_url(ts: datetime) -> str:
    return f"{FILES}{ts.strftime('%Y%m%d%H%M%S')}.gkg.csv.zip"


def windows_to_fetch(latest: datetime, done: int | None, n: int = MAX_WINDOWS) -> list[datetime]:
    """Newest first: the latest window and the ones before it not stored yet."""
    out = []
    for k in range(n):
        ts = latest - k * WINDOW
        if done is not None and window_key(ts) <= done:
            break
        out.append(ts)
    return out


def _names(field_value: str, offsets: bool = True) -> list[str]:
    """Names from ``name,offset;name,offset`` (V2) or ``name;name`` (V1) blocks."""
    out: dict[str, None] = {}
    for block in field_value.split(";"):
        name = (block.rsplit(",", 1)[0] if offsets else block).strip()
        if name:
            out.setdefault(name, None)
    return list(out)


def _locations(v2: str, v1: str) -> list[tuple[str, str]]:
    """``(full name, FIPS country code)`` per location, first mention first.

    V2ENHANCEDLOCATIONS: ``Type#FullName#CountryCode#ADM1#ADM2#Lat#Long#FeatureID#Offset``;
    V1LOCATIONS has no ADM2 and no offset. Both are ``;``-separated.
    """
    out = []
    for block in (v2 or v1).split(";"):
        p = block.split("#")
        if len(p) >= 3 and p[1]:
            out.append((p[1], p[2]))
    return out


def page_title(extras: str) -> str:
    """``<PAGE_TITLE>`` from V2EXTRASXML (present since Sept 2019; HTML-escaped)."""
    m = _TITLE_RE.search(extras or "")
    if not m:
        return ""
    return " ".join(html.unescape(_TAG_RE.sub(" ", m.group(1))).split())


def language_of(translation_info: str) -> str:
    """V2.1TRANSLATIONINFO is empty for English originals, else ``srclc:fra;eng:...``."""
    if not translation_info.strip():
        return "English"
    m = re.search(r"srclc:([a-z]{3})", translation_info)
    if not m:
        return "unknown"
    lang = pycountry.languages.get(alpha_3=m.group(1))
    return lang.name if lang else m.group(1)


def parse_date14(value: str) -> datetime:
    return datetime.strptime(value.strip(), "%Y%m%d%H%M%S").replace(tzinfo=timezone.utc)


def _haystack(title: str, url: str, orgs: list[str], places: list[str], themes: list[str]) -> str:
    u = urlsplit(url)
    parts = [title, f"{u.netloc} {u.path} {u.query}", *orgs, *places, *(t.replace("_", " ") for t in themes)]
    return normalise(" | ".join(parts))


def parse_gkg_rows(rows, matchers: list[Matcher], notes: dict[str, int]) -> list[Event]:
    """Events for the GKG 2.1 rows that match a watch term."""
    events: list[Event] = []
    for row in rows:
        notes["rows_scanned"] = notes.get("rows_scanned", 0) + 1
        if len(row) != GKG_COLUMNS:
            notes["rows_malformed"] = notes.get("rows_malformed", 0) + 1
            continue
        url = row[GKG_URL].strip()
        if row[GKG_COLLECTION] != WEB_COLLECTION or not url.startswith(("http://", "https://")):
            continue
        themes = _names(row[GKG_V2THEMES]) if row[GKG_V2THEMES] else _names(row[GKG_V1THEMES], offsets=False)
        orgs = _names(row[GKG_V2ORGS]) if row[GKG_V2ORGS] else _names(row[GKG_V1ORGS], offsets=False)
        locs = _locations(row[GKG_V2LOCATIONS], row[GKG_V1LOCATIONS])
        title = page_title(row[GKG_EXTRAS])
        theme_set = frozenset(themes)
        hay = _haystack(title, url, orgs, [name for name, _ in locs], themes)
        matcher = first_matcher(matchers, hay, theme_set)
        if matcher is None:
            continue
        term = matcher.term
        hit_themes = matched_themes(matcher, theme_set)
        if hit_themes:
            notes["matched_on_themes"] = notes.get("matched_on_themes", 0) + 1

        def build(row=row, url=url, title=title, term=term, locs=locs, hit_themes=hit_themes):
            countries: dict[str, None] = {}
            for _, fips in locs:
                iso = codes.fips_alpha3(fips)
                if iso:
                    countries.setdefault(iso, None)
            tone = row[GKG_TONE].split(",")[0].strip()
            attrs = {
                "query": term,
                "themes": hit_themes or None,
                "domain": row[GKG_DOMAIN] or None,
                "language": language_of(row[GKG_TRANSLATION]),
                "tone": round(float(tone), 2) if tone else None,
                "feed": "gkg",
                "gkg_id": row[GKG_ID],
            }
            return Event(
                source="gdelt",
                raw_id=url_id(url),
                type="news",
                title=title,
                url=url,
                published_at=parse_date14(row[GKG_DATE]),
                countries=list(countries)[:MAX_COUNTRIES],
                attrs={k: v for k, v in attrs.items() if v is not None},
            )

        ev = safe_build(build, notes)
        if ev:
            events.append(ev)
    return events


def read_gkg_zip(data: bytes, matchers: list[Matcher], notes: dict[str, int]) -> list[Event]:
    try:
        zf = zipfile.ZipFile(io.BytesIO(data))
    except zipfile.BadZipFile:
        raise SourceError("bad zip from data.gdeltproject.org") from None
    members = [i for i in zf.infolist() if i.filename.lower().endswith(".csv")]
    if len(members) != 1:
        raise SourceError("unexpected zip contents from data.gdeltproject.org")
    info = members[0]
    if info.file_size > MAX_CSV_BYTES:
        raise SourceError("GKG file over the size cap")
    # Only ever raised (process-wide), so concurrent csv users are unaffected.
    csv.field_size_limit(max(csv.field_size_limit(), MAX_FIELD_BYTES))
    with zf.open(info) as raw:
        text = io.TextIOWrapper(raw, encoding="utf-8", errors="replace", newline="")
        rows = csv.reader(text, delimiter="\t", quoting=csv.QUOTE_NONE)
        return parse_gkg_rows(rows, matchers, notes)


def download(client: httpx.Client, url: str, cap: int, deadline: float, clock) -> bytes:
    """GET with a byte cap and a wall-clock deadline. Raises ``SourceError``
    (``MissingFile`` on a 404)."""
    host = host_of(url)
    try:
        with client.stream("GET", url, timeout=FILE_TIMEOUT) as resp:
            if resp.status_code == 429:
                raise Throttled(f"HTTP 429 from {host}")
            if resp.status_code == 404:
                raise MissingFile(f"HTTP 404 from {host}")
            if resp.status_code >= 400:
                raise SourceError(f"HTTP {resp.status_code} from {host}")
            length = resp.headers.get("content-length")
            if length and length.isdigit() and int(length) > cap:
                raise SourceError(f"file over the size cap from {host}")
            buf = bytearray()
            for chunk in resp.iter_bytes():
                buf += chunk
                if len(buf) > cap:
                    raise SourceError(f"file over the size cap from {host}")
                if clock() > deadline:
                    raise SourceError(f"time budget reached downloading from {host}")
            return bytes(buf)
    except httpx.TimeoutException:
        raise SourceError(f"timeout contacting {host}") from None
    except httpx.TransportError as exc:
        raise SourceError(f"{type(exc).__name__} contacting {host}") from None


# --- connector ----------------------------------------------------------------------


@dataclass
class FileRun:
    """What :meth:`GdeltConnector.fetch_files` did with this run's windows."""

    todo: int = 0
    read: int = 0
    missing: int = 0  # earlier windows that are a 404: skipped by GDELT, passed over
    latest_missing: bool = False  # the file lastupdate.txt lists is a 404: retried next run
    failed: int = 0  # other errors (timeouts, 5xx, bad zip)
    not_run: int = 0  # over the time budget
    last_error: str | None = None

    def outcome(self, latest: int | None) -> tuple[Status | None, str | None]:
        if self.todo == 0:
            return Status.EMPTY, None  # newest window already stored
        problems = []
        if self.latest_missing:
            problems.append(f"newest GKG file {latest} not on the server yet (HTTP 404); retried next run")
        if self.failed:
            problems.append(self.last_error or "window download failed")
        if self.not_run:
            problems.append("time budget reached")
        if self.missing and not self.read:
            problems.append(f"{self.missing} earlier GKG window(s) skipped by GDELT (HTTP 404)")
        if not problems:
            return None, None  # every window read or skipped by GDELT
        msg = "; ".join(problems)
        if self.read:
            return Status.PARTIAL, msg
        if self.failed:
            return Status.ERROR, f"file feed: {msg}"
        return Status.EMPTY, f"file feed: no GKG window to read: {msg}"


class GdeltConnector(Connector):
    name = "gdelt"
    min_interval = timedelta(minutes=15)

    def __init__(self, clock=time.monotonic, budget: float = FILES_BUDGET_S, max_windows: int = MAX_WINDOWS):
        self.clock = clock
        self.budget = budget
        self.max_windows = max_windows

    def fetch_files(self, client: httpx.Client, ctx: RunContext, matchers: list[Matcher], batch: Batch) -> FileRun:
        """Reads the windows not stored yet. Raises if lastupdate.txt can't be used."""
        started = self.clock()
        listing = parse_lastupdate(get(client, LASTUPDATE, attempts=1, timeout=LIST_TIMEOUT).text)
        if "gkg" not in listing:
            raise SourceError("no GKG file in lastupdate.txt")
        size, url = listing["gkg"]
        latest = window_of(url)
        if latest is None:
            raise SourceError("unexpected GKG file name in lastupdate.txt")
        done = batch.notes.get("gkg_window")
        todo = windows_to_fetch(latest, done, self.max_windows)
        batch.notes["gkg_latest"] = window_key(latest)
        if size > MAX_ZIP_BYTES:
            raise SourceError("GKG file over the size cap")
        run = FileRun(todo=len(todo))
        newest_done = done or 0
        for ts in todo:  # newest first, so the budget drops the oldest
            if self.clock() - started > self.budget:
                run.not_run += 1
                continue
            key = window_key(ts)
            try:
                data = download(client, gkg_url(ts), MAX_ZIP_BYTES, started + self.budget, self.clock)
                batch.notes["bytes_downloaded"] = batch.notes.get("bytes_downloaded", 0) + len(data)
                batch.events.extend(read_gkg_zip(data, matchers, batch.notes))
            except MissingFile:
                if ts == latest:
                    run.latest_missing = True  # listed, so it will exist: don't move past it
                else:
                    run.missing += 1  # GDELT skipped this window: nothing to come back for
                    newest_done = max(newest_done, key)
                continue
            except SourceError as exc:
                run.failed += 1
                run.last_error = str(exc)
                continue
            run.read += 1
            newest_done = max(newest_done, key)
        if newest_done:
            batch.notes["gkg_window"] = newest_done
        batch.notes["windows_read"] = run.read
        batch.notes["windows_failed"] = run.failed
        for note, n in (
            ("missing_windows", run.missing),
            ("latest_missing", int(run.latest_missing)),
            ("windows_not_run", run.not_run),
        ):
            if n:
                batch.notes[note] = n
        return run

    def fetch_doc(self, client: httpx.Client, matchers: list[Matcher], batch: Batch) -> None:
        """One DOC API call for all terms. Raises ``Throttled`` on the first throttle reply."""
        q, skipped = doc_api_query(matchers)
        batch.notes["doc_terms_skipped"] = skipped
        if q is None:
            raise SourceError("no watch term can be sent to the DOC API")
        params = {
            "query": q,
            "mode": "artlist",
            "format": "json",
            "maxrecords": MAX_RECORDS,
            "timespan": TIMESPAN,
            "sort": "datedesc",
        }
        resp = get(client, f"{API}?{urlencode(params)}", attempts=1, timeout=API_TIMEOUT)
        events = parse_articles(decode(resp.text), None, batch.notes)
        for ev in events:
            # The API matched the full text; we can only attribute a term from the title/URL.
            term = first_match(matchers, _haystack(ev.title, ev.url or "", [], [], []))
            batch.events.append(dataclasses.replace(ev, attrs={**ev.attrs, "query": term}) if term else ev)
        batch.notes["doc_articles"] = len(events)

    def fetch(self, client: httpx.Client, ctx: RunContext) -> Batch:
        terms: list[WatchTerm] = ctx.terms("gdelt_query")
        if not terms:
            return Batch(status=Status.SKIPPED, error="no gdelt_query watch terms")
        matchers = [parse_query(t.term) for t in terms]
        batch = Batch()
        batch.notes["terms"] = len(matchers)
        batch.notes["terms_unsupported"] = sum(1 for m in matchers if not m.supported)
        done = ctx.state.get(self.name, {}).get("gkg_window")
        if isinstance(done, int) and not isinstance(done, bool):
            batch.notes["gkg_window"] = done  # carried forward even if this run fails over

        try:
            files = self.fetch_files(client, ctx, matchers, batch)
        except SourceError as exc:
            file_error = str(exc)
        else:
            # The listing was read: whatever the window files did, no DOC call.
            return self._finish(batch, *files.outcome(batch.notes.get("gkg_latest")))

        # Fallback: lastupdate.txt could not be read. One DOC call, no back-off.
        batch.notes["doc_fallback"] = 1
        try:
            self.fetch_doc(client, matchers, batch)
        except Throttled as exc:
            return self._finish(batch, Status.THROTTLED, f"file feed: {file_error}; DOC API: {exc}")
        except SourceError as exc:
            return self._finish(batch, Status.ERROR, f"file feed: {file_error}; DOC API: {exc}")
        return self._finish(batch, Status.PARTIAL, f"file feed: {file_error}; used the DOC API")

    @staticmethod
    def _finish(batch: Batch, status: Status | None, error: str | None) -> Batch:
        unique: dict[str, Event] = {}
        for ev in batch.events:
            unique.setdefault(ev.raw_id, ev)  # newest window first: keep its copy
        events = list(unique.values())
        if len(events) > MAX_EVENTS:
            batch.notes["dropped_over_cap"] = len(events) - MAX_EVENTS
            events = events[:MAX_EVENTS]
        batch.events = events
        batch.notes["matched"] = len(events)
        batch.status, batch.error = status, error
        return batch
