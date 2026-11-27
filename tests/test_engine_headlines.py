"""Engine 1.3.4 (risk-engine.md §5.10 A–G): real 27 Nov headlines that 1.3.3 linked as country news;
engine 1.3.5 (§5.10 C): real 26–27 Nov trade-policy headlines with a ban, a restriction or a curb.

Every line of the briefs' test tables is a case here (tests/engine_nov27_headlines.py holds the rows);
the suppliers are test data. Each must-drop row is also checked against the rule named for it."""

from __future__ import annotations

import pytest

pytest.importorskip("rapidfuzz")  # the repository-wide job does not install the engine's dependencies

from engine import EventTag, assess, dupes, relevance as R  # noqa: E402
from engine_fixtures import ent, event, news, snap, tag  # noqa: E402
from engine_nov27_headlines import CONFIG, CONSTRUCTED, COPY_GROUPS, ROWS, SUPPLIERS, TAGS, item, reaches  # noqa: E402

DROP = [r for r in ROWS if r.expect == "drop"]
KEEP = [r for r in ROWS if r.expect == "keep"]
EITHER = [r for r in ROWS if r.expect == "either"]


def _id(r) -> str:
    return f"{r.category}-{r.country}-{r.title[:40]}"


# --- the table ----------------------------------------------------------------------------------


@pytest.mark.parametrize("row", DROP, ids=_id)
def test_must_no_longer_count(row):
    assert not reaches(row)


@pytest.mark.parametrize("row", KEEP, ids=_id)
def test_must_still_count(row):
    assert reaches(row)


# Either verdict is acceptable; these are the 1.3.4 and 1.3.5 results, recorded so a change shows.
# ("Carney courts Chinese investment" and "Japan Weighs Safety Duties" were here in 1.3.4, both
# False; since 1.3.5 they are must-drop rows: "deals" and "duties" alone are no trade-policy word.)
EITHER_RESULT = {
    "\"Terrorism knows no borders\"": True,  # India named, "terrorism" a conflict word, no anniversary word
    "ForMin Neculaescu": True,  # "German" names Germany, "military" a conflict word
    "India seizes Nepal flood crisis": True,  # India named, "flood" a hazard word
    "Nvidia Chip Sellers Scramble": False,  # China named; "tightens … rules" is no trade-policy word
    "Investigation Reveals Nvidia Chips": False,  # "Chinese-Owned" names China; no trade-policy word
}


@pytest.mark.parametrize("row", EITHER, ids=_id)
def test_either_way_results(row):
    [want] = [v for k, v in EITHER_RESULT.items() if row.title.startswith(k)]
    assert reaches(row) is want


def _head(row):
    return R.headline(row.title, row.url)


@pytest.mark.parametrize("row", DROP, ids=_id)
def test_each_drop_is_the_named_fix(row):
    """The rule the brief names for the row is one that rejects it."""
    head, tag_type = _head(row), TAGS[row.category]
    checks = {
        "A": lambda: row.country not in R.central_countries(head, [], row.countries or ()),
        "B": lambda: head != row.title,
        "C": lambda: not R.category_cue(head, tag_type),
        "D": lambda: R.not_current(head),
        "E": lambda: not R.national_scale(head) and not R.sector_hit(head, frozenset({"module", "capacitor"})),
        "F": lambda: row.country not in R.sanction_targets(head),
    }
    for fix in (f.strip() for f in row.fixes.split(",")):
        assert checks[fix](), fix


def test_the_first_place_threshold_separates_the_table():
    """A: among the rows whose headline names no country, every must-keep article GDELT located in
    one country and every must-drop one in four or more; 3 is the widest limit that drops them all."""
    def unnamed(rows):
        return [r for r in rows if r.countries and not R.title_facts(_head(r)).countries]

    kept = [len(set(r.countries)) for r in unnamed(KEEP)]
    dropped = [len(set(r.countries)) for r in unnamed(DROP) if "A" in r.fixes]
    assert max(kept) == 1 and min(dropped) == 4
    assert max(kept) <= R.FIRST_PLACE_MAX_COUNTRIES < min(dropped)
    assert CONFIG.first_place_max_countries == R.FIRST_PLACE_MAX_COUNTRIES == 3


