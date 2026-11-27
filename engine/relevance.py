"""Is a country what a news item is about? (risk-engine.md §5.10, engine 1.3.0–1.3.4). Pure functions.

The engine sees a news item's title, its tag (the model's reading of that same title) and,
for GDELT file articles, ``countries``: every country GDELT located anywhere in the article
text, first mention first. A country found only deep inside a long article is a passing
mention, so a news *country* match counts only when the country is **central**:

* it is named in the headline (a name, alias, adjective such as "Chinese", or the capital used
  for the government, such as "Beijing"), or it is in the tag's countries; or
* (1.3.4) neither names any country and GDELT located only a few countries
  (``first_place_max_countries``): then the first place GDELT located (``countries[0]``).

GDELT does not give mention counts in what the feed stores, so "most-mentioned" cannot be
told apart from "first-mentioned"; the first is used. Since 1.3.4 every title rule reads the
**headline** (:func:`headline`: the title without a trailing outlet or section name and without a
leading wire tag), and a country match also needs a cue word of the item's category
(:func:`category_cue`) and a current event (:func:`not_current`). Sanctions and cyber have their
own checks instead of a cue (:func:`sanction_targets`, :func:`national_scale`, :func:`sector_hit`).

Everything here is vocabulary plus token windows, deterministic and cached per title.
"""

from __future__ import annotations

import json
import re
import unicodedata
from dataclasses import dataclass
from functools import lru_cache
from typing import Iterable, Mapping, Sequence
from urllib.parse import urlsplit

from engine.reference import DATA

# Exact ISO names that usually mean something else in a headline (as jobs/analyse/places.py).
AMBIGUOUS = frozenset({"georgia", "congo", "jordan", "chad"})
# Clause breaks: a window never crosses them.
BREAKS = frozenset({";", ":", "|", "?", "!", "—", "–"})
JOINS = frozenset({"and", "or", "&", ","})

# --- sanctions (§5.10) ------------------------------------------------------------------
# A sanctions word or phrase (normalised tokens).
SANCTION_TERMS: tuple[tuple[str, ...], ...] = tuple(tuple(t.split()) for t in (
    "sanction", "sanctions", "sanctioned", "sanctioning", "embargo", "embargoes", "embargoed",
    "blacklist", "blacklists", "blacklisted", "blacklisting", "ban", "bans", "banned", "banning",
    "curb", "curbs", "designation", "designations", "designates", "designated",
    "entity list", "export control", "export controls", "export curb", "export curbs", "export ban",
    "export bans", "export restriction", "export restrictions", "export rule", "export rules",
    "chip curbs", "asset freeze", "asset freezes", "assets frozen", "freezes assets", "trade restrictions",
))  # fmt: skip
# Sanction words that are also a verb taking the target as object ("US sanctions Chinese firms").
SANCTION_VERBS = frozenset({
    "sanctions", "sanctioned", "sanctioning", "blacklists", "blacklisted", "blacklisting", "bans", "banned",
    "banning", "embargoes", "designates", "designated", "curbs",
})  # fmt: skip
# "<term> on|against <country>".
TARGET_PREPOSITIONS = frozenset({"on", "against", "targeting", "targets", "at", "upon", "onto", "hitting"})
# "<country> hit with / faces / protests ... sanctions": the country is on the receiving end.
RECEIVING = frozenset({
    "hit", "hits", "slapped", "faces", "face", "facing", "faced", "protests", "protest", "condemns", "condemn",
    "slams", "retaliates", "retaliate", "vows", "responds", "rejects", "denounces", "decries", "criticises",
    "criticizes", "blasts", "under",
})  # fmt: skip
# "<country> sanctioned / blacklisted": passive, the country (or its firms) is the target.
PASSIVE = frozenset({
    "sanctioned", "blacklisted", "embargoed", "designated", "banned", "targeted", "penalised", "penalized",
})  # fmt: skip
# "<country> imposes / widens / adds ...": the country imposes the measure. (A sanctions verb
# right after a country makes it the actor only when another country follows as the object:
# "US sanctions Chinese firms", but "Iran sanctions squeeze ..." is about sanctions on Iran.)
# The plain forms are for two or more countries ("The Netherlands and China plan ..."); every
# country joined by "and", "or" or a comma shares the verb that follows the last one (1.3.1).
ACTOR_VERBS = frozenset({
    "imposes", "impose", "imposed", "widens", "widen", "expands", "expand", "adds", "add", "added",
    "announces", "announce", "unveils", "unveil", "slaps", "slap", "levies", "levy", "tightens", "tighten",
    "extends", "extend", "approves", "approve", "agrees", "agree", "adopts", "adopt", "introduces",
    "introduce", "considers", "consider", "mulls", "mull", "weighs", "weigh", "plans", "plan", "readies",
    "ready", "threatens", "threaten", "warns", "warn", "places", "place", "puts", "put", "issues", "issue",
    "prepares", "prepare", "eyes", "eye", "drafts", "draft", "proposes", "propose", "says", "say", "urges",
    "urge", "calls", "call", "lifts", "lift", "eases", "ease", "coordinate", "coordinates", "align", "aligns",
    "join", "joins", "placed", "moves", "move", "moved",
})  # fmt: skip
# "... under / by <country> sanctions": the imposer.
ACTOR_BEFORE = frozenset({"under", "by", "from"})
# "<country> ... to <term>" puts the country on a list only with a placing verb before the
# country ("adds Chinese firms to entity list"), a placed-verb right before "to" ("China added
# to US entity list": then the country is no actor), or the term straight after "to" ("Chinese
# firms to the entity list"). Never an infinitive: in "The Netherlands and China plan to
# coordinate export controls" both impose the measure, and "China to blacklist US firms" is China
# acting. A country between "to" and the term owns the list ("to US entity list").
PLACING_VERBS = frozenset({
    "add", "adds", "added", "adding", "put", "puts", "putting", "place", "places", "placed", "placing",
    "move", "moves", "moved", "moving",
})  # fmt: skip
PLACED = frozenset({"added", "placed", "put", "moved"})
# Words that may stand between "to" and the term: "to the entity list", "to its blacklist".
DETERMINERS = frozenset({"the", "a", "an", "its", "their", "new"})
# Terms that right after "to" are a verb ("to sanction", "to ban"), not the list.
INFINITIVE_TERMS = frozenset({"sanction", "blacklist", "ban", "embargo", "curb", "designate"})
# A country (or list of countries) right after one of these is no subject, so a verb after it is
# not its own: in "Sanctions on China and Russia widen" the sanctions widen.
NOT_SUBJECT = TARGET_PREPOSITIONS | {"of", "to", "with", "for", "over", "into"}
# F (1.3.4): after "to" or a modal a base-form term is a verb whose object is the target: "China
# to blacklist US defence firms" (the US), "EU will sanction Russian banks".
TO_OR_MODAL = frozenset({"to", "will", "would", "could", "may", "might", "must", "should", "can", "cannot"})

