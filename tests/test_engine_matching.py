"""One test (at least) per match rule in risk-engine.md §5.2, plus the §5.1 key table."""

from __future__ import annotations

from dataclasses import replace

import pytest

pytest.importorskip("rapidfuzz")

from engine import DEFAULT_CONFIG, assess  # noqa: E402
from engine.geo import haversine_km, radius_km  # noqa: E402
from engine.reference import chokepoint_id, chokepoints  # noqa: E402
from engine.software import cpe_vendor_product, purl_to_osv, vendor_product_key  # noqa: E402
from engine_fixtures import NOW, anomaly, ent, event, fx_history, news, quake, snap, tag  # noqa: E402

CFG = replace(DEFAULT_CONFIG, min_score=0.0)  # keep every match visible in these tests


def run(entities, events=(), anomalies=(), series=None, base="EUR", config=CFG):
    return assess(entities, snap(events, anomalies, series), base_currency=base, config=config)


def only(entities, *a, **kw):
    out = run(entities, *a, **kw)
    assert len(out) == 1, [x.fingerprint for x in out]
    return out[0]


# --- geo_radius -------------------------------------------------------------------------------


@pytest.mark.parametrize("m, r", [(5.0, 50), (6.0, 100), (7.0, 200), (8.5, 400), (3.0, 25), (None, 100)])
def test_earthquake_radius(m, r):
    attrs = {} if m is None else {"magnitude": m}
    assert radius_km(event("usgs", "q", "hazard.earthquake", lat=0, lon=0, attrs=attrs), CFG) == pytest.approx(r)


@pytest.mark.parametrize(
    "type_, r",
    [("hazard.tropical_cyclone", 300), ("hazard.tsunami", 300), ("hazard.drought", 400), ("hazard.flood", 100),
     ("hazard.volcano", 100), ("hazard.wildfire", 50), ("hazard.severe_storm", 100), ("port.disruption", 50)],
)  # fmt: skip
def test_other_radii(type_, r):
    assert radius_km(event("gdacs", "x", type_, lat=0, lon=0), CFG) == r


def test_geo_confidence_falls_linearly_with_distance():
    site = ent("plant", "site", lat=0.0, lon=0.0)
    # M6 → R 100 km. ~1 degree of longitude at the equator ≈ 111 km.
    near = only([site], [quake("q", 0.0, 0.25, magnitude=6.0)])
    d = haversine_km(0, 0, 0, 0.25)
    assert near.evidence[0].match_type == "geo_radius"
    assert near.evidence[0].match_confidence == pytest.approx(round(1 - d / 100, 3))
    assert near.evidence[0].snapshot["distance_km"] == pytest.approx(d, abs=0.1)
    edge = only([site], [quake("q", 0.0, 0.899, magnitude=6.0)])  # ~99.97 km: floor 0.05
    assert edge.evidence[0].match_confidence == 0.05
    assert run([site], [quake("q", 0.0, 0.91, magnitude=6.0)]) == []  # outside


def test_position_beats_country_for_hazards():
    """A site with a position is matched by radius only, even if the quake is in its country."""
    site = ent("plant", "site", lat=35.0, lon=139.0, countries=["JPN"])
    far = quake("far", 43.0, 145.0, countries=["JPN"])  # Hokkaido, ~1,000 km away
    assert run([site], [far]) == []
    no_pos = ent("plant2", "site", countries=["JPN"])  # no position → country match
    a = only([no_pos], [far])
    assert a.evidence[0].match_type == "country" and a.evidence[0].match_confidence == 0.8
    assert a.fingerprint.endswith("|natural_hazard|country:JPN") and a.countries == ("JPN",)


def test_hazard_without_position_still_matches_by_country():
    site = ent("plant", "site", lat=35.0, lon=139.0, countries=["JPN"])
    flood = event("gdacs", "FL-1", "hazard.flood", countries=["JPN"], severity=2 / 3)  # no lat/lon
    assert only([site], [flood]).evidence[0].match_type == "country"


def test_customers_are_not_matched_by_position():
    cust = ent("cust", "customer", lat=0.0, lon=0.0)
    assert run([cust], [quake("q", 0.0, 0.1)]) == []


# --- country ------------------------------------------------------------------------------------


