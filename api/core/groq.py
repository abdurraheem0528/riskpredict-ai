"""Groq for exactly two jobs (contracts decision 9):

1. suggesting a column mapping, which the user then confirms, and
2. wording the advice text for a risk.

Groq never produces a number that is stored as a score. Model ids come from
``GROQ_MAPPING_MODEL`` / ``GROQ_ADVICE_MODEL`` (defaults: ``openai/gpt-oss-120b`` and
``openai/gpt-oss-20b``, the models on Groq's free plan). When Groq is not configured,
down, rate-limited or out of quota, callers get ``GroqUnavailable`` and fall back
(heuristic mapping, template advice).

**Reasoning models.** gpt-oss models think before they answer, and the thinking counts
against ``max_completion_tokens``. A budget sized for the answer alone can be used up by
the reasoning, leaving ``content`` empty (``finish_reason = "length"``). So for them this
sends ``reasoning_effort = "low"`` and ``include_reasoning = false`` (the reasoning is
not needed back), and each caller's budget leaves room for both.

**Free-plan limits** (per model, per organization): 30 requests/min, 1,000 requests/day,
8,000 tokens/min, 200,000 tokens/day. Budgets per call: mapping ≤ ~1.5k prompt +
``MAPPING_MAX_TOKENS``; advice ≤ ~0.6k prompt + ``ADVICE_MAX_TOKENS``. Groq returns its
remaining per-minute tokens and per-day requests in response headers; ``chat`` reads
them and, when a call that may wait (the worker's advice) would go over the minute's
tokens, waits for the reset (at most ``MAX_WAIT_SECONDS``). Advice stops for the rest
of the process once fewer than ``ADVICE_DAY_RESERVE`` of the model's requests are left
today, so mapping suggestions keep working when both use one model.

Logs and exception texts carry a short reason or code only: never the prompt, the
reply or the key.
"""

from __future__ import annotations

import json
import logging
import re
import time
from typing import Any

import httpx

from api.core import http
from api.core.config import Settings

log = logging.getLogger("riskpredict.groq")

MAPPING_MAX_TOKENS = 3000
ADVICE_MAX_TOKENS = 1000
MAX_WAIT_SECONDS = 30.0
ADVICE_DAY_RESERVE = 100

# Per process and model (Groq's limits are per model): what its last response said is left
# (x-ratelimit-* headers).
_LIMITS: dict[str, dict[str, float | None]] = {}


class GroqUnavailable(Exception):
    """Safe to log: a short reason, never the prompt or the reply."""


def is_reasoning_model(model: str) -> bool:
    return "gpt-oss" in model.casefold()


def _seconds(value: str | None) -> float | None:
    """Groq's reset durations: ``7.66s``, ``2m59.56s``, ``1h2m3s``, ``250ms``."""
    if not value:
        return None
    total, found = 0.0, False
    for num, unit in re.findall(r"(\d+(?:\.\d+)?)(ms|h|m|s)", value.strip()):
        found = True
        total += float(num) * {"h": 3600.0, "m": 60.0, "s": 1.0, "ms": 0.001}[unit]
    return total if found else None


def _int(value: str | None) -> int | None:
    try:
        return int(value) if value is not None else None
    except ValueError:
        return None


def _limits(model: str) -> dict[str, float | None]:
    return _LIMITS.setdefault(model, {"tokens_left": None, "tokens_reset_at": None, "requests_left": None})


def _note_limits(model: str, resp: httpx.Response) -> None:
    lim, h = _limits(model), resp.headers
    tokens = _int(h.get("x-ratelimit-remaining-tokens"))
    if tokens is not None:
        lim["tokens_left"] = tokens
        reset = _seconds(h.get("x-ratelimit-reset-tokens"))
        lim["tokens_reset_at"] = time.monotonic() + reset if reset is not None else None
    requests = _int(h.get("x-ratelimit-remaining-requests"))
    if requests is not None:
        lim["requests_left"] = requests


