"""Helpers shared by the API tests (imported as a plain module)."""

from __future__ import annotations

import json
import uuid


def sign_up(client, email: str | None = None, password: str = "correct horse battery", workspace: str = "Acme") -> dict:
    email = email or f"user-{uuid.uuid4().hex[:8]}@example.test"
    r = client.post("/v1/auth/sign-up", json={"email": email, "password": password, "workspace_name": workspace})
    assert r.status_code == 201, r.text
    out = r.json()
    out["email"], out["password"] = email, password
    return out


def hdr(auth: dict | str, workspace: str | None = None) -> dict:
    token = auth if isinstance(auth, str) else auth["access_token"]
    h = {"Authorization": f"Bearer {token}"}
    ws = workspace if workspace is not None else (auth.get("workspace_id") if isinstance(auth, dict) else None)
    if ws:
        h["X-Workspace-Id"] = ws
    return h


def groq_reply(content) -> dict:
    text = content if isinstance(content, str) else json.dumps(content)
    return {"choices": [{"message": {"role": "assistant", "content": text}}]}


def event_names(owner_db, workspace_id: str | None = None) -> list[str]:
    """The live-update events written so far (biz.events), oldest first."""
    rows = owner_db.execute(
        "SELECT event FROM biz.events WHERE %s::uuid IS NULL OR workspace_id = %s ORDER BY id", (workspace_id, workspace_id)
    ).fetchall()
    return [r["event"] for r in rows]