def test_a_country_named_in_the_headline_still_counts_wherever_gdelt_located_it():
    # A changes only the first place; China is named, so the order of GDELT's list does not matter
    [row] = [r for r in KEEP if r.title.startswith("Chinese military patrols")]
    assert row.countries[0] == "PHL" and reaches(row)


# --- B: the headline ------------------------------------------------------------------------------


@pytest.mark.parametrize("title, url, head", [
    ("Yemen Crisis: Houthi Forward Gains near Aden – Modern Tokyo Times", None,
     "Yemen Crisis: Houthi Forward Gains near Aden"),
    ("Yemen Crisis: Houthi Forward Gains near Aden – Modern Tokyo Times", "https://moderntokyotimes.com/a/",
     "Yemen Crisis: Houthi Forward Gains near Aden"),
    ("FIFA hackers had access to 'business and personal information,' organization says - National", None,
     "FIFA hackers had access to 'business and personal information,' organization says"),
    ("The Revolt Against Oligarchy | Countercurrents", None, "The Revolt Against Oligarchy"),
    ("US to hike tariffs on China, EU, Brazil and others over subsidized factories | News, Sports, Jobs", None,
     "US to hike tariffs on China, EU, Brazil and others over subsidized factories"),
    ("Indonesia's Natuna compromise with China: A point of no return - Academia", None,
     "Indonesia's Natuna compromise with China: A point of no return"),
    ("US plans tariff hikes on China, EU and others | The Arkansas Democrat-Gazette", None,
     "US plans tariff hikes on China, EU and others"),
    ("Ethiopia's Calculated Use of Maritime Diplomacy | Eritrea Ministry of Information", None,
     "Ethiopia's Calculated Use of Maritime Diplomacy"),
    # the URL tells the outlet: its host name, or its initials
    ("Strike Wave Over Pay Across Europe! - Workers Revolutionary Party", "https://wrp.org.uk/news/x/",
     "Strike Wave Over Pay Across Europe!"),
    ("Port strike enters third day - Countercurrents", "https://countercurrents.org/2026/11/x/",
     "Port strike enters third day"),
    ("Port strike enters third day - Lanka Business Online", None, "Port strike enters third day"),
    ("Port strike enters third day - Reuters", None, "Port strike enters third day"),
    ("Port strike enters third day | World | Example Daily", None, "Port strike enters third day"),
    # wire tags
    ("Analysis-Trump's new tariffs aim to prise global trade from China's grip", None,
     "Trump's new tariffs aim to prise global trade from China's grip"),
    ("EXCLUSIVE-China weighs curbs on rare earth exports", None, "China weighs curbs on rare earth exports"),
    ("UPDATE 2-Japan quake halts chip plants", None, "Japan quake halts chip plants"),
    ("UPDATE 2-EXCLUSIVE-Japan quake halts chip plants", None, "Japan quake halts chip plants"),
    ("Exclusive: China weighs curbs on rare earth exports", None, "China weighs curbs on rare earth exports"),
])  # fmt: skip
def test_headline_drops_the_outlet_and_the_wire_tag(title, url, head):
    assert R.headline(title, url) == head


@pytest.mark.parametrize("title, url", [
    # never part of a real headline: lower-case words, a clause, a short head, a compound word
    ("All-time great war film with 96% rating leaves Netflix on December 1 - last chance to see", None),
    ("Strike Wave Over Pay Across Europe! - Workers Revolutionary Party", None),  # no outlet word, no URL
    ("Strike Wave Over Pay Across Europe! - Workers Revolutionary Party", "https://example.org/x"),
    ("China holds live-fire drills near disputed islands", None),
    ("Live-fire drills near Taiwan resume", None),
    ("Focus-group study finds support for tariffs", None),
    ("Taiwan detains China-linked cargo ship over severed undersea cable", None),
    ("Ukraine war | Russia Launches Drone Attack", None),  # what stays would be two words
    ("Ukraine war live | Russia launches 40 drones at Kyiv overnight, officials say", None),  # longer than the head
    ("Rail strike shuts ports - Germany braces for delays", None),
    ("China Imposes Export Curbs - US Firms Hit Hard", None),
    ("Russia 'preparing new mass strike soon': Zelenskiy", None),
    ("Germany: INSA poll shows 58 percent of citizens demand early elections amid fiscal crisis", None),
    ("Pound-to-Euro Forecast: GBP Slides as ECB Turns Hawkish", None),
    ("2025–26 budget sets new export levies", None),
])  # fmt: skip
def test_headline_never_strips_a_real_headline(title, url):
    assert R.headline(title, url) == title


