"""Deterministic titles, explanations and evidence snapshots (no AI)."""

from __future__ import annotations

from functools import lru_cache
from typing import Any

from engine.models import Anomaly, Entity, FeedEvent, rfc3339
from engine.reference import chokepoint_by_id

MAX_TITLE = 300

HAZARD_LABELS = {
    "earthquake": "Earthquake",
    "tropical_cyclone": "Tropical cyclone",
    "flood": "Flood",
    "volcano": "Volcanic activity",
    "drought": "Drought",
    "wildfire": "Wildfire",
    "tsunami": "Tsunami",
    "severe_storm": "Severe storm",
    "landslide": "Landslide",
    "temperature_extreme": "Extreme temperature",
}
TAG_LABELS = {
    "conflict": "Conflict",
    "protest": "Protests",
    "sanctions": "Sanctions news",
    "trade_policy": "Trade policy change",
    "port_shipping_disruption": "Shipping disruption",
    "natural_hazard": "Natural hazard",
    "cyber_incident": "Cyber incident",
    "labour_action": "Labour action",
    "financial_stress": "Financial stress",
    "other": "News",
}
CATEGORY_LABELS = {
    "natural_hazard": "Natural hazard",
    "logistics": "Logistics disruption",
    "geopolitical": "Geopolitical unrest",
    "sanctions": "Sanctions",
    "trade_policy": "Trade policy change",
    "cyber": "Cyber threat",
    "financial": "Financial stress",
    "labour": "Labour action",
    "other": "News",
}
SERIES_LABELS = {
    "portwatch.port_calls": "port calls",
    "portwatch.import_volume": "import volume",
    "portwatch.export_volume": "export volume",
    "wb.FP.CPI.TOTL.ZG": "inflation",
    "wb.NY.GDP.MKTP.KD.ZG": "GDP growth",
    "wb.NE.TRD.GNFS.ZS": "trade share of GDP",
    "wb.PA.NUS.FCRF": "official exchange rate",
    "wb.LP.LPI.OVRL.XQ": "logistics performance",
    "wb.NV.IND.MANF.ZS": "manufacturing share of GDP",
    "gdelt.articles": "news volume",
}
SERIES_UNITS = {
    "portwatch.port_calls": "calls",
    "portwatch.import_volume": "metric tonnes",
    "portwatch.export_volume": "metric tonnes",
    "wb.FP.CPI.TOTL.ZG": "% annual",
    "wb.NY.GDP.MKTP.KD.ZG": "% annual",
    "wb.NE.TRD.GNFS.ZS": "% of GDP",
    "wb.PA.NUS.FCRF": "LCU per USD",
    "wb.LP.LPI.OVRL.XQ": "score 1-5",
    "wb.NV.IND.MANF.ZS": "% of GDP",
    "gdelt.articles": "articles per day",
}


@lru_cache(maxsize=512)
def country_name(code: str) -> str:
    try:
        import pycountry

        c = pycountry.countries.get(alpha_3=code)
    except Exception:  # pragma: no cover - pycountry is a dependency of jobs.codes
        c = None
    return (getattr(c, "common_name", None) or c.name) if c else code


def _cut(s: str, n: int = MAX_TITLE) -> str:
    s = " ".join(s.split())
    return s if len(s) <= n else s[: n - 1].rstrip() + "…"


def hazard_label(type_: str) -> str:
    h = type_.removeprefix("hazard.")
    return HAZARD_LABELS.get(h, h.replace("_", " ").capitalize() or "Natural hazard")


def series_label(series: str) -> str:
    if series in SERIES_LABELS:
        return SERIES_LABELS[series]
    return series.removeprefix("wb.").removeprefix("portwatch.").replace("_", " ")


def series_unit(series: str, key: str) -> str | None:
    if series == "fx.rate" and "/" in key:
        base, quote = key.split("/", 1)
        return f"{quote} per {base}"
    return SERIES_UNITS.get(series)


def anomaly_subject(a: Anomaly) -> str:
    if a.series == "fx.rate":
        return a.key
    if a.series == "gdelt.articles":
        return f"News volume for {a.key}"
    label = series_label(a.series)
    return f"{label[:1].upper()}{label[1:]} in {country_name(a.key)}"


def anomaly_title(a: Anomaly) -> str:
    window = a.attrs.get("window_days")
    norm = f"its {window}-day norm" if window else "its norm"
    if a.method == "robust_z":
        return _cut(f"{anomaly_subject(a)} moved {abs(a.score):.1f} robust z from {norm}")
    hist = f"its {window}-day history" if window else "its history"
    return _cut(f"{anomaly_subject(a)} was unusual against {hist} (isolation score {a.score:.2f})")


def event_snapshot(ev: FeedEvent, detail: dict[str, Any]) -> dict[str, Any]:
    snap = {
        "title": ev.title or f"{ev.source} {ev.raw_id}",
        "url": ev.url,
        "type": ev.type,
        "published_at": rfc3339(ev.published_at or ev.first_seen_at),
        "severity": None if ev.severity is None else round(float(ev.severity), 3),
        "lat": ev.lat,
        "lon": ev.lon,
        "countries": list(ev.countries),
        "ports": list(ev.ports),
        "value": None,
        "baseline": None,
        "score": None,
        "unit": None,
    }
    snap.update(detail)
    return snap


