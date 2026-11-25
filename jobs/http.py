"""Shared HTTP client and error types.

Error messages built here deliberately carry only the HTTP status, the host and
an error class — never response bodies or URLs — because job logs are public.

API keys (``<SOURCE>_API_KEY`` env vars) are sent in headers wherever the
provider allows it. Where a key has to go in the query string (FRED, EIA), the
URL is never printed or stored: errors name the host only, httpx's own request
logging is turned off, and :func:`redact` scrubs every key value and every
``key=``/``token=``-style query parameter from any text that is reported.
"""

from __future__ import annotations

import logging
import os
import re
import time
from urllib.parse import urlsplit

import httpx

# httpx logs every request URL (including query strings) at INFO. Nothing in
# jobs/ configures logging today, but make sure a future basicConfig cannot leak
# a key that travels in a URL.
for _name in ("httpx", "httpcore"):
    logging.getLogger(_name).setLevel(logging.WARNING)

# Query parameter names that carry credentials at the providers we call.
_SECRET_PARAM_RE = re.compile(
    r"(?i)\b(api[_-]?key|apikey|token|access[_-]?key|subscription-key|key)=([^&#\s]+)"
)
_MIN_SECRET_LEN = 6  # don't "redact" an empty or trivially short value everywhere


def secret_values(env=None) -> list[str]:
    """Values of every ``*_API_KEY`` and ``DATABASE_URL*`` variable that is set."""
    env = os.environ if env is None else env
    out = []
    for name, value in env.items():
        if (name.endswith("_API_KEY") or name.startswith("DATABASE_URL")) and value and len(value.strip()) >= _MIN_SECRET_LEN:
            out.append(value.strip())
    # Longest first, so a key that contains another is replaced whole.
    return sorted(set(out), key=len, reverse=True)


def redact(text: str | None, env=None) -> str | None:
    """Remove API keys and credential-like query parameters from ``text``."""
    if not text:
        return text
    for value in secret_values(env):
        text = text.replace(value, "***")
    return _SECRET_PARAM_RE.sub(lambda m: f"{m.group(1)}=***", text)

USER_AGENT = "riskpredict-ai-jobs/0.1 (+https://github.com/abdurraheem0528/riskpredict-ai)"
DEFAULT_TIMEOUT = httpx.Timeout(60.0, connect=15.0)
RETRY_STATUSES = {500, 502, 503, 504}


class SourceError(Exception):
    """A source could not be read. ``str(exc)`` is safe to log and store."""

    def __init__(self, message: str = "") -> None:
        super().__init__(redact(str(message)))


class Throttled(SourceError):
    """The source asked us to slow down (HTTP 429 or a throttle message)."""


def make_client(transport: httpx.BaseTransport | None = None) -> httpx.Client:
    return httpx.Client(
        headers={"User-Agent": USER_AGENT},
        timeout=DEFAULT_TIMEOUT,
        follow_redirects=True,
        transport=transport,
    )


def host_of(url: str) -> str:
    return urlsplit(url).hostname or "?"


def request(
    client: httpx.Client,
    method: str,
    url: str,
    *,
    attempts: int = 3,
    backoff: float = 2.0,
    sleep=time.sleep,
    **kwargs,
) -> httpx.Response:
    """Send a request, retrying transient failures. Raises ``SourceError``."""
    host = host_of(url)
    last: str = "no attempt"
    for i in range(attempts):
        try:
            resp = client.request(method, url, **kwargs)
        except httpx.TimeoutException:
            last = f"timeout contacting {host}"
        except httpx.TransportError as exc:
            last = f"{type(exc).__name__} contacting {host}"
        else:
            if resp.status_code == 429:
                raise Throttled(f"HTTP 429 from {host}")
            if 300 <= resp.status_code < 400:
                # Only when the caller turned redirects off (keyed requests: a
                # custom auth header would otherwise follow to another host).
                raise SourceError(f"HTTP {resp.status_code} redirect from {host} (not followed)")
            if resp.status_code in RETRY_STATUSES:
                last = f"HTTP {resp.status_code} from {host}"
            elif resp.status_code >= 400:
                raise SourceError(f"HTTP {resp.status_code} from {host}")
            else:
                return resp
        if i + 1 < attempts:
            sleep(backoff * (2**i))
    raise SourceError(last)


def get(client: httpx.Client, url: str, **kwargs) -> httpx.Response:
    return request(client, "GET", url, **kwargs)


def get_json(client: httpx.Client, url: str, **kwargs):
    resp = get(client, url, **kwargs)
    try:
        return resp.json()
    except ValueError as exc:
        raise SourceError(f"invalid JSON from {host_of(url)}") from exc
