"""api/core/groq.py against a fake Groq that answers like the gpt-oss reasoning models.

No network: ``api.core.http.transport`` is a MockTransport. Every reply here is test data.
"""

from __future__ import annotations

import json
import logging
from dataclasses import replace

import httpx
import pytest

from api.core import groq, http, mapping
from api.core.config import DEFAULT_GROQ_ADVICE_MODEL, DEFAULT_GROQ_MAPPING_MODEL, Settings

KEY = "gsk_test_not_a_real_key"
COLUMNS = [{"name": n, "samples": s} for n, s in [
    ("Supplier ID", ["SUP-001", "SUP-002"]), ("Supplier", ["Formosa Microcircuits", "Hanbit Memory Works"]),
    ("Country", ["Taiwan", "South Korea"]), ("Main port", ["TWKHH", "KRPUS"]), ("HS code", ["8542", "8542"]),
    ("Annual spend (USD)", ["4200000", "3100000"]), ("Criticality", ["High", "High"]),
]]  # fmt: skip


def gpt_oss(content, *, reasoning="The user wants JSON. Columns: ...", finish="stop", headers=None):
    """A chat completion as Groq returns it for openai/gpt-oss-* (reasoning in its own field)."""
    body = {
        "id": "chatcmpl-test", "object": "chat.completion", "model": "openai/gpt-oss-120b",
        "choices": [{"index": 0, "finish_reason": finish,
                     "message": {"role": "assistant", "content": content, "reasoning": reasoning}}],
        "usage": {"prompt_tokens": 612, "completion_tokens": 488, "total_tokens": 1100},
    }  # fmt: skip
    return httpx.Response(200, json=body, headers=headers or {})


@pytest.fixture
def fake():
    sent: list[dict] = []
    replies: list = []

    def handler(req: httpx.Request) -> httpx.Response:
        assert req.url.path == "/openai/v1/chat/completions"
        assert req.headers["authorization"] == f"Bearer {KEY}"
        sent.append(json.loads(req.content))
        r = replies.pop(0)
        return r(req) if callable(r) else r

    http.transport = httpx.MockTransport(handler)
    groq.reset_limits()
    yield sent, replies
    http.transport = None
    groq.reset_limits()


@pytest.fixture
def settings():
    return replace(Settings.from_env(), groq_api_key=KEY, groq_mapping_model=DEFAULT_GROQ_MAPPING_MODEL,
                   groq_advice_model=DEFAULT_GROQ_ADVICE_MODEL)  # fmt: skip


def test_defaults_are_the_free_plan_gpt_oss_models(monkeypatch):
    monkeypatch.delenv("GROQ_MAPPING_MODEL", raising=False)
    monkeypatch.setenv("GROQ_ADVICE_MODEL", "")
    s = Settings.from_env()
    assert (s.groq_mapping_model, s.groq_advice_model) == ("openai/gpt-oss-120b", "openai/gpt-oss-20b")
    monkeypatch.setenv("GROQ_ADVICE_MODEL", "some/other-model")
    assert Settings.from_env().groq_advice_model == "some/other-model"


def test_reasoning_models_get_low_effort_and_room_for_reasoning_and_the_answer(fake, settings):
    sent, replies = fake
    replies.append(gpt_oss("Ask the supplier for its contingency plan."))
    assert groq.chat(settings, "openai/gpt-oss-20b", [{"role": "user", "content": "hi"}]) == (
        "Ask the supplier for its contingency plan."
    )
    body = sent[0]
    assert body["reasoning_effort"] == "low" and body["include_reasoning"] is False
    assert body["max_completion_tokens"] == groq.ADVICE_MAX_TOKENS >= 1000 and "max_tokens" not in body
    assert "reasoning_format" not in body  # not supported with gpt-oss (it conflicts with include_reasoning)
    # a non-reasoning model gets neither setting
    replies.append(gpt_oss("ok", reasoning=None))
    groq.chat(settings, "some/plain-model", [{"role": "user", "content": "hi"}])
    assert "reasoning_effort" not in sent[1] and "include_reasoning" not in sent[1]


def test_budgets_stay_well_inside_the_free_per_minute_tokens():
    prompt = mapping._SYSTEM + json.dumps({"columns": [{"column": f"Column {i}", "samples": ["x" * 80] * 5} for i in range(15)]})
    assert len(prompt) // 3 + groq.MAPPING_MAX_TOKENS < 8000 * 0.75
    assert 600 * 4 // 3 + groq.ADVICE_MAX_TOKENS < 8000 / 4  # several advice calls fit in one minute


def test_mapping_uses_json_mode_and_reads_the_header_currency(fake, settings):
    sent, replies = fake
    reply = {"entity_kind": "supplier", "columns": [
        {"column": "Supplier", "field": "name", "confidence": 0.98, "reason": "supplier names"},
        {"column": "Annual spend (USD)", "field": "annual_spend", "currency": "USD", "confidence": 0.9},
        {"column": "Main port", "field": "port", "confidence": 0.95},
    ]}  # fmt: skip
    replies.append(gpt_oss(json.dumps(reply)))
    spec, model = mapping.suggest(settings, COLUMNS)
    assert model == "groq:openai/gpt-oss-120b"
    assert sent[0]["response_format"] == {"type": "json_object"} and sent[0]["reasoning_effort"] == "low"
    assert sent[0]["max_completion_tokens"] == groq.MAPPING_MAX_TOKENS
    assert '"Annual spend (USD)"' in sent[0]["messages"][0]["content"]  # the prompt says to read it
    spend = next(c for c in spec["columns"] if c["column"] == "Annual spend (USD)")
    assert spend["currency"] == "USD"
    # a model that left the currency out still gets it from the header
    reply["columns"][1].pop("currency")
    replies.append(gpt_oss("```json\n" + json.dumps(reply) + "\n```"))  # and a fenced reply still parses
    spec, _ = mapping.suggest(settings, COLUMNS)
    assert next(c for c in spec["columns"] if c["column"] == "Annual spend (USD)")["currency"] == "USD"


