"""The 11-supplier sheet from the first live end-to-end test, on a recorded set of feed rows
(tests/fixtures/engine_sample_feeds.json, constructed test data). Engine 1.2.0 behaviour:
anchored matching, one story per feed item, inferred chokepoints, plain explanations."""

from __future__ import annotations

import re

import pytest

pytest.importorskip("rapidfuzz")  # the repository-wide job does not install the engine's dependencies

from engine import assess, scenario_results  # noqa: E402
from engine.core import entity_chokepoints  # noqa: E402
from engine.scenario import ScenarioInputs, risk_index  # noqa: E402
from engine_fixtures import anomaly, ent, news, quake, snap, tag  # noqa: E402
from engine_sample import feeds, run, summary  # noqa: E402


@pytest.fixture(scope="module")
def sample():
    ents, out = run()
    return ents, out, {e.name: e for e in ents}


def test_the_sheet_maps_with_the_spend_currency_from_the_header(sample):
    ents, _, by = sample
    assert len(ents) == 11 and all(e.kind == "supplier" for e in ents)
    assert all(e.spend_currency == "USD" and e.annual_spend is not None for e in ents)  # "Annual spend (USD)"
    f = by["Formosa Microcircuits"]
    assert (f.countries, f.ports, f.hs_codes, f.currencies, f.criticality) == (("TWN",), ("TWKHH",), ("8542",), ("TWD",), "high")


def test_after_numbers(sample):
    """The "after" column of the PR's before/after table (1.1.0: 42 risks, top 49.2, 3 Medium;
    1.2.0: 20 risks, 13 stories, top 31.0; recorded on this same sample).

    1.3.0 changes two things here: the sanctions risk of Pearl Delta Power Supplies (power
    adapters, HS 8504) is no longer strengthened by "chips" (that was a chapter-85 match; its own
    heading 8504 is not chips), so its score falls from 31.0 to 16.4 and the KRW move (24.8) is
    the top risk; and the typhoon story no longer reaches Saigon Cable Assembly through
    "electronics" (HS 8544 is wire and cable), so that risk falls below the minimum score.
    Likelihoods of multi-item risks are a little lower (each further item counts half as much)."""
    ents, out, _ = sample
    s = summary(ents, out)
    # And suppliers are placed at their city (§5.11): the M6.1 quake off Coquimbo is ~370 km from
    # Andes Copper in Santiago, outside its radius (position beats country), so no longer a risk:
    # 20 → 19 (1.3.0 matching) → 18 risks, 13 → 11 stories, top 31.0 → 24.8 (the KRW move).
    assert (s["risks"], s["stories"], s["max"]) == (18, 11, 24.8)
    assert s["bands"] == {"critical": 0, "high": 0, "medium": 0, "low": 18}
    andes = next(e for e in ents if e.name == "Andes Copper Products")
    assert andes.has_position and not [a for a in out if a.entity_id == andes.id and a.category == "natural_hazard"]


def test_no_risk_rests_on_a_commodity_or_hs_chapter_alone(sample):
    _, out, _ = sample
    assert not [a.fingerprint for a in out if "|commodity:" in a.fingerprint]
    for a in out:
        for ev in a.evidence:
            assert ev.match_type != "commodity"


def test_a_china_chip_story_does_not_reach_the_malaysian_supplier(sample):
    _, out, by = sample
    penang = by["Penang Semicon Assembly"].id
    assert not [a for a in out if a.entity_id == penang and a.category == "sanctions"]
    # it reaches the supplier *in* China, the target of the measures; since 1.3.0 the chip mention no
    # longer strengthens the match, because the supplier makes power adapters (HS 8504), not chips
    [china] = [a for a in out if a.entity_id == by["Pearl Delta Power Supplies"].id and a.category == "sanctions"]
    assert china.evidence[0].match_type == "country"
    assert all("supported_by" not in e.snapshot for e in china.evidence)
    assert {e.event_raw_id for e in china.evidence} == {"s-chips-1", "s-chips-2", "s-chips-3"}
    assert china.explanation == (
        "3 recent news reports describe sanctions aimed at China, where Pearl Delta Power Supplies is based."
    )


def test_one_story_hitting_several_suppliers_shares_one_key(sample):
    _, out, by = sample
    red_sea = [a for a in out if a.factors["story"] == "logistics|event:gdelt:s-redsea-1"]
    assert {by_name for by_name, e in by.items() if e.id in {a.entity_id for a in red_sea}} == {
        "Formosa Microcircuits", "Hanbit Memory Works", "Penang Semicon Assembly", "Pearl Delta Power Supplies",
        "Kanto Passive Components",
    }  # fmt: skip
    assert all(a.evidence[0].snapshot["inferred_from"].startswith("port ") for a in red_sea)