@pytest.mark.parametrize("kind, c", [("site", 0.8), ("supplier", 0.6), ("customer", 0.5), ("route", 0.5), ("port", 0.5)])
def test_country_confidence_by_kind(kind, c):
    e = ent("e", kind, countries=["TUR"])
    flood = event("gdacs", "FL-1", "hazard.flood", countries=["TUR"], severity=1.0)
    if kind == "customer":
        flood = news("n", tag("conflict", 0.8, countries=["TUR"]))
        c = round(c * 0.6, 3)
    assert only([e], [flood]).evidence[0].match_confidence == c


def test_tagged_news_country_match_is_weaker():
    sup = ent("s", "supplier", countries=["MMR"])
    a = only([sup], [news("n1", tag("conflict", 0.9, countries=["MMR"]))])
    assert a.category == "geopolitical" and a.evidence[0].match_confidence == round(0.6 * 0.6, 3)
    assert a.evidence[0].signal == round(0.25 + 0.5 * 0.9, 3)
    # GDELT file articles carry countries from the article text: same weight
    b = only([sup], [news("n2", tag("protest", 0.9), countries=["MMR"])])
    assert b.evidence[0].match_confidence == round(0.6 * 0.6, 3)


def test_untagged_news_and_other_tag_by_country_do_not_count():
    sup = ent("s", "supplier", countries=["MMR"])
    assert run([sup], [event("gdelt", "n", "news", countries=["MMR"])]) == []
    assert run([sup], [news("n", tag("other", 0.9, countries=["MMR"]))]) == []


def test_sanctions_and_cve_events_never_match_by_country():
    sup = ent("s", "supplier", name="Unrelated Widgets", countries=["RUS"])
    sw = ent("sw", "software", software=["pkg:pypi/requests"])
    evs = [event("ofac", "9:designation:x", "sanction.designation", countries=["RUS"], attrs={"name": "Ivan Petrov"}),
           event("cisa_kev", "CVE-1", "cyber.exploited_vulnerability", software=["Microsoft:Windows"])]  # fmt: skip
    assert run([sup, sw], evs) == []


# --- port -----------------------------------------------------------------------------------------


def test_port_match_is_exact_and_beats_country():
    route = ent("r", "route", ports=["CNSHA", "NLRTM"], countries=["NLD"])
    ev = event("portwatch", "TC-1", "port.disruption", ports=["NLRTM"], countries=["NLD"], severity=2 / 3)
    a = only([route], [ev])
    assert (a.evidence[0].match_type, a.evidence[0].match_value, a.evidence[0].match_confidence) == ("port", "NLRTM", 1.0)
    assert a.category == "logistics" and a.ports == ("NLRTM",)
    assert run([route], [replace(ev, ports=("NLAMS",), countries=())]) == []


def test_one_event_feeds_one_risk_per_entity():
    """A supplier with a position near a disrupted port that it also lists: the best match wins."""
    sup = ent("s", "supplier", lat=51.95, lon=4.14, ports=["NLRTM"], countries=["NLD"])
    ev = event("portwatch", "TC-1", "port.disruption", ports=["NLRTM"], countries=["NLD"], lat=51.95, lon=4.10,
               severity=1.0)  # fmt: skip
    a = only([sup], [ev])
    assert a.evidence[0].match_type == "port"


# --- chokepoint -------------------------------------------------------------------------------------


def test_chokepoint_table():
    ids = [c.id for c in chokepoints()]
    assert ids == ["suez", "panama", "hormuz", "bab_el_mandeb", "malacca", "singapore", "bosporus", "gibraltar",
                   "dover", "taiwan_strait", "cape_of_good_hope"]  # fmt: skip
    assert chokepoint_id("Suez Canal") == "suez" and chokepoint_id("strait of hormuz") == "hormuz"
    assert chokepoint_id("taiwan_strait") == "taiwan_strait" and chokepoint_id("Taiwan Strait") == "taiwan_strait"
    assert chokepoint_id("Atlantis") is None


