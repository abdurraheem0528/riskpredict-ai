"""Template advice (risk-engine.md §8): sensible wording for every entity kind × risk category."""

from __future__ import annotations

import re

import pytest

pytest.importorskip("rapidfuzz")  # the repository-wide job does not install the engine's dependencies

from engine import Assessment, template_advice  # noqa: E402
from engine.advice import TEMPLATES  # noqa: E402
from engine_fixtures import ent  # noqa: E402

KINDS = ("supplier", "site", "customer", "route", "port", "currency", "commodity", "software")
CATEGORIES = ("natural_hazard", "logistics", "geopolitical", "sanctions", "trade_policy", "cyber", "financial",
              "labour", "other")  # fmt: skip
NAMES = {"supplier": "Rhein Sensorik GmbH", "site": "Dortmund plant", "customer": "Nordhafen Retail",
         "route": "Kaohsiung to Hamburg", "port": "Hamburg", "currency": "Taiwan dollar",
         "commodity": "Printed circuit boards", "software": "Jinja2"}  # fmt: skip
COMPANIES = ("supplier", "customer")  # the only kinds you can ask or tell


def _risk(e, category: str, driver: str = "country:DEU") -> Assessment:
    return Assessment(f"{e.id}|{category}|{driver}", e.id, category, "t", "x", 0.5, 0.5, {})


def test_the_templates_cover_every_kind_and_category():
    assert set(TEMPLATES) == set(CATEGORIES)
    for cat, by_kind in TEMPLATES.items():
        assert set(by_kind) == set(KINDS), cat


@pytest.mark.parametrize("kind", KINDS)
@pytest.mark.parametrize("category", CATEGORIES)
def test_every_kind_and_category_reads_sensibly(kind, category):
    e = ent(f"{kind}-{category}", kind, name=NAMES[kind])
    text = template_advice(_risk(e, category), e)
    assert text == template_advice(_risk(e, category), e)  # deterministic
    assert 60 <= len(text) <= 400 and text.endswith(".") and text[0].isupper(), text
    assert NAMES[kind] in text and "{" not in text and "}" not in text, text
    assert not re.search(r"\d", text.replace(NAMES[kind], "")), text  # no numbers (as for Groq's reply)
    low = text.lower()
    if kind != "software":  # patching is for software only
        assert not re.search(r"\bpatch|deployed|vulnerab", low), text
    if kind not in COMPANIES:  # a route, port, market, product or own site cannot be asked or told
        assert not re.search(rf"\b(ask|contact|tell) {re.escape(NAMES[kind])}\b", text, re.I), text
    if kind in ("currency", "commodity", "software", "route", "port"):
        assert "your contact there" not in low, text


def test_a_suppliers_cyber_risk_is_about_the_supplier_not_a_patch():
    """The live test: Rhein Sensorik GmbH (a sensor supplier) got "Find where … is deployed and
    apply the vendor's patch"."""
    sup = ent("rhein", "supplier", name="Rhein Sensorik GmbH")
    text = template_advice(_risk(sup, "cyber", "company_name:rhein sensorik"), sup)
    assert text.startswith("Ask Rhein Sensorik GmbH whether the incident affects")
    assert "data" in text and "delays" in text and "patch" not in text
    lib = ent("lib", "software", name="Jinja2")
    assert "patch" in template_advice(_risk(lib, "cyber", "software:PyPI:jinja2"), lib)


def test_sanctions_advice_depends_on_a_list_name_match():
    for kind in COMPANIES:
        e = ent(f"s-{kind}", kind, name="Acme Trading LLC")
        listed = template_advice(_risk(e, "sanctions", "sanctions_name:acme trading"), e)
        news = template_advice(_risk(e, "sanctions", "country:CHN"), e)
        assert "possible match against the sanctions list" in listed and "compliance" in listed
        assert "match" not in news and "compliance" in news and listed != news
    site = ent("s-site", "site", name="Dortmund plant")  # sites are never name-screened
    want = TEMPLATES["sanctions"]["site"].replace("{name}", "Dortmund plant")
    assert template_advice(_risk(site, "sanctions", "sanctions_name:x"), site) == want


def test_odd_names_and_unknown_categories():
    e = ent("odd", "supplier", name="Braces {name} & Co")
    assert "Braces {name} & Co" in template_advice(_risk(e, "labour"), e)
    assert template_advice(_risk(e, "unknown"), e) == template_advice(_risk(e, "other"), e)


# --- the Groq prompt (§8, ADVICE_VERSION 2) ------------------------------------------------------

from engine import ADVICE_VERSION, advice_messages, chokepoints  # noqa: E402
from engine.advice import SYSTEM_PROMPT  # noqa: E402

MAX_STEPS, MAX_STEP_WORDS = 3, 20  # worker valid_advice: Groq's reply is held to the same


def _matched(e, category: str, match_type: str, match_value: str, **kw) -> Assessment:
    factors = {"explain": {"matched": {"match_type": match_type, "match_value": match_value}}}
    return Assessment(f"{e.id}|{category}|{match_type}:{match_value}", e.id, category, "Risk title", "Why", 0.5, 0.6,
                      factors, **kw)  # fmt: skip


def _user_lines(risk: Assessment, e) -> list[str]:
    msgs = advice_messages(risk, e)
    assert [m["role"] for m in msgs] == ["system", "user"] and msgs[0]["content"] == SYSTEM_PROMPT
    return msgs[1]["content"].splitlines()


