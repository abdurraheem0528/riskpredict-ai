"""Opaque keyset cursors: ``{"items": [...], "next_cursor": "..." | null}``.

A cursor holds the sort key of the last row and when it was issued; it stays
valid for 24 hours. It carries only values from the caller's own results, and
every query still runs under row-level security.
"""

from __future__ import annotations

import json
import time
from typing import Any

from api.core import crypto
from api.core.errors import bad_request

CURSOR_TTL = 24 * 3600


def encode(values: list[Any]) -> str:
    return crypto.b64url(json.dumps({"k": values, "t": int(time.time())}, separators=(",", ":"), default=str).encode())


def decode(cursor: str | None, n: int) -> list[Any] | None:
    if not cursor:
        return None
    try:
        data = json.loads(crypto.b64url_decode(cursor))
        values, issued = data["k"], int(data["t"])
    except (ValueError, KeyError, TypeError):
        raise bad_request("invalid cursor") from None
    if not isinstance(values, list) or len(values) != n:
        raise bad_request("invalid cursor")
    if time.time() - issued > CURSOR_TTL:
        raise bad_request("the cursor expired; start from the first page")
    return values


def page(rows: list[dict], limit: int, key: list[str], item) -> dict:
    """``rows`` was fetched with ``limit + 1``."""
    more = len(rows) > limit
    rows = rows[:limit]
    nxt = encode([rows[-1][k] for k in key]) if more and rows else None
    return {"items": [item(r) for r in rows], "next_cursor": nxt}
