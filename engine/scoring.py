"""Signal, recency, likelihood and impact (risk-engine.md §4–§6). Pure functions."""

from __future__ import annotations

import math
from datetime import datetime, timezone
from decimal import Decimal
from typing import Iterable, Mapping, Sequence
from uuid import UUID

from engine.config import EngineConfig
from engine.models import Anomaly, Entity, FeedEvent

CATEGORIES = (
    "natural_hazard", "logistics", "geopolitical", "sanctions", "trade_policy",
    "cyber", "financial", "labour", "other",
)  # fmt: skip

TAG_CATEGORIES = {
    "conflict": "geopolitical",
    "protest": "geopolitical",
    "sanctions": "sanctions",
    "trade_policy": "trade_policy",
    "port_shipping_disruption": "logistics",
    "natural_hazard": "natural_hazard",
    "cyber_incident": "cyber",
    "labour_action": "labour",
    "financial_stress": "financial",
    "other": "other",
}

SANCTION_POSITIVE = {"sanction.designation": "designation", "sanction.update": "update", "sanction.listed": "listed"}
ADVISORY_SOURCES = {"osv", "nvd"}
CPE_SOURCES = {"cisa_kev", "nvd"}


def r3(x: float) -> float:
    return round(float(x) + 0.0, 3)


def clamp(x: float, lo: float, hi: float) -> float:
    return lo if x < lo else hi if x > hi else x


# --- categories (§4) ----------------------------------------------------------------


def event_category(ev: FeedEvent) -> str | None:
    t = ev.type
    if t.startswith("scenario."):
        c = t.removeprefix("scenario.")
        return c if c in CATEGORIES else None
    if t.startswith("hazard."):
        return "natural_hazard"
    if t == "port.disruption":
        return "logistics"
    if t in SANCTION_POSITIVE:
        return "sanctions"
    if t.startswith("sanction."):
        return None  # removals are never positive evidence
    if t.startswith("cyber."):
        return "cyber"
    if ev.tag is not None:
        return TAG_CATEGORIES.get(ev.tag.event_type)
    return None


def event_class(source: str, type_: str) -> str:
    """How an event is matched: ``sanctions``, ``scenario``, ``cve``, ``hazard``, ``port`` or ``news``
    (tagged text: GDELT, RSS)."""
    if type_ in SANCTION_POSITIVE:
        return "sanctions"
    if source == "scenario":
        return "scenario"
    if type_.startswith("cyber."):
        return "cve"
    if type_.startswith("hazard."):
        return "hazard"
    if type_ == "port.disruption":
        return "port"
    return "news"


def anomaly_category(a: Anomaly) -> str | None:
    if a.series.startswith("portwatch."):
        return "logistics"
    if a.series == "fx.rate" or a.series.startswith("wb."):
        return "financial"
    if a.series == "gdelt.articles":
        return "other"
    return None


def is_standing_listing(ev: FeedEvent) -> bool:
    """A full-list sanctions entry: in force while the row exists, so it does not decay."""
    return ev.type == "sanction.listed" or (ev.type in SANCTION_POSITIVE and ev.attrs.get("full_list") is True)


# --- signal s (§5.4) -----------------------------------------------------------------


def tag_confidence(ev: FeedEvent) -> float:
    tag = ev.tag
    if tag is None:
        return 0.0
    if tag.type_confidence is not None:
        return float(tag.type_confidence)
    return 1.0 if tag.type_source == "source" else 0.0


def event_signal(ev: FeedEvent, config: EngineConfig, epss: float = 0.0) -> float | None:
    """``s`` for an event, or ``None`` if the event is never evidence (withdrawn advisory)."""
    sev = ev.severity
    if ev.source == "scenario":
        return float(sev if sev is not None else config.default_severity)
    if ev.type in SANCTION_POSITIVE:
        return config.ofac_update_signal if ev.type == "sanction.update" else config.ofac_designation_signal
    if ev.source == "cisa_kev":
        return max(config.kev_min_signal, epss)
    if ev.type.startswith("cyber."):
        if ev.attrs.get("withdrawn"):
            return None
        return max(sev if sev is not None else config.default_severity, epss)
    if ev.source == "eonet":
        hazard = ev.type.removeprefix("hazard.")
        return config.eonet_major_signal if hazard in config.eonet_major_types else config.eonet_minor_signal
    if ev.type.startswith("hazard.") or ev.type == "port.disruption":
        return float(sev if sev is not None else config.default_severity)
    if ev.tag is not None:
        if ev.tag.event_type == "other":
            return config.news_other_signal
        return config.news_signal_base + config.news_signal_slope * tag_confidence(ev)
    return float(sev if sev is not None else config.default_severity)


def anomaly_signal(a: Anomaly, config: EngineConfig) -> float:
    if a.method == "isolation_forest":
        return config.isolation_forest_factor * clamp(a.score, 0.0, 1.0)
    span = config.z_full - config.z_threshold
    return clamp(0.5 + (abs(a.score) - config.z_threshold) * 0.5 / span, 0.5, 1.0)


# --- recency r (§5.5) ----------------------------------------------------------------


def half_life_key(category: str, *, source: str | None = None, series: str | None = None, type_: str = "") -> str:
    if category == "cyber" and (source in ADVISORY_SOURCES or type_ == "cyber.vulnerability"):
        return "cyber.advisory"
    if category == "financial" and series is not None and series.startswith("wb."):
        return "financial.wb"
    return category


