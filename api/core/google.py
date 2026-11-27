"""Google OAuth (``drive.file`` + PKCE), the Sheets API and Drive ``changes.watch``.

Only the ``drive.file`` scope is requested: the app can read just the files the
user picks with the Google Picker. Tokens are never logged; errors carry only
HTTP status codes.
"""

from __future__ import annotations

import base64
import hashlib
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from urllib.parse import quote, urlencode

import httpx

from api.core import http
from api.core.config import Settings
from api.core.errors import UpstreamAuthError, UpstreamNotFound, UpstreamUnavailable

SCOPE = "https://www.googleapis.com/auth/drive.file"
AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
TOKEN_URL = "https://oauth2.googleapis.com/token"
REVOKE_URL = "https://oauth2.googleapis.com/revoke"
SHEETS = "https://sheets.googleapis.com/v4/spreadsheets"
DRIVE = "https://www.googleapis.com/drive/v3"
WATCH_SECONDS = 7 * 24 * 3600  # changes.watch channels last at most one week
SERVICE = "Google"
# A spreadsheet picked within a minute of being created can return 404 for a few seconds while
# Google grants the app its drive.file access (the 27 Nov live test). Adding a source waits that
# out (spreadsheet(waits=...)): the seconds between four reads, about 5 s in all. Nothing else
# waits, the worker least of all.
FIRST_PICK_WAITS = (1.0, 1.5, 2.5)
sleep = time.sleep  # tests replace it


def redirect_uri(settings: Settings) -> str:
    return f"{settings.public_api_url}/v1/connections/google/callback"


def code_challenge(verifier: str) -> str:
    return base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()


def authorization_url(settings: Settings, state: str, verifier: str) -> str:
    params = {
        "client_id": settings.google_client_id,
        "redirect_uri": redirect_uri(settings),
        "response_type": "code",
        "scope": SCOPE,
        "access_type": "offline",
        "prompt": "consent",
        "include_granted_scopes": "false",
        "state": state,
        "code_challenge": code_challenge(verifier),
        "code_challenge_method": "S256",
    }
    return f"{AUTH_URL}?{urlencode(params)}"


def _raise_for(resp: httpx.Response, what: str) -> None:
    if resp.status_code < 400:
        return
    if resp.status_code in (401,):
        raise UpstreamAuthError(SERVICE, "Google rejected the stored credentials; reconnect Google Sheets")
    if resp.status_code == 403:
        # Either the file was not picked (drive.file) or quota; both are safe to say.
        reason = ""
        try:
            reason = resp.json().get("error", {}).get("status", "")
        except ValueError:
            pass
        if reason == "PERMISSION_DENIED":
            raise UpstreamNotFound(f"{what} is not available to the app; pick it with the Google Picker")
        raise UpstreamUnavailable(SERVICE, f"Google refused the request ({resp.status_code})")
    if resp.status_code == 404:
        raise UpstreamNotFound(f"{what} was not found, or was not picked with the Google Picker")
    raise UpstreamUnavailable(SERVICE, f"Google returned {resp.status_code}")


def _send(method: str, url: str, **kw) -> httpx.Response:
    try:
        with http.client() as c:
            return c.request(method, url, **kw)
    except httpx.HTTPError:
        raise UpstreamUnavailable(SERVICE) from None


# --- OAuth ---------------------------------------------------------------------


@dataclass(frozen=True)
class TokenGrant:
    access_token: str
    expires_at: datetime
    refresh_token: str | None
    scopes: tuple[str, ...]


def _grant(data: dict) -> TokenGrant:
    expires = datetime.now(timezone.utc) + timedelta(seconds=int(data.get("expires_in", 3600)))
    return TokenGrant(
        access_token=data["access_token"],
        expires_at=expires,
        refresh_token=data.get("refresh_token"),
        scopes=tuple(sorted((data.get("scope") or "").split())),
    )


def exchange_code(settings: Settings, code: str, verifier: str) -> TokenGrant:
    resp = _send(
        "POST",
        TOKEN_URL,
        data={
            "code": code,
            "client_id": settings.google_client_id,
            "client_secret": settings.google_client_secret,
            "redirect_uri": redirect_uri(settings),
            "grant_type": "authorization_code",
            "code_verifier": verifier,
        },
    )
    if resp.status_code == 400:
        raise UpstreamAuthError(SERVICE, "Google did not accept the sign-in; try connecting again")
    _raise_for(resp, "the token")
    return _grant(resp.json())


def refresh(settings: Settings, refresh_token: str) -> TokenGrant:
    resp = _send(
        "POST",
        TOKEN_URL,
        data={
            "client_id": settings.google_client_id,
            "client_secret": settings.google_client_secret,
            "refresh_token": refresh_token,
            "grant_type": "refresh_token",
        },
    )
    if resp.status_code in (400, 401):
        # invalid_grant: revoked by the user, password change, or 6 months unused
        raise UpstreamAuthError(SERVICE, "Google access was revoked or expired; reconnect Google Sheets")
    _raise_for(resp, "the token")
    return _grant(resp.json())


def revoke(token: str) -> None:
    """Best effort; a token that is already invalid is fine."""
    try:
        _send("POST", REVOKE_URL, data={"token": token})
    except UpstreamUnavailable:
        pass


