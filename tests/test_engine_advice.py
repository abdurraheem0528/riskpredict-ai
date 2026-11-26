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
