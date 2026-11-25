"""Composio (Hobby tier) for tools other than Google Sheets.

We store only the connected-account id; Composio holds the tool's tokens
(contracts decision 7). Composio's user id is ``ws_<workspace id>``.

REST API v3.1 (checked against docs.composio.dev, Nov 2026):

* ``POST /connected_accounts/link`` {auth_config_id, user_id, callback_url}
  → {redirect_url, connected_account_id, expires_at, link_token}
* ``GET /connected_accounts/{id}`` → {status: INITIALIZING|INITIATED|ACTIVE|FAILED|EXPIRED|INACTIVE, ...}
* ``DELETE /connected_accounts/{id}``
* ``POST /tools/execute/{TOOL_SLUG}`` {connected_account_id, user_id, arguments}
  → {data, successful, error}
* ``POST /trigger_instances/{SLUG}/upsert`` {connected_account_id, user_id}

Webhooks are signed with ``webhook-id``, ``webhook-timestamp`` and
``webhook-signature: v1,<base64 HMAC-SHA256>`` over
``{webhook-id}.{webhook-timestamp}.{raw body}``, keyed with the subscription
secret as UTF-8 bytes (not base64-decoded like the Standard Webhooks
``whsec_`` form).
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import time
from dataclasses import dataclass
from typing import Any
from urllib.parse import quote
from uuid import UUID

import httpx

from api.core import http
from api.core.config import Settings
from api.core.errors import UpstreamAuthError, UpstreamNotFound, UpstreamUnavailable

SERVICE = "Composio"
TOLERANCE_SECONDS = 300

# Apps we can read rows from. A provider is offered only when it is listed here
# and has an auth config in COMPOSIO_AUTH_CONFIGS.
APP_NAMES = {"airtable": "Airtable"}


def user_id_for(workspace_id: UUID | str) -> str:
    return f"ws_{workspace_id}"


def _headers(settings: Settings) -> dict[str, str]:
    return {"x-api-key": settings.composio_api_key}


def _send(settings: Settings, method: str, path: str, **kw) -> httpx.Response:
    if not settings.composio_enabled:
        raise UpstreamUnavailable(SERVICE, "Composio is not configured on this server")
    try:
        with http.client(timeout=30) as c:
            resp = c.request(method, settings.composio_base_url + path, headers=_headers(settings), **kw)
    except httpx.HTTPError:
        raise UpstreamUnavailable(SERVICE) from None
    if resp.status_code == 404:
        raise UpstreamNotFound("not found at Composio")
    if resp.status_code in (401, 403):
        raise UpstreamUnavailable(SERVICE, "Composio rejected the server's API key")
    if resp.status_code >= 400:
        raise UpstreamUnavailable(SERVICE, f"Composio returned {resp.status_code}")
    return resp


def available_apps(settings: Settings) -> list[str]:
    if not settings.composio_enabled:
        return []
    return sorted(a for a in settings.composio_auth_configs if a in APP_NAMES)


@dataclass(frozen=True)
class Link:
    connected_account_id: str
    redirect_url: str
    auth_config_id: str


def create_link(settings: Settings, app: str, workspace_id: UUID | str, callback_url: str) -> Link:
    auth_config = settings.composio_auth_configs.get(app)
    if not auth_config:
        raise UpstreamUnavailable(SERVICE, f"no Composio auth config for {app}")
    resp = _send(
        settings,
        "POST",
        "/connected_accounts/link",
        json={"auth_config_id": auth_config, "user_id": user_id_for(workspace_id), "callback_url": callback_url},
    )
    data = resp.json()
    return Link(data["connected_account_id"], data["redirect_url"], auth_config)


# Composio status → our connection status.
STATUS_MAP = {
    "ACTIVE": "active",
    "INITIALIZING": "pending",
    "INITIATED": "pending",
    "FAILED": "error",
    "EXPIRED": "needs_reauth",
    "INACTIVE": "needs_reauth",
}


def account_status(settings: Settings, account_id: str) -> tuple[str, str | None]:
    """Our status and a safe reason for a connected account."""
    data = _send(settings, "GET", f"/connected_accounts/{quote(account_id, safe='')}").json()
    raw = str(data.get("status") or "").upper()
    status = STATUS_MAP.get(raw, "error")
    if data.get("is_disabled"):
        status = "needs_reauth"
    reason = None if status in ("active", "pending") else f"Composio reports {raw.lower() or 'an unknown status'}"
    return status, reason


def delete_account(settings: Settings, account_id: str) -> None:
    try:
        _send(settings, "DELETE", f"/connected_accounts/{quote(account_id, safe='')}")
    except UpstreamNotFound:
        pass


def execute(settings: Settings, tool: str, account_id: str, workspace_id: UUID | str, arguments: dict) -> dict:
    resp = _send(
        settings,
        "POST",
        f"/tools/execute/{quote(tool, safe='')}",
        json={"connected_account_id": account_id, "user_id": user_id_for(workspace_id), "arguments": arguments},
    )
    body = resp.json()
    if not body.get("successful", False):
        msg = str(body.get("error") or "")
        if any(w in msg.lower() for w in ("unauthor", "expired", "revoked", "invalid_grant")):
            raise UpstreamAuthError(SERVICE)
        raise UpstreamUnavailable(SERVICE, "the tool call through Composio failed")
    data = body.get("data") or {}
    # Some tools wrap their response once more.
    if isinstance(data, dict) and set(data) == {"data"} and isinstance(data["data"], dict):
        data = data["data"]
    return data if isinstance(data, dict) else {}


def upsert_trigger(settings: Settings, slug: str, account_id: str, workspace_id: UUID | str) -> None:
    _send(
        settings,
        "POST",
        f"/trigger_instances/{quote(slug, safe='')}/upsert",
        json={"connected_account_id": account_id, "user_id": user_id_for(workspace_id)},
    )


def subscribe_trigger(settings: Settings, provider: str, account_id: str, workspace_id: UUID | str) -> bool:
    """Subscribe the trigger configured for this app, if any. Best effort: the
    worker's periodic re-read keeps the source fresh without it."""
    slug = settings.composio_trigger_slugs.get(provider.split(":", 1)[-1])
    if not slug:
        return False
    try:
        upsert_trigger(settings, slug, account_id, workspace_id)
        return True
    except (UpstreamUnavailable, UpstreamNotFound):
        return False


