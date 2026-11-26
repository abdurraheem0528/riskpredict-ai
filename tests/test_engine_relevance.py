"""Engine 1.3.0: a news country must be central to the article (risk-engine.md §5.10), sanctions and
cyber need more than that, weak items add up less (§5.6), and commodity support is matched on the
entity's own HS heading (§5.3). Fixtures are constructed test data (tests/fixtures/README.md)."""

from __future__ import annotations

import pytest

pytest.importorskip("rapidfuzz")  # the repository-wide job does not install the engine's dependencies

from engine import assess, relevance as R  # noqa: E402
from engine_fixtures import ent, news, snap, tag  # noqa: E402
from engine_sample import LIVE_NOV27, run, summary  # noqa: E402


def risks(entities, events):
    return assess(entities, snap(events), base_currency=None)


def by_entity(out, entity):
    return [a for a in out if a.entity_id == entity.id]


PCB = ent("pcb", "supplier", name="Pearl River PCB Works", countries=["CHN"], hs_codes=["8534"],
          commodities=["Printed circuit boards"], criticality="critical")  # fmt: skip
SENSORS = ent("sensors", "supplier", name="Rhein Sensorik GmbH", countries=["DEU"], hs_codes=["9031"],
              commodities=["Industrial sensors"],
              attrs={"notes": "Pressure and position sensors for the test rigs"})  # fmt: skip
HOUSINGS = ent("housings", "supplier", name="Ruhr Housings", countries=["DEU"], hs_codes=["7616"],
               commodities=["Machined housings"])  # fmt: skip
RUS = ent("rus", "supplier", name="Volga Metals", countries=["RUS"])


# --- the title vocabulary -----------------------------------------------------------------------


@pytest.mark.parametrize("title, countries", [
    ("US imposes new sanctions on Chinese chipmakers", {"USA", "CHN"}),
    ("Join us for the talks", set()),  # "us" is not the United States; "US" is
    ("Tensions rise in the South China Sea", set()),
    ("Indian Ocean shipping lanes reopen", set()),
    ("Beijing protests new rules", {"CHN"}),
    ("North Korea fires missile", {"PRK"}),
    ("South Korean exports rise", {"KOR"}),
    ("Papua New Guinea quake", {"PNG"}),
    ("Germany's data watchdog fines retailer", {"DEU"}),
    ("Georgia votes", set()),  # ambiguous names are refused, as in jobs/analyse/places.py
])  # fmt: skip
def test_title_countries(title, countries):
    assert set(R.title_facts(title).countries) == countries


@pytest.mark.parametrize("title, targets", [
    ("US imposes new sanctions on Chinese chipmakers", {"CHN"}),
    ("EU sanctions against Chinese firms", {"CHN"}),
    ("US sanctions Chinese firms", {"CHN"}),
    ("Washington adds Chinese circuit board makers to export blacklist", {"CHN"}),
    ("US widens chip export curbs on China; Nvidia among firms affected", {"CHN"}),
    ("China hit with EU sanctions over support for Russia's war", {"CHN"}),
    ("Beijing protests new US semiconductor sanctions", {"CHN"}),
    ("Sanctions on China and Russia widen", {"CHN", "RUS"}),
    ("Iran oil sanctions squeeze tanker traffic in the Gulf", {"IRN"}),
    ("North Korea sanctions evasion network exposed by UN panel", {"PRK"}),
    ("EU agrees new sanctions package against Russia", {"RUS"}),
    ("Singapore charges three over AI chips smuggling under US sanctions rules", set()),
    ("European Leaders Aren't Banal, They are Evil", set()),
    ("Do sanctions on semiconductors actually work? Analysts are divided", set()),
])  # fmt: skip
def test_sanction_targets(title, targets):
    assert R.sanction_targets(title) == frozenset(targets)


# --- matching -----------------------------------------------------------------------------------


def test_a_passing_mention_inside_a_long_article_does_not_count():
    # GDELT located China somewhere in the text (not first), the title does not name it
    evil = news("x1", tag("conflict", 0.8), countries=["RUS", "BEL", "CHN"],
                title="European Leaders Aren't Banal, They are Evil")  # fmt: skip
    assert by_entity(risks([PCB, RUS], [evil]), PCB) == []
    # the first located place is central (its evidence says why)
    [a] = by_entity(risks([PCB, RUS], [evil]), RUS)
    assert a.evidence[0].snapshot["central"] == "first_place"
    # and a country named in the title counts wherever GDELT located it
    named = news("x2", tag("conflict", 0.8), countries=["USA", "CHN"],
                 title="China holds live-fire drills near disputed islands")  # fmt: skip
    [b] = risks([PCB], [named])
    assert b.evidence[0].snapshot["central"] == "title"


