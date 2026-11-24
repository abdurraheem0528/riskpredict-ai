"""The GLiNER2 wrapper: output parsing, type rules, batching and budgets.

The model is replaced by a fake that returns GLiNER2-shaped results (the
``batch_extract(..., include_confidence=True)`` format of gliner2 2.2.0) for
the real fixture titles. One optional test runs the real model when
``RUN_MODEL_TESTS=1`` and the model stack is installed and cached.
"""

import os
import sys

import pytest

from jobs.analyse import runner, tagging
from jobs.analyse.bench import fixture_inputs
from jobs.analyse.tagging import TagInput

FIXTURE_ITEMS = fixture_inputs()


def span(text, conf=0.9):
    return {"text": text, "confidence": conf}


def result(label="other", conf=0.8, company=(), location=(), commodity=()):
    return {
        "entities": {
            "company": [span(t) for t in company],
            "location": [span(t) for t in location],
            "commodity": [span(t) for t in commodity],
        },
        "event_type": {"label": label, "confidence": conf},
    }


class FakeModel:
    name = "fake/gliner2"
    revision = "0123456789abcdef0123"

    def __init__(self, responses=None, fail_calls=()):
        self.responses = responses or {}
        self.fail_calls = set(fail_calls)
        self.calls = []

    def predict(self, texts):
        self.calls.append(list(texts))
        if len(self.calls) in self.fail_calls:
            raise RuntimeError("boom")
        return [self.responses.get(t, result()) for t in texts]


def by_source(source):
    return next(i for i in FIXTURE_ITEMS if i.source == source)


def test_fixture_inputs_are_the_real_titles():
    assert [i.source for i in FIXTURE_ITEMS[:1]] == ["gdelt"]  # GDELT first
    assert {i.source for i in FIXTURE_ITEMS} == {"gdelt", "gdacs", "eonet", "ofac"}
    assert len(FIXTURE_ITEMS) == 13


def test_gdelt_uses_model_type_and_maps_places():
    item = TagInput("gdelt", "x", "Strike at Port of Rotterdam halts Shell shipments from the Netherlands to China")
    raw = result(
        "labour action",
        0.83,
        company=["Shell", "shell"],  # duplicate (case) is merged
        location=["Port of Rotterdam", "the Netherlands", "China"],
    )
    tag = tagging.build_tag(item, raw)
    assert tag.event_type == "labour_action" and tag.type_source == "model"
    assert tag.type_confidence == pytest.approx(0.83)
    assert tag.entities["companies"] == [{"text": "Shell", "confidence": 0.9}]
    assert tag.entities["places"] == [
        {"text": "Port of Rotterdam", "confidence": 0.9, "country": None},
        {"text": "the Netherlands", "confidence": 0.9, "country": "NLD"},
        {"text": "China", "confidence": 0.9, "country": "CHN"},
    ]
    assert tag.entities["countries"] == ["CHN", "NLD"]
    assert tag.entities["commodities"] == []
    assert tag.input_md5 == tagging.text_md5(item.title)


def test_low_confidence_type_becomes_other_but_guess_is_kept():
    item = by_source("gdelt")  # "Is the AI Stock Market Boom Coming to an End ?"
    tag = tagging.build_tag(item, result("financial stress", 0.31))
    assert (tag.event_type, tag.type_confidence, tag.type_source) == ("other", None, "model")
    assert tag.entities["type_guess"] == {"type": "financial_stress", "confidence": 0.31}


@pytest.mark.parametrize("source, expected", [("gdacs", "natural_hazard"), ("eonet", "natural_hazard"), ("ofac", "sanctions")])
def test_sources_with_a_known_type_keep_it(source, expected):
    item = by_source(source)
    tag = tagging.build_tag(item, result("protest", 0.95))
    assert (tag.event_type, tag.type_confidence, tag.type_source) == (expected, None, "source")
    assert tag.entities["type_guess"]["type"] == "protest"  # the model's own answer, for evaluation


def test_real_titles_places_only_match_exact_country_names():
    gdacs = next(i for i in FIXTURE_ITEMS if i.title == "Flood in Indonesia")
    eonet = next(i for i in FIXTURE_ITEMS if i.title.endswith("Greene, Tennessee"))
    region = next(i for i in FIXTURE_ITEMS if "South Sandwich" in i.title)
    assert tagging.build_tag(gdacs, result(location=["Indonesia"])).entities["countries"] == ["IDN"]
    assert tagging.build_tag(eonet, result(location=["Greene", "Tennessee"])).entities["countries"] == []
    # "South Sandwich Islands Region" is not the ISO name "South Georgia and the South Sandwich Islands".
    assert tagging.build_tag(region, result(location=["South Sandwich Islands Region"])).entities["countries"] == []