# --- webhook signature ---------------------------------------------------------------


def sign(secret: str, webhook_id: str, timestamp: str, body: bytes) -> str:
    msg = f"{webhook_id}.{timestamp}.".encode() + body
    return "v1," + base64.b64encode(hmac.new(secret.encode(), msg, hashlib.sha256).digest()).decode()


def verify(secret: str, webhook_id: str, timestamp: str, signature: str, body: bytes, now: float | None = None) -> bool:
    if not secret or not webhook_id or not timestamp or not signature:
        return False
    try:
        ts = int(timestamp)
    except ValueError:
        return False
    if abs((now if now is not None else time.time()) - ts) > TOLERANCE_SECONDS:
        return False
    expected = sign(secret, webhook_id, timestamp, body).split(",", 1)[1]
    # The header may carry several space-separated "v1,<sig>" entries (key rotation).
    for part in signature.split():
        received = part.split(",", 1)[1] if "," in part else part
        if hmac.compare_digest(expected.encode(), received.encode()):
            return True
    return False


def account_id_of(payload: dict[str, Any]) -> str | None:
    """Trigger messages carry it in ``metadata``; lifecycle events (e.g.
    ``composio.connected_account.expired``) carry the account itself in ``data``."""
    meta = payload.get("metadata") or {}
    if isinstance(meta, dict) and meta.get("connected_account_id"):
        return str(meta["connected_account_id"])
    data = payload.get("data") or {}
    if isinstance(data, dict):
        if str(payload.get("type", "")).startswith("composio.connected_account"):
            return str(data.get("id")) if data.get("id") else None
        if data.get("connected_account_id"):  # V2 payloads
            return str(data["connected_account_id"])
        if data.get("connection_id"):
            return str(data["connection_id"])
    return None


# --- Airtable adapter ------------------------------------------------------------------


@dataclass(frozen=True)
class BrowseItem:
    id: str
    name: str
    kind: str
    selectable: bool


def airtable_browse(settings: Settings, account_id: str, workspace_id: UUID | str, parent_id: str | None) -> list[BrowseItem]:
    if not parent_id:
        items: list[BrowseItem] = []
        offset = None
        for _ in range(10):
            args = {"offset": offset} if offset else {}
            data = execute(settings, "AIRTABLE_LIST_BASES", account_id, workspace_id, args)
            items += [BrowseItem(b["id"], b.get("name") or b["id"], "base", False) for b in data.get("bases") or []]
            offset = data.get("offset")
            if not offset:
                break
        return items
    tables = airtable_tables(settings, account_id, workspace_id, parent_id)
    return [BrowseItem(t["id"], t.get("name") or t["id"], "table", True) for t in tables]


def airtable_tables(settings: Settings, account_id: str, workspace_id: UUID | str, base_id: str) -> list[dict]:
    data = execute(settings, "AIRTABLE_GET_BASE_SCHEMA", account_id, workspace_id, {"baseId": base_id})
    return [t for t in data.get("tables") or [] if isinstance(t, dict) and t.get("id")]


def airtable_records(
    settings: Settings, account_id: str, workspace_id: UUID | str, base_id: str, table_id: str, max_records: int | None
) -> list[dict]:
    records: list[dict] = []
    offset = None
    for _ in range(100):  # at most 10,000 rows
        args: dict[str, Any] = {"baseId": base_id, "tableIdOrName": table_id, "pageSize": 100}
        if offset:
            args["offset"] = offset
        data = execute(settings, "AIRTABLE_LIST_RECORDS", account_id, workspace_id, args)
        records += [r for r in data.get("records") or [] if isinstance(r, dict) and r.get("id")]
        offset = data.get("offset")
        if not offset or (max_records and len(records) >= max_records):
            break
    return records[:max_records] if max_records else records
