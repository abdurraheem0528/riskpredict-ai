"""Watch terms the worker may add for a workspace (risk-engine.md §7)."""

from __future__ import annotations

from typing import Sequence

from engine.config import DEFAULT_CONFIG, EngineConfig
from engine.core import has_key, watch_term
from engine.models import Entity
from engine.software import purl_to_osv

_CRIT_ORDER = {"critical": 0, "high": 1}


def watch_terms_for(entities: Sequence[Entity], config: EngineConfig = DEFAULT_CONFIG) -> list[tuple[str, str, str]]:
    """``(kind, term, scope)`` rows: every OSV package, then at most 5 GDELT name queries.

    GDELT picks suppliers with criticality ``critical`` before ``high``, then the
    largest ``annual_spend``, then name and id, so the same entities always give
    the same five terms.
    """
    osv = sorted(
        {(name, eco) for e in entities if has_key(e, "software") for s in e.software if (o := purl_to_osv(s))
         for eco, name in [o]}
    )  # fmt: skip
    out = [("osv_package", name, eco) for name, eco in osv]
    picks = sorted(
        (e for e in entities if e.kind == "supplier" and e.criticality in _CRIT_ORDER),
        key=lambda e: (_CRIT_ORDER[e.criticality], -(float(e.annual_spend) if e.annual_spend is not None else -1.0),
                       e.name.casefold(), str(e.id)),
    )  # fmt: skip
    terms: list[str] = []
    for e in picks:
        t = watch_term(e, config)
        if t and t not in terms:
            terms.append(t)
        if len(terms) == config.watch_gdelt_max:
            break
    out += [("gdelt_query", t, "") for t in terms]
    return out
