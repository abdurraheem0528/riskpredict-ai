"""infra/scripts/deploy.py: the pure rules, and vercel-up / vercel-origin / vercel-destroy /
worker-run against an in-memory fake of the Vercel REST API and CLI (no network, no
database). The values here are test values."""

from __future__ import annotations

import base64
import importlib.util
import json
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
        ("ep-x.us-east-2.aws.neon.tech", "cle1"),
        ("ep-x-pooler.us-east-1.aws.neon.tech", "iad1"),
        ("ep-x.c-3.us-west-2.aws.neon.tech", "pdx1"),
        ("ep-x.eu-central-1.aws.neon.tech", "fra1"),
        ("ep-x.eu-west-2.aws.neon.tech", "lhr1"),
        ("ep-x.ap-southeast-1.aws.neon.tech", "sin1"),
        ("ep-x.eastus2.azure.neon.tech", "iad1"),
        ("db.example.org", "iad1"),
    ],
)
def test_vercel_region(d, host, region):
    assert d.vercel_region(host) == region
    assert d.vercel_region(host, "fra1") == "fra1"
    assert region in d.VERCEL_REGIONS


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
    assert d.new_secret("JWT_SIGNING_KEY") != d.new_secret("JWT_SIGNING_KEY")
    assert d.GENERATED == ("JWT_SIGNING_KEY", "TOKEN_ENCRYPTION_KEYS")  # INTERNAL_TOKEN is gone


def test_plan_env_never_overwrites_generated_keys(d):
    current = {"JWT_SIGNING_KEY": "old", "WEB_ORIGIN": "https://a.pages.dev", "GROQ_API_KEY": "g"}
    plan = d.plan_env(current, {"WEB_ORIGIN": "https://b.pages.dev", "GROQ_API_KEY": "g"},
                      {"JWT_SIGNING_KEY": "new", "TOKEN_ENCRYPTION_KEYS": "fresh"})  # fmt: skip
    assert plan.set == {"WEB_ORIGIN": "https://b.pages.dev", "TOKEN_ENCRYPTION_KEYS": "fresh"}
    assert plan.kept == ["JWT_SIGNING_KEY"]


def test_production_url_is_the_stable_alias(d):
    aliases = ["riskpredict-api-git-main-me.vercel.app", "riskpredict-api.vercel.app", "riskpredict-api-me.vercel.app"]
    assert d.production_url("riskpredict-api", aliases) == "https://riskpredict-api.vercel.app"
    taken = ["riskpredict-api-xyz-me.vercel.app", "riskpredict-api-three.vercel.app"]
    assert d.production_url("riskpredict-api", taken) == "https://riskpredict-api-three.vercel.app"
    with pytest.raises(d.DeployError):
        d.production_url("riskpredict-api", [])


# --- a fake Vercel ------------------------------------------------------------------------