def test_an_outlet_name_does_not_make_a_country_central():
    # "Tokyo" (the capital, so the government) was read from the outlet's name
    raw = "Yemen Crisis: Houthi Forward Gains near Aden – Modern Tokyo Times"
    assert "JPN" in R.title_facts(raw).countries and "JPN" not in R.title_facts(R.headline(raw)).countries
    # the tag reads the whole title too: a place found only in the outlet's name does not count
    jp = SUPPLIERS["JPN"]

    def tagged(*places):  # as jobs/analyse/tagging.py stores it: each place with its country, then the countries
        return EventTag("labour_action", 0.8, "model", {
            "companies": [], "commodities": [], "places": [{"text": t, "confidence": 0.8, "country": c} for t, c in places],
            "countries": sorted({c for _, c in places if c})})  # fmt: skip

    times = event("newsdata", "jt1", "news", title="Dock workers strike at Busan port - The Japan Times",
                  tag=tagged(("Busan", None), ("Japan", "JPN")))  # fmt: skip
    assert R.headline_tag_countries(times.title, R.headline(times.title), times.tag.entities) == []
    assert assess([jp], snap([times]), base_currency=None, config=CONFIG) == []
    # a country the headline names stays, with or without an outlet
    ports = event("newsdata", "jt2", "news", title="Dock workers strike at Japan's ports - The Japan Times",
                  tag=tagged(("Japan", "JPN")))  # fmt: skip
    assert R.headline_tag_countries(ports.title, R.headline(ports.title), ports.tag.entities) == ["JPN"]
    [a] = assess([jp], snap([ports]), base_currency=None, config=CONFIG)
    assert a.category == "labour" and a.evidence[0].snapshot["title"] == ports.title  # the evidence keeps the title


# --- C: a cue word of the category ------------------------------------------------------------------


@pytest.mark.parametrize("tag_type, title, cue", [
    ("conflict", "Germany: INSA poll shows 58 percent of citizens demand early elections amid fiscal crisis", False),
    ("conflict", "Minister faces conflict of interest probe in Germany", False),
    ("conflict", "Price war erupts among German supermarkets", False),
    ("conflict", "Clashes erupt on the border as troops mass", True),
    ("labour_action", "Russia 'preparing new mass strike soon': Zelenskiy", False),  # "mass strike", no labour word
    ("labour_action", "Mass strike by rail workers paralyses Belgium", True),
    ("labour_action", "Russia launches missile strikes on Kyiv", False),
    ("labour_action", "Drone strike hits fuel depot in Germany", False),
    ("labour_action", "Dock workers strike at Hamburg port", True),
    ("labour_action", "Unionized rail workers launch work-to-rule campaign, 4 train services canceled", True),
    ("trade_policy", "Japan services producer prices jump 4.2% in October, firming the case for a BOJ hike", False),
    ("trade_policy", "Japan raises import duties on steel", True),
    # 1.3.5: a ban, a restriction or a curb (real headlines, engine_nov27_headlines.py)
    ("trade_policy", "Netherlands Seeks to Block US Bill Restricting China’s Access to Chipmaking Tools", True),
    ("trade_policy", "China extends pause on tighter mineral restrictions", True),
    ("trade_policy", "Netherlands to Fight Rollout of Proposed US Curbs on ASML", True),
    ("trade_policy", "Ten Smart Cities for $50 Billion: What’s Behind Indonesia’s Deals With China", False),
    ("trade_policy", "Japan Weighs Safety Duties for AI Firms, With Penalties for Defying Orders", False),
    ("port_shipping_disruption", "Taiwan warns China could impose a blockade", True),  # constructed (CONSTRUCTED)
    ("port_shipping_disruption", "'Diplomacy key to South China Sea disputes'", False),
    ("financial_stress", "Pound-to-Euro Forecast: GBP Slides as ECB Turns Hawkish", False),
    ("financial_stress", "Lira crash deepens as Turkey imposes capital controls", True),
    ("natural_hazard", "India seizes Nepal flood crisis to rebuild influence", True),
    ("sanctions", "Anything at all", True),  # sanctions and cyber have their own checks
    ("cyber_incident", "Anything at all", True),
])  # fmt: skip
def test_category_cues(tag_type, title, cue):
    assert R.category_cue(title, tag_type) is cue


