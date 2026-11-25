"""One place that builds outbound HTTP clients (Google, Composio, Groq).

Tests replace ``transport`` with an ``httpx.MockTransport``; production code
never mocks anything. Requests never log URLs with tokens or bodies.
"""

from __future__ import annotations

import httpx

# Set only by tests (tests/apiservice/conftest.py).
transport: httpx.BaseTransport | None = None

USER_AGENT = "riskpredict-api/0.1 (+https://github.com/abdurraheem0528/riskpredict-ai)"


def client(timeout: float = 20.0) -> httpx.Client:
    return httpx.Client(
        timeout=httpx.Timeout(timeout, connect=10.0),
        headers={"User-Agent": USER_AGENT},
        transport=transport,
        follow_redirects=False,
    )
