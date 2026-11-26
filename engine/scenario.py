"""What-if scenarios (``POST /v1/scenarios``; ``ScenarioInputs`` / ``ScenarioResults`` in openapi.yaml).

A scenario runs the *same* ``assess`` twice on the same feed snapshot: once as
is (baseline) and once with the inputs applied. Nothing is written.

* ``HazardShock`` becomes a hypothetical event (source ``scenario``, published
  ``now``) with the given category, severity (= signal), countries, ports,
  chokepoints and position/radius, and goes through the normal match rules.
  "Port NLRTM closed" is ``{"type": "event", "category": "logistics",
  "severity": 1, "ports": ["NLRTM"]}``.
* ``FxShock`` becomes a hypothetical ``fx.rate`` robust-z anomaly on
  ``<base>/<currency>`` at ``now``: ``z = ln(1 + pct/100) / spread``, where
  ``spread`` is the robust spread (1.4826 × MAD) of the pair's daily log returns
  over the last 90 days, exactly as the stored anomalies are scored. The usual
  signal rule then applies (``|z|`` 3.5 → 0.5, 8 → 1.0; below 3.5 the move is
  ordinary and adds nothing).
* ``SanctionsShock`` becomes a hypothetical ``sanction.designation`` carrying the
  entity's own name and countries, screened like any OFAC row.
* ``entity_overrides`` change or drop entities before both scoring passes'
  impact step (dropping a supplier also changes everyone else's spend share).
"""

from __future__ import annotations

import math
from dataclasses import dataclass, replace
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from typing import Any, Mapping, Sequence
from uuid import UUID

from engine.config import DEFAULT_CONFIG, EngineConfig
from engine.core import ENGINE_VERSION, assess
from engine.models import Anomaly, Assessment, Entity, FeedEvent, FeedSnapshot, rfc3339
from engine.scoring import CATEGORIES

MAX_SHOCKS = 20
MAX_OVERRIDES = 200
MAX_CHANGES = 200
CRITICALITIES = ("low", "medium", "high", "critical")


class ScenarioError(ValueError):
    """Invalid scenario inputs. The message is safe to show (a Problem ``detail``)."""


@dataclass(frozen=True, slots=True)
class HazardShock:
    category: str
    severity: float
    label: str | None = None
    countries: tuple[str, ...] = ()
    ports: tuple[str, ...] = ()
    chokepoints: tuple[str, ...] = ()
    lat: float | None = None
    lon: float | None = None
    radius_km: float | None = None
    type: str = "event"


@dataclass(frozen=True, slots=True)
class FxShock:
    currency: str
    pct_change: float
    base_currency: str | None = None
    type: str = "fx"


@dataclass(frozen=True, slots=True)
class SanctionsShock:
    entity_id: UUID
    type: str = "sanction"


@dataclass(frozen=True, slots=True)
class EntityOverride:
    entity_id: UUID
    exclude: bool = False
    criticality: str | None = None
    annual_spend: Decimal | None = None
    revenue_share: float | None = None
    countries: tuple[str, ...] | None = None


@dataclass(frozen=True, slots=True)
class ScenarioInputs:
    shocks: tuple[HazardShock | FxShock | SanctionsShock, ...] = ()
    entity_overrides: tuple[EntityOverride, ...] = ()

    @classmethod
    def from_dict(cls, d: Mapping[str, Any]) -> "ScenarioInputs":
        """Parse and validate the ``ScenarioInputs`` JSON body; raises ``ScenarioError``."""
        if not isinstance(d, Mapping):
            raise ScenarioError("inputs must be an object")
        shocks_raw = d.get("shocks") or []
        overrides_raw = d.get("entity_overrides") or []
        if not isinstance(shocks_raw, list) or not isinstance(overrides_raw, list):
            raise ScenarioError("shocks and entity_overrides must be arrays")
        if not shocks_raw and not overrides_raw:
            raise ScenarioError("give at least one shock or entity override")
        if len(shocks_raw) > MAX_SHOCKS or len(overrides_raw) > MAX_OVERRIDES:
            raise ScenarioError(f"at most {MAX_SHOCKS} shocks and {MAX_OVERRIDES} entity overrides")
        return cls(tuple(_shock(s, i) for i, s in enumerate(shocks_raw)), tuple(_override(o) for o in overrides_raw))