def test_texts_are_plain(sample):
    _, out, _ = sample
    for a in out:
        assert "news news" not in a.title.lower(), a.title
        assert not re.search(r"item\(s\)|noisy|×|signal \d", a.explanation), a.explanation
        assert a.explanation.endswith(".") and a.explanation.count(". ") == 0, a.explanation
        assert [s["label"] for s in a.factors["explain"]["steps"]][-3:] in (
            ["Likelihood", "Impact", "Score"], ["Market-only cap", "Impact", "Score"]
        )  # fmt: skip


@pytest.mark.parametrize("tag_type", ["conflict", "protest", "sanctions", "trade_policy", "port_shipping_disruption",
                                      "natural_hazard", "cyber_incident", "labour_action", "financial_stress"])  # fmt: skip
def test_news_titles_say_news_once_for_every_category(tag_type):
    sup = ent("s", "supplier", name="Acme Parts", countries=["JPN"])
    # 1.3.0 (§5.10): a sanctions country match needs the country to be the target, a cyber one a
    # national-scale incident; the other categories only need Japan to be central (its tag).
    title = {"sanctions": "New sanctions on Japan", "cyber_incident": "Nationwide outage after cyberattack in Japan"}
    n = news("n1", tag(tag_type, 0.9, countries=["JPN"]), title=title.get(tag_type))
    [a] = assess([sup], snap([n]), base_currency=None)
    assert a.title.count("news") == 1 and a.title.endswith("in Japan affecting Acme Parts"), a.title
    # A second, different report (1.3.3: a copy with the same title would be the same item).
    title2 = {"sanctions": "More sanctions on Japan",
              "cyber_incident": "Second nationwide outage after cyberattack in Japan"}
    n2 = news("n2", tag(tag_type, 0.9, countries=["JPN"]), title=title2.get(tag_type))
    two = assess([sup], snap([n, n2]), base_currency=None)[0]
    if tag_type == "sanctions":
        assert two.explanation == "2 recent news reports describe sanctions aimed at Japan, where Acme Parts is based."
        assert a.explanation == "A recent news report describes sanctions aimed at Japan, where Acme Parts is based."
        return
    assert two.explanation.startswith("2 recent news reports about ") and " mention Japan, " in two.explanation
    assert a.explanation.startswith("A recent news report about ") and " mentions Japan, " in a.explanation


def test_company_and_hazard_sentences():
    sup = ent("s", "supplier", name="Acme Parts", countries=["JPN"])
    company = news("n1", tag("labour_action", 0.9, companies=[("Acme Parts", 0.9)]))
    [a] = assess([sup], snap([company]), base_currency=None)
    assert a.title == "Labour action news mentions Acme Parts"
    assert a.explanation == "A recent news report about labour action mentions Acme Parts by name."
    site = ent("p", "site", name="Osaka plant", lat=34.69, lon=135.50)
    [q] = assess([site], snap([quake("q1", 34.7, 135.5)]), base_currency=None)
    assert q.explanation == "An earthquake was recorded 1 km from Osaka plant."
    [q2] = assess([site], snap([quake("q1", 34.7, 135.5), quake("q2", 34.8, 135.6, age_h=5)]), base_currency=None)
    assert q2.explanation.startswith("2 natural hazard events were recorded near Osaka plant; the strongest")


# --- chokepoints (§5.9) --------------------------------------------------------------------------


def test_chokepoints_are_inferred_from_the_port_or_country(sample):
    _, _, by = sample
    cps = {name: dict(entity_chokepoints(e)) for name, e in by.items()}
    assert cps["Formosa Microcircuits"]["taiwan_strait"] == "port TWKHH"
    assert "taiwan_strait" in cps["Hanbit Memory Works"] and "taiwan_strait" in cps["Kanto Passive Components"]
    assert cps["Saigon Cable Assembly"]["malacca"] == "country VNM"  # no port: its country
    assert "taiwan_strait" not in cps["Penang Semicon Assembly"] and "malacca" in cps["Penang Semicon Assembly"]
    assert cps["Andes Copper Products"] == {"panama": "country CHL"}
    assert cps["Gulf Aluminium Extrusions"]["hormuz"] == "country ARE"
    # two-coast countries and Europe: nothing is guessed
    assert cps["Rheinland Prazisionsteile"] == {} and cps["Bajio Plasticos Tecnicos"] == {}


def test_declared_routes_win_and_positions_count():
    route = ent("r", "route", ports=["TWKHH", "NLRTM"], attrs={"chokepoints": ["Panama Canal"]})
    assert entity_chokepoints(route) == [("panama", None)]
    declared = ent("d", "supplier", ports=["TWKHH"], attrs={"chokepoints": ["suez"]})
    assert entity_chokepoints(declared) == [("suez", None)]
    site = ent("sg", "site", countries=["USA"], lat=1.26, lon=103.82)  # a site at the Singapore Strait
    assert dict(entity_chokepoints(site)) == {"singapore": "position"}
    assert entity_chokepoints(ent("c", "customer", countries=["TWN"])) == []  # customers are not shipped from


