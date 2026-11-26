"""Risk engine scoring maths: signal, recency decay, noisy-OR, impact, fingerprints (risk-engine.md §5–§6)."""

from __future__ import annotations

import math
import re
from dataclasses import replace
from pathlib import Path

import pytest

pytest.importorskip("rapidfuzz")

from engine import DEFAULT_CONFIG, ENGINE_VERSION, assess  # noqa: E402
from engine import scoring as S  # noqa: E402
from engine.core import fingerprint  # noqa: E402
from engine_fixtures import NOW, anomaly, ent, event, quake, snap, tag, uid  # noqa: E402

CFG = DEFAULT_CONFIG
SPEC = (Path(__file__).resolve().parent.parent / "docs/contracts/risk-engine.md").read_text()


# --- signal ---------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "ev, expected",
    [
        (event("gdacs", "EQ-1", "hazard.earthquake", severity=2 / 3), 2 / 3),
        (event("usgs", "us1", "hazard.earthquake"), 0.5),  # no severity → 0.5
        (event("portwatch", "TC-1", "port.disruption", severity=1.0), 1.0),
        (event("eonet", "E1", "hazard.wildfire"), 0.5),
        (event("eonet", "E2", "hazard.dust_haze"), 0.3),
        (event("ofac", "1:designation:x", "sanction.designation"), 1.0),
        (event("ofac", "1:update:x", "sanction.update"), 0.8),
        (event("gdelt", "n1", "news", tag=tag("conflict", 0.8)), 0.25 + 0.5 * 0.8),
        (event("gdelt", "n2", "news", tag=tag("other", None)), 0.15),
        (event("gdelt", "n3", "news", tag=tag("protest", None)), 0.25),  # NULL model confidence → 0
    ],
)
def test_event_signal(ev, expected):
    assert S.event_signal(ev, CFG) == pytest.approx(expected)


def test_cve_signals_use_epss():
    kev = event("cisa_kev", "CVE-2026-1", "cyber.exploited_vulnerability")
    assert S.event_signal(kev, CFG, epss=0.0) == 0.9  # floor
    assert S.event_signal(kev, CFG, epss=0.97) == 0.97
    osv = event("osv", "GHSA-1", "cyber.vulnerability", severity=0.4)
    assert S.event_signal(osv, CFG, epss=0.0) == 0.4
    assert S.event_signal(osv, CFG, epss=0.6) == 0.6
    assert S.event_signal(event("osv", "GHSA-2", "cyber.vulnerability"), CFG) == 0.5  # no CVSS
    withdrawn = event("osv", "GHSA-3", "cyber.vulnerability", severity=0.9, attrs={"withdrawn": "2026-11-01"})
    assert S.event_signal(withdrawn, CFG) is None


@pytest.mark.parametrize("z, s", [(3.5, 0.5), (-3.5, 0.5), (5.75, 0.75), (8.0, 1.0), (-12.0, 1.0), (2.0, 0.5)])
def test_robust_z_signal(z, s):
    assert S.anomaly_signal(anomaly("fx.rate", "EUR/TRY", score=z), CFG) == pytest.approx(s)


@pytest.mark.parametrize("score, s", [(0.7, 0.56), (1.3, 0.8), (-0.1, 0.0)])
def test_isolation_forest_signal(score, s):
    assert S.anomaly_signal(anomaly("fx.rate", "EUR/TRY", method="isolation_forest", score=score), CFG) == pytest.approx(s)


# --- recency --------------------------------------------------------------------------------


def test_recency_halves_every_half_life_and_stops_after_four():
    hl = 72.0
    assert S.recency(0, hl, CFG) == 1.0
    assert S.recency(72, hl, CFG) == pytest.approx(0.5)
    assert S.recency(144, hl, CFG) == pytest.approx(0.25)
    assert S.recency(4 * 72, hl, CFG) == pytest.approx(1 / 16)  # exactly 4 half-lives: still counted
    assert S.recency(4 * 72 + 0.01, hl, CFG) is None
    assert S.recency(-5, hl, CFG) == 1.0  # future timestamps count as now