def _estimate(messages: list[dict], max_tokens: int) -> int:
    chars = sum(len(str(m.get("content", ""))) for m in messages)
    return chars // 3 + max_tokens  # ~3–4 characters per token; the completion budget in full


def _pace(model: str, need: int, *, may_wait: bool, purpose: str) -> None:
    lim = _limits(model)
    left, reset_at = lim["tokens_left"], lim["tokens_reset_at"]
    if purpose == "advice" and lim["requests_left"] is not None and lim["requests_left"] < ADVICE_DAY_RESERVE:
        raise GroqUnavailable("daily request budget kept for mapping")
    if left is None or left >= need or reset_at is None:
        return
    wait = reset_at - time.monotonic()
    if wait <= 0:
        return
    if not may_wait or wait > MAX_WAIT_SECONDS:
        raise GroqUnavailable("over the per-minute token budget")
    time.sleep(wait)


def reset_limits() -> None:
    """Forget the remembered limits (tests)."""
    _LIMITS.clear()


def chat(settings: Settings, model: str, messages: list[dict], *, json_mode: bool = False, max_tokens: int = ADVICE_MAX_TOKENS,
         purpose: str = "advice", may_wait: bool = False) -> str:  # fmt: skip
    if not settings.groq_api_key or not model:
        raise GroqUnavailable("not configured")
    _pace(model, _estimate(messages, max_tokens), may_wait=may_wait, purpose=purpose)
    body: dict[str, Any] = {"model": model, "messages": messages, "temperature": 0, "max_completion_tokens": max_tokens}
    if is_reasoning_model(model):
        body["reasoning_effort"] = "low"
        body["include_reasoning"] = False
    if json_mode:
        body["response_format"] = {"type": "json_object"}
    try:
        with http.client(timeout=30) as c:
            resp = c.post(
                f"{settings.groq_base_url}/chat/completions",
                headers={"Authorization": f"Bearer {settings.groq_api_key}"},
                json=body,
            )
    except httpx.HTTPError:
        raise GroqUnavailable("unreachable") from None
    _note_limits(model, resp)
    if resp.status_code == 429:
        raise GroqUnavailable("rate limited or out of quota")
    if resp.status_code >= 400:
        code = None
        try:
            err = resp.json().get("error")
            code = err.get("code") or err.get("type") if isinstance(err, dict) else None
        except (ValueError, AttributeError):
            pass
        # Only Groq's error *code* (e.g. model_decommissioned, json_validate_failed), never its message.
        safe = code if isinstance(code, str) and re.fullmatch(r"[a-z0-9_.-]{1,60}", code) else None
        raise GroqUnavailable(f"status {resp.status_code}" + (f" ({safe})" if safe else ""))
    try:
        choice = resp.json()["choices"][0]
        content = choice["message"].get("content")
        finish = choice.get("finish_reason")
    except (ValueError, KeyError, IndexError, TypeError, AttributeError):
        raise GroqUnavailable("unexpected response") from None
    if not isinstance(content, str) or not content.strip():
        why = finish if isinstance(finish, str) and re.fullmatch(r"[a-z_]{1,30}", finish) else "unknown"
        raise GroqUnavailable(f"empty reply (finish_reason={why})")
    return content


_FENCE = re.compile(r"^\s*```(?:json)?\s*(.*?)\s*```\s*$", re.DOTALL)


def chat_json(settings: Settings, model: str, messages: list[dict]) -> dict:
    text = chat(settings, model, messages, json_mode=True, max_tokens=MAPPING_MAX_TOKENS, purpose="mapping")
    m = _FENCE.match(text)
    try:
        data = json.loads(m.group(1) if m else text)
    except ValueError:
        raise GroqUnavailable("reply was not JSON") from None
    if not isinstance(data, dict):
        raise GroqUnavailable("reply was not a JSON object")
    return data