def test_chokepoint_by_radius_and_by_name():
    route = ent("r", "route", ports=["CNSHA", "NLRTM"], attrs={"chokepoints": ["Suez Canal", "malacca"]})
    storm = event("gdacs", "TC-1", "hazard.tropical_cyclone", lat=2.6, lon=101.3, severity=1.0)  # in the strait
    a = only([route], [storm])
    assert a.category == "logistics" and a.evidence[0].match_type == "chokepoint" and a.evidence[0].match_value == "malacca"
    assert a.evidence[0].match_confidence > 0.9
    text = news("n1", tag("port_shipping_disruption", 0.9, places=[("Suez Canal", 0.8)]))
    b = only([route], [text])
    assert b.fingerprint.endswith("|logistics|chokepoint:suez") and b.evidence[0].match_confidence == round(0.6 * 0.8, 3)
    # a place that is not an alias, or a route that does not pass the chokepoint, does not match
    assert run([route], [news("n2", tag("port_shipping_disruption", 0.9, places=[("Suez City", 0.9)]))]) == []
    other = ent("r2", "route", ports=["USLAX"], attrs={"chokepoints": ["panama"]})
    assert run([other], [text, storm]) == []


# --- currency -----------------------------------------------------------------------------------------


def test_currency_direct_with_base_eur():
    sup = ent("s", "supplier", currencies=["TRY"])
    a = only([sup], anomalies=[anomaly("fx.rate", "EUR/TRY", score=6.0)])
    assert a.category == "financial" and a.evidence[0].match_value == "EUR/TRY"
    assert a.evidence[0].match_confidence == 1.0
    assert a.evidence[0].snapshot["title"] == "EUR/TRY moved 6.0 robust z from its 90-day norm"
    assert a.evidence[0].snapshot["unit"] == "TRY per EUR"
    assert only([sup], anomalies=[anomaly("fx.rate", "EUR/TRY", score=6.0)], base=None).evidence  # NULL base → EUR


def test_currency_stored_pair_ignored_for_other_base_and_cross_rate_detected():
    sup = ent("s", "supplier", currencies=["TRY"])
    stored = [anomaly("fx.rate", "EUR/TRY", score=6.0)]
    series = {**fx_history("TRY", start=30.0, jump_last=0.08), **fx_history("USD", start=1.1)}
    out = run([sup], anomalies=stored, series=series, base="USD")
    assert out, "the lira's 8 % jump shows up in USD/TRY too"
    [a] = out
    assert a.evidence[0].anomaly_key == "USD/TRY" and a.evidence[0].match_confidence == 0.9
    assert all(e.anomaly_key == "USD/TRY" for e in a.evidence)
    calm = {**fx_history("TRY", start=30.0), **fx_history("USD", start=1.1)}
    assert run([sup], series=calm, base="USD") == []


def test_currency_only_for_kinds_with_currency_keys():
    site = ent("site", "site", currencies=["TRY"])
    assert run([site], anomalies=[anomaly("fx.rate", "EUR/TRY", score=6.0)]) == []


# --- macro and port-activity series ----------------------------------------------------------------------


def test_world_bank_and_portwatch_anomalies_match_by_country():
    site = ent("plant", "site", countries=["ARG"], lat=-34.6, lon=-58.4)
    wb = anomaly("wb.FP.CPI.TOTL.ZG", "ARG", score=4.0, age_h=24 * 300, window_days=3660)
    pw = anomaly("portwatch.port_calls", "ARG", score=-5.0, age_h=24 * 10)
    out = {a.category: a for a in run([site], anomalies=[wb, pw])}
    assert set(out) == {"financial", "logistics"}
    assert out["financial"].evidence[0].recency == round(0.5 ** (300 * 24 / 4380), 3)
    assert out["logistics"].evidence[0].match_confidence == 0.8  # positioned sites still match country series
    assert out["logistics"].title == "Unusual port calls in Argentina affecting plant"


# --- commodity -----------------------------------------------------------------------------------------------


def test_commodity_alone_creates_no_risk_for_a_supplier():
    """A commodity or HS-chapter match only supports an anchor of the same item (§5.9)."""
    sup = ent("s", "supplier", hs_codes=["7403"], commodities=["cobalt"])
    copper = news("n1", tag("trade_policy", 0.8, commodities=[("refined copper", 0.9)]))
    cobalt = news("n2", tag("trade_policy", 0.8, commodities=[("Cobalt", 0.7)]))
    assert run([sup], [copper, cobalt]) == []