# --- cyber (§5.10) ----------------------------------------------------------------------
# A country-level cyber match needs a national-scale incident ...
NATIONAL_SCALE: tuple[tuple[str, ...], ...] = tuple(tuple(t.split()) for t in (
    "nationwide", "national", "nation", "countrywide", "government", "governments", "ministry", "ministries",
    "federal", "parliament", "election", "elections", "military", "army", "defence", "defense", "infrastructure",
    "critical infrastructure", "grid", "power grid", "electricity", "energy", "utility", "utilities", "water supply",
    "rail", "railway", "railways", "rail network", "airport", "airports", "port", "ports", "hospital", "hospitals",
    "health service", "telecom", "telecoms", "telecommunications", "mobile network", "internet", "outage",
    "outages", "blackout", "state sponsored", "state backed", "nation state", "customs", "central bank",
    "banking system", "payment system", "payments system", "emergency services",
))  # fmt: skip
# E (1.3.4): "police" left NATIONAL_SCALE, and a crime bust is never national scale ("Cyber Crime
# Police Arrest Two for Impersonation", "Noida cybercrime raid: Police rescue 164 foreigners").
CRIME_BUST = frozenset({
    "arrest", "arrests", "arrested", "arresting", "bust", "busts", "busted", "raid", "raids", "raided", "rescue",
    "rescues", "rescued", "nabbed", "apprehended",
})  # fmt: skip
# A sector cue right after one of these names the crime, not the product: "fraud module" is no power module.
CRIME_WORDS = frozenset({
    "fraud", "frauds", "scam", "scams", "crime", "cybercrime", "gang", "racket", "syndicate", "extortion",
    "phishing", "sextortion", "trafficking", "laundering", "ponzi",
})  # fmt: skip
# ... or a word naming the supplier's own sector (its products, HS keywords, notes), minus these.
GENERIC_WORDS = frozenset("""
about after also among and are based board but can component components control controller customer
equipment for from general good goods have high industrial into item items line lines low main make
made maker makers material materials more most other our over part parts plant plants position power
pressure product products range rig rigs service services sole source spare standard supplier
suppliers supply system systems test that the their them they this unit units used user using very
when where which with works
""".split())  # fmt: skip


def normalise(word: str) -> str:
    s = unicodedata.normalize("NFKD", word)
    s = "".join(ch for ch in s if not unicodedata.combining(ch)).casefold()
    s = s.replace("’", "'")
    for suffix in ("'s", "'"):
        if s.endswith(suffix):
            s = s[: -len(suffix)]
    return s.replace(".", "")


_TOKEN_RE = re.compile(r"[^\W_]+(?:[.'’][^\W_]+)*\.?|[;:|?!—–,&]")


def _token_matches(text: str) -> list[tuple[str, str, int]]:
    """``(original, normalised, end offset)`` of each word and clause mark; hyphens split words."""
    out = []
    for m in _TOKEN_RE.finditer(text.replace("-", " ")):
        raw = m.group(0)
        n = normalise(raw)
        if n:
            out.append((raw, n, m.end()))
    return out


def tokens(text: str) -> list[tuple[str, str]]:
    """``(original, normalised)`` words and clause marks; hyphens split words."""
    return [(raw, n) for raw, n, _ in _token_matches(text)]


def stem(word: str) -> str:
    """Crude singular: ``sensors`` → ``sensor``, ``batteries`` → ``battery``."""
    if len(word) > 4 and word.endswith("ies"):
        return word[:-3] + "y"
    if len(word) > 3 and word.endswith("s") and not word.endswith("ss"):
        return word[:-1]
    return word