class AccessTokenCache:
    """Short-lived access tokens per connection, in memory only."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._tokens: dict[str, tuple[str, float]] = {}

    def get(self, key: str) -> str | None:
        with self._lock:
            hit = self._tokens.get(key)
            if hit and hit[1] - 120 > time.time():
                return hit[0]
            return None

    def put(self, key: str, grant: TokenGrant) -> None:
        with self._lock:
            self._tokens[key] = (grant.access_token, grant.expires_at.timestamp())

    def drop(self, key: str) -> None:
        with self._lock:
            self._tokens.pop(key, None)


access_tokens = AccessTokenCache()


def _auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


# --- Drive / Sheets --------------------------------------------------------------


def account_email(access_token: str) -> str | None:
    resp = _send("GET", f"{DRIVE}/about", params={"fields": "user(emailAddress)"}, headers=_auth(access_token))
    if resp.status_code >= 400:
        return None
    return (resp.json().get("user") or {}).get("emailAddress")


@dataclass(frozen=True)
class SheetTab:
    gid: str
    title: str


@dataclass(frozen=True)
class Spreadsheet:
    id: str
    title: str
    tabs: tuple[SheetTab, ...]

    def tab(self, gid: str) -> SheetTab | None:
        if gid == "":
            return self.tabs[0] if self.tabs else None
        return next((t for t in self.tabs if t.gid == gid), None)


def spreadsheet(access_token: str, spreadsheet_id: str, *, waits: tuple[float, ...] = ()) -> Spreadsheet:
    """The spreadsheet's title and tabs. On a 404 it reads again after each of ``waits`` seconds
    (only when a source is added: ``FIRST_PICK_WAITS``); any other answer is final at once."""
    for wait in (*waits, None):
        resp = _send(
            "GET",
            f"{SHEETS}/{quote(spreadsheet_id, safe='')}",
            params={"fields": "properties.title,sheets.properties(sheetId,title,index)"},
            headers=_auth(access_token),
        )
        if resp.status_code != 404 or wait is None:
            break
        sleep(wait)
    _raise_for(resp, "The spreadsheet")
    data = resp.json()
    sheets = sorted(data.get("sheets") or [], key=lambda s: s.get("properties", {}).get("index", 0))
    tabs = tuple(
        SheetTab(str(s["properties"]["sheetId"]), s["properties"].get("title", "")) for s in sheets if "properties" in s
    )
    return Spreadsheet(spreadsheet_id, (data.get("properties") or {}).get("title", "Spreadsheet"), tabs)


def values(access_token: str, spreadsheet_id: str, tab_title: str, max_rows: int | None = None) -> list[list[str]]:
    title = tab_title.replace("'", "''")
    rng = f"'{title}'" if max_rows is None else f"'{title}'!1:{max_rows}"
    resp = _send(
        "GET",
        f"{SHEETS}/{quote(spreadsheet_id, safe='')}/values/{quote(rng, safe='')}",
        params={"valueRenderOption": "FORMATTED_VALUE", "majorDimension": "ROWS"},
        headers=_auth(access_token),
    )
    _raise_for(resp, "The sheet")
    return [[("" if v is None else str(v)) for v in row] for row in resp.json().get("values") or []]


def start_page_token(access_token: str) -> str:
    resp = _send("GET", f"{DRIVE}/changes/startPageToken", headers=_auth(access_token))
    _raise_for(resp, "Drive changes")
    return resp.json()["startPageToken"]


@dataclass(frozen=True)
class Channel:
    resource_id: str
    expires_at: datetime


def watch_changes(
    access_token: str, page_token: str, channel_id: str, channel_token: str, address: str, now: datetime
) -> Channel:
    expires = now + timedelta(seconds=WATCH_SECONDS)
    resp = _send(
        "POST",
        f"{DRIVE}/changes/watch",
        params={"pageToken": page_token},
        headers=_auth(access_token),
        json={
            "id": channel_id,
            "type": "web_hook",
            "address": address,
            "token": channel_token,
            "expiration": str(int(expires.timestamp() * 1000)),
        },
    )
    _raise_for(resp, "Drive changes")
    data = resp.json()
    exp_ms = data.get("expiration")
    if exp_ms:
        expires = datetime.fromtimestamp(int(exp_ms) / 1000, tz=timezone.utc)
    return Channel(resource_id=data["resourceId"], expires_at=expires)


def stop_channel(access_token: str, channel_id: str, resource_id: str | None) -> None:
    """Best effort: an expired or unknown channel is fine."""
    if not resource_id:
        return
    try:
        _send(
            "POST", f"{DRIVE}/channels/stop", headers=_auth(access_token), json={"id": channel_id, "resourceId": resource_id}
        )
    except UpstreamUnavailable:
        pass


def list_changes(access_token: str, page_token: str, max_pages: int = 10) -> tuple[set[str], str]:
    """File ids changed since ``page_token``, and the token to store for next time."""
    files: set[str] = set()
    token = page_token
    for _ in range(max_pages):
        resp = _send(
            "GET",
            f"{DRIVE}/changes",
            params={
                "pageToken": token,
                "pageSize": "1000",
                "fields": "nextPageToken,newStartPageToken,changes(fileId,removed)",
            },
            headers=_auth(access_token),
        )
        _raise_for(resp, "Drive changes")
        data = resp.json()
        files.update(c["fileId"] for c in data.get("changes") or [] if c.get("fileId"))
        if data.get("newStartPageToken"):
            return files, data["newStartPageToken"]
        token = data.get("nextPageToken") or token
        if not data.get("nextPageToken"):
            break
    return files, token