def test_commodity_strengthens_a_country_match():
    sup = ent("s", "supplier", countries=["CHL"], hs_codes=["7403"], commodities=["copper cathodes", "cobalt"])
    copper = news("n1", tag("trade_policy", 0.8, commodities=[("refined copper", 0.9)], countries=["CHL"]))
    a = only([sup], [copper])
    ev = a.evidence[0]
    assert a.fingerprint.endswith("|trade_policy|country:CHL") and ev.match_type == "country"
    anchor, support = round(0.6 * 0.6, 3), round(0.6 * 0.9, 3)
    assert ev.match_confidence == round(1 - (1 - anchor) * (1 - 0.5 * support), 3)
    # engine 1.3.0: matched on the entity's own 4-digit heading, and the sentence names its product
    assert ev.snapshot["supported_by"]["match_value"] == "HS 7403"
    assert a.explanation == (
        "A recent news report about trade policy involving Chile mentions refined copper, which is in the same "
        "product group (HS 7403) as the copper cathodes s supplies."
    )
    cobalt = news("n2", tag("trade_policy", 0.8, commodities=[("Cobalt", 0.7)], countries=["CHL"]))
    b = only([sup], [cobalt])
    assert b.evidence[0].match_confidence == round(1 - (1 - anchor) * (1 - 0.5 * round(0.8 * 0.7, 3)), 3)
    # whole words only: "copperhead" is not copper, so only the country counts
    c = only([sup], [news("n3", tag("trade_policy", 0.8, commodities=[("copperhead", 0.9)], countries=["CHL"]))])
    assert c.evidence[0].match_confidence == anchor and "supported_by" not in c.evidence[0].snapshot


def test_a_market_move_in_a_commodity_may_create_a_capped_risk():
    sup = ent("s", "supplier", hs_codes=["7403"], criticality="critical", annual_spend=1_000_000)
    moves = [event("markets", f"cu-{i}", "market.commodity_price", commodities=["copper"], severity=1.0,
                   tag=tag("financial_stress", 1.0)) for i in range(4)]  # fmt: skip
    # 1.3.0: with each further item weighted down (§5.6) four such moves stay below the cap on their own ...
    weighted = only([sup], moves)
    assert weighted.score <= 50.0 and "capped_from" not in weighted.factors["likelihood"]
    # ... so the cap itself is checked with the plain noisy-OR (decay 1), where they would pass it
    a = only([sup], moves, config=replace(CFG, likelihood_decay=1.0))
    assert a.category == "financial" and a.evidence[0].match_type == "commodity"
    assert a.score <= 50.0 and a.factors["likelihood"]["capped_from"] > a.likelihood
    assert a.title == "Market move in copper affecting s"
    assert [s["label"] for s in a.factors["explain"]["steps"]][-3:] == ["Market-only cap", "Impact", "Score"]


def test_commodity_from_event_commodities_and_not_for_sites():
    com = ent("c", "commodity", hs_codes=["27"])
    ev = event("gdelt", "n1", "news", tag=tag("financial_stress", 0.8), commodities=["crude oil"])
    assert only([com], [ev]).evidence[0].match_confidence == 0.6
    assert run([ent("site", "site", hs_codes=["27"])], [ev]) == []


# --- software -------------------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "purl, expected",
    [
        ("pkg:pypi/Jinja2@3.1.2", ("PyPI", "jinja2")),
        ("pkg:pypi/zope.interface", ("PyPI", "zope-interface")),
        ("pkg:npm/lodash@4.17.21", ("npm", "lodash")),
        ("pkg:npm/%40babel/core@7.0.0", ("npm", "@babel/core")),
        ("pkg:maven/org.apache.logging.log4j/log4j-core@2.14.1", ("Maven", "org.apache.logging.log4j:log4j-core")),
        ("pkg:golang/golang.org/x/net@v0.17.0", ("Go", "golang.org/x/net")),
        ("pkg:cargo/serde@1.0", ("crates.io", "serde")),
        ("pkg:nuget/Newtonsoft.Json@13.0.1", ("NuGet", "Newtonsoft.Json")),
        ("pkg:gem/rails@7.0", ("RubyGems", "rails")),
        ("pkg:composer/laravel/framework@10", ("Packagist", "laravel/framework")),
        ("pkg:deb/debian/openssl@3.0", None),
        ("cpe:2.3:a:microsoft:exchange_server:2019", None),
    ],
)
def test_purl_to_osv(purl, expected):
    assert purl_to_osv(purl) == expected


def test_cpe_and_kev_keys():
    assert cpe_vendor_product("cpe:2.3:a:microsoft:exchange_server:2019:*:*:*:*:*:*:*") == "microsoft:exchangeserver"
    assert vendor_product_key("Microsoft:Exchange Server") == "microsoft:exchangeserver"
    assert cpe_vendor_product("cpe:2.3:a:*:*:*") is None