def test_recency_never_older_than_365_days():
    hl = CFG.half_life("financial.wb")  # 4 half-lives would be 730 days
    assert S.recency(364 * 24, hl, CFG) is not None
    assert S.recency(366 * 24, hl, CFG) is None


def test_ongoing_event_has_age_zero():
    ev = event("gdacs", "FL-1", "hazard.flood", age_h=200, attrs={"to_date": "2026-11-30T00:00:00"})
    assert S.event_age_hours(ev, NOW) == 0.0
    ended = event("gdacs", "FL-2", "hazard.flood", age_h=200, attrs={"to_date": "2026-11-01T00:00:00"})
    assert S.event_age_hours(ended, NOW) == pytest.approx(200)


@pytest.mark.parametrize(
    "category, source, series, hl",
    [
        ("natural_hazard", "usgs", None, 72), ("logistics", "portwatch", None, 168),
        ("geopolitical", "gdelt", None, 120), ("sanctions", "ofac", None, 720),
        ("cyber", "cisa_kev", None, 720), ("cyber", "osv", None, 2160),
        ("financial", None, "fx.rate", 120), ("financial", None, "wb.FP.CPI.TOTL.ZG", 4380),
    ],
)  # fmt: skip
def test_half_lives(category, source, series, hl):
    assert CFG.half_life(S.half_life_key(category, source=source, series=series)) == hl


def test_decay_lowers_the_score_and_ages_out():
    site = ent("plant", "site", lat=34.69, lon=135.50, criticality="high")
    scores = []
    for age in (0, 72, 144, 288, 289):
        out = assess([site], snap([quake("q", 34.7, 135.5, age_h=age, severity=1.0)]), base_currency=None,
                     config=replace(CFG, min_score=0.0))
        scores.append(out[0].score if out else None)
    assert scores[0] > scores[1] > scores[2] > scores[3]
    assert scores[1] == pytest.approx(scores[0] / 2, abs=0.2)
    assert scores[4] is None  # past 4 half-lives (288 h): the risk is gone


# --- likelihood -----------------------------------------------------------------------------


def test_noisy_or():
    assert S.noisy_or([], 0.99) == 0.0
    assert S.noisy_or([0.5], 0.99) == 0.5
    assert S.noisy_or([0.5, 0.5], 0.99) == 0.75
    assert S.noisy_or([0.3, 0.2, 0.1], 0.99) == pytest.approx(1 - 0.7 * 0.8 * 0.9, abs=1e-3)
    assert S.noisy_or([1.0], 0.99) == 0.99  # capped
    assert S.noisy_or([0.9] * 10, 0.99) == 0.99


def test_contribution_is_rounded_product():
    assert S.contribution(0.71, 0.93, 0.88) == round(0.71 * 0.93 * 0.88, 3)


def test_two_anomaly_methods_on_one_point_are_separate_items():
    lira = ent("try", "currency", currencies=["TRY"])
    one = assess([lira], snap(anomalies=[anomaly("fx.rate", "EUR/TRY", score=5.75)]), base_currency="EUR")
    both = assess([lira], snap(anomalies=[anomaly("fx.rate", "EUR/TRY", score=5.75),
                                          anomaly("fx.rate", "EUR/TRY", method="isolation_forest", score=0.9)]),
                  base_currency="EUR")  # fmt: skip
    assert len(both[0].evidence) == 2
    assert both[0].likelihood > one[0].likelihood


def test_likelihood_uses_exactly_the_kept_evidence():
    site = ent("plant", "site", lat=0.0, lon=0.0)
    quakes = [quake(f"q{i:02}", 0.0, 0.01 * i, severity=0.2, age_h=i) for i in range(60)]
    [a] = assess([site], snap(quakes), base_currency=None, config=replace(CFG, max_evidence=50))
    assert len(a.evidence) == 50
    assert a.likelihood == S.noisy_or([e.contribution for e in a.evidence], 0.99)
    assert [e.contribution for e in a.evidence] == sorted((e.contribution for e in a.evidence), reverse=True)
    assert a.factors["likelihood"]["items"][0]["evidence_key"] == a.evidence[0].evidence_key