def test_a_country_named_only_by_its_adjective_is_matched_from_the_title():
    # NewsData / DOC API articles have no countries, and the model does not tag "German" as a place
    strike = news("n1", tag("labour_action", 0.85), title="German rail freight strike to run for three days")
    [a] = risks([SENSORS], [strike])
    assert a.category == "labour" and a.evidence[0].match_value == "DEU"


def test_sanctions_need_the_country_to_be_the_target():
    passing = [
        news("e1", tag("sanctions", 0.66), countries=["BEL", "RUS", "UKR", "USA", "CHN"],
             title="European Leaders Aren't Banal, They are Evil"),
        news("e2", tag("sanctions", 0.58, countries=["ETH", "ERI"]), countries=["ETH", "ERI", "SAU", "CHN"],
             title="Ethiopia's Calculated Use of Maritime Diplomacy | Eritrea Ministry of Information"),
        news("e3", tag("sanctions", 0.9, countries=["RUS"]), countries=["RUS", "BEL", "CHN"],
             title="EU agrees new sanctions package against Russia"),
        news("e4", tag("sanctions", 0.82, commodities=[("AI chips", 0.84)], countries=["SGP"]),
             countries=["SGP", "USA", "CHN"],
             title="Singapore charges three over AI chips smuggling under US sanctions rules"),
    ]  # fmt: skip
    out = risks([PCB, RUS], passing)
    assert by_entity(out, PCB) == []
    [rus] = by_entity(out, RUS)  # Russia is the target of e3
    assert [e.event_raw_id for e in rus.evidence] == ["e3"]
    kept = news("k1", tag("sanctions", 0.9, countries=["USA"]), countries=["USA", "CHN"],
                title="US imposes new sanctions on Chinese chipmakers")  # fmt: skip
    [a] = risks([PCB], [*passing, kept])
    assert a.category == "sanctions" and [e.event_raw_id for e in a.evidence] == ["k1"]
    assert a.explanation == (
        "A recent news report describes sanctions aimed at China, where Pearl River PCB Works is based."
    )


def test_cyber_needs_a_national_incident_or_the_suppliers_sector():
    dropped = [
        news("c1", tag("cyber_incident", 0.9, companies=[("FIFA", 0.88)]), countries=["CHE", "USA", "DEU"],
             title="Hackers gain access to personal data at FIFA"),
        news("c2", tag("cyber_incident", 0.87), countries=["DEU", "USA", "GBR"],  # Germany first, still not national
             title="Ransomware attacks rose 40% worldwide this year, report finds"),
        news("c3", tag("cyber_incident", 0.74, countries=["DEU"]), countries=["DEU"],
             title="Germany's data watchdog fines online retailer over customer data leak"),
    ]  # fmt: skip
    assert risks([SENSORS, HOUSINGS], dropped) == []
    rail = news("k1", tag("cyber_incident", 0.91, countries=["DEU"]), countries=["DEU"],
                title="Germany hit by nationwide cyberattack on rail network")  # fmt: skip
    sensor = news("k2", tag("cyber_incident", 0.9), countries=["DEU", "AUT"],
                  title="Ransomware attack halts production at German sensor maker")  # fmt: skip
    out = risks([SENSORS, HOUSINGS], [*dropped, rail, sensor])
    [s] = by_entity(out, SENSORS)
    assert s.category == "cyber" and {e.event_raw_id for e in s.evidence} == {"k1", "k2"}
    [h] = by_entity(out, HOUSINGS)  # a housing maker is reached by the national incident only
    assert [e.event_raw_id for e in h.evidence] == ["k1"]


def test_non_news_events_keep_their_own_location():
    from engine_fixtures import event

    flood = event("gdacs", "FL-1", "hazard.flood", countries=["DEU", "CHN"], severity=0.66, title="Flood alert")
    assert {a.entity_id for a in risks([PCB, SENSORS], [flood])} == {PCB.id, SENSORS.id}


# --- commodity support on the HS heading (§5.3) ----------------------------------------------------


def _china(t, commodities, title="New sanctions on China"):
    return news(t, tag("sanctions", 0.85, commodities=commodities, countries=["CHN"]), countries=["CHN"], title=title)


