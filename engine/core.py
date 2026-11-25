"""``assess``: entities + a feed snapshot → assessments. Pure and deterministic.

No I/O, no clock, no randomness: the same inputs give the same assessments in
the same order (risk-engine.md §10). The rules are in risk-engine.md §4–§6;
``engine/README.md`` explains each one with an example.

Per (entity, evidence item) only the *best* match is kept (highest
contribution, then confidence, then the precedence below), so one quake near a
plant that is also in the quake's country feeds one risk, not two.
"""

from __future__ import annotations

import hashlib
import math
from collections import defaultdict
from functools import lru_cache
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Iterable, Mapping, Sequence
from uuid import UUID

from engine import names, scoring as S, software as sw, texts
from engine.config import DEFAULT_CONFIG, EngineConfig
from engine.geo import EARTH_RADIUS_KM, geo_confidence, haversine_km, radius_km
from engine.models import Anomaly, Assessment, Entity, Evidence, FeedEvent, FeedSnapshot
from engine.reference import chokepoint_by_alias, chokepoint_id, chokepoints, hs_chapter_keywords, keyword_in

ENGINE_VERSION = "1.0.0"

# §5.1: which keys each entity kind is matched on.
KIND_KEYS: Mapping[str, frozenset[str]] = {
    "supplier": frozenset({"geo", "country", "port", "name", "commodity", "currency"}),
    "site": frozenset({"geo", "country", "port"}),
    "customer": frozenset({"country", "name", "currency"}),
    "route": frozenset({"port", "country", "chokepoint"}),
    "port": frozenset({"port", "geo", "country"}),
    "currency": frozenset({"currency"}),
    "commodity": frozenset({"commodity"}),
    "software": frozenset({"software"}),
}
# Tie-break between matches of one item to one entity (lower wins).
PRECEDENCE = {
    "port": 0, "geo_radius": 1, "chokepoint": 2, "software": 3, "sanctions_name": 4,
    "company_name": 5, "currency": 6, "commodity": 7, "country": 8,
}  # fmt: skip
GEO_CATEGORIES = {"natural_hazard", "logistics"}
MAX_FINGERPRINT = 200


def has_key(entity: Entity, key: str) -> bool:
    return key in KIND_KEYS.get(entity.kind, frozenset())


def watch_term(entity: Entity, config: EngineConfig = DEFAULT_CONFIG) -> str | None:
    """The GDELT query for a supplier/customer name (§7), also the ``gdelt.articles`` key it produces."""
    n = names.normalise_name(entity.name, config.legal_forms)
    if len(n) < config.watch_name_min_length:
        return None
    return f'"{n}"'


def fingerprint(entity_id: UUID, category: str, driver: str) -> str:
    fp = f"{entity_id}|{category}|{driver}"
    if len(fp) <= MAX_FINGERPRINT:
        return fp
    mt, _, mv = driver.partition(":")
    return f"{entity_id}|{category}|{mt}:sha256:{hashlib.sha256(mv.encode()).hexdigest()[:32]}"


# --- internal records ------------------------------------------------------------------


@dataclass(slots=True)
class _Match:
    match_type: str
    match_value: str
    confidence: float
    driver: str
    category: str | None = None  # overrides the event's category (chokepoint → logistics)
    detail: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class _Item:
    entity: Entity
    category: str
    match_type: str
    match_value: str
    driver: str
    confidence: float
    signal: float
    recency: float
    p: float
    key: str
    event: FeedEvent | None
    anomaly: Anomaly | None
    detail: dict[str, Any]
    snapshot: dict[str, Any] = field(default_factory=dict)

    def rank(self) -> tuple:
        return (self.p, self.confidence, -PRECEDENCE[self.match_type], self.match_value)


