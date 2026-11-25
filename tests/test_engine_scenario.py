"""What-if scenarios (pure part), watch terms (§7) and advice helpers (§8)."""

from __future__ import annotations

import re

import pytest

pytest.importorskip("rapidfuzz")

from engine import (  # noqa: E402
    ENGINE_VERSION,
    ScenarioError,
    ScenarioInputs,
    advice_messages,
    assess,
    needs_new_advice,
    scenario_results,
    template_advice,
    watch_terms_for,
)
from engine_fixtures import NOW, ent, fx_history, news, quake, snap, tag  # noqa: E402


def results(entities, inputs, feeds=None, base="EUR"):
    return scenario_results(entities, feeds or snap(), ScenarioInputs.from_dict(inputs), base_currency=base)


# --- parsing --------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "body, msg",
    [
        ({}, "at least one"),
        ({"shocks": [{"type": "meteor"}]}, "type must be"),
        ({"shocks": [{"type": "event", "category": "aliens", "severity": 1}]}, "unknown category"),
        ({"shocks": [{"type": "event", "category": "logistics", "severity": 2}]}, "severity"),
        ({"shocks": [{"type": "event", "category": "logistics", "severity": 1, "ports": ["XX"]}]}, "invalid port"),
        ({"shocks": [{"type": "event", "category": "logistics", "severity": 1, "lat": 3}]}, "both lat and lon"),
        ({"shocks": [{"type": "event", "category": "logistics", "severity": 1, "chokepoints": ["x"]}]}, "chokepoint"),
        ({"shocks": [{"type": "fx", "currency": "XYZ", "pct_change": 5}]}, "currency"),
        ({"shocks": [{"type": "fx", "currency": "TRY", "pct_change": 900}]}, "pct_change"),
        ({"shocks": [{"type": "sanction", "entity_id": "nope"}]}, "uuid"),
        ({"entity_overrides": [{"entity_id": "00000000-0000-0000-0000-000000000001", "criticality": "huge"}]},
         "criticality"),
        ({"entity_overrides": [{"entity_id": "00000000-0000-0000-0000-000000000001", "annual_spend": "-1"}]},
         "zero or more"),
        ({"shocks": [{"type": "fx", "currency": "TRY", "pct_change": 1}] * 21}, "at most"),
    ],
)
def test_invalid_inputs(body, msg):
    with pytest.raises(ScenarioError, match=msg):
        ScenarioInputs.from_dict(body)


def test_valid_inputs_parse():
    i = ScenarioInputs.from_dict({
        "shocks": [{"type": "event", "category": "logistics", "severity": 1, "ports": ["nl rtm"],
                    "chokepoints": ["Suez Canal"]},
                   {"type": "fx", "currency": "TRY", "pct_change": -20}],
        "entity_overrides": [{"entity_id": "00000000-0000-0000-0000-000000000001", "annual_spend": "125.50"}],
    })  # fmt: skip
    assert i.shocks[0].ports == ("NLRTM",) and i.shocks[0].chokepoints == ("suez",)
    assert i.shocks[1].pct_change == -20.0 and str(i.entity_overrides[0].annual_spend) == "125.50"


# --- shocks -----------------------------------------------------------------------------------


def test_port_closed():
    route = ent("r", "route", name="Shanghai-Rotterdam", ports=["CNSHA", "NLRTM"], criticality="critical")
    site = ent("p", "site", ports=["DEHAM"])
    res = results([route, site], {"shocks": [{"type": "event", "category": "logistics", "severity": 1,
                                              "ports": ["NLRTM"], "label": "Rotterdam closed"}]})  # fmt: skip
    assert res["baseline"]["risk_count"] == 0 and res["scenario"]["risk_count"] == 1
    [c] = res["changes"]
    assert c["entity_name"] == "Shanghai-Rotterdam" and c["baseline_score"] is None
    assert c["scenario_score"] == round(100 * 0.99 * 0.8, 1) == c["delta"]
    assert c["title"] == "Scenario: Rotterdam closed affecting Shanghai-Rotterdam"
    assert res["scenario"]["by_category"]["logistics"] == 1 and res["engine_version"] == ENGINE_VERSION
    assert res["as_of"] == "2026-11-24T12:00:00Z"