def test_support_matches_the_entitys_own_heading_and_names_its_product():
    # "AI chips" is HS 8542; the supplier makes printed circuit boards (8534): no support, and no "like those"
    [a] = risks([PCB], [_china("n1", [("AI chips", 0.84)])])
    assert "supported_by" not in a.evidence[0].snapshot
    assert "like those" not in a.explanation and "chips" not in a.explanation
    # a circuit-board story strengthens it, and the sentence names the supplier's own product
    [b] = risks([PCB], [_china("n2", [("circuit boards", 0.8)])])
    assert b.evidence[0].snapshot["supported_by"]["match_value"] == "HS 8534"
    assert b.explanation == (
        "A recent news report describes sanctions aimed at China, and it mentions circuit boards, which is in the "
        "same product group (HS 8534) as the printed circuit boards Pearl River PCB Works supplies."
    )
    assert b.evidence[0].match_confidence > a.evidence[0].match_confidence
    # the exact product name: a plain sentence
    [c] = risks([PCB], [_china("n3", [("printed circuit boards", 0.8)])])
    assert c.evidence[0].snapshot["supported_by"]["match_value"] == "printed circuit boards"
    assert c.explanation.endswith("it mentions printed circuit boards, which Pearl River PCB Works supplies.")


def test_a_two_digit_code_still_matches_on_its_chapter():
    chapter_only = ent("ch", "supplier", name="Delta Electric", countries=["CHN"], hs_codes=["85"])
    [a] = risks([chapter_only], [_china("n1", [("AI chips", 0.84)])])
    assert a.evidence[0].snapshot["supported_by"]["match_value"] == "HS 85"
    assert a.explanation.endswith(
        "it mentions AI chips, which is in the product group (HS 85) that Delta Electric supplies."
    )
    six = ent("six", "supplier", name="Hsinchu Logic", countries=["CHN"], hs_codes=["854231"],
              commodities=["Processors"])  # fmt: skip
    [b] = risks([six], [_china("n2", [("AI chips", 0.84)])])
    assert b.evidence[0].snapshot["supported_by"]["match_value"] == "HS 8542"  # 6 digits → heading 8542


# --- the 27 Nov live sheet, before / after -----------------------------------------------------------

# Engine 1.2.0 on tests/fixtures/engine_live_nov27_feeds.json (recorded before the change):
# 12 risks, 12 stories, max likelihood 0.957, 1 risk ≥ 0.8 likely, top score 70.9, the China sanctions
# risk on 11 items ("11 recent news reports about sanctions involving China mention AI chips like those
# Pearl River PCB Works supplies (HS 8534)").
KEPT = {  # (supplier, category) → the items that still reach it
    ("Pearl River PCB Works", "sanctions"): {"k-us-chinese", "k-pcb-blacklist", "k-china-hit"},
    ("Pearl River PCB Works", "geopolitical"): {f"k-china-drills-{i}" for i in range(1, 8)},
    ("Pearl River PCB Works", "logistics"): {"k-shenzhen-port"},
    ("Rhein Sensorik GmbH", "cyber"): {"k-de-rail", "k-sensor-maker"},
    ("Rhein Sensorik GmbH", "labour"): {"k-de-rail-strike"},
    # Formosa is placed at its city, Hsinchu (§5.11): the M6.3 USGS quake near Hualien is 108 km away,
    # at the edge of its 123 km radius (confidence 0.13), so on its own it stays below the minimum score.
    ("Formosa Microcircuits", "natural_hazard"): {"k-tw-quake-news"},
    ("Formosa Microcircuits", "geopolitical"): {"k-china-drills-6"},
    ("Penang Semicon Assembly", "natural_hazard"): {"k-penang-flood"},
    ("Haiphong Cable & Harness", "natural_hazard"): {"FL-2001"},
    ("Haiphong Cable & Harness", "logistics"): {"k-haiphong"},
}


@pytest.fixture(scope="module")
def live():
    ents, out = run(LIVE_NOV27)
    return ents, out, {e.id: e.name for e in ents}


def test_live_sheet_keeps_the_relevant_stories_only(live):
    _, out, names = live
    got = {(names[a.entity_id], a.category): {e.event_raw_id for e in a.evidence} for a in out}
    assert got == KEPT
    every = {e.event_raw_id for a in out for e in a.evidence}
    assert not {r for r in every if r.startswith("x-")}  # no passing mention reaches anyone


def test_live_sheet_after_numbers(live):
    ents, out, _ = live
    s = summary(ents, out)
    assert (s["risks"], s["stories"], s["max_likelihood"], s["likely"]) == (10, 10, 0.458, 0)
    assert s["by_category"] == {"cyber": 1, "geopolitical": 2, "labour": 1, "logistics": 2, "natural_hazard": 3,
                                "sanctions": 1}  # fmt: skip
    [sanc] = [a for a in out if a.category == "sanctions"]
    assert sanc.explanation == (
        "3 recent news reports describe sanctions aimed at China, and one mentions circuit board, which is in the "
        "same product group (HS 8534) as the printed circuit boards Pearl River PCB Works supplies."
    )
    # seven central but weak conflict stories: no longer "79% likely" (1.2.0), but corroborated
    [geo] = [a for a in out if a.category == "geopolitical" and len(a.evidence) == 7]
    assert 0.3 < geo.likelihood < 0.4
    for a in out:
        assert "like those" not in a.explanation
