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

from engine import names, relevance as rel, scoring as S, software as sw, texts
from engine.config import DEFAULT_CONFIG, EngineConfig
from engine.geo import EARTH_RADIUS_KM, geo_confidence, haversine_km, radius_km
from engine.models import Anomaly, Assessment, Entity, Evidence, FeedEvent, FeedSnapshot
from engine.reference import (
    chokepoint_by_alias, chokepoint_id, chokepoints, hs_keywords, keyword_in, lane_for_country,
)

ENGINE_VERSION = "1.3.1"

# §5.1: which keys each entity kind is matched on. Routes declare their chokepoints;
# suppliers, sites and ports get them inferred from their port or position (§5.9).
KIND_KEYS: Mapping[str, frozenset[str]] = {
    "supplier": frozenset({"geo", "country", "port", "name", "commodity", "currency", "chokepoint"}),
    "site": frozenset({"geo", "country", "port", "chokepoint"}),
    "customer": frozenset({"country", "name", "currency"}),
    "route": frozenset({"port", "country", "chokepoint"}),
    "port": frozenset({"port", "geo", "country", "chokepoint"}),
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
# Kinds whose only key *is* a commodity or currency: for them that match is the anchor.
SELF_ANCHORED_KINDS = frozenset({"commodity", "currency"})


def has_key(entity: Entity, key: str) -> bool:
    return key in KIND_KEYS.get(entity.kind, frozenset())


def entity_chokepoints(entity: Entity) -> list[tuple[str, str | None]]:
    """``(chokepoint id, inferred_from)`` for an entity; ``inferred_from`` is ``None`` when declared.

    Declared chokepoints (``attrs.chokepoints``, routes) always win. A supplier, site or port
    without them gets the chokepoints of the main sea lanes from its first port's country
    (or, with no port, its first country), plus any chokepoint whose radius holds its
    position (§5.9). ``inferred_from`` says what it was inferred from: ``port TWKHH``,
    ``country TWN`` or ``position``.
    """
    if not has_key(entity, "chokepoint"):
        return []
    raw = entity.attrs.get("chokepoints") if isinstance(entity.attrs, Mapping) else None
    declared = [c for c in dict.fromkeys(chokepoint_id(v) for v in (raw if isinstance(raw, list) else [])) if c]
    if declared or entity.kind == "route":
        return [(c, None) for c in declared]
    out: dict[str, str] = {}
    origin, via = None, None
    if entity.ports:
        from jobs.codes import country_alpha3

        origin, via = country_alpha3(entity.ports[0][:2]), f"port {entity.ports[0]}"
    elif entity.countries:
        origin, via = entity.countries[0], f"country {entity.countries[0]}"
    lane = lane_for_country(origin) if origin else None
    if lane is not None:
        for c in lane.chokepoints:
            out.setdefault(c, via)
    if entity.has_position:
        for cp in chokepoints():
            if haversine_km(entity.lat, entity.lon, cp.lat, cp.lon) <= cp.radius_km:
                out.setdefault(cp.id, "position")
    return sorted(out.items())


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


def _choke_match(cid: str, via: str | None, c: float, cfg: EngineConfig, detail: dict | None = None) -> _Match:
    detail = dict(detail or {})
    if via is not None:
        c *= cfg.inferred_chokepoint_factor
        detail["inferred_from"] = via
    return _Match("chokepoint", cid, c, f"chokepoint:{cid}", "logistics", detail)


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
    market_only: bool = False  # a market-wide move with no anchor: capped (§5.9)

    def rank(self) -> tuple:
        return (self.p, self.confidence, -PRECEDENCE[self.match_type], self.match_value)


class _Index:
    """Entities by match key, so each feed item is compared only with entities it can match."""

    def __init__(self, entities: Sequence[Entity], config: EngineConfig):
        self.entities = entities
        self.by_country: dict[str, list[Entity]] = defaultdict(list)
        self.by_port: dict[str, list[Entity]] = defaultdict(list)
        self.by_choke: dict[str, list[tuple[Entity, str | None]]] = defaultdict(list)
        self.by_osv: dict[str, list[Entity]] = defaultdict(list)
        self.by_cpe: dict[str, list[Entity]] = defaultdict(list)
        self.by_currency: dict[str, list[Entity]] = defaultdict(list)
        self.by_term: dict[str, list[Entity]] = defaultdict(list)
        # (entity, (HS value, keywords) per 4-digit heading or 2-digit-only chapter, own products casefold)
        self.commodity: list[tuple[Entity, tuple[tuple[str, tuple[str, ...]], ...], set[str]]] = []
        self.named = names.NameIndex(config.legal_forms, config.name_min_length)
        self.positioned: list[Entity] = []
        for e in entities:
            if has_key(e, "country"):
                for c in e.countries:
                    self.by_country[c].append(e)
            if has_key(e, "port"):
                for p in e.ports:
                    self.by_port[p].append(e)
            for cid, via in entity_chokepoints(e):
                self.by_choke[cid].append((e, via))
            if has_key(e, "software"):
                for kind, key, _ in sw.entity_keys(e.software):
                    (self.by_osv if kind == "osv" else self.by_cpe)[key].append(e)
            if has_key(e, "currency"):
                for c in e.currencies:
                    self.by_currency[c].append(e)
            if has_key(e, "commodity") and (e.hs_codes or e.commodities):
                self.commodity.append((e, hs_keywords(e.hs_codes), {c.casefold() for c in e.commodities}))
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
        # Support-only matches (commodity / currency, §5.9), applied after every anchor is in.
        self.support: dict[tuple[UUID, str], _Item] = {}
        self.sector_cache: dict[UUID, frozenset[str]] = {}  # entity → cyber sector cues (§5.10)

    # --- offering a match -----------------------------------------------------------------

    def _offer(self, entity: Entity, m: _Match, *, category: str, signal: float, rec: float,
               key: str, event: FeedEvent | None = None, anomaly: Anomaly | None = None,
               market: bool = False) -> None:  # fmt: skip
        c = S.r3(S.clamp(m.confidence, 0.0, 1.0))
        s, r = S.r3(signal), S.r3(rec)
        item = _Item(entity, category, m.match_type, m.match_value, m.driver, c, s, r,
                     S.contribution(s, r, c), key, event, anomaly, m.detail)  # fmt: skip
        k = (entity.id, key)
        support = m.match_type in self.cfg.support_match_types and entity.kind not in SELF_ANCHORED_KINDS
        if support and not market:
            target = self.support  # only strengthens an anchor of the same item (§5.9)
        else:
            item.market_only = market
            target = self.best
        cur = target.get(k)
        if cur is None or item.rank() > cur.rank():
            target[k] = item

    def _apply_support(self) -> None:
        """Fold each support match into the anchor match of the same (entity, item), or drop it."""
        w = self.cfg.support_weight
        for k in sorted(self.support, key=lambda t: (str(t[0]), t[1])):
            sup = self.support[k]
            anchor = self.best.get(k)
            if anchor is None or anchor.market_only:
                continue  # a commodity or currency match alone is not a risk
            c = S.r3(S.clamp(1.0 - (1.0 - anchor.confidence) * (1.0 - w * sup.confidence), 0.0, 1.0))
            anchor.detail = {**anchor.detail, "supported_by": {
                "match_type": sup.match_type, "match_value": sup.match_value,
                "matched_text": sup.detail.get("matched_text"), "confidence": sup.confidence,
                "anchor_confidence": anchor.confidence}}  # fmt: skip
            anchor.confidence = c
            anchor.p = S.contribution(anchor.signal, anchor.recency, c)
        self.support.clear()

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
        self._offer(entity, m, category=category, signal=signal, rec=rec, key=ev.key, event=ev,
                    market=ev.type.startswith(self.cfg.market_event_prefix))  # fmt: skip

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
        self._apply_support()

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
                    for e, via in idx.by_choke[cp.id]:
                        yield e, _choke_match(cp.id, via, c, cfg, {"distance_km": round(d, 1), "radius_km": cp.radius_km})
        if cls == "scenario":
            for raw in ev.attrs.get("chokepoints") or ():
                cid = chokepoint_id(raw)
                for e, via in idx.by_choke.get(cid or "", ()):
                    yield e, _choke_match(cid, via, 1.0, cfg)
        # countries (position beats country for hazards/logistics with a positioned event). A news
        # item's country counts only when it is central to the item (§5.10): named in the title or
        # its tag, or the first place located in the article; never a passing mention.
        if news:
            countries = rel.central_countries(ev.title, self._tag_countries(ev), ev.countries) if idx.by_country else {}
            factor = cfg.news_country_factor
        else:
            countries, factor = dict.fromkeys(ev.countries, None), 1.0
        for code, why in countries.items():
            for e in idx.by_country.get(code, ()):
                if cat in GEO_CATEGORIES and ev.has_position and e.has_position and has_key(e, "geo"):
                    continue
                if news and not self._news_country_counts(ev, cat, code, e):
                    continue
                c = cfg.country_confidence.get(e.kind)
                if c:
                    yield e, _Match("country", code, c * factor, f"country:{code}",
                                    detail={"central": why} if why else {})  # fmt: skip
        if news:
            yield from self._chokepoint_text(ev)
            yield from self._companies(ev)
            yield from self._commodities(ev)

    def _news_country_counts(self, ev: FeedEvent, cat: str, code: str, entity: Entity) -> bool:
        """§5.10: a central country is enough, except for sanctions (the country must be what the
        measure is directed at) and cyber (a national-scale incident, or the entity's sector)."""
        if cat == "sanctions":
            return code in rel.sanction_targets(ev.title, self.cfg.sanctions_target_window)
        if cat == "cyber":
            return rel.national_scale(ev.title) or rel.sector_hit(ev.title, self._sector_cues(entity))
        return True

    def _sector_cues(self, entity: Entity) -> frozenset[str]:
        cues = self.sector_cache.get(entity.id)
        if cues is None:
            notes = entity.attrs.get("notes") if isinstance(entity.attrs, Mapping) else None
            keywords = [k for _, kws in hs_keywords(entity.hs_codes) for k in kws]
            cues = rel.sector_cues(entity.commodities, keywords, notes if isinstance(notes, str) else None)
            self.sector_cache[entity.id] = cues
        return cues

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
            for e, via in self.idx.by_choke.get(cp.id, ()):
                yield e, _choke_match(cp.id, via, self.cfg.chokepoint_text_factor * _conf(place), self.cfg,
                                      {"matched_text": place["text"]})  # fmt: skip

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
        cfg = self.cfg
        found = [(c["text"], _conf(c)) for c in _tag_list(ev, "commodities")]
        found += [(t, 1.0) for t in ev.commodities]
        for e, hs, texts_ in self.idx.commodity:
            for text, conf in found:
                t = text.casefold().strip()
                if t in texts_:
                    yield e, _Match("commodity", t, cfg.commodity_text_factor * conf, f"commodity:{t}",
                                    detail={"matched_text": text})  # fmt: skip
                    continue
                # the entity's own 4-digit heading; its chapter only for a 2-digit code (§5.3)
                for code, kws in hs:
                    if any(keyword_in(t, kw) for kw in kws):
                        yield e, _Match("commodity", f"HS {code}", cfg.commodity_chapter_factor * conf,
                                        f"commodity:HS {code}", detail={"matched_text": text})  # fmt: skip

    def _sanctions(self, events: list[tuple[FeedEvent, float]]) -> None:
        """Fuzzy name screening of suppliers and customers against every sanctions row present
        (``sanction.*`` events and the full-list ``feeds.sanctions_entries``)."""
        cfg = self.cfg
        if not events or not len(self.idx.named):
            return
        index = _sanctions_index([ev for ev, _ in events], cfg.legal_forms, cfg.name_min_length)
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
                self._offer(e, m, category=cat, signal=signal, rec=rec, key=a.evidence_key, anomaly=a,
                            market=a.series == "fx.rate")  # fmt: skip

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
            likelihood = S.likelihood([i.p for i in items], cfg)
            impact, ifac = S.impact(entity, category, shares.get(entity.id), cfg)
            raw = cfg.category_multipliers[category] * (
                ifac["criticality_weight"] if ifac["exposure_norm"] is None
                else 0.5 * ifac["criticality_weight"] + 0.5 * ifac["exposure_norm"]
            )  # fmt: skip
            uncapped = None
            if all(i.market_only for i in items) and 100 * likelihood * impact > cfg.market_only_score_cap:
                uncapped = likelihood
                likelihood = math.floor(cfg.market_only_score_cap / (100 * impact) * 1000) / 1000
            if round(100 * likelihood * impact, 1) < cfg.min_score:
                continue
            for i in items:
                i.snapshot = (
                    texts.event_snapshot(i.event, i.detail) if i.event is not None
                    else texts.anomaly_snapshot(i.anomaly, i.detail)
                )  # fmt: skip
            out.append(self._assessment(fp, entity, category, items, likelihood, impact, ifac,
                                        clamped=not (cfg.impact_min <= raw <= cfg.impact_max),
                                        uncapped=uncapped))  # fmt: skip
        out.sort(key=lambda a: (-a.score, a.fingerprint))
        return out

    def _assessment(self, fp, entity, category, items, likelihood, impact, ifac, clamped, uncapped) -> Assessment:
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
        lfac: dict[str, Any] = {
            "method": "noisy_or",
            "value": likelihood,
            "decay": self.cfg.likelihood_decay,  # 1.3.0: the k-th strongest item counts decay^(k-1) of itself
            "items": [
                {"evidence_key": i.key, "signal": i.signal, "recency": i.recency,
                 "match_confidence": i.confidence, "contribution": i.p, "weight": w}
                for i, w in zip(items, S.item_weights(len(items), self.cfg))
            ],  # fmt: skip
        }
        if uncapped is not None:
            lfac["capped_from"] = uncapped
        factors = {
            "likelihood": lfac,
            "impact": ifac,
            "engine_version": ENGINE_VERSION,
            # One news story (or hazard, or listing) hitting several entities shares this key,
            # so lists and counts can show it once (§5.9).
            "story": f"{category}|{items[0].key}",
            "explain": texts.how_scored(category, entity, items, likelihood, ifac, clamped, uncapped, self.cfg),
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
            explanation=texts.explanation(category, entity, items),
            likelihood=likelihood,
            impact=impact,
            factors=factors,
            countries=tuple(sorted(c for c in countries if len(c) == 3 and c.isalpha() and c.isupper())),
            ports=tuple(sorted(p for p in ports if len(p) == 5)),
            lat=entity.lat if entity.has_position else None,
            lon=entity.lon if entity.has_position else None,
            evidence=evidence,
        )


# The last sanctions name index built, with the exact event objects it was built from. The
# worker reuses one cached tuple of sanctions rows (engine.db) for every workspace, so the
# ~18k full-list entries and their aliases are indexed once per list change, not per run.
_SANCTIONS_INDEX: dict[str, Any] = {}


def _sanctions_index(events: list[FeedEvent], legal_forms: frozenset[str], min_length: int) -> names.NameIndex:
    """Primary names and aliases of ``events``; refs are ``(position in events, 'name' | 'alias')``.

    Cached by object identity: the cache keeps the events alive, so equal ids mean the very
    same (frozen) events in the same order, and the index is exactly what a rebuild gives.
    """
    key = (tuple(map(id, events)), legal_forms, min_length)
    if _SANCTIONS_INDEX.get("key") == key:
        return _SANCTIONS_INDEX["index"]
    index = names.NameIndex(legal_forms, min_length)
    for i, ev in enumerate(events):
        primary = ev.attrs.get("name")
        if isinstance(primary, str):
            index.add(primary, (i, "name"))
        for alias in names.dedupe(names.alias_names(ev.attrs.get("aliases"))):
            index.add(alias, (i, "alias"))
    _SANCTIONS_INDEX.clear()
    _SANCTIONS_INDEX.update(key=key, events=events, index=index)
    return index


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