# --- the headline (B, engine 1.3.4) ----------------------------------------------------------------
# GDELT and RSS titles are the page's <title>: often the headline, then the outlet or section
# ("… – Modern Tokyo Times", "… - National", "… | Countercurrents", "… | News, Sports, Jobs"), and
# Reuters copies start with a wire tag ("Analysis-", "EXCLUSIVE-", "UPDATE 2-"). Every title rule
# reads the headline alone, so "Tokyo" in an outlet's name does not make Japan central.

# "<tag>-Headline": these tags in any case; "Live-fire drills" or "Focus-group" are headlines.
_WIRE_HYPHEN = re.compile(
    r"^(?:(?i:analysis|exclusive|factbox|explainer|breakingviews|refile|corrected|(?:update|wrapup)\s+\d+)"
    r"|EXCLUSIVE|INSIGHT|FOCUS|COLUMN|INTERVIEW|TIMELINE|GRAPHIC|UPDATE|WRAPUP|ANALYSIS|BREAKING)\s*-\s*(?=\w|['‘\"“])"
)
# "<Tag>: Headline".
_WIRE_COLON = re.compile(
    r"^(?i:analysis|exclusive|factbox|explainer|insight|breaking(?:\s+news)?|just\s+in|opinion|editorial|"
    r"watch|video|live|explained|in\s+pictures|photos|(?:update|wrapup)(?:\s+\d+)?)\s*:\s+"
)
# A trailing segment starts after the last of these: " - ", " – ", " — " (spaced, so "China-linked"
# and "2025–26" are no breaks), "|" or " :: ".
_TAIL = re.compile(r"\s+[-–—]\s+|\s*\|\s*|\s+::\s+")
# An outlet's or section's name ends with one of these ("Modern Tokyo Times", "The Arkansas
# Democrat-Gazette") ...
OUTLET_LAST = frozenset("""
times post news herald tribune gazette journal daily telegraph chronicle observer guardian inquirer
examiner courier dispatch monitor insider magazine media network tv radio online press wire agency
reporter digest bulletin weekly express mirror review today standard star sun mail report bureau
newspaper broadcasting channel information info hindu mint reuters bloomberg afp xinhua ani pti ians
yonhap kyodo bernama anadolu tass interfax cnn bbc cnbc nikkei politico axios forbes ndtv firstpost
""".split())  # fmt: skip
# ... or has one of these anywhere ("News, Sports, Jobs", "Times of India", "Gulf News Online").
OUTLET_ANY = frozenset({"news", "times", "tv", "radio", "media", "press", "online", "newspaper"})
# A whole segment that is a section of the site.
SECTIONS = frozenset("""
national world business local politics opinion opinions sport sports economy markets finance tech
technology science health entertainment lifestyle academia asia europe africa americas international
region regional home latest top stories nation analysis features editorial editorials columns
""".split()) | {"world news", "latest news", "breaking news", "top stories", "middle east", "local news",
                "national news", "business news", "india news", "asia pacific"}  # fmt: skip
CONNECTORS = frozenset({"of", "the", "and", "for", "de", "la", "le", "el", "al", "du", "des", "del", "on", "in", "at"})
_DOMAIN = re.compile(r"\w\.(?:com|org|net|co|in|uk|au|ca|ng|pk|info|io|news)\b", re.I)
_WORDS = re.compile(r"[^\W_]+")
# Host labels that say nothing about the outlet.
_HOST_NOISE = frozenset({"www", "m", "amp", "mobile", "news", "com", "org", "net", "co", "gov", "ac", "edu", "info"})


def _host_labels(url: str | None) -> tuple[str, ...]:
    if not isinstance(url, str) or not url.strip():
        return ()
    try:
        host = (urlsplit(url.strip()).hostname or "").lower()
    except ValueError:
        return ()
    labels = host.split(".")[:-1]  # the last label is the top-level domain
    return tuple(x for x in labels if len(x) >= 3 and x not in _HOST_NOISE)


def _is_outlet(seg: str, pipe: bool, host: tuple[str, ...]) -> bool:
    """Is ``seg`` (the text after the last separator) the outlet or a section, not headline?"""
    words = _WORDS.findall(seg)
    if not words or len(words) > 6:
        return False
    folded = [w.casefold() for w in words]
    squash = "".join(folded)
    bare = "".join(folded[1:]) if folded[0] == "the" and len(folded) > 1 else squash
    acronym = "".join(w[0] for w, f in zip(words, folded) if f not in CONNECTORS)
    for label in host:  # moderntokyotimes.com ↔ "Modern Tokyo Times", wrp.org.uk ↔ "Workers Revolutionary Party"
        if label in (squash, bare) or (len(acronym) >= 2 and label == acronym.casefold()):
            return True
        if len(bare) >= 5 and len(label) >= 5 and (bare in label or label in bare):
            return True
    if _DOMAIN.search(seg):
        return True
    if not all(w[0].isupper() or w[0].isdigit() or f in CONNECTORS for w, f in zip(words, folded)):
        return False  # "… - last chance to see", "… - minister says": headline words, kept
    if folded[-1] in OUTLET_LAST or OUTLET_ANY.intersection(folded) or " ".join(folded) in SECTIONS:
        return True
    return pipe and len(words) <= 4  # "| Countercurrents": a pipe sets off the site's name