class FakeVercel:
    """Just the REST endpoints deploy.py uses, plus `vercel deploy --prod` (run_cli)."""

    def __init__(self):
        self.projects: dict[str, dict] = {}
        self.env: dict[str, dict[str, dict]] = {}  # project id → key → row
        self.calls: list[tuple[str, str]] = []
        self.deploys: list[dict] = []
        self.patches: list[dict] = []

    def call(self, method, path, body=None, *, ok404=False, tries=5):
        p = path.split("?")[0]
        self.calls.append((method, p))
        by_name = {v["name"]: v for v in self.projects.values()}
        if method == "GET" and p == "/v2/user":
            return 200, {"user": {"id": "user_me", "defaultTeamId": "team_me"}}
        if method == "GET" and (m := re.fullmatch(r"/v9/projects/([\w-]+)", p)):
            proj = self.projects.get(m.group(1)) or by_name.get(m.group(1))
            return (200, proj) if proj else (404, None)
        if method == "POST" and p == "/v11/projects":
            pid = f"prj_{len(self.projects) + 1}"
            self.projects[pid] = {"id": pid, "name": body["name"], "accountId": "team_me", **body}
            self.env[pid] = {}
            return 200, self.projects[pid]
        if method == "PATCH" and (m := re.fullmatch(r"/v9/projects/(prj_\d+)", p)):
            self.patches.append(body)
            self.projects[m.group(1)].update(body)
            return 200, self.projects[m.group(1)]
        if method == "DELETE" and (m := re.fullmatch(r"/v9/projects/(prj_\d+)", p)):
            del self.projects[m.group(1)]
            return 204, None
        if (m := re.fullmatch(r"/v10/projects/(prj_\d+)/env", p)):
            rows = self.env[m.group(1)]
            if method == "GET":
                assert "decrypt=true" in path
                return 200, {"envs": [dict(r, decrypted=r["key"] != "UNDECRYPTED") for r in rows.values()]}
            assert "upsert=true" in path and body["type"] == "encrypted" and body["target"] == ["production"]
            rows[body["key"]] = {"id": f"env_{body['key']}", "key": body["key"], "value": body["value"],
                                 "type": "encrypted", "target": ["production"]}  # fmt: skip
            return 201, {"created": {"key": body["key"]}, "failed": []}
        if method == "GET" and (m := re.fullmatch(r"/v1/projects/(prj_\d+)/env/env_(\w+)", p)):
            return 200, self.env[m.group(1)][m.group(2)]
        if method == "GET" and (m := re.fullmatch(r"/v13/deployments/([\w.-]+)", p)):
            dep = next(x for x in self.deploys if x["url"] == m.group(1))
            return 200, {"id": dep["id"], "readyState": "READY", "aliasAssigned": True,
                         "alias": ["riskpredict-api-git-main-me.vercel.app", "riskpredict-api.vercel.app"]}  # fmt: skip
        raise AssertionError(f"unexpected {method} {path}")

    def cli(self, directory: Path, proj: dict) -> str:
        files = {str(f.relative_to(directory)) for f in directory.rglob("*") if f.is_file()}
        n = len(self.deploys) + 1
        self.deploys.append({"id": f"dpl_{n}", "url": f"riskpredict-api-{n}abc-me.vercel.app", "project": proj["id"],
                             "files": files, "vercel_json": json.loads((directory / "vercel.json").read_text()),
                             "env": dict(self.env[proj["id"]])})  # fmt: skip
        return f"https://riskpredict-api-{n}abc-me.vercel.app"


@pytest.fixture
def fake(d, monkeypatch):
    fv = FakeVercel()
    state = {"role": False, "script": [], "health": []}
    monkeypatch.setattr(d.Api, "call", lambda self, *a, **k: fv.call(*a, **k))
    monkeypatch.setattr(d, "run_cli", fv.cli)
    monkeypatch.setattr(d, "app_role_exists", lambda: state["role"])

    def roles(*extra, password=None):
        state["script"].append((extra, password is not None))
        state["role"] = True

    monkeypatch.setattr(d, "run_roles_script", roles)
    monkeypatch.setattr(d, "wait_healthy", lambda url: state["health"].append(url) or {"status": "ok", "version": "1"})
    real = d._bundle_module()
    report = {"total_mb": 271.8, "limit_mb": 500, "largest": ["scipy 80 MB"]}
    monkeypatch.setattr(real, "check", lambda out, guard: report)  # sizes: tested in tests/test_vercel_bundle.py
    monkeypatch.setattr(d, "_bundle_module", lambda: real)
    monkeypatch.setattr(d.time, "sleep", lambda s: None)
    for k, v in {"VERCEL_TOKEN": "vtok-test-value", "DATABASE_URL_OWNER": OWNER, "GROQ_API_KEY": "gk-test-value",
                 "GOOGLE_OAUTH_CLIENT_ID": "", "COMPOSIO_API_KEY": "", "VERCEL_TEAM_ID": ""}.items():  # fmt: skip
        monkeypatch.setenv(k, v)
    monkeypatch.delenv("GITHUB_OUTPUT", raising=False)
    return fv, state


def up(d, *extra):
    assert d.main(["vercel-up", "--web-origin", "https://riskpredict.pages.dev", *extra]) == 0


def env_of(fv) -> dict[str, str]:
    [pid] = fv.env
    return {k: r["value"] for k, r in fv.env[pid].items()}