def _codes(values, what: str, check) -> tuple[str, ...]:
    if values is None:
        return ()
    if not isinstance(values, list):
        raise ScenarioError(f"{what} must be an array")
    out = []
    for v in values:
        n = check(v) if isinstance(v, str) else None
        if not n:
            raise ScenarioError(f"invalid {what[:-1] if what.endswith('s') else what} code")
        out.append(n)
    return tuple(dict.fromkeys(out))


def _country(v: str) -> str | None:
    from jobs import codes

    return v if codes.is_country(v) else None


def _currency(v: str | None) -> str | None:
    from jobs import codes

    return v if isinstance(v, str) and codes.is_currency(v) else None


def _num(v, what: str, lo: float, hi: float) -> float:
    if isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v) or not lo <= v <= hi:
        raise ScenarioError(f"{what} must be a number from {lo:g} to {hi:g}")
    return float(v)


def _uuid(v, what: str) -> UUID:
    try:
        return UUID(str(v))
    except (ValueError, TypeError):
        raise ScenarioError(f"{what} must be a uuid") from None


def _shock(s: Any, i: int):
    if not isinstance(s, Mapping):
        raise ScenarioError(f"shock {i + 1} must be an object")
    t = s.get("type")
    if t == "event":
        from jobs import codes

        cat = s.get("category")
        if cat not in CATEGORIES:
            raise ScenarioError(f"shock {i + 1}: unknown category")
        lat, lon = s.get("lat"), s.get("lon")
        if (lat is None) != (lon is None):
            raise ScenarioError(f"shock {i + 1}: give both lat and lon, or neither")
        from engine.reference import chokepoint_id

        chokes = []
        for c in s.get("chokepoints") or []:
            cid = chokepoint_id(c) if isinstance(c, str) else None
            if not cid:
                raise ScenarioError(f"shock {i + 1}: unknown chokepoint")
            chokes.append(cid)
        label = s.get("label")
        if label is not None and (not isinstance(label, str) or len(label) > 200):
            raise ScenarioError(f"shock {i + 1}: label must be text of at most 200 characters")
        return HazardShock(
            category=cat,
            severity=_num(s.get("severity"), "severity", 0, 1),
            label=label,
            countries=_codes(s.get("countries"), "countries", _country),
            ports=_codes(s.get("ports"), "ports", codes.locode),
            chokepoints=tuple(dict.fromkeys(chokes)),
            lat=None if lat is None else _num(lat, "lat", -90, 90),
            lon=None if lon is None else _num(lon, "lon", -180, 180),
            radius_km=None if s.get("radius_km") is None else _num(s.get("radius_km"), "radius_km", 1, 2000),
        )
    if t == "fx":
        ccy = _currency(s.get("currency"))
        if not ccy:
            raise ScenarioError(f"shock {i + 1}: invalid currency code")
        base = s.get("base_currency")
        if base is not None and not _currency(base):
            raise ScenarioError(f"shock {i + 1}: invalid base currency code")
        return FxShock(ccy, _num(s.get("pct_change"), "pct_change", -90, 500), base)
    if t == "sanction":
        return SanctionsShock(_uuid(s.get("entity_id"), "entity_id"))
    raise ScenarioError(f"shock {i + 1}: type must be event, fx or sanction")