class _Index:
    """Entities by match key, so each feed item is compared only with entities it can match."""

    def __init__(self, entities: Sequence[Entity], config: EngineConfig):
        self.entities = entities
        self.by_country: dict[str, list[Entity]] = defaultdict(list)
        self.by_port: dict[str, list[Entity]] = defaultdict(list)
        self.by_choke: dict[str, list[Entity]] = defaultdict(list)
        self.by_osv: dict[str, list[Entity]] = defaultdict(list)
        self.by_cpe: dict[str, list[Entity]] = defaultdict(list)
        self.by_currency: dict[str, list[Entity]] = defaultdict(list)
        self.by_term: dict[str, list[Entity]] = defaultdict(list)
        self.commodity: list[tuple[Entity, set[str], set[str]]] = []
        self.named = names.NameIndex(config.legal_forms, config.name_min_length)
        self.positioned: list[Entity] = []
        for e in entities:
            if has_key(e, "country"):
                for c in e.countries:
                    self.by_country[c].append(e)
            if has_key(e, "port"):
                for p in e.ports:
                    self.by_port[p].append(e)
            if has_key(e, "chokepoint"):
                raw = e.attrs.get("chokepoints") if isinstance(e.attrs, Mapping) else None
                for cid in dict.fromkeys(chokepoint_id(v) for v in (raw if isinstance(raw, list) else [])):
                    if cid:
                        self.by_choke[cid].append(e)
            if has_key(e, "software"):
                for kind, key, _ in sw.entity_keys(e.software):
                    (self.by_osv if kind == "osv" else self.by_cpe)[key].append(e)
            if has_key(e, "currency"):
                for c in e.currencies:
                    self.by_currency[c].append(e)
            if has_key(e, "commodity") and (e.hs_codes or e.commodities):
                self.commodity.append((e, {h[:2] for h in e.hs_codes}, {c.casefold() for c in e.commodities}))
            if has_key(e, "name"):
                self.named.add(e.name, e)
                term = watch_term(e, config)
                if term:
                    self.by_term[term].append(e)
            if has_key(e, "geo") and e.has_position:
                self.positioned.append(e)
        self.pos_lat = [math.radians(e.lat) for e in self.positioned]
        self.pos_lon = [math.radians(e.lon) for e in self.positioned]

    def near(self, lat: float, lon: float, radius: float) -> list[tuple[Entity, float]]:
        """Positioned entities within ``radius`` km of a point, with their distances."""
        if not self.positioned:
            return []
        import numpy as np

        la, lo = np.asarray(self.pos_lat), np.asarray(self.pos_lon)
        p1, l1 = math.radians(lat), math.radians(lon)
        a = np.sin((la - p1) / 2) ** 2 + math.cos(p1) * np.cos(la) * np.sin((lo - l1) / 2) ** 2
        d = 2 * EARTH_RADIUS_KM * np.arcsin(np.minimum(1.0, np.sqrt(a)))
        return [(self.positioned[i], float(d[i])) for i in np.flatnonzero(d <= radius)]


def _event_class(ev: FeedEvent) -> str:
    if ev.type in S.SANCTION_POSITIVE:
        return "sanctions"
    if ev.source == "scenario":
        return "scenario"
    if ev.type.startswith("cyber."):
        return "cve"
    if ev.type.startswith("hazard."):
        return "hazard"
    if ev.type == "port.disruption":
        return "port"
    return "news"


def _tag_list(ev: FeedEvent, name: str) -> list[dict]:
    if ev.tag is None or not isinstance(ev.tag.entities, Mapping):
        return []
    v = ev.tag.entities.get(name)
    return [x for x in v if isinstance(x, dict) and isinstance(x.get("text"), str)] if isinstance(v, list) else []


def _conf(x: dict) -> float:
    try:
        return S.clamp(float(x.get("confidence", 0.0)), 0.0, 1.0)
    except (TypeError, ValueError):
        return 0.0


