"""Reading a source's rows from its tool (Google Sheets directly, Airtable via Composio).

Shared by the API (creating a source, previewing a mapping) and the worker
(syncs). Row contents stay in memory and are never logged.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import Any
from uuid import UUID

from api.core import composio, crypto, google
from api.core.config import Settings
from api.core.errors import UpstreamAuthError, UpstreamUnavailable
from api.core.mapping import RawRow

MAX_ROWS = 10_000


@dataclass(frozen=True)
class ConnectionRef:
    workspace_id: UUID
    id: UUID
    provider: str
    status: str
    refresh_token_ciphertext: bytes | None = None
    refresh_token_key_id: str | None = None
    composio_connected_account_id: str | None = None

    @classmethod
    def from_row(cls, row: dict) -> ConnectionRef:
        blob = row.get("refresh_token_ciphertext")
        return cls(
            workspace_id=row["workspace_id"],
            id=row["id"],
            provider=row["provider"],
            status=row["status"],
            refresh_token_ciphertext=bytes(blob) if blob is not None else None,
            refresh_token_key_id=row.get("refresh_token_key_id"),
            composio_connected_account_id=row.get("composio_connected_account_id"),
        )

    @property
    def app(self) -> str:
        return self.provider.split(":", 1)[1] if self.provider.startswith("composio:") else ""


CONNECTION_COLUMNS = (
    "id, workspace_id, provider, status, refresh_token_ciphertext, refresh_token_key_id, composio_connected_account_id"
)


def google_refresh_token(settings: Settings, conn: ConnectionRef) -> str:
    if conn.refresh_token_ciphertext is None or not conn.refresh_token_key_id:
        raise UpstreamAuthError(google.SERVICE)
    try:
        return crypto.decrypt_token(
            conn.refresh_token_ciphertext, conn.refresh_token_key_id, settings.token_keys, conn.workspace_id, conn.id
        )
    except crypto.DecryptError:
        raise UpstreamAuthError(google.SERVICE, "the stored Google token cannot be read; reconnect Google Sheets") from None


def google_access_token(settings: Settings, conn: ConnectionRef, *, fresh: bool = False) -> google.TokenGrant | str:
    key = str(conn.id)
    if not fresh and (hit := google.access_tokens.get(key)):
        return hit
    grant = google.refresh(settings, google_refresh_token(settings, conn))
    google.access_tokens.put(key, grant)
    return grant if fresh else grant.access_token


def _token(settings: Settings, conn: ConnectionRef) -> str:
    tok = google_access_token(settings, conn)
    return tok if isinstance(tok, str) else tok.access_token


@dataclass
class Table:
    name: str
    url: str | None
    columns: list[dict]  # SourceColumn
    rows: list[RawRow]
    header: list[str]

    def content_hash(self) -> str:
        h = hashlib.sha256(json.dumps(self.header, ensure_ascii=False).encode())
        for r in self.rows:
            h.update(b"\n" + r.digest().encode())
        return h.hexdigest()


def _unique_headers(raw: list[str], width: int) -> list[str]:
    out: list[str] = []
    seen: dict[str, int] = {}
    for i in range(width):
        name = (raw[i] if i < len(raw) else "").strip() or f"Column {_col_letter(i)}"
        name = name[:200]
        if name in seen:
            seen[name] += 1
            name = f"{name} ({seen[name]})"
        else:
            seen[name] = 1
        out.append(name)
    return out


def _col_letter(i: int) -> str:
    s = ""
    i += 1
    while i:
        i, r = divmod(i - 1, 26)
        s = chr(65 + r) + s
    return s


def _columns(header: list[str], rows: list[RawRow]) -> list[dict]:
    cols = []
    for idx, name in enumerate(header):
        samples: list[str] = []
        for r in rows:
            v = (r.values.get(name) or "").strip()
            if v and v not in samples:
                samples.append(v[:100])
            if len(samples) == 5:
                break
        cols.append({"index": idx, "name": name, "samples": samples})
    return cols


def _cell(v: Any) -> str:
    if v is None:
        return ""
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, (list, tuple)):
        return ", ".join(_cell(x) for x in v if _cell(x))
    if isinstance(v, dict):
        for k in ("name", "text", "label", "email", "url", "id"):
            if k in v:
                return _cell(v[k])
        return ""
    return str(v)


def read_google(settings: Settings, conn: ConnectionRef, spreadsheet_id: str, gid: str, header_row: int,
                max_rows: int | None = None, *, first_pick: bool = False) -> Table:  # fmt: skip
    token = _token(settings, conn)
    meta = google.spreadsheet(token, spreadsheet_id, waits=google.FIRST_PICK_WAITS if first_pick else ())
    tab = meta.tab(gid)
    if tab is None:
        from api.core.errors import UpstreamNotFound

        raise UpstreamNotFound("that tab is not in the spreadsheet")
    limit = None if max_rows is None else header_row + max_rows
    grid = google.values(token, spreadsheet_id, tab.title, max_rows=limit)
    head = grid[header_row - 1] if len(grid) >= header_row else []
    body = grid[header_row : header_row + (max_rows or MAX_ROWS)]
    width = max([len(head), *(len(r) for r in body)] or [0])
    header = _unique_headers(head, width)
    rows: list[RawRow] = []
    for i, r in enumerate(body):
        if not any(c.strip() for c in r):
            continue
        rows.append(RawRow(row_number=header_row + 1 + i, record_id=None,
                           values={header[j]: r[j] for j in range(min(len(r), width))}))  # fmt: skip
    name = meta.title if len(meta.tabs) == 1 else f"{meta.title} / {tab.title}"
    url = f"https://docs.google.com/spreadsheets/d/{spreadsheet_id}/edit#gid={tab.gid}"
    return Table(name=name[:300] or "Sheet", url=url, columns=_columns(header, rows), rows=rows, header=header)


def read_composio(settings: Settings, conn: ConnectionRef, base_id: str, table_id: str,
                  max_rows: int | None = None) -> Table:  # fmt: skip
    if conn.app != "airtable":
        raise UpstreamUnavailable(composio.SERVICE, f"reading {conn.app} is not supported yet")
    account = conn.composio_connected_account_id or ""
    tables = composio.airtable_tables(settings, account, conn.workspace_id, base_id)
    meta = next((t for t in tables if t.get("id") == table_id or t.get("name") == table_id), None)
    if meta is None:
        from api.core.errors import UpstreamNotFound

        raise UpstreamNotFound("that table is not in the base")
    header = _unique_headers([f.get("name", "") for f in meta.get("fields") or [] if isinstance(f, dict)], len(meta.get("fields") or []))
    records = composio.airtable_records(settings, account, conn.workspace_id, base_id, meta["id"], max_rows or MAX_ROWS)
    rows = []
    for rec in records:
        fields = rec.get("fields") or {}
        for k in fields:
            if k not in header:
                header.append(str(k)[:200])
        rows.append(RawRow(row_number=None, record_id=str(rec["id"]), values={k: _cell(v) for k, v in fields.items()}))
    url = f"https://airtable.com/{base_id}/{meta['id']}"
    return Table(name=str(meta.get("name") or table_id)[:300], url=url, columns=_columns(header, rows), rows=rows, header=header)


def read(settings: Settings, conn: ConnectionRef, kind: str, external_id: str, external_table_id: str,
         header_row: int, max_rows: int | None = None, *, first_pick: bool = False) -> Table:  # fmt: skip
    """The table as it is now. ``first_pick`` (adding a source only, never the worker): a Google
    sheet that answers 404 is read again over about 5 s, while Google's grant for a sheet picked
    moments after it was created comes through (``google.FIRST_PICK_WAITS``)."""
    if kind == "google_sheet":
        return read_google(settings, conn, external_id, external_table_id, header_row, max_rows, first_pick=first_pick)
    return read_composio(settings, conn, external_id, external_table_id, max_rows)


def row_url(source_url: str | None, kind: str, row_number: int | None, row_key: str) -> str | None:
    if not source_url:
        return None
    if kind == "google_sheet" and row_number:
        return f"{source_url}&range=A{row_number}"
    if kind == "composio_table" and row_key.startswith("rec"):
        return f"{source_url}/{row_key}"
    return None
