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
# What a tagged news report is about, as a noun phrase. Titles say "<Topic> news …" and
# sentences "news reports about <topic>", so "news" is added exactly once.
TAG_TOPICS = {
    "conflict": "conflict",
    "protest": "protests",
    "sanctions": "sanctions",
    "trade_policy": "trade policy",
    "port_shipping_disruption": "shipping disruption",
    "natural_hazard": "natural hazards",
    "cyber_incident": "cyber incidents",
    "labour_action": "labour action",
    "financial_stress": "financial stress",
    "other": None,
}
# The same topics in the singular, for titles ("Protest news", "Natural hazard news").
TITLE_TOPICS = {"protest": "protest", "natural_hazard": "natural hazard", "cyber_incident": "cyber incident"}
CATEGORY_TOPICS = {
    "natural_hazard": "natural hazards",
    "logistics": "shipping disruption",
    "geopolitical": "conflict",
    "sanctions": "sanctions",
    "trade_policy": "trade policy",
    "cyber": "cyber incidents",
    "financial": "financial stress",
    "labour": "labour action",
    "other": None,
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


def news_topic(tag_type: str | None, category: str) -> str | None:
    return TAG_TOPICS[tag_type] if tag_type in TAG_TOPICS else CATEGORY_TOPICS.get(category)


def news_label(tag_type: str | None, category: str) -> str:
    """``Sanctions news``, ``Conflict news``, ``News``: the title prefix for a news item."""
    t = TITLE_TOPICS.get(tag_type or "") or news_topic(tag_type, category)
    if t == "natural hazards":
        t = "natural hazard"
    return f"{t[:1].upper()}{t[1:]} news" if t else "News"


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
    label = news_label(tag_type, category)
    lane = item.detail.get("inferred_from") is not None
    on = f"on the shipping lane from {name}" if lane else f"on route {name}"

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
            return _cut(f"{h} {_where(mt, mv)} {on}")
        return _cut(f"{h} {_where(mt, mv)} affecting {name}")
    if ev is not None and ev.type == "port.disruption":
        if mt == "geo_radius":
            return _cut(f"Port disruption near {name}")
        if mt == "chokepoint":
            return _cut(f"Port disruption {_where(mt, mv)} {on}")
        return _cut(f"Port disruption {_where(mt, mv)} affecting {name}")
    if ev is not None and ev.source == "scenario":
        label = ev.attrs.get("label") or CATEGORY_LABELS[category]
        return _cut(f"Scenario: {label} affecting {name}")
    if mt == "chokepoint":
        return _cut(f"{label} {_where(mt, mv)} {on}")
    if mt == "company_name":
        return _cut(f"{label} mentions {name}")
    if mt == "commodity":
        what = item.detail.get("matched_text") or mv
        if ev is not None and ev.type.startswith("market."):
            return _cut(f"Market move in {what} affecting {name}")
        return _cut(f"{label} on {what} affecting {name}")
    return _cut(f"{label} {_where(mt, mv)} affecting {name}")




# --- the one-sentence explanation (§3) and "How this was scored" (factors.explain) ----------


def _plural(n: int, one: str, many: str | None = None) -> str:
    return f"{n} {one if n == 1 else (many or one + 's')}"


def _a(word: str) -> str:
    return f"{'an' if word[:1].lower() in 'aeiou' else 'a'} {word}"


def _the(name: str) -> str:
    return name if name.lower().startswith("the ") else f"the {name}"


def _choke_name(cid: str) -> str:
    c = chokepoint_by_id(cid)
    return c.name if c else cid


def _product(entity: Entity) -> str | None:
    """The entity's own product as written in its sheet, in running text ("printed circuit boards")."""
    p = next((c.strip() for c in entity.commodities if c.strip()), None)
    if p and p[:1].isupper() and (len(p) == 1 or p[1:2].islower()):
        p = p[:1].lower() + p[1:]
    return p


def _lane(item, name: str) -> str:
    """Which shipments pass a chokepoint: a declared route, or a lane inferred from the port."""
    via = item.detail.get("inferred_from")
    if via is None:
        return f"which route {name} passes"
    if via.startswith("port "):
        return f"which shipments from {name} likely pass (inferred from the port {via[5:]})"
    if via.startswith("country "):
        return f"which shipments from {name} likely pass (inferred from {country_name(via[8:])})"
    return f"which is next to {name}"


def _support_clause(entity: Entity, item) -> str | None:
    sup = item.detail.get("supported_by")
    if not sup:
        return None
    text = sup.get("matched_text") or sup.get("match_value") or ""
    hs = sup.get("match_value", "")
    if not hs.startswith("HS "):
        return f"{text}, which {entity.name} supplies"  # the entity's own product name
    # Matched on the entity's HS heading (or a 2-digit chapter): name the entity's own product
    # and never claim the article is about it when it names another one (1.3.0).
    product = _product(entity)
    if product is None:
        return f"{text}, which is in the product group ({hs}) that {entity.name} supplies"
    if text.casefold() == product.casefold():
        return f"{product}, which {entity.name} supplies ({hs})"
    return f"{text}, which is in the same product group ({hs}) as the {product} {entity.name} supplies"


def _news_sentence(category: str, entity: Entity, items, top) -> str:
    name, mt, mv = entity.name, top.match_type, top.match_value
    n = sum(1 for i in items if i.event is not None and i.event.tag is not None) or 1
    topic = news_topic(top.event.tag.event_type if top.event.tag else None, category)
    about = f" about {topic}" if topic else ""
    reports = "A recent news report" if n == 1 else f"{n} recent news reports"
    verb = "mentions" if n == 1 else "mention"
    if mt == "country":
        where = country_name(mv)
        sup = _support_clause(entity, top)
        # 1.3.0: a sanctions country match means the measure is aimed at the country (§5.10).
        aimed = category == "sanctions" and topic == "sanctions"
        lead = f"{reports} {'describes' if n == 1 else 'describe'} sanctions aimed at {where}" if aimed else None
        if sup:
            k = sum(1 for i in items if i.detail.get("supported_by"))
            if lead and (n == 1 or k >= n):
                return f"{lead}, and {'it mentions' if n == 1 else 'they mention'} {sup}."
            if lead:
                return f"{lead}, and {'one' if k == 1 else k} {'mentions' if k == 1 else 'mention'} {sup}."
            if n == 1 or k >= n:
                return f"{reports}{about} involving {where} {verb} {sup}."
            return (f"{reports}{about} involve {where}, and {'one' if k == 1 else k} "
                    f"{'mentions' if k == 1 else 'mention'} {sup}.")  # fmt: skip
        if lead:
            return f"{lead}, where {name} is based."
        return f"{reports}{about} {verb} {where}, where {name} is based."
    if mt == "company_name":
        return f"{reports}{about} {verb} {name} by name."
    if mt == "chokepoint":
        return f"{reports}{about} {verb} {_the(_choke_name(mv))}, {_lane(top, name)}."
    if mt == "commodity":
        what = top.detail.get("matched_text") or mv
        return f"{reports}{about} {verb} {what}, which {name} covers."
    if mt == "port":
        return f"{reports}{about} {verb} the port {mv}, which {name} uses."
    return f"{reports}{about} {verb} {mv}, which matches {name}."


def _event_sentence(category: str, entity: Entity, items, top) -> str:
    name, mt, mv, ev = entity.name, top.match_type, top.match_value, top.event
    n = len(items)
    if ev.type.startswith("hazard."):
        what, many = hazard_label(ev.type).lower(), "natural hazard events"
    elif ev.type == "port.disruption":
        what, many = "port disruption", "port disruptions"
    else:
        what, many = CATEGORY_LABELS[category].lower(), f"{CATEGORY_LABELS[category].lower()} events"
    one = f"{_a(what)[:1].upper()}{_a(what)[1:]}"
    if mt == "geo_radius":
        d = top.detail.get("distance_km")
        far = f"{d:.0f} km from {name}" if isinstance(d, (int, float)) else f"near {name}"
        if n == 1:
            return f"{one} was recorded {far}."
        if isinstance(d, (int, float)):
            return f"{n} {many} were recorded near {name}; the strongest, {_a(what)}, was {d:.0f} km away."
        return f"{n} {many} were recorded near {name}."
    lead = f"{one} was reported" if n == 1 else f"{n} {many} were reported"
    if mt == "country":
        return f"{lead} in {country_name(mv)}, where {name} is based."
    if mt == "port":
        return f"{lead} at the port {mv}, which {name} uses."
    if mt == "chokepoint":
        return f"{lead} near {_the(_choke_name(mv))}, {_lane(top, name)}."
    return f"{lead} matching {name}."


def _scenario_sentence(entity: Entity, top) -> str:
    name, mt, mv, ev = entity.name, top.match_type, top.match_value, top.event
    label = ev.attrs.get("label") or ev.title or "the scenario event"
    if ev.type == "sanction.designation":
        return f"In this what-if, {name} is designated on a sanctions list."
    if mt == "country":
        via = f"through {country_name(mv)}, where it is based"
    elif mt == "port":
        via = f"through the port {mv}"
    elif mt == "chokepoint":
        v = top.detail.get("inferred_from")
        via = f"through {_the(_choke_name(mv))}" + (
            " on its route" if v is None else f", inferred from the {'port ' + v[5:] if v.startswith('port ') else 'location'}"
        )
    elif mt == "geo_radius":
        d = top.detail.get("distance_km")
        via = f"{d:.0f} km away" if isinstance(d, (int, float)) else "nearby"
    else:
        via = f"on {mv}"
    return f"In this what-if, “{label}” reaches {name} {via}."


def _anomaly_sentence(entity: Entity, items, top) -> str:
    a, name, mt = top.anomaly, entity.name, top.match_type
    if a.series == "fx.rate":
        base, _, quote = a.key.partition("/")
        lead = f"The {quote} moved unusually sharply against the {base}"
        if a.attrs.get("scenario"):
            lead = f"In this what-if, the {quote} moves sharply against the {base}"
        if entity.kind == "supplier":
            return f"{lead}, and you pay {name} in {quote}."
        if entity.kind == "customer":
            return f"{lead}, and {name} pays you in {quote}."
        return f"{lead}."
    if mt == "company_name":
        return f"News volume about {name} is unusually high."
    label = series_label(a.series)
    window = a.attrs.get("window_days")
    hist = f"its {window}-day history" if window else "its history"
    return f"{label[:1].upper()}{label[1:]} in {country_name(top.match_value)}, where {name} is based, is unusual against {hist}."


def explanation(category: str, entity: Entity, items) -> str:
    """One plain sentence: what reached the entity and through what. The numbers are in
    ``factors.explain`` (``how_scored``)."""
    top = items[0]
    name, mt, ev = entity.name, top.match_type, top.event
    if top.anomaly is not None:
        return _anomaly_sentence(entity, items, top)
    if ev.source == "scenario":
        return _scenario_sentence(entity, top)
    if mt == "sanctions_name":
        listed = top.detail.get("matched_name") or top.match_value
        how = "an alias" if top.detail.get("matched_on") == "alias" else "a name"
        return f"{name} closely matches “{listed}”, {how} on a US sanctions list."
    if mt == "software":
        product = (top.detail.get("software_display") or top.match_value).replace(":", " ")
        exploited = any(i.event is not None and (i.event.source == "cisa_kev" or i.event.type == "cyber.exploited_vulnerability")
                        for i in items)  # fmt: skip
        if len(items) == 1:
            kind = "known exploited vulnerability" if exploited else "published vulnerability"
            return f"{_a(kind)[:1].upper()}{_a(kind)[1:]} affects {product}, which is in your software list."
        tail = ", and at least one is known to be exploited" if exploited else ""
        return f"{len(items)} published vulnerabilities affect {product}, which is in your software list{tail}."
    if ev.tag is not None and not ev.type.startswith(("hazard.", "cyber.")) and ev.type != "port.disruption":
        return _news_sentence(category, entity, items, top)
    return _event_sentence(category, entity, items, top)


MATCH_LABELS = {
    "country": "country", "geo_radius": "distance", "port": "port", "chokepoint": "chokepoint",
    "currency": "currency", "commodity": "commodity", "software": "software",
    "sanctions_name": "sanctions name", "company_name": "company name",
}  # fmt: skip


def _f3(x: float) -> str:
    return f"{x:.3f}"


def how_scored(category: str, entity: Entity, items, likelihood: float, impact_factors: dict, clamped: bool,
               uncapped: float | None, config) -> dict[str, Any]:  # fmt: skip
    """The numbers behind the score, for the collapsed "How this was scored" section."""
    top = items[0]
    snap = top.snapshot
    shown = top.match_value
    if top.match_type == "country":
        shown = f"{country_name(top.match_value)} ({top.match_value})"
    elif top.match_type == "chokepoint":
        shown = _choke_name(top.match_value)
    sup = top.detail.get("supported_by")
    steps: list[dict[str, Any]] = []
    if sup:
        w = config.support_weight
        steps.append({
            "label": "Match",
            "formula": f"{MATCH_LABELS[top.match_type]} {_f3(sup['anchor_confidence'])}, strengthened by "
                       f"{sup.get('matched_text') or sup['match_value']} ({sup['match_value']}) {_f3(sup['confidence'])}: "
                       f"1 − (1 − {_f3(sup['anchor_confidence'])}) × (1 − {w} × {_f3(sup['confidence'])}) = {_f3(top.confidence)}",
            "value": top.confidence,
        })  # fmt: skip
    steps.append({
        "label": "Strongest signal",
        "formula": f"signal {_f3(top.signal)} × recency {_f3(top.recency)} × match {_f3(top.confidence)} = {_f3(top.p)}",
        "value": top.p,
    })
    ps = [i.p for i in items]
    d = config.likelihood_decay

    def term(k: int, p: float) -> str:
        w = d**k
        return f"(1 − {_f3(p)})" if w == 1 else f"(1 − {w:g} × {_f3(p)})"

    terms = " × ".join(term(k, p) for k, p in enumerate(ps[:5])) + (f" × … ({len(ps) - 5} more)" if len(ps) > 5 else "")
    raw_l = uncapped if uncapped is not None else likelihood
    weighted = f", each further item counting {d:g}× the one before" if len(ps) > 1 and d != 1 else ""
    steps.append({
        "label": "Likelihood",
        "formula": f"noisy-OR of {_plural(len(ps), 'item')}{weighted}: 1 − {terms} = {_f3(raw_l)}"
                   + (f", capped at {config.likelihood_cap}" if uncapped is None and raw_l >= config.likelihood_cap else ""),
        "value": raw_l,
    })  # fmt: skip
    if uncapped is not None:
        steps.append({
            "label": "Market-only cap",
            "formula": f"a market move with no other match is capped at score {config.market_only_score_cap:g}: "
                       f"likelihood {_f3(uncapped)} → {_f3(likelihood)}",
            "value": likelihood,
        })  # fmt: skip
    f = impact_factors
    crit = f["criticality"] or "not set"
    if f["exposure_share"] is None:
        base = f"criticality {f['criticality_weight']} ({crit}; exposure unknown)"
    else:
        base = (f"(0.5 × criticality {f['criticality_weight']} ({crit}) + 0.5 × exposure {f['exposure_norm']} "
                f"(share {f['exposure_share'] * 100:.1f} %))")  # fmt: skip
    steps.append({
        "label": "Impact",
        "formula": f"{f['category_multiplier']} ({category.replace('_', ' ')}) × {base} = {_f3(f['value'])}"
                   + (", kept within 0.05–1" if clamped else ""),
        "value": f["value"],
    })  # fmt: skip
    score = round(100 * likelihood * f["value"], 1)
    steps.append({"label": "Score", "formula": f"100 × {_f3(likelihood)} × {_f3(f['value'])} = {score:.1f}", "value": score})
    src = top.event.source if top.event is not None else f"{top.anomaly.series} {top.anomaly.method}"
    return {
        "matched": {
            "match_type": top.match_type,
            "match_value": top.match_value,
            "label": f"{MATCH_LABELS[top.match_type]}: {shown}",
            "matched_text": top.detail.get("matched_text") or top.detail.get("matched_name"),
            "inferred_from": top.detail.get("inferred_from"),
            "supported_by": None if not sup else {
                "match_type": sup["match_type"], "match_value": sup["match_value"],
                "matched_text": sup.get("matched_text"), "confidence": sup["confidence"],
            },
        },
        "strongest": {
            "evidence_key": top.key,
            "title": snap.get("title", ""),
            "source": src,
            "published_at": snap.get("published_at"),
        },
        "item_count": len(items),
        "steps": steps,
    }
