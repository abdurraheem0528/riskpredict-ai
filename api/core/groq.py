"""Groq (Llama) for exactly two jobs (contracts decision 9):

1. suggesting a column mapping, which the user then confirms, and
2. wording the advice text for a risk.

Groq never produces a number that is stored as a score. Model ids come from
``GROQ_MAPPING_MODEL`` / ``GROQ_ADVICE_MODEL``. When Groq is not configured,
down, rate-limited or out of quota, callers get ``GroqUnavailable`` and fall back
(heuristic mapping, template advice).
"""

from __future__ import annotations

import json
from typing import Any

import httpx

from api.core import http
from api.core.config import Settings


class GroqUnavailable(Exception):
    """Safe to log: a short reason, never the prompt or the reply."""


def chat(settings: Settings, model: str, messages: list[dict], *, json_mode: bool = False, max_tokens: int = 800) -> str:
    if not settings.groq_api_key or not model:
        raise GroqUnavailable("not configured")
    body: dict[str, Any] = {"model": model, "messages": messages, "temperature": 0, "max_tokens": max_tokens}
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
    if resp.status_code == 429:
        raise GroqUnavailable("rate limited or out of quota")
    if resp.status_code >= 400:
        raise GroqUnavailable(f"status {resp.status_code}")
    try:
        return resp.json()["choices"][0]["message"]["content"] or ""
    except (ValueError, KeyError, IndexError, TypeError):
        raise GroqUnavailable("unexpected response") from None


def chat_json(settings: Settings, model: str, messages: list[dict]) -> dict:
    text = chat(settings, model, messages, json_mode=True, max_tokens=1500)
    try:
        data = json.loads(text)
    except ValueError:
        raise GroqUnavailable("reply was not JSON") from None
    if not isinstance(data, dict):
        raise GroqUnavailable("reply was not a JSON object")
    return data