def test_other_output_shapes_are_accepted():
    item = by_source("ofac")
    raw = {"entities": {"company": "H N B TRADING CO LLC", "location": None}, "event_type": [{"label": "sanctions", "confidence": 0.7}]}
    tag = tagging.build_tag(item, raw)
    assert tag.entities["companies"] == [{"text": "H N B TRADING CO LLC"}]
    assert tag.entities["places"] == []


def test_unknown_label_is_rejected():
    with pytest.raises(ValueError):
        tagging.build_tag(by_source("gdelt"), result("weather"))


def test_every_type_has_a_unique_prompt_label():
    labels = [label for label, _ in tagging.EVENT_TYPES.values()]
    assert len(labels) == len(set(labels)) == 10
    assert set(tagging.EVENT_TYPES) == {
        "conflict", "protest", "sanctions", "trade_policy", "port_shipping_disruption",
        "natural_hazard", "cyber_incident", "labour_action", "financial_stress", "other",
    }


def test_model_version_changes_with_the_label_schema(monkeypatch):
    before = tagging.model_version("abcdef1234567890")
    assert before.startswith("abcdef123456+schema.")
    monkeypatch.setitem(tagging.EVENT_TYPES, "protest", ("protest", "a different description"))
    assert tagging.model_version("abcdef1234567890") != before


def test_tag_inputs_batches_and_counts():
    model = FakeModel({FIXTURE_ITEMS[0].title: result("weather")})  # one unparseable result
    run = tagging.tag_inputs(model, FIXTURE_ITEMS, chunk=5)
    assert [len(c) for c in model.calls] == [5, 5, 3]
    assert run.notes == {"unparsed": 1, "tagged": 12}
    assert not run.stopped_early


def test_failed_chunk_is_skipped_and_two_in_a_row_stop():
    run = tagging.tag_inputs(FakeModel(fail_calls={1}), FIXTURE_ITEMS, chunk=5)
    assert run.notes["failed"] == 5 and run.notes["tagged"] == 8
    assert run.error == "inference failed (RuntimeError)"

    run = tagging.tag_inputs(FakeModel(fail_calls={1, 2}), FIXTURE_ITEMS, chunk=5)
    assert run.stopped_early and run.notes == {"failed": 10, "not_run": 3, "tagged": 0}


def test_time_budget_stops_between_chunks():
    t = [0.0]

    class Slow(FakeModel):
        def predict(self, texts):
            t[0] += 4.0
            return super().predict(texts)

    run = tagging.tag_inputs(Slow(), FIXTURE_ITEMS, chunk=4, budget_s=10.0, clock=lambda: t[0])
    assert run.stopped_early and run.notes["tagged"] == 12 and run.notes["not_run"] == 1


def test_error_messages_never_contain_input_text():
    class Leaky(FakeModel):
        def predict(self, texts):
            raise ValueError(f"cannot handle {texts[0]}")

    run = tagging.tag_inputs(Leaky(), FIXTURE_ITEMS, chunk=20)
    assert "Stock Market" not in (run.error or "")


def test_runner_reports_status_and_type_counts():
    res, tags, ident = runner.tag(lambda: FakeModel(), lambda m, v: list(FIXTURE_ITEMS))
    assert res.status == "ok" and res.inputs == res.outputs == 13
    assert ident == ("fake/gliner2", tagging.model_version(FakeModel.revision))
    # 4 GDACS + 5 EONET hazards, 3 OFAC sanctions (source types); the GDELT title gets the fake's "other".
    assert {k: v for k, v in res.notes.items() if k.startswith("type.")} == {
        "type.natural_hazard": 9, "type.sanctions": 3, "type.other": 1,
    }
    assert len(tags) == 13


def test_runner_without_model_libraries_skips(monkeypatch):
    for name in ("torch", "gliner2"):
        monkeypatch.setitem(sys.modules, name, None)  # makes `import torch` raise ImportError
    res, tags, ident = runner.tag(tagging.Gliner2Model, lambda m, v: pytest.fail("must not query"))
    assert res.status == "skipped" and "not installed" in res.error
    assert tags == [] and ident is None


def test_runner_with_nothing_pending():
    res, _, _ = runner.tag(lambda: FakeModel(), lambda m, v: [])
    assert res.status == "empty"


@pytest.mark.skipif(os.environ.get("RUN_MODEL_TESTS") != "1", reason="set RUN_MODEL_TESTS=1 to run the real model")
def test_real_model_on_fixture_titles():
    pytest.importorskip("torch")
    pytest.importorskip("gliner2")
    model = tagging.Gliner2Model()
    run = tagging.tag_inputs(model, FIXTURE_ITEMS)
    assert run.notes["tagged"] == len(FIXTURE_ITEMS)
    for t in run.tags:
        assert t.event_type in tagging.EVENT_TYPES
        assert t.entities["type_guess"]["type"] in tagging.EVENT_TYPES
        assert 0 <= (t.type_confidence or 0) <= 1