def _headline(title: str, host: tuple[str, ...]) -> str:
    t = " ".join(title.split())
    for _ in range(3):
        m = _WIRE_HYPHEN.match(t) or _WIRE_COLON.match(t)
        if not m:
            break
        t = t[m.end():]
    for _ in range(3):
        seps = list(_TAIL.finditer(t))
        if not seps:
            break
        last = seps[-1]
        head, seg = t[: last.start()], t[last.end():]
        n_head = len(_WORDS.findall(head))
        if n_head < 3 or len(_WORDS.findall(seg)) >= n_head or not _is_outlet(seg, "|" in last.group(0), host):
            break
        t = head.rstrip()
    return t


@lru_cache(maxsize=32768)
def _headline_cached(title: str, host: tuple[str, ...]) -> str:
    return _headline(title, host)


def headline(title: str | None, url: str | None = None) -> str:
    """The headline of a feed title (B, 1.3.4): a trailing outlet or section segment ("… – Modern
    Tokyo Times", "… - National", "… | News, Sports, Jobs") and a leading wire tag ("Analysis-",
    "EXCLUSIVE-", "UPDATE 2-") dropped. ``url`` helps: a segment that is the URL's host name
    (moderntokyotimes.com) or its initials (wrp.org.uk) is the outlet. A segment is dropped only
    when it is shorter than what stays and that keeps at least three words, so a headline is
    never cut to a stub."""
    if not isinstance(title, str):
        return ""
    return _headline_cached(title, _host_labels(url))


# --- the country vocabulary -------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class _Term:
    code: str | None  # None: an excluded phrase ("South China Sea")
    kind: str  # name | adjective | capital | exclude
    exact_case: bool  # "US" only in capitals


@lru_cache(maxsize=1)
def _vocabulary() -> tuple[dict[tuple[str, ...], tuple[_Term, ...]], int]:
    raw = json.loads((DATA / "country_terms.json").read_text(encoding="utf-8"))
    vocab: dict[tuple[str, ...], list[_Term]] = {}

    def add(phrase: str, term: _Term) -> None:
        key = tuple(n for _, n in tokens(phrase))
        if not key or (len(key) == 1 and key[0] in AMBIGUOUS):
            return
        vocab.setdefault(key, [])
        if term not in vocab[key]:
            vocab[key].append(term)

    try:
        import pycountry

        for c in pycountry.countries:
            for attr in ("name", "common_name", "official_name"):
                v = getattr(c, attr, None)
                if v and "," not in v and "(" not in v:
                    add(v, _Term(c.alpha_3, "name", False))
    except Exception:  # pragma: no cover - pycountry is a dependency of jobs.codes
        pass
    for code, spec in raw["countries"].items():
        for kind, key in (("name", "aliases"), ("adjective", "adjectives"), ("capital", "capitals")):
            for v in spec.get(key, ()):
                letters = v.replace(".", "")
                add(v, _Term(code, kind, letters.isupper() and len(letters) <= 5))
    for v in raw["exclude"]:
        add(v, _Term(None, "exclude", False))
    longest = max((len(k) for k in vocab), default=1)
    return {k: tuple(v) for k, v in vocab.items()}, longest


@dataclass(frozen=True, slots=True)
class Mention:
    code: str
    start: int  # token index
    end: int  # one past the last token
    kind: str
    possessive: bool = False  # "India's", "Philippines'" (1.3.4)


@dataclass(frozen=True, slots=True)
class TitleFacts:
    words: tuple[str, ...]  # normalised tokens
    mentions: tuple[Mention, ...]

    @property
    def countries(self) -> frozenset[str]:
        return frozenset(m.code for m in self.mentions)


@lru_cache(maxsize=32768)
def title_facts(title: str) -> TitleFacts:
    text = title or ""
    toks = _token_matches(text)
    words = tuple(n for _, n, _ in toks)
    vocab, longest = _vocabulary()
    mentions: list[Mention] = []
    i = 0
    while i < len(toks):
        hit = None
        for n in range(min(longest, len(toks) - i), 0, -1):
            terms = vocab.get(words[i : i + n])
            if not terms:
                continue
            raw = " ".join(r for r, _, _ in toks[i : i + n]).replace(".", "")
            ok = [t for t in terms if not t.exact_case or raw.isupper()]
            if ok:
                hit = (n, ok[0])
                break
        if hit is None:
            i += 1
            continue
        n, term = hit
        if term.code is not None:
            last_raw, _, end = toks[i + n - 1]
            possessive = last_raw.casefold().endswith(("'s", "’s")) or text[end : end + 1] in ("'", "’")
            mentions.append(Mention(term.code, i, i + n, term.kind, possessive))
        i += n
    return TitleFacts(words, tuple(mentions))


# A: the first place GDELT located stands for the article only when the article is about few places.
FIRST_PLACE_MAX_COUNTRIES = 3


def central_countries(title: str, tag_countries: Iterable[str], countries: Sequence[str],
                      first_place_max: int = FIRST_PLACE_MAX_COUNTRIES) -> dict[str, str]:  # fmt: skip
    """Country → why it is central: ``title`` (named in the headline or its tag) or ``first_place``.

    ``title`` is the headline (:func:`headline`). Since 1.3.4 (A) the first place GDELT located
    (``countries[0]``) counts only when neither the headline nor the tag names any country and
    GDELT located at most ``first_place_max`` countries: in "Two Italians Killed in Kuwait; Iran
    Claims …" Germany, located first among ten countries, is a passing mention."""
    out: dict[str, str] = {}
    for c in [*sorted(title_facts(title).countries), *tag_countries]:
        out.setdefault(c, "title")
    if not out and countries and len(set(countries)) <= first_place_max:
        out[countries[0]] = "first_place"
    return out