def test_first_run_creates_everything_and_prints_no_secret(d, fake, capsys):
    fv, state = fake
    up(d)
    out = capsys.readouterr().out
    [proj] = fv.projects.values()
    assert proj["name"] == "riskpredict-api" and proj["framework"] == "fastapi"
    assert proj["ssoProtection"] is None and proj["passwordProtection"] is None  # public: no Deployment Protection
    env = env_of(fv)
    assert env["WEB_ORIGIN"] == "https://riskpredict.pages.dev" and env["CLIENT_IP_HEADER"] == "x-real-ip"
    assert env["PUBLIC_API_URL"] == "https://riskpredict-api.vercel.app"
    assert "-pooler." in env["DATABASE_URL_APP"] and "GOOGLE_OAUTH_CLIENT_ID" not in env
    assert "INTERNAL_TOKEN" not in env and "API_INTERNAL_URL" not in env
    for k in ("JWT_SIGNING_KEY", "TOKEN_ENCRYPTION_KEYS", "DATABASE_URL_APP"):
        assert f"::add-mask::{env[k]}" in out  # masked ...
        assert out.count(env[k]) == 1  # ... and never printed anywhere else
    assert "gk-test-value" not in out and "vtok-test-value" not in out and "ownerpw" not in out
    # Two deploys on the first run: PUBLIC_API_URL is only known once the project has its address.
    assert len(fv.deploys) == 2
    assert "PUBLIC_API_URL" not in fv.deploys[0]["env"] and "PUBLIC_API_URL" in fv.deploys[1]["env"]
    files = fv.deploys[0]["files"]
    assert {"app.py", "pyproject.toml", "vercel.json", "pkg/api/main.py", "pkg/engine/__init__.py",
            "pkg/worker/passes.py", "pkg/jobs/codes.py"} <= files  # fmt: skip
    assert not [f for f in files if f.startswith(("web/", "design/", "tests/", "api/", "pkg/jobs/connectors"))]
    assert "pkg/worker/main.py" not in files and "pkg/jobs/analyse/tagging.py" not in files
    assert fv.deploys[0]["vercel_json"]["regions"] == ["cle1"]  # next to us-east-2
    assert fv.deploys[0]["vercel_json"]["functions"]["app.py"]["maxDuration"] <= 300
    assert state["script"] == [((), True)]  # created with --password-stdin
    assert state["health"] == ["https://riskpredict-api.vercel.app"]
    assert "bundle size: 271.8 MB of Vercel's 500 MB" in out


def test_groq_models_default_to_the_free_plan_models_and_follow_the_variables(d, fake, monkeypatch):
    fv, _ = fake
    monkeypatch.delenv("GROQ_MAPPING_MODEL", raising=False)
    monkeypatch.setenv("GROQ_ADVICE_MODEL", "")
    up(d)
    env = env_of(fv)
    assert env["GROQ_MAPPING_MODEL"] == "openai/gpt-oss-120b" and env["GROQ_ADVICE_MODEL"] == "openai/gpt-oss-20b"
    monkeypatch.setenv("GROQ_ADVICE_MODEL", "openai/gpt-oss-120b")
    up(d)
    assert env_of(fv)["GROQ_ADVICE_MODEL"] == "openai/gpt-oss-120b"
    assert set(d.GROQ_MODEL_DEFAULTS) <= set(d.WORKER_KEYS)  # the worker reads them from Vercel too


def test_rerun_keeps_secrets_and_role_and_adds_optional_ones(d, fake, monkeypatch, capsys):
    fv, state = fake
    up(d)
    before = env_of(fv)
    monkeypatch.setenv("GOOGLE_OAUTH_CLIENT_ID", "cid.apps.googleusercontent.com")
    monkeypatch.setenv("GROQ_API_KEY", "gk2")
    capsys.readouterr()
    up(d)
    out = capsys.readouterr().out
    after = env_of(fv)
    for k in ("JWT_SIGNING_KEY", "TOKEN_ENCRYPTION_KEYS", "DATABASE_URL_APP", "PUBLIC_API_URL"):
        assert after[k] == before[k], k
    assert after["GOOGLE_OAUTH_CLIENT_ID"] == "cid.apps.googleusercontent.com" and after["GROQ_API_KEY"] == "gk2"
    assert state["script"][-1] == (("--check",), False)
    assert len(fv.projects) == 1 and len(fv.deploys) == 3  # one deploy: the address is known now
    for k in ("JWT_SIGNING_KEY", "TOKEN_ENCRYPTION_KEYS", "DATABASE_URL_APP"):  # read back: masked first
        assert out.count(before[k]) == 1 and f"::add-mask::{before[k]}" in out


def test_rotate_changes_only_the_app_password(d, fake):
    fv, state = fake
    up(d)
    before = env_of(fv)
    up(d, "--rotate-app-password")
    after = env_of(fv)
    assert after["DATABASE_URL_APP"] != before["DATABASE_URL_APP"]
    assert after["TOKEN_ENCRYPTION_KEYS"] == before["TOKEN_ENCRYPTION_KEYS"]
    assert state["script"][-1] == (("--rotate",), True)