def test_the_prompt_asks_for_three_short_steps_and_no_invented_places():
    p = SYSTEM_PROMPT
    assert "exactly 3 steps" in p and "one step per line" in p and "no numbering, bullets or headings" in p
    assert "one sentence of at most 16 words and starts with a verb" in p
    assert "Name no place, route, port or company unless it appears in the message" in p
    assert "name only the route given as the usual detour" in p and "do not name a route" in p
    # the rules it had before
    for rule in ("Do not add any new numbers", "Do not include links", "Do not restate the score",
                 "Do not invent facts"):  # fmt: skip
        assert rule in p
    assert ADVICE_VERSION == "2"


def test_a_chokepoint_risk_says_where_and_the_usual_detour():
    route = ent("busan-rtm", "route", name="Busan to Rotterdam", countries=["KOR", "NLD"], ports=["KRPUS", "NLRTM"])
    lines = _user_lines(_matched(route, "logistics", "chokepoint", "bab_el_mandeb"), route)
    assert lines[:4] == ['Entity: route "Busan to Rotterdam"', "Category: logistics", "Place: Bab el-Mandeb",
                         "Usual detour: around the Cape of Good Hope"]  # fmt: skip
    assert lines[4] == "Risk: Risk title"
    hormuz = _user_lines(_matched(route, "logistics", "chokepoint", "hormuz"), route)
    assert hormuz[2:4] == ["Place: Strait of Hormuz", "Usual detour: none by sea"]
    panama = _user_lines(_matched(route, "logistics", "chokepoint", "panama"), route)
    assert panama[3] == "Usual detour: through the Suez Canal, or by rail between US West Coast and East Coast ports"


def test_a_country_or_port_risk_says_where_and_no_detour():
    site = ent("eindhoven", "site", name="Atlas Eindhoven Assembly Plant", countries=["NLD"])
    lines = _user_lines(_matched(site, "trade_policy", "country", "NLD", countries=("NLD",)), site)
    assert lines[1:3] == ["Category: trade_policy", "Place: the Netherlands"] and lines[3].startswith("Risk:")
    port = ent("hph", "supplier", name="Haiphong Cable & Harness", countries=["VNM"], ports=["VNHPH"])
    lines = _user_lines(_matched(port, "logistics", "port", "VNHPH", ports=("VNHPH",)), port)
    assert lines[2] == "Place: Haiphong" and not any(x.startswith("Usual detour") for x in lines)


def test_a_hazard_by_distance_names_the_entitys_city_as_the_api_does():
    sup = ent("formosa", "supplier", name="Formosa Microcircuits", countries=["TWN"], ports=["TWKHH"], city="Hsinchu")
    lines = _user_lines(_matched(sup, "natural_hazard", "geo_radius", "84 km", countries=("TWN",)), sup)
    assert lines[2] == "Place: Hsinchu"
    no_city = ent("formosa2", "supplier", name="Formosa Microcircuits", countries=["TWN"], ports=["TWKHH"])
    assert _user_lines(_matched(no_city, "natural_hazard", "geo_radius", "84 km", countries=("TWN",)), no_city)[2] \
        == "Place: Taiwan"


def test_no_place_line_for_a_market_or_company_match():
    sup = ent("twd", "supplier", name="Formosa Microcircuits", countries=["TWN"])
    for mt, mv in (("currency", "EUR/TWD"), ("company_name", "formosa microcircuits"), ("commodity", "copper")):
        lines = _user_lines(_matched(sup, "financial", mt, mv), sup)
        assert not any(x.startswith(("Place:", "Usual detour:")) for x in lines), (mt, lines)


def test_every_chokepoint_has_the_detour_the_brief_gives():
    want = {
        "suez": "around the Cape of Good Hope", "bab_el_mandeb": "around the Cape of Good Hope",
        "panama": "through the Suez Canal, or by rail between US West Coast and East Coast ports",
        "malacca": "through the Sunda Strait or the Lombok Strait",
        "singapore": "through the Sunda Strait or the Lombok Strait",
        "taiwan_strait": "east of Taiwan, through the open Pacific",
        "cape_of_good_hope": "through the Red Sea and the Suez Canal, when they are safe",
        "hormuz": "none by sea", "bosporus": "none by sea", "gibraltar": "none by sea", "dover": "none by sea",
    }  # fmt: skip
    assert {c.id: c.detour for c in chokepoints()} == want


def _steps(text: str) -> list[str]:
    """The dashboard's ``adviceSteps`` (web/src/screens/RiskView.tsx): one per line, or one per
    sentence when the text is one paragraph."""
    lines = [re.sub(r"^\s*(?:[-*•]|\d+[.)])\s*", "", x).strip() for x in re.split(r"\n+", text)]
    lines = [x for x in lines if x]
    if len(lines) > 1:
        return lines
    return [s.strip() for s in re.findall(r"[^.!?]+[.!?]+(?=\s|$)|[^.!?]+$", text) if s.strip()] or [text]


def test_every_template_is_at_most_three_steps_of_twenty_words():
    texts = [t for by_kind in TEMPLATES.values() for t in by_kind.values()]
    from engine.advice import SANCTIONS_NAME

    texts += list(SANCTIONS_NAME.values())
    for text in texts:
        steps = _steps(text)  # {name} counts as one word
        assert 1 <= len(steps) <= MAX_STEPS, text
        assert all(len(s.split()) <= MAX_STEP_WORDS for s in steps), [(len(s.split()), s) for s in steps]
    assert _steps("Call them. Order early! Check terms") == ["Call them.", "Order early!", "Check terms"]