def _override(o: Any) -> EntityOverride:
    if not isinstance(o, Mapping):
        raise ScenarioError("each entity override must be an object")
    crit = o.get("criticality")
    if crit is not None and crit not in CRITICALITIES:
        raise ScenarioError("criticality must be low, medium, high or critical")
    spend = o.get("annual_spend")
    if spend is not None:
        try:
            spend = Decimal(str(spend))
        except InvalidOperation:
            raise ScenarioError("annual_spend must be a decimal string") from None
        if not spend.is_finite() or spend < 0:
            raise ScenarioError("annual_spend must be zero or more")
    share = o.get("revenue_share")
    return EntityOverride(
        entity_id=_uuid(o.get("entity_id"), "entity_id"),
        exclude=bool(o.get("exclude", False)),
        criticality=crit,
        annual_spend=spend,
        revenue_share=None if share is None else _num(share, "revenue_share", 0, 1),
        countries=None if o.get("countries") is None else _codes(o.get("countries"), "countries", _country),
    )


# --- applying inputs --------------------------------------------------------------------------


def apply_overrides(entities: Sequence[Entity], overrides: Sequence[EntityOverride]) -> list[Entity]:
    by_id = {o.entity_id: o for o in overrides}
    out = []
    for e in entities:
        o = by_id.get(e.id)
        if o is None:
            out.append(e)
            continue
        if o.exclude:
            continue
        changes: dict[str, Any] = {}
        if o.criticality is not None:
            changes["criticality"] = o.criticality
        if o.annual_spend is not None:
            changes["annual_spend"] = o.annual_spend
            if e.spend_currency is None:
                raise ScenarioError("annual_spend override needs an entity with a spend currency")
        if o.revenue_share is not None:
            changes["revenue_share"] = Decimal(str(o.revenue_share))
        if o.countries is not None:
            changes["countries"] = o.countries
        out.append(replace(e, **changes))
    return out


def _robust_spread(returns: list[float]) -> float | None:
    from jobs.analyse.anomalies import robust_scale

    if len(returns) < 2:
        return None
    import numpy as np

    _, scale = robust_scale(np.asarray(returns, dtype=float))
    return scale if scale > 0 else None


def _pair_points(series, base: str, quote: str) -> list[tuple[datetime, float]]:
    """``quote per base`` by day, from the stored ``EUR/<CCY>`` rates (EUR is 1)."""

    def eur(ccy: str) -> dict[datetime, float] | None:  # None: the currency is EUR itself
        if ccy == "EUR":
            return None
        return {ts: v for ts, v in series.get(("fx.rate", f"EUR/{ccy}"), ()) if v > 0}

    b, q = eur(base), eur(quote)
    if (b is not None and not b) or (q is not None and not q) or (b is None and q is None):
        return []
    if b is not None and q is not None:
        days = sorted(set(b) & set(q))
    else:
        days = sorted(b if b is not None else q)
    return [(ts, (q[ts] if q is not None else 1.0) / (b[ts] if b is not None else 1.0)) for ts in days]