def test_a_role_left_by_render_is_rotated_on_the_first_vercel_deploy(d, fake):
    fv, state = fake
    state["role"] = True  # the failed Render run created it; nobody knows its password
    up(d)
    assert state["script"] == [(("--rotate",), True)]
    assert "DATABASE_URL_APP" in env_of(fv)


def test_protection_that_stays_on_is_an_error(d, fake, monkeypatch, capsys):
    fv, _ = fake
    orig = fv.call

    def sticky(method, path, body=None, **k):
        status, res = orig(method, path, body, **k)
        if method == "PATCH":
            res = dict(res, ssoProtection={"deploymentType": "all_except_custom_domains"})
        return status, res

    monkeypatch.setattr(d.Api, "call", lambda self, *a, **k: sticky(*a, **k))
    assert d.main(["vercel-up", "--web-origin", "https://x.pages.dev"]) == 1
    assert "Deployment Protection" in capsys.readouterr().out


def test_vercel_origin_redeploys_only_on_change(d, fake):
    fv, _ = fake
    up(d)
    n = len(fv.deploys)
    assert d.main(["vercel-origin", "--web-origin", "https://riskpredict.pages.dev", "--region", "cle1"]) == 0
    assert len(fv.deploys) == n
    assert d.main(["vercel-origin", "--web-origin", "https://riskpredict-9z.pages.dev", "--region", "cle1"]) == 0
    assert len(fv.deploys) == n + 1 and env_of(fv)["WEB_ORIGIN"] == "https://riskpredict-9z.pages.dev"
    assert fv.deploys[-1]["vercel_json"]["regions"] == ["cle1"]


def test_vercel_destroy_deletes_the_project_once(d, fake, capsys):
    fv, _ = fake
    up(d)
    assert d.main(["vercel-destroy"]) == 0
    assert fv.projects == {}
    assert d.main(["vercel-destroy"]) == 0
    assert "already gone" in capsys.readouterr().out
    assert not [c for c in fv.calls if "env" in c[1] and c[0] != "GET" and c[0] != "POST"]  # no env edits


def test_worker_run_passes_only_the_worker_keys_masked(d, fake, monkeypatch, capsys):
    fv, _ = fake
    up(d)
    pid = next(iter(fv.env))
    fv.env[pid]["COMPOSIO_API_KEY"] = {"id": "env_COMPOSIO_API_KEY", "key": "COMPOSIO_API_KEY", "value": "cmp-secret",
                                       "type": "encrypted", "target": ["production"]}  # fmt: skip
    fv.env[pid]["UNDECRYPTED"] = {"id": "env_UNDECRYPTED", "key": "UNDECRYPTED", "value": "", "type": "encrypted",
                                  "target": ["production"]}  # fmt: skip
    seen = {}

    def run(cmd, cwd=None, env=None, **k):
        seen["cmd"], seen["env"] = cmd, env
        return type("P", (), {"returncode": 0})()

    monkeypatch.setattr(d.subprocess, "run", run)
    monkeypatch.setenv("DATABASE_URL_FEEDS", "postgresql://not-for-the-worker")
    capsys.readouterr()
    assert d.main(["worker-run"]) == 0
    out = capsys.readouterr().out
    stored = env_of(fv)
    assert seen["cmd"][1:] == ["-m", "worker", "--once"]
    child = seen["env"]
    assert child["DATABASE_URL_APP"] == stored["DATABASE_URL_APP"]
    assert child["TOKEN_ENCRYPTION_KEYS"] == stored["TOKEN_ENCRYPTION_KEYS"] and child["COMPOSIO_API_KEY"] == "cmp-secret"
    assert "JWT_SIGNING_KEY" not in child and "VERCEL_TOKEN" not in child and "WEB_ORIGIN" not in child
    for v in (stored["DATABASE_URL_APP"], stored["TOKEN_ENCRYPTION_KEYS"], "cmp-secret", stored["GROQ_API_KEY"]):
        assert f"::add-mask::{v}" in out and out.count(v) == 1


def test_calls_are_scoped_to_the_default_team_unless_one_is_set(d, fake, monkeypatch):
    assert d.vercel().query == {"teamId": "team_me"}  # the Hobby account's own team
    monkeypatch.setenv("VERCEL_TEAM_ID", "team_other")
    assert d.vercel().query == {"teamId": "team_other"}


def test_worker_run_skips_cleanly_before_the_first_deploy(d, fake, monkeypatch, capsys):
    assert d.main(["worker-run"]) == 0
    assert "Worker skipped" in capsys.readouterr().out
    monkeypatch.setenv("VERCEL_TOKEN", "")
    assert d.main(["worker-run"]) == 0