# --- sanctions: is the country the target? ----------------------------------------------------


@lru_cache(maxsize=32)
def _by_first(phrases: tuple[tuple[str, ...], ...]) -> dict[str, tuple[tuple[str, ...], ...]]:
    """Phrases by their first word, longest first (so a scan looks up one word per token)."""
    out: dict[str, list[tuple[str, ...]]] = {}
    for p in phrases:
        out.setdefault(p[0], []).append(p)
    return {k: tuple(sorted(v, key=len, reverse=True)) for k, v in out.items()}


def _term_spans(words: Sequence[str]) -> list[tuple[int, int]]:
    spans, i, index = [], 0, _by_first(SANCTION_TERMS)
    while i < len(words):
        best = next((len(t) for t in index.get(words[i], ()) if tuple(words[i : i + len(t)]) == t), 0)
        if best:
            spans.append((i, i + best))
            i += best
        else:
            i += 1
    return spans


@lru_cache(maxsize=8192)
def sanction_targets(title: str, window: int = 5) -> frozenset[str]:
    """Countries a sanctions headline is directed **at** (not merely mentioned in it).

    * ``<term> [on|against …] <country>``: "sanctions on Chinese chipmakers", "export curbs
      on China"; a verb-form term may take the country directly ("US sanctions Chinese firms").
    * ``<country> … to <term>`` with a placing verb (add, put, place, move) before the country
      or between it and "to", or with the term straight after "to" (articles and the
      imposer's name aside): "adds Chinese circuit board makers to export blacklist", "China
      added to US entity list"; never an infinitive ("China plans to tighten export controls").
    * ``<country> hit with / faces / protests … <term>``: "China hit with EU sanctions",
      "Beijing protests new US sanctions"; a country between such a word and the term is the
      imposer, not the target.
    * ``<country> [x] <term>``: "Iran oil sanctions", "North Korea sanctions evasion".
    * ``<country> sanctioned / blacklisted``.
    * (1.3.4) ``to|will|… <base-form term> <country>``: "China to blacklist US defence firms".

    A country followed by an imposing verb ("US imposes", "Washington adds") or preceded by
    "under" / "by" is the actor, and so is every country joined to it by "and", "or" or a
    comma ("The Netherlands and China plan …"), and (1.3.4) a country in the possessive up to
    three words before an export measure ("India's Export Restriction on Drugs"). Windows are
    ``window`` tokens and never cross ``; : | ?``. ``title`` is the headline (:func:`headline`).
    """
    facts = title_facts(title)
    w, ms = facts.words, facts.mentions
    if not ms:
        return frozenset()
    terms = _term_spans(w)
    if not terms:
        return frozenset()
    in_term = {i for s, e in terms for i in range(s, e)}

    def clean(a: int, b: int) -> bool:
        return not any(x in BREAKS for x in w[a:b])

    # Lists of countries ("The Netherlands and China", "US, UK and Canada"): first[k] is the
    # first country of k's list. A verb after the last one is the whole list's.
    first = list(range(len(ms)))
    for k in range(1, len(ms)):
        between = w[ms[k - 1].end : ms[k].start]
        if between and all(x in JOINS for x in between):
            first[k] = first[k - 1]
    actors: set[int] = set()
    for k, m in enumerate(ms):
        if m.start > 0 and w[m.start - 1] in ACTOR_BEFORE:
            actors.add(k)
        # F (1.3.4): "India's export restriction", "China's new export controls": the possessor
        # imposes the export measure (a target after it still counts: "… on Japan").
        if m.possessive:
            nxt = next(((s, e) for s, e in terms if s >= m.end), None)
            if nxt is not None and nxt[0] - m.end <= 3 and w[nxt[0]] == "export" and clean(m.end, nxt[0]) and not any(
                    x in TARGET_PREPOSITIONS for x in w[m.end : nxt[0]]):
                actors.add(k)
        head = ms[first[k]]
        if head.start > 0 and w[head.start - 1] in NOT_SUBJECT:
            continue
        after = w[m.end : m.end + 2]
        verb = next((i for i, x in enumerate(after) if x in ACTOR_VERBS), None)
        # "China added to …", "Chinese firms added to …": put on the list, not putting
        nxt_word = w[m.end + verb + 1] if verb is not None and m.end + verb + 1 < len(w) else ""
        placed = verb is not None and after[verb] in PLACED and nxt_word in ("to", "onto")
        acts = verb is not None and not placed and not any(x in PASSIVE or x in RECEIVING for x in after[:1])
        if m.end < len(w) and w[m.end] in SANCTION_VERBS and any(m.end < o.start <= m.end + 3 for o in ms):
            acts = True
        if acts:
            actors.update(j for j in range(first[k], k + 1))
    targets: set[int] = set()
    for k, m in enumerate(ms):
        # <country> hit with / faces / protests ... <term>
        for r in range(m.end, min(len(w), m.end + 4)):
            if w[r] in BREAKS:
                break
            if w[r] in RECEIVING or w[r] in PASSIVE:
                if w[r] in PASSIVE and r - m.end <= 3:
                    targets.add(k)
                    break
                t_end = next((s for s, _ in terms if r < s <= r + window), None)
                if t_end is not None and clean(r, t_end):
                    targets.add(k)
                    actors.update(j for j, o in enumerate(ms) if r < o.start < t_end and j != k)
                    break
        # <country> [x [y]] <term> (not "China to blacklist …", an infinitive: China acts)
        nxt = next(((s, e) for s, e in terms if s >= m.end), None)
        if nxt is not None and nxt[0] - m.end <= 2 and clean(m.end, nxt[0]):
            filler = w[m.end : nxt[0]]
            if not any((x in ACTOR_VERBS and x not in in_term) or x in ("to", "onto") for x in filler):
                targets.add(k)
        # <country> ... to <term>, with a placing verb ("adds Chinese firms to entity list")
        for r in range(m.end, min(len(w), m.end + window + 1)):
            if w[r] in BREAKS:
                break
            if w[r] not in ("to", "onto"):
                continue
            s = next((s for s, _ in terms if r < s <= r + 3), None)
            if s is None:
                continue
            lead = range(max(0, m.start - (window - 1)), m.start)
            placed = any(w[j] in PLACING_VERBS and clean(j, m.start) for j in lead) or (
                r > m.end and w[r - 1] in PLACED)
            owners = [j for j, o in enumerate(ms) if r < o.start < s]
            in_owner = {i for j in owners for i in range(ms[j].start, ms[j].end)}
            straight = all(w[i] in DETERMINERS or i in in_owner for i in range(r + 1, s)) and not (
                s == r + 1 and w[s] in INFINITIVE_TERMS)
            if placed or straight:
                targets.add(k)
                actors.update(owners)
            break
    # <term> [on|against ...] <country> [and <country>]
    for s, e in terms:
        verb = w[e - 1] in SANCTION_VERBS
        bare = e - s == 1 and w[s] in INFINITIVE_TERMS and s > 0 and w[s - 1] in TO_OR_MODAL
        prep = False
        for r in range(e, min(len(w), e + window + 1)):
            if w[r] in BREAKS:
                break
            if w[r] in TARGET_PREPOSITIONS:
                prep = True
            hit = next((k for k, m in enumerate(ms) if m.start == r), None)
            if hit is None:
                continue
            if prep or (verb and r - e <= 2) or (bare and r == e):
                targets.add(hit)
                # "on China and Russia"
                j = ms[hit].end
                while j < len(w) and w[j] in JOINS:
                    nxt_m = next((k for k, m in enumerate(ms) if m.start == j + 1), None)
                    if nxt_m is None:
                        break
                    targets.add(nxt_m)
                    j = ms[nxt_m].end
            break
    return frozenset(ms[k].code for k in targets - actors)