class _Assessor:
    def __init__(self, entities: Sequence[Entity], feeds: FeedSnapshot, base_currency: str | None, config: EngineConfig):
        self.cfg = config
        self.now = feeds.now
        self.feeds = feeds
        self.base = base_currency or "EUR"
        self.entities = sorted(entities, key=lambda e: str(e.id))
        self.idx = _Index(self.entities, config)
        self.best: dict[tuple[UUID, str], _Item] = {}

    # --- offering a match -----------------------------------------------------------------

    def _offer(self, entity: Entity, m: _Match, *, category: str, signal: float, rec: float,
               key: str, event: FeedEvent | None = None, anomaly: Anomaly | None = None) -> None:  # fmt: skip
        c = S.r3(S.clamp(m.confidence, 0.0, 1.0))
        s, r = S.r3(signal), S.r3(rec)
        item = _Item(entity, category, m.match_type, m.match_value, m.driver, c, s, r,
                     S.contribution(s, r, c), key, event, anomaly, m.detail)  # fmt: skip
        k = (entity.id, key)
        cur = self.best.get(k)
        if cur is None or item.rank() > cur.rank():
            self.best[k] = item

    def _offer_event(self, ev: FeedEvent, entity: Entity, m: _Match, base_category: str, signal: float) -> None:
        category = m.category or base_category
        if category == "other" and m.match_type != "company_name":
            return  # tag `other` only counts through a company-name match (§4)
        if S.is_standing_listing(ev):
            rec = 1.0
        else:
            hl = self.cfg.half_life(S.half_life_key(category, source=ev.source, type_=ev.type))
            rec = S.recency(S.event_age_hours(ev, self.now), hl, self.cfg)
            if rec is None:
                return
        self._offer(entity, m, category=category, signal=signal, rec=rec, key=ev.key, event=ev)

    # --- events ---------------------------------------------------------------------------

    def epss_for(self, ev: FeedEvent) -> float:
        cves = [ev.raw_id] if ev.source == "cisa_kev" else []
        aliases = ev.attrs.get("aliases")
        if isinstance(aliases, list):
            cves += [a for a in aliases if isinstance(a, str) and a.startswith("CVE-")]
        if isinstance(ev.attrs.get("cve"), str):
            cves.append(ev.attrs["cve"])
        best = 0.0
        for cve in cves:
            pts = self.feeds.series.get(("epss.score", cve))
            if pts:
                best = max(best, float(pts[-1][1]))
        return best

    def run_events(self) -> None:
        cfg = self.cfg
        removed = self._removals()
        sanctions: list[tuple[FeedEvent, float]] = []
        for ev in sorted(self.feeds.events, key=lambda e: e.key):
            cat = S.event_category(ev)
            if cat is None:
                continue
            cls = _event_class(ev)
            signal = S.event_signal(ev, cfg, self.epss_for(ev) if cls == "cve" else 0.0)
            if signal is None:
                continue
            if cls == "sanctions":
                eid = ev.attrs.get("entity_id")
                when = ev.published_at or ev.first_seen_at
                if eid is not None and str(eid) in removed and (when is None or when <= removed[str(eid)]):
                    continue  # a later sanction.removal for the same OFAC entity id
                sanctions.append((ev, signal))
                continue
            for entity, m in self._event_matches(ev, cat, cls):
                self._offer_event(ev, entity, m, cat, signal)
        self._sanctions(sanctions)

    def _removals(self) -> dict[str, datetime]:
        out: dict[str, datetime] = {}
        for ev in self.feeds.events:
            if ev.type == "sanction.removal" and ev.attrs.get("entity_id") is not None:
                when = ev.published_at or ev.first_seen_at or self.now
                k = str(ev.attrs["entity_id"])
                out[k] = max(out.get(k, when), when)
        return out

    def _event_matches(self, ev: FeedEvent, cat: str, cls: str) -> Iterable[tuple[Entity, _Match]]:
        cfg, idx = self.cfg, self.idx
        if cls == "cve":
            yield from self._software(ev)
            return
        news = cls == "news"
        if news and cat == "other":
            yield from self._companies(ev)
            return
        # ports
        if not news:
            for p in ev.ports:
                for e in idx.by_port.get(p, ()):
                    yield e, _Match("port", p, 1.0, f"port:{p}")
        # position: radius around the event, and chokepoints for routes
        if not news and ev.has_position and (cat in GEO_CATEGORIES or cls == "scenario"):
            r = radius_km(ev, cfg)
            for e, d in idx.near(ev.lat, ev.lon, r):
                c = geo_confidence(d, r, cfg)
                if c is not None:
                    yield e, _Match("geo_radius", f"{d:.0f} km", c, "geo_radius:near",
                                    detail={"distance_km": round(d, 1), "radius_km": round(r, 1)})  # fmt: skip
            for cp in chokepoints():
                if not idx.by_choke.get(cp.id):
                    continue
                d = haversine_km(ev.lat, ev.lon, cp.lat, cp.lon)
                c = geo_confidence(d, cp.radius_km, cfg)
                if c is not None:
                    for e in idx.by_choke[cp.id]:
                        yield e, _Match("chokepoint", cp.id, c, f"chokepoint:{cp.id}", "logistics",
                                        {"distance_km": round(d, 1), "radius_km": cp.radius_km})  # fmt: skip
        if cls == "scenario":
            for raw in ev.attrs.get("chokepoints") or ():
                cid = chokepoint_id(raw)
                for e in idx.by_choke.get(cid or "", ()):
                    yield e, _Match("chokepoint", cid, 1.0, f"chokepoint:{cid}", "logistics")
        # countries (position beats country for hazards/logistics with a positioned event)
        if news:
            countries = dict.fromkeys([*ev.countries, *self._tag_countries(ev)])
            factor = cfg.news_country_factor
        else:
            countries, factor = dict.fromkeys(ev.countries), 1.0
        for code in countries:
            for e in idx.by_country.get(code, ()):
                if cat in GEO_CATEGORIES and ev.has_position and e.has_position and has_key(e, "geo"):
                    continue
                c = cfg.country_confidence.get(e.kind)
                if c:
                    yield e, _Match("country", code, c * factor, f"country:{code}")
        if news:
            yield from self._chokepoint_text(ev)
            yield from self._companies(ev)
            yield from self._commodities(ev)

    @staticmethod
    def _tag_countries(ev: FeedEvent) -> list[str]:
        if ev.tag is None or not isinstance(ev.tag.entities, Mapping):
            return []
        v = ev.tag.entities.get("countries")
        return [c for c in v if isinstance(c, str)] if isinstance(v, list) else []

    def _software(self, ev: FeedEvent) -> Iterable[tuple[Entity, _Match]]:
        cfg, idx = self.cfg, self.idx
        cpe_only = ev.source in S.CPE_SOURCES
        osv_only = ev.source in S.ADVISORY_SOURCES and not cpe_only
        for s in ev.software:
            if not cpe_only and ":" in s:
                eco, pkg = s.split(":", 1)
                key = sw.osv_key(eco, pkg)
                for e in idx.by_osv.get(key, ()):
                    yield e, _Match("software", key, cfg.software_osv_confidence, f"software:{key}",
                                    detail={"software_display": s})  # fmt: skip
            if not osv_only:
                key = sw.vendor_product_key(s)
                for e in idx.by_cpe.get(key or "", ()):
                    yield e, _Match("software", key, cfg.software_cpe_confidence, f"software:{key}",
                                    detail={"software_display": s})  # fmt: skip

    def _chokepoint_text(self, ev: FeedEvent) -> Iterable[tuple[Entity, _Match]]:
        for place in _tag_list(ev, "places"):
            cp = chokepoint_by_alias(place["text"])
            if cp is None:
                continue
            for e in self.idx.by_choke.get(cp.id, ()):
                yield e, _Match("chokepoint", cp.id, self.cfg.chokepoint_text_factor * _conf(place),
                                f"chokepoint:{cp.id}", "logistics", {"matched_text": place["text"]})  # fmt: skip

    def _companies(self, ev: FeedEvent) -> Iterable[tuple[Entity, _Match]]:
        cfg = self.cfg
        for comp in _tag_list(ev, "companies"):
            for e, _, sim in self.idx.named.search(comp["text"], cfg.company_threshold):
                n = names.normalise_name(e.name, cfg.legal_forms)
                yield e, _Match("company_name", n, cfg.company_tag_factor * _conf(comp), f"company_name:{n}",
                                detail={"matched_text": comp["text"], "similarity": sim})  # fmt: skip

    def _commodities(self, ev: FeedEvent) -> Iterable[tuple[Entity, _Match]]:
        if not self.idx.commodity:
            return
        cfg, chapters = self.cfg, hs_chapter_keywords()
        found = [(c["text"], _conf(c)) for c in _tag_list(ev, "commodities")]
        found += [(t, 1.0) for t in ev.commodities]
        for e, hs, texts_ in self.idx.commodity:
            for text, conf in found:
                t = text.casefold().strip()
                if t in texts_:
                    yield e, _Match("commodity", t, cfg.commodity_text_factor * conf, f"commodity:{t}",
                                    detail={"matched_text": text})  # fmt: skip
                    continue
                for ch in sorted(hs):
                    if any(keyword_in(t, kw) for kw in chapters.get(ch, ())):
                        yield e, _Match("commodity", f"HS {ch}", cfg.commodity_chapter_factor * conf,
                                        f"commodity:HS {ch}", detail={"matched_text": text})  # fmt: skip

    def _sanctions(self, events: list[tuple[FeedEvent, float]]) -> None:
        """Fuzzy name screening of suppliers and customers against every sanctions row present."""
        cfg = self.cfg
        if not events or not len(self.idx.named):
            return
        index = names.NameIndex(cfg.legal_forms, cfg.name_min_length)
        for i, (ev, _) in enumerate(events):
            primary = ev.attrs.get("name")
            if isinstance(primary, str):
                index.add(primary, (i, "name"))
            for alias in names.dedupe(names.alias_names(ev.attrs.get("aliases"))):
                index.add(alias, (i, "alias"))
        if not len(index):
            return
        for e in self.idx.entities:
            if not has_key(e, "name"):
                continue
            per_event: dict[int, tuple[float, str, str]] = {}
            for (i, how), listed, sim in index.search(e.name, cfg.sanctions_threshold):
                if i not in per_event or sim > per_event[i][0]:
                    per_event[i] = (sim, listed, how)
            for i, (sim, listed, how) in sorted(per_event.items()):
                ev, signal = events[i]
                c = cfg.sanctions_base_confidence + cfg.sanctions_span * (sim - cfg.sanctions_threshold) / (
                    1.0 - cfg.sanctions_threshold
                )
                ours, theirs = set(e.countries), set(ev.countries)
                if ours & theirs:
                    c += cfg.sanctions_country_bonus
                elif ours and theirs:
                    c -= cfg.sanctions_country_penalty
                c = S.clamp(c, 0.0, 1.0)
                if c < cfg.sanctions_min_confidence:
                    continue
                primary = ev.attrs.get("name") if isinstance(ev.attrs.get("name"), str) else listed
                detail = {"matched_name": listed, "matched_on": how, "similarity": sim,
                          "sanctions_entity_id": ev.attrs.get("entity_id"),
                          "programs": ev.attrs.get("programs")}  # fmt: skip
                m = _Match("sanctions_name", primary, c, f"sanctions_name:{names.normalise_name(primary)}",
                           detail=detail)  # fmt: skip
                self._offer_event(ev, e, m, "sanctions", signal)

    # --- anomalies ------------------------------------------------------------------------

    def run_anomalies(self) -> None:
        cfg, idx = self.cfg, self.idx
        stored = sorted(self.feeds.anomalies, key=lambda a: a.evidence_key)
        cross = self._cross_fx() if self.base != "EUR" else []
        for a in [*stored, *cross]:
            cat = S.anomaly_category(a)
            if cat is None:
                continue
            matches: list[tuple[Entity, _Match]] = []
            if a.series == "fx.rate":
                base, _, quote = a.key.partition("/")
                if a.attrs.get("scenario"):
                    c = cfg.fx_direct_confidence  # a what-if move names its own pair
                else:
                    is_cross = bool(a.attrs.get("cross"))
                    if is_cross == (self.base == "EUR") or base != self.base:
                        continue  # stored EUR pairs only with base EUR; cross pairs only otherwise
                    c = cfg.fx_cross_confidence if is_cross else cfg.fx_direct_confidence
                matches = [(e, _Match("currency", a.key, c, f"currency:{a.key}")) for e in idx.by_currency.get(quote, ())]
            elif a.series.startswith(("wb.", "portwatch.")):
                for e in idx.by_country.get(a.key, ()):
                    c = cfg.country_confidence.get(e.kind)
                    if c:
                        matches.append((e, _Match("country", a.key, c, f"country:{a.key}")))
            elif a.series == "gdelt.articles":
                for e in idx.by_term.get(a.key, ()):
                    n = names.normalise_name(e.name, cfg.legal_forms)
                    matches.append((e, _Match("company_name", n, cfg.company_anomaly_confidence, f"company_name:{n}",
                                              detail={"watch_term": a.key})))  # fmt: skip
            if not matches:
                continue
            hl = cfg.half_life(S.half_life_key(cat, series=a.series))
            rec = S.recency((self.now - a.ts).total_seconds() / 3600.0, hl, cfg)
            if rec is None:
                continue
            signal = S.anomaly_signal(a, cfg)
            for e, m in matches:
                self._offer(e, m, category=cat, signal=signal, rec=rec, key=a.evidence_key, anomaly=a)

    def _cross_fx(self) -> list[Anomaly]:
        """Spike detection on ``<base>/<CCY>`` cross rates, in memory (never stored in feeds)."""
        out: list[Anomaly] = []
        quotes = sorted(q for q in self.idx.by_currency if q != self.base)
        if not quotes:
            return out
        base_pts = self._eur_points(self.base)
        if base_pts is None:
            return out
        for q in quotes:
            q_pts = self._eur_points(q)
            if q_pts is None:
                continue
            common = sorted(set(q_pts) & set(base_pts))
            points = [(ts, q_pts[ts] / base_pts[ts]) for ts in common if base_pts[ts] > 0]
            out.extend(_cross_flags(f"{self.base}/{q}", tuple(points)))
        return out

    def _eur_points(self, ccy: str) -> dict[datetime, float] | None:
        if ccy == "EUR":
            pts = next((p for (s, k), p in sorted(self.feeds.series.items()) if s == "fx.rate" and k.startswith("EUR/")), None)
            return {ts: 1.0 for ts, _ in pts} if pts else None
        pts = self.feeds.series.get(("fx.rate", f"EUR/{ccy}"))
        return {ts: float(v) for ts, v in pts if v and v > 0} if pts else None

    # --- assessments ----------------------------------------------------------------------

    def assessments(self) -> list[Assessment]:
        cfg = self.cfg
        shares = S.exposure_shares(self.entities, S.latest_rates(self.feeds.series))
        groups: dict[str, list[_Item]] = defaultdict(list)
        for item in self.best.values():
            if item.p > 0:
                groups[fingerprint(item.entity.id, item.category, item.driver)].append(item)
        out: list[Assessment] = []
        for fp in sorted(groups):
            items = sorted(groups[fp], key=lambda i: (-i.p, i.key))[: cfg.max_evidence]
            entity, category = items[0].entity, items[0].category
            likelihood = S.noisy_or((i.p for i in items), cfg.likelihood_cap)
            impact, ifac = S.impact(entity, category, shares.get(entity.id), cfg)
            raw = cfg.category_multipliers[category] * (
                ifac["criticality_weight"] if ifac["exposure_norm"] is None
                else 0.5 * ifac["criticality_weight"] + 0.5 * ifac["exposure_norm"]
            )  # fmt: skip
            if round(100 * likelihood * impact, 1) < cfg.min_score:
                continue
            for i in items:
                i.snapshot = (
                    texts.event_snapshot(i.event, i.detail) if i.event is not None
                    else texts.anomaly_snapshot(i.anomaly, i.detail)
                )  # fmt: skip
            out.append(self._assessment(fp, entity, category, items, likelihood, impact, ifac,
                                        clamped=not (cfg.impact_min <= raw <= cfg.impact_max)))  # fmt: skip
        out.sort(key=lambda a: (-a.score, a.fingerprint))
        return out

    def _assessment(self, fp, entity, category, items, likelihood, impact, ifac, clamped) -> Assessment:
        evidence = tuple(
            Evidence(
                kind="event" if i.event is not None else "anomaly",
                match_type=i.match_type,
                match_value=i.match_value,
                match_confidence=i.confidence,
                signal=i.signal,
                recency=i.recency,
                contribution=i.p,
                snapshot=i.snapshot,
                event_source=i.event.source if i.event is not None else None,
                event_raw_id=i.event.raw_id if i.event is not None else None,
                anomaly_series=i.anomaly.series if i.anomaly is not None else None,
                anomaly_key=i.anomaly.key if i.anomaly is not None else None,
                anomaly_ts=i.anomaly.ts if i.anomaly is not None else None,
                anomaly_method=i.anomaly.method if i.anomaly is not None else None,
            )
            for i in items
        )
        factors = {
            "likelihood": {
                "method": "noisy_or",
                "value": likelihood,
                "items": [
                    {"evidence_key": i.key, "signal": i.signal, "recency": i.recency,
                     "match_confidence": i.confidence, "contribution": i.p}
                    for i in items
                ],  # fmt: skip
            },
            "impact": ifac,
            "engine_version": ENGINE_VERSION,
        }
        matched_c = [i.match_value for i in items if i.match_type == "country"]
        matched_p = [i.match_value for i in items if i.match_type == "port"]
        countries = tuple(dict.fromkeys(matched_c)) if matched_c else tuple(entity.countries)
        ports = tuple(dict.fromkeys(matched_p)) if matched_p else tuple(entity.ports)
        return Assessment(
            fingerprint=fp,
            entity_id=entity.id,
            category=category,
            title=texts.title(category, entity, items[0]),
            explanation=texts.explanation(category, entity, items, likelihood, ifac, clamped),
            likelihood=likelihood,
            impact=impact,
            factors=factors,
            countries=tuple(sorted(c for c in countries if len(c) == 3 and c.isalpha() and c.isupper())),
            ports=tuple(sorted(p for p in ports if len(p) == 5)),
            lat=entity.lat if entity.has_position else None,
            lon=entity.lon if entity.has_position else None,
            evidence=evidence,
        )


@lru_cache(maxsize=512)
def _cross_flags(key: str, points: tuple[tuple[datetime, float], ...]) -> tuple[Anomaly, ...]:
    """``jobs.analyse.anomalies.detect`` on one cross-rate series (memoised: pure, and the same
    pair recurs across workspaces with the same base currency)."""
    from jobs.analyse.anomalies import detect

    det = detect("fx.rate", key, list(points))
    return tuple(
        Anomaly("fx.rate", key, f.ts, f.method, f.value, f.baseline, f.score, {**f.attrs, "cross": True})
        for f in det.flags
    )


def assess(
    entities: Sequence[Entity], feeds: FeedSnapshot, *, base_currency: str | None, config: EngineConfig = DEFAULT_CONFIG
) -> list[Assessment]:
    """Score every (entity, category, driver) with evidence; highest score first.

    Assessments below ``config.min_score`` are left out (``write_risks`` then
    resolves any open risk with that fingerprint).
    """
    a = _Assessor(entities, feeds, base_currency, config)
    a.run_events()
    a.run_anomalies()
    return a.assessments()