@pytest.mark.parametrize("content, finish", [("", "length"), (None, "length"), ("   ", "stop")])
def test_an_empty_reply_is_a_fallback_with_a_code_only(fake, settings, caplog, content, finish):
    _, replies = fake
    replies.append(gpt_oss(content, reasoning="very long private reasoning " * 50, finish=finish))
    with pytest.raises(groq.GroqUnavailable) as exc:
        groq.chat(settings, "openai/gpt-oss-20b", [{"role": "user", "content": "secret prompt"}])
    assert str(exc.value) == f"empty reply (finish_reason={finish})"
    replies.append(gpt_oss(content, finish=finish))
    with caplog.at_level(logging.INFO):
        spec, model = mapping.suggest(settings, COLUMNS)
    assert model == "heuristic" and spec["columns"]
    logged = caplog.text
    assert "empty reply (finish_reason=" in logged
    assert "private reasoning" not in logged and KEY not in logged and "Formosa" not in logged


def test_errors_carry_groqs_code_never_its_message(fake, settings):
    _, replies = fake
    replies.append(httpx.Response(400, json={"error": {"message": "The model `llama-3.3-70b` has been decommissioned "
                                                       "for gsk_test_not_a_real_key", "type": "invalid_request_error",
                                                       "code": "model_decommissioned"}}))  # fmt: skip
    with pytest.raises(groq.GroqUnavailable) as exc:
        groq.chat(settings, "llama-3.3-70b", [{"role": "user", "content": "x"}])
    assert str(exc.value) == "status 400 (model_decommissioned)" and KEY not in str(exc.value)
    replies.append(httpx.Response(200, json={"choices": []}))
    with pytest.raises(groq.GroqUnavailable, match="unexpected response"):
        groq.chat(settings, "openai/gpt-oss-20b", [{"role": "user", "content": "x"}])
    replies.append(gpt_oss("not json at all"))
    with pytest.raises(groq.GroqUnavailable, match="reply was not JSON"):
        groq.chat_json(settings, "openai/gpt-oss-120b", [{"role": "user", "content": "x"}])


def test_pacing_follows_groqs_rate_limit_headers(fake, settings, monkeypatch):
    _, replies = fake
    slept: list[float] = []
    monkeypatch.setattr(groq.time, "sleep", lambda s: slept.append(s))
    low = {"x-ratelimit-remaining-tokens": "300", "x-ratelimit-reset-tokens": "7.5s",
           "x-ratelimit-remaining-requests": "999", "x-ratelimit-reset-requests": "1m26.4s"}  # fmt: skip
    replies.append(gpt_oss("first", headers=low))
    groq.chat(settings, "openai/gpt-oss-20b", [{"role": "user", "content": "x"}])
    # the next call needs more than the 300 tokens left: the worker waits for the reset ...
    replies.append(gpt_oss("second"))
    assert groq.chat(settings, "openai/gpt-oss-20b", [{"role": "user", "content": "x"}], may_wait=True) == "second"
    assert len(slept) == 1 and 7.0 < slept[0] <= 7.5
    # ... a request (which may not wait) falls back instead of calling; limits are per model
    replies.append(gpt_oss("third", headers=low))
    groq.chat(settings, "openai/gpt-oss-20b", [{"role": "user", "content": "x"}], may_wait=True)
    with pytest.raises(groq.GroqUnavailable, match="per-minute token budget"):
        groq.chat(settings, "openai/gpt-oss-20b", [{"role": "user", "content": "x"}], purpose="mapping")
    replies.append(gpt_oss("{}"))
    assert groq.chat_json(settings, "openai/gpt-oss-120b", [{"role": "user", "content": "x"}]) == {}
    # near the daily request limit, advice stops so mapping keeps working
    groq.reset_limits()
    replies.append(gpt_oss("fourth", headers={"x-ratelimit-remaining-requests": "40"}))
    groq.chat(settings, "openai/gpt-oss-20b", [{"role": "user", "content": "x"}])
    with pytest.raises(groq.GroqUnavailable, match="daily request budget"):
        groq.chat(settings, "openai/gpt-oss-20b", [{"role": "user", "content": "x"}], purpose="advice")
    replies.append(gpt_oss("{}"))
    assert groq.chat_json(settings, "openai/gpt-oss-120b", [{"role": "user", "content": "x"}]) == {}


@pytest.mark.parametrize("value, seconds", [("7.66s", 7.66), ("2m59.56s", 179.56), ("1h2m3s", 3723.0), ("250ms", 0.25),
                                            ("", None), ("soon", None)])  # fmt: skip
def test_reset_durations(value, seconds):
    got = groq._seconds(value)
    assert got == pytest.approx(seconds) if seconds is not None else got is None