def shock_feeds(inputs: ScenarioInputs, entities: Sequence[Entity], feeds: FeedSnapshot,
                base_currency: str | None, config: EngineConfig = DEFAULT_CONFIG) -> FeedSnapshot:  # fmt: skip
    now = feeds.now
    by_id = {e.id: e for e in entities}
    events: list[FeedEvent] = []
    anomalies: list[Anomaly] = []
    for i, s in enumerate(inputs.shocks, 1):
        raw_id = f"shock-{i}"
        if isinstance(s, HazardShock):
            attrs: dict[str, Any] = {"label": s.label or None, "chokepoints": list(s.chokepoints)}
            if s.radius_km is not None:
                attrs["radius_km"] = s.radius_km
            events.append(FeedEvent(
                source="scenario", raw_id=raw_id, type=f"scenario.{s.category}",
                title=s.label or f"Scenario: {s.category.replace('_', ' ')}", published_at=now, first_seen_at=now,
                countries=s.countries, ports=s.ports, severity=s.severity, lat=s.lat, lon=s.lon,
                attrs={k: v for k, v in attrs.items() if v},
            ))  # fmt: skip
        elif isinstance(s, SanctionsShock):
            e = by_id.get(s.entity_id)
            if e is None:
                raise ScenarioError(f"shock {i}: entity not found in this workspace")
            events.append(FeedEvent(
                source="scenario", raw_id=raw_id, type="sanction.designation", title=f"Scenario: {e.name} designated",
                published_at=now, first_seen_at=now, countries=e.countries,
                attrs={"name": e.name, "entity_id": f"scenario:{raw_id}"},
            ))  # fmt: skip
        elif isinstance(s, FxShock):
            base = s.base_currency or base_currency or "EUR"
            if base == s.currency:
                raise ScenarioError(f"shock {i}: currency and base currency are the same")
            pts = _pair_points(feeds.series, base, s.currency)
            since = now - timedelta(days=90)
            recent = [(t, v) for t, v in pts if t >= since]
            returns = [math.log(b / a) for (_, a), (_, b) in zip(recent, recent[1:]) if a > 0 and b > 0]
            spread = _robust_spread(returns)
            if spread is None or not recent:
                raise ScenarioError(f"shock {i}: no recent exchange-rate history for {base}/{s.currency}")
            last = recent[-1][1]
            z = math.log(1 + s.pct_change / 100.0) / spread
            if abs(z) < config.z_threshold:
                continue  # an ordinary move: it would not be flagged, so it adds no evidence
            anomalies.append(Anomaly(
                "fx.rate", f"{base}/{s.currency}", now, "robust_z", last * (1 + s.pct_change / 100.0), last,
                round(z, 4), {"scenario": True, "window_days": 90, "pct_change": s.pct_change},
            ))  # fmt: skip
    return replace(feeds, events=(*feeds.events, *events), anomalies=(*feeds.anomalies, *anomalies))


def risk_index(assessments: Sequence[Assessment], entities: Sequence[Entity],
               config: EngineConfig = DEFAULT_CONFIG) -> float:  # fmt: skip
    """The What-if *risk index*, 0–100 (risk-engine.md §12).

    Per entity: the worst score in each category, combined across categories
    (``E = 100 × (1 − Π_c (1 − worst_c / 100))``): many risks of one kind do not stack,
    different kinds do. The index is the criticality-weighted mean of ``E`` over *all*
    the workspace's entities (an entity with no risk counts as 0), so it does not grow
    with the number of risks, it moves whenever a scenario adds or removes exposure, and
    with one entity it equals that entity's combined score.
    """
    if not entities:
        return 0.0
    worst: dict[UUID, dict[str, float]] = {}
    for a in assessments:
        per = worst.setdefault(a.entity_id, {})
        per[a.category] = max(per.get(a.category, 0.0), a.score)
    total = weights = 0.0
    for e in entities:
        w = config.criticality_weights.get(e.criticality or "", config.default_criticality_weight)
        prod = 1.0
        for score in worst.get(e.id, {}).values():
            prod *= 1 - score / 100.0
        total += w * 100 * (1 - prod)
        weights += w
    return round(total / weights, 1) if weights else 0.0


def _side(assessments: Sequence[Assessment], entities: Sequence[Entity], config: EngineConfig = DEFAULT_CONFIG) -> dict:
    by_cat = {c: 0 for c in CATEGORIES}
    for a in assessments:
        by_cat[a.category] += 1
    return {
        "risk_count": len(assessments),
        "max_score": max((a.score for a in assessments), default=None),
        "portfolio_score": risk_index(assessments, entities, config),
        "by_category": by_cat,
    }