def test_software_osv_and_kev():
    lib = ent("lib", "software", software=["pkg:pypi/Requests@2.31.0"])
    exch = ent("ex", "software", name="Mail server", software=["cpe:2.3:a:microsoft:exchange_server:2019:*:*:*:*:*:*:*"])
    osv = event("osv", "GHSA-1", "cyber.vulnerability", software=["PyPI:requests"], severity=0.75, age_h=24,
                attrs={"aliases": ["CVE-2026-9"]})  # fmt: skip
    kev = event("cisa_kev", "CVE-2026-5", "cyber.exploited_vulnerability", software=["Microsoft:Exchange Server"],
                age_h=24, title="Microsoft Exchange Server RCE")  # fmt: skip
    epss = {("epss.score", "CVE-2026-9"): ((NOW, 0.95),)}
    out = {a.entity_id: a for a in run([lib, exch], [osv, kev], series=epss)}
    a = out[lib.id]
    assert a.evidence[0].match_value == "PyPI:requests" and a.evidence[0].match_confidence == 1.0
    assert a.evidence[0].signal == 0.95  # EPSS above the CVSS score
    b = out[exch.id]
    assert b.evidence[0].match_confidence == 0.9 and b.evidence[0].signal == 0.9
    assert b.title == "Exploited vulnerability in Microsoft Exchange Server"
    # versions are not compared, and other packages do not match
    assert run([ent("x", "software", software=["pkg:pypi/requests-oauthlib"])], [osv]) == []


def test_old_kev_ages_out_but_osv_lasts_longer():
    lib = ent("lib", "software", software=["pkg:pypi/requests"])
    exch = ent("ex", "software", software=["cpe:2.3:a:microsoft:exchange_server"])
    age = 24 * 150  # past KEV's 120-day window, inside OSV's 360
    out = run([lib, exch], [event("osv", "G", "cyber.vulnerability", software=["PyPI:requests"], age_h=age),
                            event("cisa_kev", "CVE-1", "cyber.exploited_vulnerability",
                                  software=["Microsoft:Exchange Server"], age_h=age)])  # fmt: skip
    assert [a.entity_id for a in out] == [lib.id]


# --- company_name ------------------------------------------------------------------------------------------------


def test_company_news_and_news_volume():
    sup = ent("s", "supplier", name="Nordwind Logistik GmbH", criticality="high")
    n = news("n1", tag("labour_action", 0.9, companies=[("Nordwind Logistik", 0.8)]))
    a = only([sup], [n])
    assert a.category == "labour" and a.evidence[0].match_type == "company_name"
    assert a.evidence[0].match_confidence == round(0.7 * 0.8, 3)
    other = news("n2", tag("other", 0.3, companies=[("Nordwind Logistik", 0.9)]))
    assert only([sup], [other]).category == "other"  # `other` counts only through a name
    vol = anomaly("gdelt.articles", '"nordwind logistik"', score=6.0)
    c = only([sup], anomalies=[vol])
    assert c.category == "other" and c.evidence[0].match_confidence == 0.8
    assert c.title == "Unusual news volume about Nordwind Logistik GmbH"
    near_miss = news("n3", tag("labour_action", 0.9, companies=[("Nordwind Logistics", 0.9)]))
    assert run([sup], [near_miss]) == []  # 0.94 < 0.95


# --- kinds and the key table --------------------------------------------------------------------------------------


def test_kind_key_table_is_respected():
    """A software entity with countries is still matched on software only."""
    sw = ent("sw", "software", countries=["JPN"], lat=35.0, lon=139.0)
    assert run([sw], [quake("q", 35.0, 139.0, countries=["JPN"])]) == []
    cur = ent("c", "currency", countries=["JPN"], currencies=["JPY"])
    assert run([cur], [event("gdacs", "FL", "hazard.flood", countries=["JPN"])]) == []


def test_empty_inputs():
    assert run([], [quake("q", 0, 0)]) == []
    assert run([ent("s", "site", lat=0.0, lon=0.0)]) == []


def test_min_score_drops_weak_risks():
    sup = ent("s", "supplier", countries=["MMR"], criticality="low")
    weak = news("n1", tag("protest", 0.0, countries=["MMR"]))  # 0.25 × 0.36 × 0.175 impact ≈ 1.6
    assert run([sup], [weak], config=DEFAULT_CONFIG) == []
    assert run([sup], [weak])
