"""The What-if risk index (risk-engine.md §12): exposure per entity, the index, story counts."""

from __future__ import annotations

import random

import pytest

pytest.importorskip("rapidfuzz")  # the repository-wide job does not install the engine's dependencies

from engine import Assessment, scenario_results  # noqa: E402
from engine.config import DEFAULT_CONFIG  # noqa: E402
from engine.scenario import (  # noqa: E402
    ScenarioInputs,
    _index,
    _side,
    combine_scores,
    entity_exposures,
    risk_index,
    story_count,
)
from engine_fixtures import ent  # noqa: E402

CATS = ("logistics", "geopolitical", "sanctions", "cyber", "financial")


def risk(e, category: str, score: float, key: str | None = None) -> Assessment:
    fp = f"{e.id}|{category}|{key or score}"
    return Assessment(fp, e.id, category, "t", "x", score / 100.0, 1.0, {"story": f"{category}|{key}"} if key else {})


def test_combine_scores():
    assert combine_scores([]) == 0.0
    assert combine_scores([40.0]) == pytest.approx(40.0)
    assert combine_scores([40.0, 30.0]) == pytest.approx(100 * (1 - 0.6 * 0.7**0.5))
    assert combine_scores([30.0, 40.0]) == combine_scores([40.0, 30.0])  # order does not matter
    many = combine_scores([40.0] * 50)
    assert 40 < many < 100 * (1 - 0.6**2)  # never more than one more copy of the highest
    assert combine_scores([100.0, 10.0]) == 100.0


def test_a_lower_risk_of_the_same_kind_still_raises_the_index():
    """The live test: "Suez Canal closes" added a logistics risk of 27 to suppliers that already
    had a higher shipping risk, and the index stayed at 62 → 62 (the worst per category)."""
    formosa = ent("f", "supplier", name="Formosa Microcircuits", criticality="high")
    other = ent("o", "supplier", criticality="medium")
    base = [risk(formosa, "logistics", 41.0, "port"), risk(formosa, "geopolitical", 35.0, "n1"),
            risk(other, "logistics", 30.0, "port")]  # fmt: skip
    scen = [*base, risk(formosa, "logistics", 27.0, "suez"), risk(other, "logistics", 19.0, "suez")]
    b, s = risk_index(base, [formosa, other]), risk_index(scen, [formosa, other])
    assert b < s, (b, s)


def test_the_index_never_falls_below_zero_or_above_the_most_exposed_entity():
    a = ent("a", "site", criticality="critical")
    b = ent("b", "site", criticality="low")
    out = [risk(a, "logistics", 40.0, "x"), risk(a, "cyber", 30.0, "y"), risk(b, "labour", 70.0, "z")]
    side = _side(out, [a, b])
    assert side["most_exposed"] == {"entity_id": str(b.id), "entity_name": "b", "exposure": 70.0, "risk_count": 1}
    exp_a = 100 * (1 - 0.6 * 0.7)
    assert side["portfolio_score"] == round((1.0 * exp_a + 0.25 * 70.0) / 1.25, 1) <= 70.0
    assert _side([], [a, b])["most_exposed"] is None and _side([], [a, b])["portfolio_score"] == 0.0
    assert _side(out, [])["portfolio_score"] == 0.0


def test_story_count_counts_stories_not_risks():
    a, b = ent("a"), ent("b")
    out = [risk(a, "logistics", 20.0, "suez"), risk(b, "logistics", 30.0, "suez"), risk(a, "cyber", 10.0)]
    assert story_count(out) == 2  # one story reaching two entities, plus a risk with no story key
    side = _side(out, [a, b])
    assert (side["risk_count"], side["story_count"]) == (3, 2)


def test_adding_or_raising_any_risk_strictly_raises_the_index():
    """The property §12 promises, on random portfolios (unrounded)."""
    rng = random.Random(7)
    ents = [ent(f"e{i}", criticality=rng.choice([None, "low", "medium", "high", "critical"])) for i in range(8)]
    for _ in range(400):
        out = [risk(rng.choice(ents), rng.choice(CATS), round(rng.uniform(1, 99), 1), f"k{j}")
               for j in range(rng.randint(0, 25))]  # fmt: skip
        before = _index(entity_exposures(out), ents, DEFAULT_CONFIG)
        exposures = entity_exposures(out)
        assert before <= max(exposures.values(), default=0.0) + 1e-9
        added = [*out, risk(rng.choice(ents), rng.choice(CATS), round(rng.uniform(0.1, 99), 1), "new")]
        assert _index(entity_exposures(added), ents, DEFAULT_CONFIG) > before
        if out:
            i = rng.randrange(len(out))
            r = out[i]
            if r.score < 99.9:
                up = Assessment(r.fingerprint, r.entity_id, r.category, "t", "x",
                                min(0.999, r.likelihood + rng.uniform(0.001, 0.2)), 1.0, r.factors)  # fmt: skip
                raised = [*out[:i], up, *out[i + 1 :]]
                assert _index(entity_exposures(raised), ents, DEFAULT_CONFIG) > before


# --- the 11-supplier sample (tests/fixtures/engine_sample_feeds.json) -------------------------------


@pytest.fixture(scope="module")
def sample_entities():
    from engine_sample import suppliers

    return suppliers()


@pytest.mark.parametrize(
    "chokepoint, label",
    [("suez", "Suez Canal closes"), ("taiwan_strait", "Taiwan Strait closes"), ("panama", "Panama Canal closes")],
)
def test_a_closed_chokepoint_raises_the_index_on_the_sample(sample_entities, chokepoint, label):
    from engine_sample import feeds

    body = {"shocks": [{"type": "event", "category": "logistics", "severity": 0.8, "chokepoints": [chokepoint],
                        "label": label}]}  # fmt: skip
    res = scenario_results(sample_entities, feeds(), ScenarioInputs.from_dict(body), base_currency=None)
    b, s = res["baseline"], res["scenario"]
    assert res["changes"] and all(c["delta"] > 0 for c in res["changes"])  # adds or raises, lowers nothing
    assert b["portfolio_score"] < s["portfolio_score"], (b["portfolio_score"], s["portfolio_score"])
    for side in (b, s):
        assert side["portfolio_score"] <= side["most_exposed"]["exposure"]
        assert side["most_exposed"]["exposure"] >= side["max_score"]
        assert 0 < side["story_count"] <= side["risk_count"]
    assert s["risk_count"] >= b["risk_count"] and s["story_count"] >= b["story_count"]