# --- cyber: national scale, or the supplier's own sector -----------------------------------


def _has_phrase(words: Sequence[str], phrases: tuple[tuple[str, ...], ...]) -> bool:
    index = _by_first(phrases)
    return any(tuple(words[i : i + len(p)]) == p for i, w in enumerate(words) for p in index.get(w, ()))


@lru_cache(maxsize=8192)
def national_scale(title: str) -> bool:
    """A cyber headline about government, critical infrastructure or a nationwide outage, and
    not a crime bust (1.3.4: arrested, busted, raid, rescue …)."""
    words = title_facts(title).words
    return _has_phrase(words, NATIONAL_SCALE) and CRIME_BUST.isdisjoint(words)


def sector_cues(products: Iterable[str], keywords: Iterable[str], notes: str | None) -> frozenset[str]:
    """Stemmed content words naming what an entity makes: its products / commodities, the HS
    keywords of its codes and its notes, minus generic words (``GENERIC_WORDS``)."""
    out: set[str] = set()
    for text in [*products, *keywords, notes or ""]:
        for _, n in tokens(text):
            if len(n) >= 4 and n.isalpha() and n not in GENERIC_WORDS:
                out.add(stem(n))
    return frozenset(out)


def sector_hit(title: str, cues: frozenset[str]) -> bool:
    """A word of the entity's sector in the headline, except right after a crime word (1.3.4:
    "Delhi Police bust … delivery fraud module" names no power module)."""
    if not cues:
        return False
    w = title_facts(title).words
    return any(stem(x) in cues and not (i > 0 and w[i - 1] in CRIME_WORDS) for i, x in enumerate(w))


def tag_countries(entities: Mapping | None) -> list[str]:
    if not isinstance(entities, Mapping):
        return []
    v = entities.get("countries")
    return [c for c in v if isinstance(c, str)] if isinstance(v, list) else []


def _contains(words: Sequence[str], part: Sequence[str]) -> bool:
    n = len(part)
    return n > 0 and any(tuple(words[i : i + n]) == tuple(part) for i in range(len(words) - n + 1))


def headline_tag_countries(title: str, head: str, entities: Mapping | None) -> list[str]:
    """The tag's countries without those it found only in the outlet's name (B, 1.3.4): the tagger
    reads the whole title, so "… - The Japan Times" may give it the place "Japan". A country stays
    when one of its places is in the headline, or when its place text is not found in the title."""
    countries = tag_countries(entities)
    if not countries or head == title:
        return countries
    places = entities.get("places") if isinstance(entities, Mapping) else None
    head_words, title_words = title_facts(head).words, title_facts(title).words
    kept: set[str] = set()
    outlet_only: set[str] = set()
    for p in places if isinstance(places, list) else ():
        if not isinstance(p, Mapping) or not isinstance(p.get("country"), str) or not isinstance(p.get("text"), str):
            continue
        part = [n for _, n in tokens(p["text"])]
        if not _contains(head_words, part) and _contains(title_words, part):
            outlet_only.add(p["country"])
        else:
            kept.add(p["country"])
    return [c for c in countries if c in kept or c not in outlet_only]