def test_the_1_3_5_cue_words():
    """Trade policy: the bans, restrictions and curbs of the tagger's label; shipping: the blockades
    and blockages of its label. "Deal(s)" and "duties" alone stay out (they are not the category)."""
    trade, ship = set(R.CATEGORY_CUES["trade_policy"]), set(R.CATEGORY_CUES["port_shipping_disruption"])
    for w in ("ban", "bans", "banned", "banning", "restrict", "restricts", "restricted", "restricting",
              "restriction", "restrictions", "curb", "curbs", "curbed", "curbing"):  # fmt: skip
        assert (w,) in trade, w
    for w in ("blockade", "blockades", "blockaded", "blockage", "blockages"):
        assert (w,) in ship, w
    assert not {("deal",), ("deals",), ("duties",), ("duty",)} & trade


@pytest.mark.parametrize("row", CONSTRUCTED, ids=_id)
def test_a_blockade_is_a_shipping_cue(row):
    # constructed: "blockade" is the headline's only shipping word, so it counts since 1.3.5 only
    head = _head(row)
    assert R.category_cue(head, TAGS[row.category])
    assert not R.category_cue(head.replace("blockade", "embargo"), TAGS[row.category])
    assert reaches(row)


def test_chokepoint_and_company_matches_need_no_cue():
    # C is for country matches only: a company named in the news still counts without a cue word
    sup = ent("acme", "supplier", name="Acme Parts", countries=["JPN"])
    company = news("c1", tag("conflict", 0.9, companies=[("Acme Parts", 0.9)]), title="Acme Parts chief speaks out")
    [a] = assess([sup], snap([company]), base_currency=None, config=CONFIG)
    assert a.evidence[0].match_type == "company_name"
    route = ent("r", "route", name="Asia–Europe loop", attrs={"chokepoints": ["suez"]})
    canal = news("c2", tag("conflict", 0.9, places=[("Suez Canal", 0.9)]), title="Talks on the Suez Canal continue")
    out = assess([route], snap([canal]), base_currency=None, config=CONFIG)
    assert out and out[0].evidence[0].match_type == "chokepoint"


# --- D: not current events --------------------------------------------------------------------------


@pytest.mark.parametrize("title, old", [
    ("26/11 Attacks: 18 Years Later, Remembering the Night That Changed Mumbai", True),
    ("Ten years on, Fukushima towns still empty", True),
    ("City commemorates victims of the 2016 quake", True),
    ("M/S Reliance Chemotax Industries Ltd vs Union Of India on 26 November, 2026", True),
    ("Tata Steel Ltd v. State Of Odisha on 3 March, 2025", True),
    ("India recalls envoy from Canada over terrorism row", False),  # recalls an envoy: news
    ("Toyota recalls 1 million cars in Japan", False),  # a product recall: news
    ("Memorial Hospital in Ohio hit by ransomware attack", False),  # an institution's name
    ("Japan exports rise 5% year-on-year in October", False),
    ("Fighting continues for years on the border", False),  # no number before "years"
    ("Court orders India to pay arbitration award on 26 November", False),  # no "<party> vs <party>"
])  # fmt: skip
def test_not_current(title, old):
    assert R.not_current(title) is old


