"""Fixtures for the API and worker tests (session B), star-imported by each test module.

(Not a conftest.py: a second module named ``conftest`` would shadow
``tests/conftest.py``, which the other test modules import by name.)

Runs against the throwaway Postgres in ``TEST_DATABASE_URL`` (CI: the
``ci-api`` workflow's postgres:16 service). The API connects as a real
``rp_app`` login created here, so row-level security applies exactly as in
production. Google, Composio and Groq are replaced only here, through
``api.core.http.transport``; nothing in ``api/`` or ``worker/`` mocks anything.
All rows written by these tests are test fixtures.
"""

from __future__ import annotations

import base64
import importlib.util
import os
import secrets
from pathlib import Path
from typing import Callable

import pytest

import httpx
import psycopg
from psycopg import sql
from psycopg.conninfo import conninfo_to_dict, make_conninfo

ROOT = Path(__file__).resolve().parents[2]
DSN = os.environ.get("TEST_DATABASE_URL")
APP_LOGIN = "test_api_app_login"


def _roles_script():
    spec = importlib.util.spec_from_file_location("create_login_roles", ROOT / "infra/scripts/create_login_roles.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class Dsn(str):
    def __repr__(self) -> str:  # never show the password in test output
        return f"<dsn for {conninfo_to_dict(self).get('user')}>"


@pytest.fixture(scope="session")
def app_dsn():
    if not DSN:
        pytest.skip("TEST_DATABASE_URL not set")
    from jobs import migrate

    with psycopg.connect(DSN, autocommit=True) as conn:
        conn.execute("DROP SCHEMA IF EXISTS biz CASCADE")
        conn.execute("DROP SCHEMA IF EXISTS feeds CASCADE")
    migrate.apply(DSN)
    password = secrets.token_urlsafe(24)
    roles = _roles_script()
    with psycopg.connect(DSN, autocommit=True) as conn:
        conn.execute(sql.SQL("DROP ROLE IF EXISTS {}").format(sql.Identifier(APP_LOGIN)))
        roles.ensure_login_role(conn, APP_LOGIN, "rp_app", password)
    params = conninfo_to_dict(DSN)
    params.update(user=APP_LOGIN, password=password)
    yield Dsn(make_conninfo(**params))
    with psycopg.connect(DSN, autocommit=True) as conn:
        conn.execute("DROP SCHEMA IF EXISTS biz CASCADE")
        conn.execute("DROP SCHEMA IF EXISTS feeds CASCADE")
        conn.execute(sql.SQL("DROP ROLE IF EXISTS {}").format(sql.Identifier(APP_LOGIN)))


@pytest.fixture
def owner_db(app_dsn):
    """Superuser connection for arranging fixtures and inspecting results."""
    with psycopg.connect(DSN, autocommit=True, row_factory=psycopg.rows.dict_row) as conn:
        conn.execute("TRUNCATE biz.workspaces, biz.users CASCADE")
        conn.execute("TRUNCATE feeds.events, feeds.series, feeds.anomalies, feeds.source_health, feeds.analysis_status,"
                     " feeds.sanctions_entries CASCADE")
        conn.execute("DELETE FROM feeds.watch_terms WHERE origin = 'workspace'")
        yield conn


def _key() -> str:
    return base64.urlsafe_b64encode(secrets.token_bytes(32)).decode()


@pytest.fixture
def settings(app_dsn):
    from api.core.config import Settings, parse_key_ring

    return Settings(
        database_url=app_dsn,
        jwt_signing_key=secrets.token_bytes(32),
        token_keys=parse_key_ring(f"k2:{_key()},k1:{_key()}"),
        internal_token=secrets.token_urlsafe(24),
        web_origin="https://dash.example.test",
        public_api_url="https://api.example.test",
        api_internal_url="http://api.internal.test",
        google_client_id="test-client-id",
        google_client_secret="test-client-secret",
        google_picker_api_key="test-picker-key",
        google_project_number="123456",
        composio_api_key="test-composio-key",
        composio_webhook_secret="test-composio-secret",
        composio_auth_configs={"airtable": "ac_test"},
        groq_api_key="test-groq-key",
        groq_mapping_model="test-mapping-model",
        groq_advice_model="test-advice-model",
    )


class FakeHttp:
    """Routes outbound requests to test handlers by (method, host + path prefix)."""

    def __init__(self) -> None:
        self.routes: list[tuple[str, str, Callable[[httpx.Request], httpx.Response]]] = []
        self.calls: list[httpx.Request] = []

    def on(self, method: str, prefix: str, handler: Callable | dict | list, status: int = 200) -> None:
        if not callable(handler):
            body = handler
            handler = lambda req: httpx.Response(status, json=body)  # noqa: E731
        self.routes.insert(0, (method.upper(), prefix, handler))

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.calls.append(request)
        url = f"{request.url.host}{request.url.path}"
        hits = [r for r in self.routes if request.method == r[0] and url.startswith(r[1])]
        if hits:  # the longest (most specific) prefix wins; among equals, the latest registered
            return max(hits, key=lambda r: len(r[1]))[2](request)
        return httpx.Response(599, json={"error": f"no test route for {request.method} {url}"})

    def sent_to(self, prefix: str) -> list[httpx.Request]:
        return [r for r in self.calls if f"{r.url.host}{r.url.path}".startswith(prefix)]


@pytest.fixture
def fake_http():
    from api.core import google, http

    fake = FakeHttp()
    http.transport = httpx.MockTransport(fake)
    google.access_tokens._tokens.clear()
    yield fake
    http.transport = None
    google.access_tokens._tokens.clear()


@pytest.fixture
def client(settings, owner_db, fake_http):
    from fastapi.testclient import TestClient

    from api.main import create_app

    app = create_app(settings)
    with TestClient(app, base_url="https://api.example.test", follow_redirects=False) as c:
        c.rp = app.state.rp
        yield c


__all__ = ["app_dsn", "owner_db", "settings", "fake_http", "client", "FakeHttp", "DSN"]