def test_inferred_chokepoints_count_at_half_a_declared_route():
    route = ent("r", "route", ports=["TWKHH", "NLRTM"], attrs={"chokepoints": ["taiwan_strait"]})
    sup = ent("s", "supplier", ports=["TWKHH"])
    text = news("n1", tag("port_shipping_disruption", 0.9, places=[("Taiwan Strait", 0.8)]))
    out = {a.entity_id: a for a in assess([route, sup], snap([text]), base_currency=None)}
    assert out[route.id].evidence[0].match_confidence == round(0.6 * 0.8, 3)
    assert out[sup.id].evidence[0].match_confidence == round(0.6 * 0.8 * 0.5, 3)
    assert out[sup.id].evidence[0].snapshot["inferred_from"] == "port TWKHH"
    assert out[sup.id].factors["story"] == out[route.id].factors["story"]


def test_taiwan_strait_closes_reaches_the_east_asian_suppliers_only(sample):
    ents, _, _ = sample
    shock = {"shocks": [{"type": "event", "category": "logistics", "severity": 0.8, "chokepoints": ["taiwan_strait"],
                         "label": "Taiwan Strait closes"}]}  # fmt: skip
    res = _run_scenario(ents, shock)
    up = {c["entity_name"] for c in res["changes"] if c["delta"] > 0}
    assert {"Formosa Microcircuits", "Pearl Delta Power Supplies", "Hanbit Memory Works", "Kanto Passive Components"} <= up
    assert not up & {"Rheinland Prazisionsteile", "Bajio Plasticos Tecnicos"}
    assert up <= {"Formosa Microcircuits", "Pearl Delta Power Supplies", "Hanbit Memory Works", "Kanto Passive Components"}
    b, s = res["baseline"]["portfolio_score"], res["scenario"]["portfolio_score"]
    assert 0 < b < s < 100, (b, s)
    assert s <= res["scenario"]["max_score"]  # a mean of per-entity scores: never above the worst entity
    [c] = [c for c in res["changes"] if c["entity_name"] == "Formosa Microcircuits"]
    assert c["title"] == "Scenario: Taiwan Strait closes affecting Formosa Microcircuits"


def _run_scenario(ents, body):
    return scenario_results(ents, feeds(), ScenarioInputs.from_dict(body), base_currency=None)


# --- the risk index (§12) ------------------------------------------------------------------------


def test_risk_index_does_not_grow_with_the_number_of_risks():
    site = ent("p", "site", lat=35.0, lon=139.0, criticality="high")
    many = assess([site], snap([quake(f"q{i}", 35.0, 139.0, severity=0.4, age_h=i) for i in range(40)]),
                  base_currency=None)  # fmt: skip
    one = assess([site], snap([quake("q0", 35.0, 139.0, severity=0.4)]), base_currency=None)
    assert len(many) == len(one) == 1  # one risk either way; and across risks:
    sup = ent("s", "supplier", countries=["JPN"], criticality="high")
    stories = [news(f"n{i}", tag(t, 0.9, countries=["JPN"])) for i, t in enumerate(["conflict"] * 30)]
    out = assess([sup], snap(stories), base_currency=None)
    top = max(a.score for a in out)
    # more risks of one kind add, but never more than one more copy of the worst (§12)
    assert top <= risk_index(out, [sup]) < round(100 * (1 - (1 - top / 100) ** 2), 1) < 100


def test_risk_index_is_a_weighted_mean_over_all_entities():
    a = ent("a", "site", lat=35.0, lon=139.0, criticality="critical")
    b = ent("b", "site", lat=-33.9, lon=151.2, criticality="low")
    out = assess([a, b], snap([quake("q", 35.0, 139.0, severity=1.0)]), base_currency=None)
    [r] = out
    assert risk_index(out, [a, b]) == round((1.0 * r.score + 0.25 * 0) / 1.25, 1)
    assert risk_index([], [a, b]) == 0.0 and risk_index(out, []) == 0.0


def test_scenario_index_before_and_after_differ_when_exposure_is_added(sample):
    ents, _, _ = sample
    res = _run_scenario(ents, {"shocks": [{"type": "event", "category": "geopolitical", "severity": 0.9,
                                            "countries": ["DEU"], "label": "Unrest in Germany"}]})  # fmt: skip
    assert res["scenario"]["portfolio_score"] > res["baseline"]["portfolio_score"]
    assert [c["entity_name"] for c in res["changes"]] == ["Rheinland Prazisionsteile"]


def test_market_only_moves_are_capped_below_high():
    sup = ent("s", "supplier", currencies=["TRY"], criticality="critical", annual_spend=10_000_000)
    other = ent("o", "supplier", annual_spend=1)
    [a] = assess([sup, other], snap(anomalies=[anomaly("fx.rate", "EUR/TRY", score=12.0, age_h=0)]), base_currency=None)
    assert a.score <= 50.0 and a.factors["likelihood"]["capped_from"] > a.likelihood