def test_hazard_shock_by_position_and_chokepoint():
    plant = ent("p", "site", lat=35.0, lon=139.0)
    route = ent("r", "route", attrs={"chokepoints": ["hormuz"]})
    res = results([plant, route], {"shocks": [
        {"type": "event", "category": "natural_hazard", "severity": 0.8, "lat": 35.1, "lon": 139.1, "radius_km": 50},
        {"type": "event", "category": "geopolitical", "severity": 0.9, "chokepoints": ["hormuz"]}]})  # fmt: skip
    cats = {c["entity_id"]: c["category"] for c in res["changes"]}
    assert cats == {str(plant.id): "natural_hazard", str(route.id): "logistics"}


def test_fx_shock_is_sized_by_the_pairs_own_volatility():
    lira = ent("l", "currency", currencies=["TRY"])
    feeds = snap(series=fx_history("TRY", start=30.0))
    small = results([lira], {"shocks": [{"type": "fx", "currency": "TRY", "pct_change": 0.1}]}, feeds)
    big = results([lira], {"shocks": [{"type": "fx", "currency": "TRY", "pct_change": -25}]}, feeds)
    assert small["changes"] == []  # an ordinary daily move adds nothing
    [c] = big["changes"]
    assert c["scenario_score"] == round(100 * 0.99 * 0.3, 1)  # |z| ≥ 8 → signal 1 (L capped at 0.99), I 0.6 × 0.5
    with pytest.raises(ScenarioError, match="no recent exchange-rate history"):
        results([lira], {"shocks": [{"type": "fx", "currency": "TRY", "pct_change": -25}]})
    with pytest.raises(ScenarioError, match="same"):
        results([lira], {"shocks": [{"type": "fx", "currency": "EUR", "pct_change": 5}]})


def test_fx_shock_against_another_base():
    lira = ent("l", "currency", currencies=["TRY"])
    feeds = snap(series={**fx_history("TRY", start=30.0), **fx_history("USD", start=1.1)})
    res = results([lira], {"shocks": [{"type": "fx", "currency": "TRY", "base_currency": "USD", "pct_change": 30}]},
                  feeds, base="EUR")  # fmt: skip
    assert len(res["changes"]) == 1 and res["changes"][0]["fingerprint"].endswith("currency:USD/TRY")


def test_sanctions_shock_screens_the_entity_itself():
    sup = ent("s", "supplier", name="Acme Trading LLC", countries=["ARE"], criticality="high")
    other = ent("o", "supplier", name="Zeta Components")
    res = results([sup, other], {"shocks": [{"type": "sanction", "entity_id": str(sup.id)}]})
    [c] = res["changes"]
    assert c["entity_id"] == str(sup.id) and c["category"] == "sanctions"
    with pytest.raises(ScenarioError, match="not found"):
        results([sup], {"shocks": [{"type": "sanction", "entity_id": "00000000-0000-0000-0000-000000000009"}]})


# --- overrides ----------------------------------------------------------------------------------


def test_overrides_use_the_same_scoring():
    a = ent("a", "site", lat=35.0, lon=139.0, criticality="low")
    b = ent("b", "site", lat=35.0, lon=139.0, criticality="low")
    feeds = snap([quake("q", 35.0, 139.0, severity=1.0)])
    res = results([a, b], {"entity_overrides": [{"entity_id": str(a.id), "criticality": "critical"},
                                                {"entity_id": str(b.id), "exclude": True}]}, feeds)  # fmt: skip
    by = {c["entity_id"]: c for c in res["changes"]}
    base = assess([a], feeds, base_currency="EUR")[0].score
    assert by[str(a.id)]["baseline_score"] == base and by[str(a.id)]["scenario_score"] == pytest.approx(base * 4, abs=0.2)
    assert by[str(b.id)]["scenario_score"] is None and by[str(b.id)]["delta"] == -base
    assert res["changes"][0]["entity_id"] == str(a.id)  # largest |delta| first


def test_dropping_a_supplier_changes_others_spend_share():
    a = ent("a", "supplier", countries=["MMR"], annual_spend=50, criticality="medium")
    b = ent("b", "supplier", annual_spend=450)
    feeds = snap([news("n", tag("conflict", 1.0, countries=["MMR"]))])
    res = results([a, b], {"entity_overrides": [{"entity_id": str(b.id), "exclude": True}]}, feeds)
    [c] = res["changes"]
    assert c["entity_id"] == str(a.id) and c["delta"] > 0  # a's share goes from 10 % to 100 % (capped at 25 %)


def test_override_countries_add_matches():
    sup = ent("s", "supplier")
    feeds = snap([news("n", tag("conflict", 1.0, countries=["MMR"]))])
    res = results([sup], {"entity_overrides": [{"entity_id": str(sup.id), "countries": ["MMR"]}]}, feeds)
    assert len(res["changes"]) == 1
    with pytest.raises(ScenarioError, match="not in this workspace"):
        results([sup], {"entity_overrides": [{"entity_id": "00000000-0000-0000-0000-000000000009", "exclude": True}]})