def anomaly_snapshot(a: Anomaly, detail: dict[str, Any]) -> dict[str, Any]:
    snap = {
        "title": anomaly_title(a),
        "url": None,
        "type": a.series,
        "published_at": rfc3339(a.ts),
        "severity": None,
        "lat": None,
        "lon": None,
        "countries": [a.key] if (a.series.startswith(("wb.", "portwatch."))) else [],
        "ports": [],
        "value": a.value,
        "baseline": None if a.baseline != a.baseline else a.baseline,  # NaN → null
        "score": a.score,
        "unit": series_unit(a.series, a.key),
    }
    snap.update(detail)
    return snap


def _where(match_type: str, match_value: str) -> str:
    """``in Japan`` / ``at port NLRTM`` / ``near Suez Canal`` / ``on <value>``."""
    if match_type == "country":
        return f"in {country_name(match_value)}"
    if match_type == "port":
        return f"at port {match_value}"
    if match_type == "chokepoint":
        c = chokepoint_by_id(match_value)
        return f"near {c.name if c else match_value}"
    return f"on {match_value}"


def title(category: str, entity: Entity, item) -> str:
    """Title from the risk's strongest item (``item`` is an ``engine.core._Item``)."""
    name = entity.name
    mt, mv = item.match_type, item.match_value
    ev: FeedEvent | None = item.event
    a: Anomaly | None = item.anomaly
    tag_type = ev.tag.event_type if ev is not None and ev.tag is not None else None
    news_label = TAG_LABELS.get(tag_type or "", CATEGORY_LABELS[category])

    if category == "sanctions" and mt == "sanctions_name":
        verb = {"sanction.update": "update", "sanction.listed": "listing"}.get(ev.type if ev else "", "designation")
        return _cut(f"Sanctions {verb} may match {name}")
    if category == "cyber" and mt == "software":
        product = item.detail.get("software_display") or mv
        product = product.replace(":", " ")
        exploited = ev is not None and (ev.source == "cisa_kev" or ev.type == "cyber.exploited_vulnerability")
        return _cut(f"{'Exploited vulnerability' if exploited else 'Vulnerability'} in {product}")
    if category == "financial" and mt == "currency" and a is not None:
        base, _, quote = a.key.partition("/")
        return _cut(f"{quote} moved sharply against {base} ({name})" if quote else f"{a.key} moved sharply ({name})")
    if a is not None and mt == "country":
        return _cut(f"Unusual {series_label(a.series)} in {country_name(mv)} affecting {name}")
    if a is not None and mt == "company_name":
        return _cut(f"Unusual news volume about {name}")
    if ev is not None and ev.type.startswith("hazard."):
        h = hazard_label(ev.type)
        if mt == "geo_radius":
            return _cut(f"{h} near {name}")
        if mt == "chokepoint":
            return _cut(f"{h} {_where(mt, mv)} on route {name}")
        return _cut(f"{h} {_where(mt, mv)} affecting {name}")
    if ev is not None and ev.type == "port.disruption":
        if mt == "geo_radius":
            return _cut(f"Port disruption near {name}")
        if mt == "chokepoint":
            return _cut(f"Port disruption {_where(mt, mv)} on route {name}")
        return _cut(f"Port disruption {_where(mt, mv)} affecting {name}")
    if ev is not None and ev.source == "scenario":
        label = ev.attrs.get("label") or CATEGORY_LABELS[category]
        return _cut(f"Scenario: {label} affecting {name}")
    if mt == "chokepoint":
        return _cut(f"{news_label} {_where(mt, mv)} on route {name}")
    if mt == "company_name":
        return _cut(f"{news_label} news mentions {name}" if tag_type != "other" else f"News mentions {name}")
    if mt == "commodity":
        what = item.detail.get("matched_text") or mv
        return _cut(f"{news_label} news on {what} affecting {name}")
    return _cut(f"{news_label} {_where(mt, mv)} affecting {name}")


def explanation(category: str, entity: Entity, items, likelihood: float, impact_factors: dict, clamped: bool) -> str:
    top = items[0]
    snap = top.snapshot
    when = (snap.get("published_at") or "")[:10]
    src = top.event.source if top.event is not None else f"{top.anomaly.series} {top.anomaly.method}"
    shown = top.match_value if top.match_type != "country" else f"{top.match_value} ({country_name(top.match_value)})"
    parts = [
        f"{len(items)} item(s) matched {entity.kind} \"{entity.name}\" on {top.match_type} {shown.rstrip('.')}.",
        f"Strongest: \"{snap.get('title', '')}\" ({src}{', ' + when if when else ''}): "
        f"signal {top.signal:.3f} × recency {top.recency:.3f} × match {top.confidence:.3f} = {top.p:.3f}.",
        f"Likelihood {likelihood:.3f} is the noisy-OR of {len(items)} item(s).",
    ]
    f = impact_factors
    crit = f["criticality"] or "not set"
    if f["exposure_share"] is None:
        base = f"criticality weight {f['criticality_weight']} ({crit}; exposure unknown)"
    else:
        base = (
            f"(0.5 × criticality weight {f['criticality_weight']} ({crit}) + 0.5 × exposure {f['exposure_norm']} "
            f"(share {f['exposure_share'] * 100:.1f} %))"
        )
    parts.append(
        f"Impact {f['value']:.3f} = {f['category_multiplier']} ({category}) × {base}"
        + (", clamped to 0.05–1." if clamped else ".")
    )
    return " ".join(parts)
