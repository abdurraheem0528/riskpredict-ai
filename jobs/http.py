"""Shared HTTP client and error types.

Error messages built here deliberately carry only the HTTP status, the host and
an error class — never response bodies — because job logs are public.
"""

from __future__ import annotations

import time
from urllib.parse import urlsplit

import httpx

USER_AGENT = "riskpredict-ai-jobs/0.1 (+https://github.com/abdurraheem0528/riskpredict-ai)"
DEFAULT_TIMEOUT = httpx.Timeout(60.0, connect=15.0)
RETRY_STATUSES = {500, 502, 503, 504}


class SourceError(Exception):
    """A source could not be read. ``str(exc)`` is safe to log and store."""


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
