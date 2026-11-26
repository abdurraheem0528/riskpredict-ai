"""Rate limits shared by every API instance, kept in the database.

Serverless: each request may run on a different instance, so a counter in
memory would protect nothing. ``biz.rate_hit`` (a SECURITY DEFINER function,
migration 050) counts hits in a fixed window and answers how long to wait;
``rp_app`` cannot read the table itself. Keys are HMAC-SHA256 with a key
derived from ``JWT_SIGNING_KEY``, so no email or IP address is stored in clear.
"""

from __future__ import annotations

import hashlib
import hmac

from api.core.errors import Problem


class RateLimiter:
    def __init__(self, db, secret: bytes) -> None:
        self.db = db
        # Separate from the signing key itself; stable across instances and deploys.
        self._key = hmac.new(secret or b"riskpredict", b"riskpredict rate limit keys v1", hashlib.sha256).digest()

    def key_hash(self, key: str) -> bytes:
        return hmac.new(self._key, key.encode(), hashlib.sha256).digest()

    def hit(self, key: str, limit: int, window: float) -> None:
        """Count one hit; raise ``429 rate_limited`` when over ``limit`` per ``window`` seconds."""
        with self.db.tx() as conn:
            retry = conn.execute(
                "SELECT biz.rate_hit(%s, %s, %s) AS retry", (self.key_hash(key), int(limit), max(1, int(window)))
            ).fetchone()["retry"]
        if retry:
            raise Problem(429, "rate_limited", "too many attempts; try again later", headers={"Retry-After": str(retry)})