# --- C: a cue word of the item's category (engine 1.3.4) ---------------------------------------
# The tag of a bare title is often wrong (an election poll, a court judgment or a politician's
# remarks tagged conflict; "Japan services producer prices jump" tagged trade policy), so a news
# item matched on a country alone counts only when its headline has a word of its tag's category.
# The lists follow the tagger's own label descriptions (jobs/analyse/tagging.py EVENT_TYPES);
# sanctions and cyber have their own checks (sanction_targets, national_scale / sector_hit).


def _phrases(text: str) -> tuple[tuple[str, ...], ...]:
    return tuple(tuple(p.split()) for p in text.strip().split(","))


CATEGORY_CUES: Mapping[str, tuple[tuple[str, ...], ...]] = {
    # "war, military strikes, armed attacks, terrorism or fighting"
    "conflict": _phrases("""
        war, wars, warfare, wartime, warship, warships, warplane, warplanes, military, militaries, militant,
        militants, militia, militias, army, armies, armed, troops, troop, soldier, soldiers, navy, naval, marines,
        airstrike, airstrikes, air strike, air strikes, air raid, air raids, attack, attacks, attacked, attacking,
        attacker, attackers, assault, assaults, terror, terrorism, terrorist, terrorists, fighting, fighters,
        killed, kill, kills, killing, killings, missile, missiles, drone, drones, rocket, rockets, shelling,
        shelled, artillery, bomb, bombs, bombing, bombings, bombed, ambush, ambushes, ambushed, gunfire, gunmen,
        gunman, insurgent, insurgents, insurgency, rebel, rebels, clash, clashes, clashed, ceasefire, truce,
        invasion, invade, invades, invaded, hostilities, hostage, hostages, coup, junta, combat, frontline,
        front line, conflict, conflicts, jets, fighter jets, live fire, drills, coast guard, security forces,
        spy, spies, spying, espionage, border dispute, territorial dispute, maritime dispute, disputed islands,
        disputed waters, contested waters"""),
    # "demonstrations, riots or civil unrest"
    "protest": _phrases("""
        protest, protests, protester, protesters, protestor, protestors, protesting, protested, demonstration,
        demonstrations, demonstrators, rally, rallies, riot, riots, rioting, rioters, unrest, uprising, curfew,
        crackdown, tear gas, sit in"""),
    # "tariffs, trade deals, import or export bans and quotas". 1.3.5: bans, restrictions and curbs
    # ("… US Bill Restricting China's Access to Chipmaking Tools"). "Deal(s)" and "duties" alone stay out.
    "trade_policy": _phrases("""
        tariff, tariffs, export, exports, exported, exporting, exporter, exporters, import, imports, imported,
        importing, importer, importers, quota, quotas, trade, controls, dumping, customs, levy, levies, subsidy,
        subsidies, subsidised, subsidized, protectionism, protectionist, wto, import duty, import duties,
        export duty, export duties, customs duty, customs duties, embargo, embargoes, ban, bans, banned, banning,
        restrict, restricts, restricted, restricting, restriction, restrictions, curb, curbs, curbed, curbing"""),
    # "port closures, congestion, canal or strait blockages, attacks on vessels, freight delays"
    # (1.3.5: blockades and blockages)
    "port_shipping_disruption": _phrases("""
        port, ports, shipping, ship, ships, shipment, shipments, shipper, shippers, vessel, vessels, cargo,
        freight, container, containers, canal, canals, strait, straits, maritime, tanker, tankers, terminal,
        terminals, harbour, harbor, harbours, harbors, dock, docks, dockworkers, dockers, seafarers, sea lane,
        sea lanes, shipping lane, shipping lanes, congestion, ferry, ferries, boxship, blockade, blockades,
        blockaded, blockage, blockages"""),
    # "earthquakes, storms, floods, wildfires, volcanoes, droughts"
    "natural_hazard": _phrases("""
        earthquake, earthquakes, quake, quakes, tremor, tremors, aftershock, aftershocks, storm, storms,
        cyclone, cyclones, typhoon, typhoons, hurricane, hurricanes, tornado, tornadoes, flood, floods, flooding,
        flooded, floodwaters, flash flood, wildfire, wildfires, bushfire, bushfires, forest fire, forest fires,
        volcano, volcanoes, volcanic, eruption, eruptions, erupts, erupted, drought, droughts, landslide,
        landslides, mudslide, mudslides, tsunami, tsunamis, heatwave, heatwaves, heat wave, monsoon, blizzard,
        snowstorm, heavy rain, heavy rains, downpour, downpours, rainstorm, hailstorm, sandstorm, deluge"""),
    # "strikes, walkouts or industrial action by workers or unions"
    "labour_action": _phrases("""
        strike, strikes, striking, strikers, walkout, walkouts, walk out, walks out, union, unions, unionised,
        unionized, unionise, unionize, trade union, workers, industrial action, work to rule, stoppage,
        stoppages, lockout, lockouts, picket, pickets, picketing, go slow, pay dispute, wage dispute,
        labour dispute, labor dispute, industrial dispute, pay talks, wage talks, collective bargaining,
        collective agreement"""),
    # "currency crises, defaults, bank failures, market crashes or capital controls"
    "financial_stress": _phrases("""
        crisis, crises, default, defaults, defaulted, defaulting, collapse, collapses, collapsed, collapsing,
        crash, crashes, crashed, bank failure, bank failures, bank run, bankrupt, bankruptcy, bankruptcies,
        insolvency, insolvent, capital controls, devaluation, devalue, devalues, devalued, bailout, bailouts,
        hyperinflation, meltdown, contagion, debt restructuring"""),
}
# Phrases that hold a cue word but are not the category ("price war", "conflict of interest").
NOT_CUES: Mapping[str, tuple[tuple[str, ...], ...]] = {
    "conflict": _phrases("""
        price war, price wars, trade war, trade wars, tariff war, tariff wars, bidding war, culture war,
        culture wars, war of words, war chest, star wars, heart attack, heart attacks, panic attack,
        conflict of interest, conflicts of interest"""),
}
# Labour: a "strike" next to one of these is a military strike ("missile strike", "drone strikes"),
# and right after "mass" or "massive" it is labour only with another labour word in the headline
# ("Russia 'preparing new mass strike soon': Zelenskiy" is no walkout; "mass strike by rail workers" is).
STRIKE_WORDS = frozenset({"strike", "strikes", "striking"})
AMBIGUOUS_STRIKE = frozenset({"mass", "massive"})
MILITARY_WORDS = frozenset({
    "missile", "missiles", "drone", "drones", "air", "airstrike", "military", "rocket", "rockets", "artillery",
    "nuclear", "retaliatory", "preemptive", "emptive", "precision", "cruise", "ballistic", "army", "navy",
    "naval", "troops", "bomb", "bombs", "bomber", "bombers",
})  # fmt: skip


