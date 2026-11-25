"""The worker's only link to the API: push SSE events, and long-poll for wake-up hints.

Both go to ``API_INTERNAL_URL`` (Northflank private network) with
``Authorization: Bearer ${INTERNAL_TOKEN}``. Neither touches the database, so
waiting for work costs no Neon compute.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field

import httpx

from api.core import http
from api.core.config import Settings

log = logging.getLogger("riskpredict.worker.api")


@dataclass
class Outbox:
    """SSE events collected during one unit of work, sent after its commit."""

    events: list[dict] = field(default_factory=list)

    def add(self, workspace_id, event: str, data: dict) -> None:
        self.events.append({"workspace_id": str(workspace_id), "event": event, "data": data})


class ApiClient:
    def __init__(self, settings: Settings):
        self.base = settings.api_internal_url
        self.token = settings.internal_token

    @property
    def enabled(self) -> bool:
        return bool(self.base and self.token)

    def _headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self.token}"}

    def push(self, outbox: Outbox) -> None:
        if not self.enabled or not outbox.events:
            outbox.events.clear()
            return
        events, outbox.events = outbox.events, []
        try:
            with http.client(timeout=10) as c:
                for i in range(0, len(events), 500):
                    resp = c.post(f"{self.base}/internal/events", json={"events": events[i : i + 500]}, headers=self._headers())
                    if resp.status_code >= 400:
                        log.warning("event push refused (%s)", resp.status_code)
        except httpx.HTTPError as exc:
            log.warning("event push failed (%s); clients will refetch on reconnect", type(exc).__name__)

    def wait_for_work(self, wait: float) -> dict[str, list[str]] | None:
        """Hints by workspace id, or None when the API is unreachable."""
        if not self.enabled:
            return None
        try:
            with http.client(timeout=wait + 15) as c:
                resp = c.get(f"{self.base}/internal/work", params={"wait": f"{wait:.0f}"}, headers=self._headers())
        except httpx.HTTPError:
            return None
        if resp.status_code != 200:
            return None
        try:
            return {i["workspace_id"]: i["reasons"] for i in resp.json().get("items", [])}
        except (ValueError, KeyError, TypeError):
            return None
