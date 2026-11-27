"""Is a country what a news item is about? (risk-engine.md §5.10, engine 1.3.0). Pure functions.

The engine sees a news item's title, its tag (the model's reading of that same title) and,
for GDELT file articles, ``countries``: every country GDELT located anywhere in the article
text, first mention first. A country found only deep inside a long article is a passing
mention, so a news *country* match counts only when the country is **central**:

* it is named in the title (a name, alias, adjective such as "Chinese", or the capital used
  for the government, such as "Beijing"), or it is in the tag's countries; or
* it is the first place GDELT located in the article (``countries[0]``).

GDELT does not give mention counts in what the feed stores, so "most-mentioned" cannot be
told apart from "first-mentioned"; the first is used. Two categories need more than a
central country (:func:`sanction_targets`, :func:`national_scale`, :func:`sector_hit`).

Everything here is vocabulary plus token windows, deterministic and cached per title.
"""

from __future__ import annotations

import json
import re
import unicodedata
from dataclasses import dataclass
from functools import lru_cache
from typing import Iterable, Mapping, Sequence

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

# --- cyber (§5.10) ----------------------------------------------------------------------
# A country-level cyber match needs a national-scale incident ...
NATIONAL_SCALE: tuple[tuple[str, ...], ...] = tuple(tuple(t.split()) for t in (
    "nationwide", "national", "nation", "countrywide", "government", "governments", "ministry", "ministries",
    "federal", "parliament", "election", "elections", "military", "army", "defence", "defense", "infrastructure",
    "critical infrastructure", "grid", "power grid", "electricity", "energy", "utility", "utilities", "water supply",
    "rail", "railway", "railways", "rail network", "airport", "airports", "port", "ports", "hospital", "hospitals",
    "health service", "telecom", "telecoms", "telecommunications", "mobile network", "internet", "outage",
    "outages", "blackout", "state sponsored", "state backed", "nation state", "customs", "central bank",
    "banking system", "payment system", "payments system", "emergency services", "police",
))  # fmt: skip
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


def tokens(text: str) -> list[tuple[str, str]]:
    """``(original, normalised)`` words and clause marks; hyphens split words."""
    out = []
    for m in _TOKEN_RE.finditer(text.replace("-", " ")):
        raw = m.group(0)
        n = normalise(raw)
        if n:
            out.append((raw, n))
    return out


def stem(word: str) -> str:
    """Crude singular: ``sensors`` → ``sensor``, ``batteries`` → ``battery``."""
    if len(word) > 4 and word.endswith("ies"):
        return word[:-3] + "y"
    if len(word) > 3 and word.endswith("s") and not word.endswith("ss"):
        return word[:-1]
    return word


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


@dataclass(frozen=True, slots=True)
class TitleFacts:
    words: tuple[str, ...]  # normalised tokens
    mentions: tuple[Mention, ...]

    @property
    def countries(self) -> frozenset[str]:
        return frozenset(m.code for m in self.mentions)


@lru_cache(maxsize=32768)
def title_facts(title: str) -> TitleFacts:
    toks = tokens(title or "")
    words = tuple(n for _, n in toks)
    vocab, longest = _vocabulary()
    mentions: list[Mention] = []
    i = 0
    while i < len(toks):
        hit = None
        for n in range(min(longest, len(toks) - i), 0, -1):
            terms = vocab.get(words[i : i + n])
            if not terms:
                continue
            raw = " ".join(r for r, _ in toks[i : i + n]).replace(".", "")
            ok = [t for t in terms if not t.exact_case or raw.isupper()]
            if ok:
                hit = (n, ok[0])
                break
        if hit is None:
            i += 1
            continue
        n, term = hit
        if term.code is not None:
            mentions.append(Mention(term.code, i, i + n, term.kind))
        i += n
    return TitleFacts(words, tuple(mentions))


def central_countries(title: str, tag_countries: Iterable[str], countries: Sequence[str]) -> dict[str, str]:
    """Country → why it is central: ``title`` (named in the title or its tag) or ``first_place``
    (the first place GDELT located in the article). Title countries first, then the first place."""
    out: dict[str, str] = {}
    for c in [*sorted(title_facts(title).countries), *tag_countries]:
        out.setdefault(c, "title")
    if countries:
        out.setdefault(countries[0], "first_place")
    return out


# --- sanctions: is the country the target? ----------------------------------------------------


@lru_cache(maxsize=4)
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

    A country followed by an imposing verb ("US imposes", "Washington adds") or preceded by
    "under" / "by" is the actor, and so is every country joined to it by "and", "or" or a
    comma ("The Netherlands and China plan …"). Windows are ``window`` tokens and never cross
    ``; : | ?``.
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
        prep = False
        for r in range(e, min(len(w), e + window + 1)):
            if w[r] in BREAKS:
                break
            if w[r] in TARGET_PREPOSITIONS:
                prep = True
            hit = next((k for k, m in enumerate(ms) if m.start == r), None)
            if hit is None:
                continue
            if prep or (verb and r - e <= 2):
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
    """A cyber headline about government, critical infrastructure or a nationwide outage."""
    return _has_phrase(title_facts(title).words, NATIONAL_SCALE)


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
    if not cues:
        return False
    return any(stem(x) in cues for x in title_facts(title).words)


def tag_countries(entities: Mapping | None) -> list[str]:
    if not isinstance(entities, Mapping):
        return []
    v = entities.get("countries")
    return [c for c in v if isinstance(c, str)] if isinstance(v, list) else []
