"""infra/scripts/deploy.py: the pure rules, and render-up against an in-memory fake of the
Render API (no network, no database). The values here are test values."""

from __future__ import annotations

import base64
import importlib.util
import re
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture(scope="module")
def d():
    spec = importlib.util.spec_from_file_location("deploy_script", ROOT / "infra/scripts/deploy.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules["deploy_script"] = mod  # dataclasses look their module up here
    spec.loader.exec_module(mod)
    return mod


OWNER = "postgresql://neondb_owner:ownerpw@ep-cool-dew-123456.us-east-2.aws.neon.tech/neondb?sslmode=require&channel_binding=require"


@pytest.mark.parametrize(
    "host, pooled",
    [
        ("ep-cool-dew-123456.us-east-2.aws.neon.tech", "ep-cool-dew-123456-pooler.us-east-2.aws.neon.tech"),
        ("ep-cool-dew-123456-pooler.us-east-2.aws.neon.tech", "ep-cool-dew-123456-pooler.us-east-2.aws.neon.tech"),
        ("ep-a-1.c-2.eu-central-1.aws.neon.tech", "ep-a-1-pooler.c-2.eu-central-1.aws.neon.tech"),
        ("db.example.org", "db.example.org"),
    ],
)
def test_pooled_host(d, host, pooled):
    assert d.pooled_host(host) == pooled
    assert d.direct_host(d.pooled_host(host)).split(".")[0] == host.split(".")[0].removesuffix("-pooler")


@pytest.mark.parametrize(
    "host, region",
    [
        ("ep-x.us-east-2.aws.neon.tech", "ohio"),
        ("ep-x-pooler.us-east-1.aws.neon.tech", "virginia"),
        ("ep-x.c-3.us-west-2.aws.neon.tech", "oregon"),
        ("ep-x.eu-central-1.aws.neon.tech", "frankfurt"),
        ("ep-x.eu-west-2.aws.neon.tech", "frankfurt"),
        ("ep-x.ap-southeast-1.aws.neon.tech", "singapore"),
        ("ep-x.eastus2.azure.neon.tech", "virginia"),
        ("db.example.org", "ohio"),
    ],
)
def test_render_region(d, host, region):
    assert d.render_region(host) == region
    assert d.render_region(host, "frankfurt") == "frankfurt"


def test_app_url_uses_pooled_host_and_owner_tls(d):
    from psycopg.conninfo import conninfo_to_dict

    url = d.app_url(OWNER, "p@ss/word")
    parts = conninfo_to_dict(url)
    assert parts["user"] == "riskpredict_app" and parts["password"] == "p@ss/word"
    assert parts["host"] == "ep-cool-dew-123456-pooler.us-east-2.aws.neon.tech"
    assert parts["dbname"] == "neondb" and parts["sslmode"] == "require" and parts["channel_binding"] == "require"
    assert "ownerpw" not in url


def test_generated_secrets_have_the_formats_config_accepts(d):
    from api.core.config import parse_key_ring

    jwt = base64.b64decode(d.new_secret("JWT_SIGNING_KEY"))
    assert len(jwt) == 32
    ring = parse_key_ring(d.new_secret("TOKEN_ENCRYPTION_KEYS"))
    assert ring[0][0] == "k1" and len(ring[0][1]) == 32
    assert len(d.new_secret("INTERNAL_TOKEN")) >= 40
    assert d.new_secret("INTERNAL_TOKEN") != d.new_secret("INTERNAL_TOKEN")


def test_forwarded_allow_ips_never_trusts_everyone(d):
    v = d.forwarded_allow_ips([])
    assert "*" not in v and "10.0.0.0/8" in v and "173.245.48.0/20" in v


def test_plan_env_never_overwrites_generated_keys(d):
    current = {"JWT_SIGNING_KEY": "old", "WEB_ORIGIN": "https://a.pages.dev", "GROQ_API_KEY": "g"}
    plan = d.plan_env(current, {"WEB_ORIGIN": "https://b.pages.dev", "GROQ_API_KEY": "g"},
                      {"JWT_SIGNING_KEY": "new", "INTERNAL_TOKEN": "fresh"})  # fmt: skip
    assert plan.set == {"WEB_ORIGIN": "https://b.pages.dev", "INTERNAL_TOKEN": "fresh"}
    assert plan.kept == ["JWT_SIGNING_KEY"]


# --- render-up against a fake Render API ----------------------------------------------------


class FakeRender:
    def __init__(self):
        self.services: dict[str, dict] = {}
        self.env: dict[str, dict[str, str]] = {}
        self.calls: list[tuple[str, str]] = []
        self.deploys = 0

    def call(self, method, path, body=None, *, ok404=False, tries=5):
        self.calls.append((method, path.split("?")[0]))
        p = path.split("?")[0]
        if p == "/owners":
            return 200, [{"owner": {"id": "tea-1", "name": "me"}, "cursor": "c"}]
        if p == "/services" and method == "GET":
            return 200, [{"service": s, "cursor": "c"} for s in self.services.values()]
        if p == "/services" and method == "POST":
            sid = f"srv-{len(self.services) + 1}"
            details = dict(body["serviceDetails"], url=f"https://riskpredict-api-x{len(self.services)}.onrender.com")
            self.services[sid] = {"id": sid, "name": body["name"], "serviceDetails": details}
            self.env[sid] = {e["key"]: e["value"] for e in body["envVars"]}
            self.create_body = body
            return 201, {"service": self.services[sid], "deployId": "dep-auto"}
        m = re.fullmatch(r"/services/(srv-\d+)(/.*)?", p)
        sid, rest = m.group(1), m.group(2) or ""
        if rest == "/env-vars":
            return 200, [{"envVar": {"key": k, "value": v}, "cursor": k} for k, v in self.env[sid].items()]
        if rest.startswith("/env-vars/") and method == "PUT":
            self.env[sid][rest.split("/")[-1]] = body["value"]
            return 200, {"key": rest.split("/")[-1], "value": body["value"]}
        if rest == "/deploys" and method == "POST":
            self.deploys += 1
            self.last_commit = body.get("commitId")
            return 201, {"id": f"dep-{self.deploys}", "status": "created"}
        if rest.startswith("/deploys/") and rest.endswith("/cancel"):
            return 200, {"id": "dep-auto", "status": "canceled"}
        if rest.startswith("/deploys/"):
            return 200, {"id": rest.split("/")[2], "status": "live"}
        if method == "PATCH" and rest == "":
            return 200, self.services[sid]
        raise AssertionError(f"unexpected {method} {path}")


@pytest.fixture
def fake(d, monkeypatch, capsys):
    fr = FakeRender()
    state = {"role": False, "script": []}
    monkeypatch.setattr(d.Api, "call", lambda self, *a, **k: fr.call(*a, **k))
    monkeypatch.setattr(d, "app_role_exists", lambda: state["role"])

    def roles(*extra, password=None):
        state["script"].append((extra, password is not None))
        state["role"] = True

    monkeypatch.setattr(d, "run_roles_script", roles)
    monkeypatch.setattr(d, "wait_healthy", lambda url: {"status": "ok", "schema_version": "041_x", "version": "1"})
    monkeypatch.setattr(d, "cloudflare_ranges", lambda: d.CLOUDFLARE_FALLBACK)
    monkeypatch.setattr(d.time, "sleep", lambda s: None)
    for k, v in {"RENDER_API_KEY": "rk", "DATABASE_URL_OWNER": OWNER, "GROQ_API_KEY": "gk",
                 "GITHUB_REPOSITORY": "o/r", "GITHUB_SHA": "abc123", "GOOGLE_OAUTH_CLIENT_ID": "",
                 "RENDER_OWNER_ID": "", "COMPOSIO_API_KEY": ""}.items():  # fmt: skip
        monkeypatch.setenv(k, v)
    monkeypatch.delenv("GITHUB_OUTPUT", raising=False)
    return fr, state


def up(d, *extra):
    assert d.main(["render-up", "--web-origin", "https://riskpredict.pages.dev", *extra]) == 0


def test_first_run_creates_everything_once(d, fake, capsys):
    fr, state = fake
    up(d)
    out = capsys.readouterr().out
    body = fr.create_body
    assert body["serviceDetails"]["plan"] == "free" and body["serviceDetails"]["runtime"] == "docker"
    assert body["serviceDetails"]["region"] == "ohio" and body["autoDeployTrigger"] == "off"
    assert body["serviceDetails"]["envSpecificDetails"]["dockerfilePath"] == "./infra/render/Dockerfile"
    assert body["repo"] == "https://github.com/o/r" and body["branch"] == "main"
    env = fr.env["srv-1"]
    assert env["PUBLIC_API_URL"] == "https://riskpredict-api-x0.onrender.com"
    assert env["WEB_ORIGIN"] == "https://riskpredict.pages.dev"
    assert env["API_INTERNAL_URL"] == "http://127.0.0.1:10000" and env["PORT"] == "10000"
    assert "-pooler." in env["DATABASE_URL_APP"] and "GOOGLE_OAUTH_CLIENT_ID" not in env
    for k in ("JWT_SIGNING_KEY", "TOKEN_ENCRYPTION_KEYS", "INTERNAL_TOKEN", "DATABASE_URL_APP"):
        assert f"::add-mask::{env[k]}" in out  # masked ...
        assert out.count(env[k]) == 1  # ... and never printed anywhere else
    assert ("POST", "/services/srv-1/deploys/dep-auto/cancel") in fr.calls
    assert fr.deploys == 1 and fr.last_commit == "abc123"
    assert state["script"] == [((), True)]  # created with --password-stdin


def test_rerun_keeps_secrets_and_role_and_adds_optional_ones(d, fake, monkeypatch, capsys):
    fr, state = fake
    up(d)
    before = dict(fr.env["srv-1"])
    monkeypatch.setenv("GOOGLE_OAUTH_CLIENT_ID", "cid.apps.googleusercontent.com")
    monkeypatch.setenv("GROQ_API_KEY", "gk2")
    up(d)
    after = fr.env["srv-1"]
    for k in ("JWT_SIGNING_KEY", "TOKEN_ENCRYPTION_KEYS", "INTERNAL_TOKEN", "DATABASE_URL_APP"):
        assert after[k] == before[k], k
    assert after["GOOGLE_OAUTH_CLIENT_ID"] == "cid.apps.googleusercontent.com" and after["GROQ_API_KEY"] == "gk2"
    assert state["script"][-1] == (("--check",), False)
    assert len(fr.services) == 1 and fr.deploys == 2


def test_rotate_changes_only_the_app_password(d, fake):
    fr, state = fake
    up(d)
    before = dict(fr.env["srv-1"])
    up(d, "--rotate-app-password")
    after = fr.env["srv-1"]
    assert after["DATABASE_URL_APP"] != before["DATABASE_URL_APP"]
    assert after["TOKEN_ENCRYPTION_KEYS"] == before["TOKEN_ENCRYPTION_KEYS"]
    assert state["script"][-1] == (("--rotate",), True)


def test_existing_role_without_render_value_is_rotated(d, fake):
    fr, state = fake
    state["role"] = True  # e.g. after Destroy without drop_app_role
    up(d)
    assert state["script"] == [(("--rotate",), True)]
    assert "DATABASE_URL_APP" in fr.env["srv-1"]


def test_refuses_a_paid_service(d, fake, capsys):
    fr, _ = fake
    up(d)
    fr.services["srv-1"]["serviceDetails"]["plan"] = "starter"
    assert d.main(["render-up", "--web-origin", "https://x.pages.dev"]) == 1
    assert "not free" in capsys.readouterr().out


def test_render_origin_redeploys_only_on_change(d, fake):
    fr, _ = fake
    up(d)
    n = fr.deploys
    assert d.main(["render-origin", "--web-origin", "https://riskpredict.pages.dev"]) == 0
    assert fr.deploys == n
    assert d.main(["render-origin", "--web-origin", "https://riskpredict-9z.pages.dev"]) == 0
    assert fr.deploys == n + 1 and fr.env["srv-1"]["WEB_ORIGIN"] == "https://riskpredict-9z.pages.dev"
