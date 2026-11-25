"""In-memory sliding-window rate limits (one API instance, no Redis)."""

from __future__ import annotations

import math
import threading
import time
from collections import deque

from api.core.errors import Problem


class RateLimiter:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._hits: dict[str, deque[float]] = {}

    def hit(self, key: str, limit: int, window: float) -> None:
        """Count one hit; raise ``429 rate_limited`` when over ``limit`` per ``window`` seconds."""
        now = time.monotonic()
        with self._lock:
            q = self._hits.setdefault(key, deque())
            while q and q[0] <= now - window:
                q.popleft()
            if len(q) >= limit:
                retry = max(1, math.ceil(q[0] + window - now))
                raise Problem(429, "rate_limited", "too many attempts; try again later", headers={"Retry-After": str(retry)})
            q.append(now)
            if len(self._hits) > 50_000:  # bound memory under abuse
                for k in [k for k, v in self._hits.items() if not v or v[-1] <= now - 3600][:10_000]:
                    del self._hits[k]

    def reset(self) -> None:
        with self._lock:
            self._hits.clear()
