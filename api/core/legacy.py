"""Plain wording for risks an older engine wrote and the current one no longer rewrites.

Engine 1.2.0 rewrites a risk's title and explanation each time it rescores it, but a risk
it has since resolved (or one a user dismissed) keeps what the engine before it wrote:
an explanation with the raw formula ("2 item(s) … noisy-OR …"). ``GET /v1/risks/{id}``
re-renders such an explanation from the stored evidence with the engine's own text
functions (``engine.texts.explanation``, unchanged), and keeps the stored text when it
cannot (no engine on this server, no evidence, or anything unexpected).

A risk the current engine wrote carries ``factors.explain``; only risks without it are
re-rendered. The evidence snapshot has no news tag, so a news item reads with its risk
category's topic ("A recent news report about sanctions …").
"""

from __future__ import annotations

import logging
from types import SimpleNamespace as NS
from typing import Any

log = logging.getLogger("riskpredict.legacy")


def written_by_old_engine(row: dict) -> bool:
    factors = row.get("factors")
    return not (isinstance(factors, dict) and factors.get("explain"))


def _item(ev: dict) -> NS:
    snap = ev.get("snapshot") if isinstance(ev.get("snapshot"), dict) else {}
    event = anomaly = None
    if ev["kind"] == "event":
        typ = str(snap.get("type") or "")
        event = NS(source=ev.get("event_source") or "", raw_id=ev.get("event_raw_id") or "", type=typ,
                   title=snap.get("title"), attrs={}, tag=NS(event_type=None) if typ == "news" else None)  # fmt: skip
    else:
        anomaly = NS(series=ev.get("anomaly_series") or "", key=ev.get("anomaly_key") or "",
                     method=ev.get("anomaly_method"), attrs={})  # fmt: skip
    return NS(match_type=ev["match_type"], match_value=ev["match_value"], confidence=ev["match_confidence"],
              signal=ev["signal"], recency=ev["recency"], p=ev["contribution"], snapshot=snap, detail=snap,
              event=event, anomaly=anomaly)  # fmt: skip


def explanation(row: dict, evidence: list[dict], entity: dict | None) -> str | None:
    """The re-rendered explanation, or ``None`` to keep the stored one."""
    if not written_by_old_engine(row) or not evidence or entity is None:
        return None
    from api.core import engine as engine_iface

    if engine_iface.load() is None:
        return None
    try:
        from engine import texts

        ent: Any = NS(name=entity["name"], kind=entity["kind"], hs_codes=tuple(entity.get("hs_codes") or ()))
        items = [_item(e) for e in sorted(evidence, key=lambda e: -(e["contribution"] or 0))]
        text = texts.explanation(row["category"], ent, items)
    except Exception as exc:  # the stored text is still right, only less plain
        log.info("kept the stored explanation (%s)", type(exc).__name__)
        return None
    return text or None