def parse_time(value) -> datetime | None:
    """A timestamp from ``attrs`` (ISO 8601 string or datetime); naive means UTC."""
    if isinstance(value, datetime):
        ts = value
    elif isinstance(value, str) and value.strip():
        s = value.strip().replace("Z", "+00:00")
        try:
            ts = datetime.fromisoformat(s)
        except ValueError:
            return None
    else:
        return None
    return ts.replace(tzinfo=timezone.utc) if ts.tzinfo is None else ts.astimezone(timezone.utc)


def event_age_hours(ev: FeedEvent, now: datetime) -> float:
    """Hours since the event (0 while ``attrs.to_date`` is in the future)."""
    to_date = parse_time(ev.attrs.get("to_date"))
    if to_date is not None and to_date > now:
        return 0.0
    ref = ev.published_at or ev.first_seen_at
    if ref is None:
        return 0.0
    return max(0.0, (now - ref).total_seconds() / 3600.0)


def recency(age_hours: float, half_life_hours: float, config: EngineConfig) -> float | None:
    """``0.5 ^ (age / half-life)``; ``None`` past 4 half-lives (at most 365 days)."""
    age = max(0.0, age_hours)
    if age > min(config.cutoff_half_lives * half_life_hours, config.max_age_days * 24.0):
        return None
    return 0.5 ** (age / half_life_hours)


# --- likelihood (§5.6) ---------------------------------------------------------------


def contribution(signal: float, rec: float, confidence: float) -> float:
    return r3(signal * rec * confidence)


def noisy_or(ps: Iterable[float], cap: float) -> float:
    """Plain noisy-OR ``min(cap, 1 − Π(1 − p))`` (engine ≤ 1.2.0; kept for comparisons)."""
    prod = 1.0
    for p in ps:
        prod *= 1.0 - clamp(p, 0.0, 1.0)
    return r3(min(cap, 1.0 - prod))


def item_weights(n: int, config: EngineConfig) -> list[float]:
    """Weight of the 1st, 2nd, … strongest item: ``decay^(k−1)`` (1, 0.5, 0.25, … by default)."""
    return [r3(config.likelihood_decay**k) for k in range(n)]


def likelihood(ps: Sequence[float], config: EngineConfig) -> float:
    """Engine 1.3.0 (§5.6): a noisy-OR in which each further item counts less.

    ``L = min(cap, 1 − Π_k (1 − w_k · p_k))`` over the contributions sorted strongest first,
    with ``w_k = decay^(k−1)``. The strongest item counts in full, so one strong item scores
    exactly as before; ``L ≤ Σ w_k p_k`` and the weights sum to less than ``1 / (1 − decay)``
    (2 by default), so L stays below twice the strongest contribution however many weak items
    there are (seven items of 0.3: 0.49, where the plain noisy-OR gave 0.92).
    """
    prod = 1.0
    for k, p in enumerate(sorted(ps, reverse=True)):
        prod *= 1.0 - config.likelihood_decay**k * clamp(p, 0.0, 1.0)
    return r3(min(config.likelihood_cap, 1.0 - prod))


# --- impact (§6) ---------------------------------------------------------------------


def latest_rates(series: Mapping[tuple[str, str], Sequence[tuple[datetime, float]]]) -> dict[str, float]:
    """``CCY → units per EUR`` from the newest ``fx.rate`` point of each ``EUR/<CCY>``; EUR = 1."""
    out = {"EUR": 1.0}
    for (name, key), pts in series.items():
        if name == "fx.rate" and key.startswith("EUR/") and pts:
            v = pts[-1][1]
            if v and v > 0 and math.isfinite(v):
                out[key[4:]] = float(v)
    return out


def exposure_shares(entities: Sequence[Entity], rates: Mapping[str, float]) -> dict[UUID, float | None]:
    """Supplier spend share and customer revenue share; others (and unknowns) ``None``.

    Spend is converted to one currency with the latest ECB rates (EUR as the
    pivot: a share does not depend on which common currency is used, so this
    equals converting to ``base_currency``). If some spend currency has no
    rate, raw amounts are used only when every supplier shares one currency;
    otherwise every supplier's share is unknown.
    """
    out: dict[UUID, float | None] = {}
    spenders = [e for e in entities if e.kind == "supplier" and e.annual_spend is not None and e.spend_currency]
    amounts: dict[UUID, float] | None
    if all(e.spend_currency in rates for e in spenders):
        amounts = {e.id: float(e.annual_spend) / rates[e.spend_currency] for e in spenders}
    elif len({e.spend_currency for e in spenders}) == 1:
        amounts = {e.id: float(e.annual_spend) for e in spenders}
    else:
        amounts = None
    total = sum(amounts.values()) if amounts else 0.0
    for e in entities:
        if e.kind == "supplier":
            out[e.id] = amounts[e.id] / total if amounts and total > 0 and e.id in amounts else None
        elif e.kind == "customer":
            out[e.id] = float(e.revenue_share) if isinstance(e.revenue_share, (Decimal, int, float)) else None
        else:
            out[e.id] = None
    return out


def impact(entity: Entity, category: str, share: float | None, config: EngineConfig) -> tuple[float, dict]:
    w = config.criticality_weights.get(entity.criticality or "", config.default_criticality_weight)
    m = config.category_multipliers[category]
    if share is None:
        n = None
        base = w
    else:
        n = min(1.0, share / config.full_exposure_share)
        base = 0.5 * w + 0.5 * n
    value = r3(clamp(m * base, config.impact_min, config.impact_max))
    factors = {
        "value": value,
        "criticality": entity.criticality,
        "criticality_weight": w,
        "exposure_share": None if share is None else round(share, 4),
        "exposure_norm": None if n is None else r3(n),
        "category_multiplier": m,
    }
    return value, factors