# --- impact ---------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "criticality, share, category, expected",
    [
        (None, None, "sanctions", 0.5),  # NULL criticality = 0.5, exposure unknown
        ("critical", None, "sanctions", 1.0),
        ("low", None, "other", 0.1),  # 0.4 × 0.25
        ("low", 0.0, "other", 0.05),  # 0.4 × 0.125 = 0.05 (the floor)
        ("high", 0.125, "logistics", 0.5),  # 0.8 × (0.375 + 0.25)
        ("high", 0.5, "natural_hazard", 0.787),  # share above 25 % is maximal: 0.9 × (0.375 + 0.5)
        ("critical", 0.25, "cyber", 0.9),
    ],
)
def test_impact_formula(criticality, share, category, expected):
    value, f = S.impact(ent("x", criticality=criticality), category, share, CFG)
    assert value == expected
    assert f["value"] == expected and f["category_multiplier"] == CFG.category_multipliers[category]
    assert f["exposure_norm"] == (None if share is None else min(1.0, share / 0.25))


def test_impact_floor_and_ceiling():
    assert S.impact(ent("x", criticality="low"), "other", 0.0, CFG)[0] == 0.05
    assert S.impact(ent("x", criticality="critical"), "sanctions", 3.0, CFG)[0] == 1.0


def test_exposure_shares_convert_spend_with_the_latest_rate():
    a = ent("a", annual_spend=100, spend_currency="EUR")
    b = ent("b", annual_spend=200, spend_currency="USD")  # 200 USD at 2 USD/EUR = 100 EUR
    c = ent("c")  # no spend: unknown
    cust = ent("cust", "customer", revenue_share=0.3)
    site = ent("site", "site")
    rates = {"EUR": 1.0, "USD": 2.0}
    shares = S.exposure_shares([a, b, c, cust, site], rates)
    assert shares[a.id] == pytest.approx(0.5) and shares[b.id] == pytest.approx(0.5)
    assert shares[c.id] is None and shares[site.id] is None
    assert shares[cust.id] == pytest.approx(0.3)


def test_exposure_shares_without_rates():
    same = [ent("a", annual_spend=100, spend_currency="JPY"), ent("b", annual_spend=300, spend_currency="JPY")]
    assert S.exposure_shares(same, {"EUR": 1.0})[same[0].id] == pytest.approx(0.25)  # one currency: raw amounts
    mixed = [ent("a", annual_spend=100, spend_currency="JPY"), ent("b", annual_spend=300, spend_currency="KRW")]
    assert S.exposure_shares(mixed, {"EUR": 1.0, "JPY": 160.0})[mixed[0].id] is None  # KRW unknown
    zero = [ent("a", annual_spend=0, spend_currency="EUR")]
    assert S.exposure_shares(zero, {"EUR": 1.0})[zero[0].id] is None  # total 0


def test_latest_rates():
    pts = ((NOW.replace(day=20), 30.0), (NOW.replace(day=21), 31.5))
    assert S.latest_rates({("fx.rate", "EUR/TRY"): pts}) == {"EUR": 1.0, "TRY": 31.5}


# --- fingerprints, config, determinism -------------------------------------------------------


def test_fingerprint_is_stable_and_bounded():
    e = uid("x")
    assert fingerprint(e, "logistics", "port:NLRTM") == f"{e}|logistics|port:NLRTM"
    long = fingerprint(e, "sanctions", "sanctions_name:" + "very long name " * 20)
    assert len(long) <= 200 and long == fingerprint(e, "sanctions", "sanctions_name:" + "very long name " * 20)
    assert long.startswith(f"{e}|sanctions|sanctions_name:sha256:")