# --- E: cyber ---------------------------------------------------------------------------------------


def test_a_crime_bust_is_not_a_national_incident():
    assert not R.national_scale("Hyderabad: Cyber Crime Police Arrest Two for Impersonation, Circulating Obscene Photos")
    assert not R.national_scale("Police raid finds hackers behind attack on national rail network")  # a bust
    assert R.national_scale("Germany hit by nationwide cyberattack on rail network")
    assert R.national_scale("Taiwan weathers wave of cyberattacks ahead of Saturday's elections")
    assert ("police",) not in R.NATIONAL_SCALE


def test_a_sector_word_inside_a_crime_phrase_is_no_sector_hit():
    cues = R.sector_cues(["Power modules"], [], None)
    assert "module" in cues
    title = "Delhi Police bust fake Instagram shopping, Amazon delivery fraud module; two arrested, Rs 1.04 lakh refunded"
    assert not R.sector_hit(title, cues)
    assert R.sector_hit("Ransomware attack halts production at Indian power module maker", cues)
    assert R.sector_hit("Fraud ring targets power module suppliers", cues)  # "power module", not "fraud module"
    ind = SUPPLIERS["IND"]
    maker = event("newsdata", "m1", "news", title="Ransomware attack halts production at Indian power module maker",
                  tag=tag("cyber_incident", 0.9))  # fmt: skip
    [a] = assess([ind], snap([maker]), base_currency=None, config=CONFIG)
    assert a.category == "cyber"


# --- F: sanctions -----------------------------------------------------------------------------------


@pytest.mark.parametrize("title, targets", [
    ("COVID-19 and its Two-Folded Impact vis a vis India's Export Restriction on Drugs", set()),
    ("China's export controls on gallium hit chipmakers", set()),
    ("China's new export controls on Japan anger Tokyo", {"JPN"}),  # the target after "on" still counts
    ("Philippines' export ban on nickel ore", set()),
    ("China to blacklist US defence firms", {"USA"}),
    ("EU will sanction Russian banks", {"RUS"}),
    ("Calls to ban TikTok in US grow", set()),  # the ban is in the US, not on it
    ("Sanctions on China's chipmakers widen", {"CHN"}),  # a possessive after "on" is still the target
])  # fmt: skip
def test_sanction_targets_1_3_4(title, targets):
    assert R.sanction_targets(title) == frozenset(targets)


# --- G: copies --------------------------------------------------------------------------------------


@pytest.mark.parametrize("group", COPY_GROUPS, ids=lambda g: g[0][:40])
def test_copies_that_differ_by_outlet_or_wire_tag_count_once(group):
    keys = {dupes.title_key(t) for t in group}
    assert len(keys) == 1 and None not in keys
    chn = SUPPLIERS["CHN"]
    items = [event("gdelt", f"g{i}", "news", title=t, tag=tag("trade_policy", 0.8), countries=("CHN",))
             for i, t in enumerate(group)]  # fmt: skip
    [a] = assess([chn], snap(items), base_currency=None, config=CONFIG)
    assert len(a.evidence) == 1


def test_spelling_variants_stay_separate():
    # prise / pry and subsidized / subsidised are kept apart: no spelling table (two reports at most)
    assert dupes.title_key("US to hike tariffs on China EU Brazil and others over subsidised factories") != \
        dupes.title_key("US to hike tariffs on China, EU, Brazil, and others over subsidized factories")  # fmt: skip
    assert dupes.title_key("Trump's new tariffs aim to prise global trade from China's grip") != \
        dupes.title_key("Trump's new tariffs aim to pry global trade from China's grip")  # fmt: skip


def test_the_api_copy_keys_use_the_headline_too():
    a, b = COPY_GROUPS[0]
    assert dupes.item_keys("gdelt", "news", a, None) == dupes.item_keys("newsdata", "news", b, None)


def test_rows_are_real_items():
    # the table rows are country matches (no company, place or commodity in their tags)
    for r in ROWS:
        assert item(r).tag.entities["companies"] == [] and item(r).tag.entities["places"] == []