def compare(baseline: Sequence[Assessment], scenario: Sequence[Assessment], entities: Mapping[UUID, Entity],
            risk_ids: Mapping[str, UUID], now: datetime, *, baseline_entities: Sequence[Entity] | None = None,
            scenario_entities: Sequence[Entity] | None = None, config: EngineConfig = DEFAULT_CONFIG) -> dict:  # fmt: skip
    """``ScenarioResults`` from two assessment lists (and each side's entities, for the index)."""
    b = {a.fingerprint: a for a in baseline}
    s = {a.fingerprint: a for a in scenario}
    changes = []
    for fp in sorted(set(b) | set(s)):
        ba, sa = b.get(fp), s.get(fp)
        bs, ss = (ba.score if ba else None), (sa.score if sa else None)
        delta = round((ss or 0.0) - (bs or 0.0), 1)
        if delta == 0:
            continue
        a = sa or ba
        e = entities.get(a.entity_id)
        rid = risk_ids.get(fp)
        changes.append({
            "fingerprint": fp, "risk_id": str(rid) if rid else None, "entity_id": str(a.entity_id),
            "entity_name": e.name if e else "", "category": a.category, "title": a.title,
            "baseline_score": bs, "scenario_score": ss, "delta": delta, "lat": a.lat, "lon": a.lon,
            "countries": list(a.countries),
        })  # fmt: skip
    changes.sort(key=lambda c: (-abs(c["delta"]), c["fingerprint"]))
    base_ents = list(entities.values()) if baseline_entities is None else baseline_entities
    scen_ents = base_ents if scenario_entities is None else scenario_entities
    return {
        "baseline": _side(baseline, base_ents, config),
        "scenario": _side(scenario, scen_ents, config),
        "changes": changes[:MAX_CHANGES],
        "engine_version": ENGINE_VERSION,
        "as_of": rfc3339(now),
    }


def scenario_results(entities: Sequence[Entity], feeds: FeedSnapshot, inputs: ScenarioInputs, *,
                     base_currency: str | None, risk_ids: Mapping[str, UUID] | None = None,
                     config: EngineConfig = DEFAULT_CONFIG) -> dict:  # fmt: skip
    """The pure part of ``run_scenario`` (no I/O)."""
    known = {e.id for e in entities}
    for o in inputs.entity_overrides:
        if o.entity_id not in known:
            raise ScenarioError("an entity override names an entity not in this workspace")
    changed = apply_overrides(entities, inputs.entity_overrides)
    baseline = assess(entities, feeds, base_currency=base_currency, config=config)
    shocked = shock_feeds(inputs, entities, feeds, base_currency, config)
    scenario = assess(changed, shocked, base_currency=base_currency, config=config)
    by_id = {e.id: e for e in [*entities, *changed]}
    return compare(baseline, scenario, by_id, risk_ids or {}, feeds.now, baseline_entities=entities,
                   scenario_entities=changed, config=config)  # fmt: skip


def run_scenario(conn, workspace_id: UUID, inputs: ScenarioInputs | Mapping[str, Any], *,
                 now: datetime | None = None, config: EngineConfig = DEFAULT_CONFIG) -> dict:  # fmt: skip
    """``ScenarioResults`` for ``POST /v1/scenarios``. Reads only; writes nothing."""
    from engine.db import _cur, base_currency, check_context, load_entities, load_feeds

    check_context(conn, workspace_id)
    if not isinstance(inputs, ScenarioInputs):
        inputs = ScenarioInputs.from_dict(inputs)
    now = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    entities = load_entities(conn)
    changed = apply_overrides(entities, inputs.entity_overrides)
    # Read the feed rows either side could match (overrides may add countries).
    feeds = load_feeds(conn, [*entities, *changed], now=now, config=config)
    base = base_currency(conn)
    result = scenario_results(entities, feeds, inputs, base_currency=base, config=config)
    fps = [c["fingerprint"] for c in result["changes"]]
    if fps:
        ids = dict(
            (fp, rid) for rid, fp in _cur(conn).execute(
                "SELECT id, fingerprint FROM biz.risks WHERE workspace_id = %s AND fingerprint = ANY(%s::text[])",
                (workspace_id, fps),
            )
        )  # fmt: skip
        for c in result["changes"]:
            c["risk_id"] = str(ids[c["fingerprint"]]) if c["fingerprint"] in ids else None
    return result
