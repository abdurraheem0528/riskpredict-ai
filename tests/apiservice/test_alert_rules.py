"""Which risk changes raise which alerts (risk-engine.md §9), legacy titles, and the plain
re-rendered explanation of risks an older engine wrote. Pure: no database."""

from __future__ import annotations

from types import SimpleNamespace as NS

import pytest

pytest.importorskip("fastapi")  # the repository-wide CI job does not install the API

from api.core import legacy  # noqa: E402
from api.core.serialize import title_text  # noqa: E402
from worker.scoring import _alerts_for, sanctions_name_match  # noqa: E402

EID = "6f2c1d7e-0000-4000-8000-000000000001"


def _risk(driver: str, title: str = "t", category: str = "sanctions") -> dict:
    return {"id": "r1", "category": category, "title": title, "fingerprint": f"{EID}|{category}|{driver}"}


def _created(score: float):
    return NS(change="created", new_score=score, old_score=None)


def test_only_a_list_name_match_is_a_possible_sanctions_match():
    name = _risk("sanctions_name:acme trading", "Sanctions listing may match Acme Trading")
    [(kind, sev, title, key)] = _alerts_for(_created(30), name, 60)
    assert (kind, sev, key) == ("sanctions_match", "critical", "sanctions_match:r1")
    assert title == "Possible sanctions match: Sanctions listing may match Acme Trading"
    assert sanctions_name_match(_risk("sanctions_name:sha256:" + "0" * 32))  # a long name, hashed

    news = _risk("country:CHN", "Sanctions news on AI chips affecting Haiphong Cable & Harness")
    assert not sanctions_name_match(news)
    assert _alerts_for(_created(30), news, 60) == []  # below the workspace's threshold: no alert
    [(kind, sev, title, _)] = _alerts_for(_created(65), news, 60)
    assert (kind, sev, title) == ("risk_new", "warning", "New risk: Sanctions news on AI chips affecting Haiphong Cable & Harness")
    [(kind, *_)] = _alerts_for(NS(change="rescored", new_score=70, old_score=50), news, 60)
    assert kind == "risk_escalated"
    assert not sanctions_name_match(_risk("sanctions_name:x", category="geopolitical"))


def test_titles_from_the_older_engine_say_news_once():
    assert title_text("Sanctions news news on AI chips affecting Haiphong Cable & Harness") == (
        "Sanctions news on AI chips affecting Haiphong Cable & Harness"
    )
    assert title_text("Possible sanctions match: News news on X") == "Possible sanctions match: News on X"
    assert title_text("Good news newsletter") == "Good news newsletter" and title_text("") == ""


def _evidence(**kw) -> dict:
    row = {"kind": "event", "event_source": "gdelt", "event_raw_id": "a1", "anomaly_series": None, "anomaly_key": None,
           "anomaly_method": None, "match_type": "country", "match_value": "CHN", "match_confidence": 0.6,
           "signal": 0.9, "recency": 0.8, "contribution": 0.432,
           "snapshot": {"title": "Chip export rules tighten", "type": "news"}}  # fmt: skip
    row.update(kw)
    return row


def test_an_old_engines_explanation_is_re_rendered_plainly():
    pytest.importorskip("rapidfuzz")  # the engine's dependencies
    old = {"category": "sanctions", "factors": {"likelihood": {"method": "noisy_or"}},
           "explanation": "2 item(s): noisy-OR 1 − (1 − 0.432) × (1 − 0.2) = 0.546"}  # fmt: skip
    ent = {"kind": "supplier", "name": "Haiphong Cable & Harness", "hs_codes": ["8544"]}
    text = legacy.explanation(old, [_evidence(), _evidence(event_raw_id="a2", contribution=0.2)], ent)
    assert text == "2 recent news reports about sanctions mention China, where Haiphong Cable & Harness is based."
    hazard = _evidence(event_source="usgs", match_type="geo_radius", match_value="12 km",
                       snapshot={"title": "M6.1", "type": "hazard.earthquake", "distance_km": 12.0})  # fmt: skip
    assert legacy.explanation({**old, "category": "natural_hazard"}, [hazard], ent) == (
        "An earthquake was recorded 12 km from Haiphong Cable & Harness."
    )
    fx = _evidence(kind="anomaly", event_source=None, anomaly_series="fx.rate", anomaly_key="USD/VND",
                   anomaly_method="robust_z", match_type="currency", match_value="USD/VND", snapshot={})  # fmt: skip
    assert legacy.explanation({**old, "category": "financial"}, [fx], ent) == (
        "The VND moved unusually sharply against the USD, and you pay Haiphong Cable & Harness in VND."
    )
    # written by the current engine, no evidence, or no entity: keep the stored text
    assert legacy.explanation({**old, "factors": {"explain": {"steps": []}}}, [_evidence()], ent) is None
    assert legacy.explanation(old, [], ent) is None and legacy.explanation(old, [_evidence()], None) is None
    legacy.explanation(old, [_evidence(kind="anomaly", snapshot=None)], ent)  # odd rows: never raises