def _spans(words: Sequence[str], phrases: tuple[tuple[str, ...], ...]) -> list[tuple[int, int]]:
    index = _by_first(phrases)
    out = []
    for i, w in enumerate(words):
        for p in index.get(w, ()):
            if tuple(words[i : i + len(p)]) == p:
                out.append((i, i + len(p)))
    return out


@lru_cache(maxsize=16384)
def category_cue(title: str, tag_type: str) -> bool:
    """Does the headline carry a cue word of the tag's category? True for a tag type with no cue
    list (sanctions, cyber and other have their own checks)."""
    cues = CATEGORY_CUES.get(tag_type)
    if cues is None:
        return True
    w = title_facts(title).words
    blocked = {i for s, e in _spans(w, NOT_CUES.get(tag_type, ())) for i in range(s, e)}
    hits = [(s, e) for s, e in _spans(w, cues) if not blocked.intersection(range(s, e))]
    if tag_type != "labour_action":
        return bool(hits)
    others = [s for s, e in hits if not (e - s == 1 and w[s] in STRIKE_WORDS)]
    for s, e in hits:
        if e - s == 1 and w[s] in STRIKE_WORDS:
            if MILITARY_WORDS.intersection(w[max(0, s - 2) : s + 3]):
                continue  # "missile strike", "drone strikes on Kyiv": not labour
            if s > 0 and w[s - 1] in AMBIGUOUS_STRIKE and not others:
                continue  # "new mass strike soon": labour only with a second labour word
        return True
    return False


# --- D: not a current event (engine 1.3.4) ------------------------------------------------------
# Anniversary and remembrance pieces ("26/11 Attacks: 18 Years Later, Remembering the Night …") and
# court-record listings ("M/S Reliance Chemotax Industries Ltd vs Union Of India on 26 November,
# 2026") are about past events, so they never count through a country match.
REMEMBRANCE = _phrases("""
    anniversary, anniversaries, remembers, remembering, remembered, remembrance, commemorates, commemorate,
    commemorated, commemorating, commemoration, commemorations, memorial, memorials, recalls horror,
    recalls horrors, recalls the horror, recalls the horrors, recalled the horror""")
# "Memorial Hospital hit by ransomware" is news about an institution.
MEMORIAL_NAMES = frozenset({
    "hospital", "hospitals", "medical", "health", "healthcare", "university", "college", "school", "stadium",
    "airport", "clinic", "institute", "foundation", "bank",
})  # fmt: skip
NUMBER_WORDS = frozenset(
    "two three four five six seven eight nine ten eleven twelve fifteen twenty thirty forty fifty hundred".split()
)
_MONTHS = "january|february|march|april|may|june|july|august|september|october|november|december"
_COURT_LISTING = re.compile(
    rf"\S.*\s(?:vs?\.?|versus)\s+\S.*\son\s+\d{{1,2}}\s+(?:{_MONTHS}),?\s+\d{{4}}\s*$", re.IGNORECASE
)


@lru_cache(maxsize=16384)
def not_current(title: str) -> bool:
    """An anniversary or remembrance piece, or a court-record listing (D). "Recalls ambassador",
    "recalls envoy" and product recalls are current news."""
    if _COURT_LISTING.search(title or ""):
        return True
    w = title_facts(title).words
    for s, e in _spans(w, REMEMBRANCE):
        if w[s] in ("memorial", "memorials") and e < len(w) and w[e] in MEMORIAL_NAMES:
            continue
        return True
    # "18 Years Later", "15 years on", "Ten years on"
    for i in range(1, len(w) - 1):
        if w[i] == "years" and w[i + 1] in ("later", "on") and (w[i - 1].isdigit() or w[i - 1] in NUMBER_WORDS):
            return True
    return False