def test_geo_matches_share_one_fingerprint():
    site = ent("plant", "site", lat=34.69, lon=135.50)
    out = assess([site], snap([quake("q1", 34.7, 135.5), quake("q2", 34.6, 135.4)]), base_currency=None)
    assert len(out) == 1 and out[0].fingerprint == f"{site.id}|natural_hazard|geo_radius:near"
    assert len(out[0].evidence) == 2


def test_score_property_matches_the_generated_column():
    site = ent("plant", "site", lat=34.69, lon=135.50, criticality="high")
    [a] = assess([site], snap([quake("q1", 34.7, 135.5)]), base_currency=None)
    assert a.score == round(100 * a.likelihood * a.impact, 1)
    assert a.factors["engine_version"] == ENGINE_VERSION
    assert re.fullmatch(r"\d+\.\d+\.\d+", ENGINE_VERSION)


def test_default_config_matches_the_contract():
    """Spot-check that DEFAULT_CONFIG holds the numbers written in risk-engine.md."""
    assert CFG.min_score == 5.0 and CFG.max_evidence == 50 and CFG.likelihood_cap == 0.99
    assert "min(0.99, 1 − Π (1 − p_i))" in SPEC
    assert dict(CFG.criticality_weights) == {"low": 0.25, "medium": 0.5, "high": 0.75, "critical": 1.0}
    assert "{low: 0.25, medium: 0.5, high: 0.75, critical: 1.0, NULL: 0.5}" in SPEC
    for cat, m in CFG.category_multipliers.items():
        assert re.search(rf"\b{cat} {m}\b", SPEC), cat
    assert CFG.sanctions_threshold == 0.92 and "sim ≥ 0.92" in SPEC
    assert CFG.company_threshold == 0.95 and "sim ≥ 0.95" in SPEC
    assert dict(CFG.country_confidence) == {"site": 0.8, "supplier": 0.6, "customer": 0.5, "route": 0.5, "port": 0.5}
    assert math.isclose(CFG.full_exposure_share, 0.25)
    # §5.9 (engine 1.2.0)
    assert CFG.support_weight == 0.5 and "`w = support_weight = 0.5`" in SPEC
    assert CFG.market_only_score_cap == 50.0 and "market_only_score_cap = 50" in SPEC
    assert CFG.inferred_chokepoint_factor == 0.5 and "`inferred_chokepoint_factor = 0.5`" in SPEC
    assert CFG.support_match_types == {"commodity", "currency"} and CFG.market_event_prefix == "market."


def test_same_inputs_same_assessments_in_the_same_order():
    ents = [ent(f"s{i}", "site", lat=34.0 + i / 10, lon=135.0, countries=["JPN"]) for i in range(5)]
    evs = [quake(f"q{i}", 34.0 + i / 7, 135.1, age_h=i) for i in range(6)]
    evs += [event("gdacs", "FL-1", "hazard.flood", countries=["JPN"], severity=1 / 3)]
    a = assess(ents, snap(evs), base_currency=None)
    b = assess(list(reversed(ents)), snap(list(reversed(evs))), base_currency=None)
    assert a == b and len(a) >= 5


def test_engine_imports_only_what_the_contract_allows():
    """jobs.codes and jobs.analyse.anomalies only; nothing that calls the network or draws random numbers."""
    allowed_jobs = {"jobs.codes", "jobs.analyse.anomalies", "jobs"}
    banned = ("httpx", "requests", "urllib.request", "socket", "random", "groq", "openai", "anthropic")
    root = Path(__file__).resolve().parent.parent / "engine"
    for path in sorted(root.glob("*.py")):
        src = path.read_text()
        for mod in re.findall(r"^\s*(?:from|import)\s+(jobs[\w.]*)", src, re.M):
            assert mod in allowed_jobs, f"{path.name} imports {mod}"
        for mod in re.findall(r"^\s*(?:from|import)\s+([\w.]+)", src, re.M):
            assert not mod.startswith(banned), f"{path.name} imports {mod}"
