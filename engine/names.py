"""Company-name normalisation and fuzzy similarity (risk-engine.md §5.2).

Name matching is the only fuzzy match in the engine. The steps, in order:

1. Unicode NFKD, then drop combining marks (``Müller`` → ``Muller``).
2. casefold; a few undecomposable letters transliterated (``æ`` → ``ae``,
   ``ø`` → ``o``, ``ł`` → ``l`` …); ``&`` → `` and ``; apostrophes and dots are removed inside words
   (``O'Neil`` → ``oneil``, ``S.A.`` → ``sa``); every other non-alphanumeric
   character becomes a space (``Al-Noor`` → ``al noor``).
3. Drop legal-form tokens (``ltd``, ``llc``, ``gmbh``, ``sa``, … see
   ``config.LEGAL_FORMS``). If *every* token is a legal form, they are kept.
4. Similarity is ``rapidfuzz.fuzz.token_sort_ratio(a, b) / 100``, i.e. the
   Levenshtein ratio of the two names with their tokens sorted. That makes word
   order irrelevant (``Trading Acme`` = ``Acme Trading``) while a changed letter
   still costs. Computed as ``fuzz.ratio`` on pre-sorted strings, which is the
   same number and lets one name be compared with thousands at C speed.
"""

from __future__ import annotations

import re
import unicodedata
from functools import lru_cache
from typing import Iterable, Sequence

from engine.config import DEFAULT_CONFIG, LEGAL_FORMS

_SQUASH = re.compile(r"['’`.]")
# Latin letters that NFKD does not decompose.
_TRANSLIT = str.maketrans({"æ": "ae", "ø": "o", "œ": "oe", "ł": "l", "đ": "d", "ð": "d", "þ": "th", "ı": "i"})
_NON_ALNUM = re.compile(r"[^0-9a-z]+")


@lru_cache(maxsize=131072)  # pure; the worker screens the same list for every workspace
def normalise_name(name: str | None, legal_forms: frozenset[str] = LEGAL_FORMS) -> str:
    if not name:
        return ""
    s = unicodedata.normalize("NFKD", str(name))
    s = "".join(ch for ch in s if not unicodedata.combining(ch))
    s = s.casefold().translate(_TRANSLIT).replace("&", " and ")
    s = _SQUASH.sub("", s)
    s = unicodedata.normalize("NFKD", s)  # casefold can produce new decomposable characters
    s = "".join(ch for ch in s if not unicodedata.combining(ch))
    tokens = [t for t in _NON_ALNUM.sub(" ", s).split() if t]
    kept = [t for t in tokens if t not in legal_forms]
    return " ".join(kept or tokens)


def sorted_tokens(normalised: str) -> str:
    return " ".join(sorted(normalised.split()))


def similarity(a: str, b: str, legal_forms: frozenset[str] = LEGAL_FORMS) -> float:
    """token_sort_ratio of the two normalised names, 0–1."""
    from rapidfuzz import fuzz

    na, nb = normalise_name(a, legal_forms), normalise_name(b, legal_forms)
    if not na or not nb:
        return 0.0
    return fuzz.ratio(sorted_tokens(na), sorted_tokens(nb)) / 100.0


class NameIndex:
    """Many candidate names, searchable by one query name at a time.

    ``add(name, ref)`` stores the normalised, token-sorted form; ``search``
    returns ``(ref, original_name, similarity)`` for every candidate at or above
    ``threshold``, best first (ties by insertion order, so results are stable).
    """

    def __init__(self, legal_forms: frozenset[str] = LEGAL_FORMS, min_length: int = DEFAULT_CONFIG.name_min_length):
        self._legal = legal_forms
        self._min = min_length
        self._choices: list[str] = []
        self._refs: list[tuple[object, str]] = []

    def __len__(self) -> int:
        return len(self._choices)

    def add(self, name: str | None, ref: object) -> bool:
        n = normalise_name(name, self._legal)
        if len(n.replace(" ", "")) < self._min:
            return False
        self._choices.append(sorted_tokens(n))
        self._refs.append((ref, str(name)))
        return True

    def search(self, query: str | None, threshold: float) -> list[tuple[object, str, float]]:
        from rapidfuzz import fuzz, process

        n = normalise_name(query, self._legal)
        if len(n.replace(" ", "")) < self._min or not self._choices:
            return []
        hits = process.extract(
            sorted_tokens(n), self._choices, scorer=fuzz.ratio, score_cutoff=threshold * 100.0, limit=None
        )
        hits.sort(key=lambda h: (-h[1], h[2]))
        return [(self._refs[i][0], self._refs[i][1], round(score / 100.0, 4)) for _, score, i in hits]


def alias_names(aliases: object) -> list[str]:
    """``attrs.aliases`` in any shape the OFAC connectors may store it.

    A list of strings, or of objects with ``name`` / ``full_name`` / ``text``,
    or a single string. Anything else is ignored.
    """
    if aliases is None:
        return []
    if isinstance(aliases, str):
        return [aliases]
    out: list[str] = []
    if isinstance(aliases, Iterable):
        for a in aliases:
            if isinstance(a, str):
                out.append(a)
            elif isinstance(a, dict):
                v = a.get("name") or a.get("full_name") or a.get("text")
                if isinstance(v, str):
                    out.append(v)
    return out


def dedupe(values: Sequence[str]) -> list[str]:
    return list(dict.fromkeys(v for v in values if v))