def test_portfolio_score():
    a = ent("a", "site", lat=35.0, lon=139.0, criticality="critical")
    feeds = snap([quake("q", 35.0, 139.0, severity=1.0)])
    res = results([a], {"entity_overrides": [{"entity_id": str(a.id), "criticality": "low"}]}, feeds)
    s = res["baseline"]
    assert s["risk_count"] == 1 and s["portfolio_score"] == s["max_score"]
    assert set(s["by_category"]) == {"natural_hazard", "logistics", "geopolitical", "sanctions", "trade_policy",
                                     "cyber", "financial", "labour", "other"}  # fmt: skip


# --- watch terms ------------------------------------------------------------------------------------


def test_watch_terms():
    ents = [
        ent("lib1", "software", software=["pkg:pypi/Jinja2@3", "pkg:npm/%40babel/core@7", "pkg:deb/debian/curl"]),
        ent("lib2", "software", software=["pkg:pypi/jinja2@2"]),
        ent("not-software", "supplier", software=["pkg:pypi/flask"]),
        *[ent(f"s{i}", "supplier", name=f"Supplier Number {i} GmbH", criticality="high", annual_spend=i * 10)
          for i in range(5)],
        ent("crit", "supplier", name="Critical Parts Ltd", criticality="critical"),
        ent("short", "supplier", name="Xy Ltd", criticality="critical"),  # "xy": too short
        ent("low", "supplier", name="Low Priority Inc", criticality="low"),
        ent("cust", "customer", name="Big Customer", criticality="critical"),
    ]  # fmt: skip
    terms = watch_terms_for(ents)
    assert terms[:2] == [("osv_package", "@babel/core", "npm"), ("osv_package", "jinja2", "PyPI")]
    gdelt = [t for t in terms if t[0] == "gdelt_query"]
    assert gdelt == [
        ("gdelt_query", '"critical parts"', ""),
        ("gdelt_query", '"supplier number 4"', ""),
        ("gdelt_query", '"supplier number 3"', ""),
        ("gdelt_query", '"supplier number 2"', ""),
        ("gdelt_query", '"supplier number 1"', ""),
    ]
    assert watch_terms_for([]) == []


# --- advice --------------------------------------------------------------------------------------


def _risk():
    sup = ent("s", "supplier", name="Nordwind Logistik", countries=["DEU"])
    feeds = snap([news("n1", tag("labour_action", 0.9, companies=[("Nordwind Logistik", 0.9)]),
                       title="Strike at Nordwind Logistik depots")])  # fmt: skip
    [a] = assess([sup], feeds, base_currency="EUR")
    return a, sup


def test_advice_messages_carry_only_the_allowed_facts():
    a, sup = _risk()
    msgs = advice_messages(a, sup)
    assert [m["role"] for m in msgs] == ["system", "user"]
    system, user = msgs[0]["content"], msgs[1]["content"]
    assert "120 words" in system and "no new numbers" in system.lower().replace("do not add any new", "no new")
    assert "Strike at Nordwind Logistik depots (2026-11-24)" in user
    assert f"score: {a.score:.1f}" in user and "supplier \"Nordwind Logistik\"" in user
    assert "DEU" not in user  # entity attributes beyond kind and name stay out


def test_template_advice_is_deterministic_and_has_no_digits():
    a, sup = _risk()
    text = template_advice(a, sup)
    assert text == template_advice(a, sup) and "Nordwind Logistik" in text
    assert not re.search(r"\d", text.replace(sup.name, ""))


@pytest.mark.parametrize(
    "old, new, oe, ne, want",
    [
        (None, 10.0, set(), {"a"}, True),
        (41.0, 59.0, {"a"}, {"a"}, False),  # same 40–60 band
        (59.0, 61.0, {"a"}, {"a"}, True),
        (100.0, 99.0, {"a"}, {"a"}, False),  # 100 is in the top band
        (30.0, 30.0, {"a", "b"}, {"a", "c"}, True),
    ],
)
def test_needs_new_advice(old, new, oe, ne, want):
    assert needs_new_advice(old, new, oe, ne) is want


def test_now_is_part_of_the_results():
    res = results([ent("a", "site")], {"entity_overrides": [{"entity_id": str(ent("a", "site").id)}]})
    assert res["as_of"] == NOW.strftime("%Y-%m-%dT%H:%M:%SZ") and res["changes"] == []
