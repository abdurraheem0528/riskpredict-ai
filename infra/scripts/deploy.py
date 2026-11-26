"""Deploy / destroy helpers for .github/workflows/deploy.yml, destroy.yml and worker.yml.

Runs inside GitHub Actions only (never on the owner's computer). Standard library
only, apart from psycopg for the role checks. Every secret this script creates or
reads is announced to the runner with ``::add-mask::`` before it is used anywhere,
and no secret value is ever printed, written to $GITHUB_OUTPUT or to a file.

    python infra/scripts/deploy.py pages-ensure                 # Cloudflare Pages project, prints its URL
    python infra/scripts/deploy.py vercel-up --web-origin URL   # role + Vercel project + env + bundle + deploy
    python infra/scripts/deploy.py vercel-origin --web-origin URL
    python infra/scripts/deploy.py vercel-destroy
    python infra/scripts/deploy.py pages-destroy
    python infra/scripts/deploy.py drop-app-role
    python infra/scripts/deploy.py worker-run                   # python -m worker --once, env from Vercel

Environment (only what each command needs):
    VERCEL_TOKEN (and VERCEL_TEAM_ID only to use a team other than the token's default
    one), CLOUDFLARE_API_TOKEN, CLOUDFLARE_ACCOUNT_ID,
    DATABASE_URL_OWNER, GROQ_API_KEY, the optional Google / Composio secrets.

**Where the API's secrets live: only in the Vercel project's environment.** Deploy
writes them there (generated ones once, never overwritten); the worker job reads
them back from there at its start (``worker-run``) with the same ``VERCEL_TOKEN``,
masks each one, and hands them to ``python -m worker --once`` as its environment,
in memory, never through a file. For that, every variable is stored as Vercel
type ``encrypted`` (encrypted at rest, readable through the API by a token with
access to the project), not ``sensitive`` (which nobody can read back, so the
worker could not run). The trade-off: anyone holding ``VERCEL_TOKEN`` can read
them, which a token able to deploy this project could do anyway (it can deploy
code that prints them). Nothing has to be copied by hand.

The pooled Neon host rule (DATABASE_URL_APP): take the owner URL's host and add
``-pooler`` to its first label, the endpoint id:
``ep-cool-dew-123456.us-east-2.aws.neon.tech`` → ``ep-cool-dew-123456-pooler.us-east-2.aws.neon.tech``.
"""

from __future__ import annotations

import argparse
import base64
import importlib.util
import json
import os
import re
import secrets
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
VERCEL_API = "https://api.vercel.com"
CF_API = "https://api.cloudflare.com/client/v4"
VERCEL_CLI = "vercel@65.14.0"  # pinned; at least two weeks old when chosen

PROJECT_NAME = os.environ.get("VERCEL_PROJECT_NAME", "riskpredict-api")
PAGES_PROJECT = os.environ.get("PAGES_PROJECT_NAME", "riskpredict")
APP_ROLE, APP_GROUP = "riskpredict_app", "rp_app"
HEALTH_PATH = "/healthz"
BUNDLE_GUARD_MB = 400  # Vercel's limit is 500 MB uncompressed; stay well under it

# Generated once, when the variable is missing on Vercel, and never overwritten:
# rotating TOKEN_ENCRYPTION_KEYS would make the stored Google refresh tokens unreadable.
# (INTERNAL_TOKEN is gone: there is no worker → API endpoint any more.)
GENERATED = ("JWT_SIGNING_KEY", "TOKEN_ENCRYPTION_KEYS")
# Copied from GitHub secrets / variables when they are set; skipped (left as they are on
# Vercel) when empty, so a later run of Deploy adds them.
OPTIONAL = (
    "GOOGLE_OAUTH_CLIENT_ID", "GOOGLE_OAUTH_CLIENT_SECRET", "GOOGLE_PICKER_API_KEY", "GOOGLE_CLOUD_PROJECT_NUMBER",
    "COMPOSIO_API_KEY", "COMPOSIO_WEBHOOK_SECRET", "COMPOSIO_AUTH_CONFIGS", "COMPOSIO_TRIGGER_SLUGS",
    "GROQ_MAPPING_MODEL", "GROQ_ADVICE_MODEL",
)  # fmt: skip
SECRET_KEYS = {"DATABASE_URL_APP", "GROQ_API_KEY", *GENERATED, "GOOGLE_OAUTH_CLIENT_SECRET", "GOOGLE_PICKER_API_KEY",
               "COMPOSIO_API_KEY", "COMPOSIO_WEBHOOK_SECRET"}  # fmt: skip
# What `python -m worker --once` needs from the Vercel project (nothing else is passed on).
WORKER_KEYS = (
    "DATABASE_URL_APP", "TOKEN_ENCRYPTION_KEYS", "PUBLIC_API_URL", "GOOGLE_OAUTH_CLIENT_ID", "GOOGLE_OAUTH_CLIENT_SECRET",
    "COMPOSIO_API_KEY", "COMPOSIO_AUTH_CONFIGS", "COMPOSIO_TRIGGER_SLUGS", "GROQ_API_KEY", "GROQ_MAPPING_MODEL",
    "GROQ_ADVICE_MODEL",
)  # fmt: skip

# Neon region (from the host) → the closest Vercel function region.
NEON_TO_VERCEL = {
    "us-east-1": "iad1", "us-east-2": "cle1", "us-west-2": "pdx1", "eu-central-1": "fra1", "eu-west-2": "lhr1",
    "eu-west-1": "dub1", "ap-southeast-1": "sin1", "ap-southeast-2": "syd1", "ap-northeast-1": "hnd1",
    "ap-south-1": "bom1", "sa-east-1": "gru1", "ca-central-1": "yul1",
    # Azure
    "eastus2": "iad1", "eastus": "iad1", "westus3": "sfo1", "germanywestcentral": "fra1",
}  # fmt: skip
VERCEL_REGIONS = sorted(set(NEON_TO_VERCEL.values()) | {"cdg1", "arn1", "icn1", "hkg1", "kix1", "cpt1"})
DEFAULT_REGION = "iad1"


class DeployError(Exception):
    """Safe to print: never holds a secret value."""


# --- output helpers ---------------------------------------------------------------------


def mask(value: str) -> str:
    """Tell the runner to redact ``value`` from every later log line, before it is used."""
    if value:
        for line in value.splitlines() or [value]:
            if line.strip():
                print(f"::add-mask::{line}", flush=True)
    return value


def info(msg: str) -> None:
    print(msg, flush=True)


def output(**values: str) -> None:
    """Non-secret values for later steps."""
    path = os.environ.get("GITHUB_OUTPUT")
    for k, v in values.items():
        if path:
            with open(path, "a", encoding="utf-8") as f:
                f.write(f"{k}={v}\n")
        info(f"{k}: {v}")


def env(name: str, required: bool = False) -> str:
    value = os.environ.get(name, "").strip()
    if required and not value:
        raise DeployError(f"{name} is not set (repository secret)")
    return value


# --- pure helpers (unit-tested in tests/test_deploy_script.py) ------------------------------


def neon_host(dsn: str) -> str:
    from psycopg.conninfo import conninfo_to_dict

    host = conninfo_to_dict(dsn).get("host") or ""
    if not host:
        raise DeployError("DATABASE_URL_OWNER has no host")
    return host


def pooled_host(host: str) -> str:
    """Add ``-pooler`` to the endpoint id (the first DNS label) of a Neon host."""
    if not host.endswith(".neon.tech"):
        return host  # not Neon (e.g. a self-hosted Postgres): use the host as it is
    first, _, rest = host.partition(".")
    if first.endswith("-pooler"):
        return host
    return f"{first}-pooler.{rest}"


def direct_host(host: str) -> str:
    first, _, rest = host.partition(".")
    return f"{first.removesuffix('-pooler')}.{rest}" if rest else host


def neon_region(host: str) -> str | None:
    """``ep-x.us-east-2.aws.neon.tech`` and ``ep-x.c-2.us-east-2.aws.neon.tech`` → ``us-east-2``."""
    labels = host.split(".")
    for cloud in ("aws", "azure"):
        if cloud in labels and labels.index(cloud) >= 2:
            return labels[labels.index(cloud) - 1]
    return None


def vercel_region(host: str, override: str = "") -> str:
    if override and override != "auto":
        return override
    return NEON_TO_VERCEL.get(neon_region(host) or "", DEFAULT_REGION)


def app_url(owner_dsn: str, password: str) -> str:
    """DATABASE_URL_APP: the riskpredict_app login on the pooled host, same database and TLS settings."""
    from psycopg.conninfo import conninfo_to_dict

    parts = conninfo_to_dict(owner_dsn)
    host = pooled_host(parts.get("host") or "")
    db = parts.get("dbname") or "neondb"
    port = f":{parts['port']}" if parts.get("port") and parts["port"] != "5432" else ""
    query = {"sslmode": parts.get("sslmode") or "require"}
    if parts.get("channel_binding"):
        query["channel_binding"] = parts["channel_binding"]
    q = urllib.parse.quote
    return f"postgresql://{APP_ROLE}:{q(password, safe='')}@{host}{port}/{q(db, safe='')}?{urllib.parse.urlencode(query)}"


def new_secret(name: str) -> str:
    if name == "JWT_SIGNING_KEY":
        return base64.b64encode(secrets.token_bytes(32)).decode()
    if name == "TOKEN_ENCRYPTION_KEYS":
        return "k1:" + base64.b64encode(secrets.token_bytes(32)).decode()
    raise ValueError(name)


@dataclass
class EnvPlan:
    set: dict[str, str] = field(default_factory=dict)  # key → value to write
    kept: list[str] = field(default_factory=list)  # present on Vercel and left alone


def plan_env(current: dict[str, str], desired: dict[str, str], generated: dict[str, str]) -> EnvPlan:
    """``desired`` always wins when it differs; ``generated`` only fills keys Vercel lacks."""
    plan = EnvPlan()
    for k, v in desired.items():
        if current.get(k) != v:
            plan.set[k] = v
    for k, v in generated.items():
        if k in current:
            plan.kept.append(k)
        else:
            plan.set[k] = v
    return plan


def production_url(project_name: str, aliases: list[str]) -> str:
    """The stable production address among a deployment's aliases: ``<project>.vercel.app``
    when Vercel gave it, else the shortest ``*.vercel.app`` alias (never the per-deployment URL)."""
    hosts = sorted({a.removeprefix("https://").rstrip("/") for a in aliases if a}, key=lambda h: (len(h), h))
    if f"{project_name}.vercel.app" in hosts:
        return f"https://{project_name}.vercel.app"
    vercel = [h for h in hosts if h.endswith(".vercel.app")]
    if not (vercel or hosts):
        raise DeployError("Vercel returned no production alias for the deployment")
    return "https://" + (vercel or hosts)[0]


# --- HTTP -------------------------------------------------------------------------------


class Api:
    def __init__(self, base: str, token: str, name: str, query: dict[str, str] | None = None):
        self.base, self.token, self.name, self.query = base, token, name, query or {}

    def call(self, method: str, path: str, body=None, *, ok404: bool = False, tries: int = 5):
        url = path if path.startswith("http") else self.base + path
        if self.query:
            url += ("&" if "?" in url else "?") + urllib.parse.urlencode(self.query)
        data = json.dumps(body).encode() if body is not None else None
        for attempt in range(tries):
            req = urllib.request.Request(url, data=data, method=method)
            req.add_header("Authorization", f"Bearer {self.token}")
            req.add_header("Accept", "application/json")
            req.add_header("User-Agent", "riskpredict-deploy")
            if data is not None:
                req.add_header("Content-Type", "application/json")
            try:
                with urllib.request.urlopen(req, timeout=60) as resp:
                    raw = resp.read()
                    return resp.status, (json.loads(raw) if raw.strip() else None)
            except urllib.error.HTTPError as exc:
                raw = exc.read()
                if exc.code == 404 and ok404:
                    return 404, None
                if exc.code in (429, 500, 502, 503, 504) and attempt < tries - 1:
                    wait = float(exc.headers.get("Retry-After") or 0) or 2 ** (attempt + 1)
                    time.sleep(min(wait, 60))
                    continue
                raise DeployError(f"{self.name} {method} {self._safe(path)} failed: HTTP {exc.code} {self._message(raw)}") from None
            except (urllib.error.URLError, TimeoutError) as exc:
                if attempt < tries - 1:
                    time.sleep(2 ** (attempt + 1))
                    continue
                raise DeployError(f"{self.name} {method} {self._safe(path)} failed: {type(exc).__name__}") from None
        raise DeployError(f"{self.name} {method} {self._safe(path)} failed")

    @staticmethod
    def _safe(path: str) -> str:
        # paths carry ids and key names only; strip query strings
        return path.split("?", 1)[0]

    @staticmethod
    def _message(raw: bytes) -> str:
        try:
            body = json.loads(raw)
        except ValueError:
            return ""
        if isinstance(body, dict):
            if body.get("errors"):  # Cloudflare
                return "; ".join(f"{e.get('code')}: {str(e.get('message'))[:200]}" for e in body["errors"][:3])
            err = body.get("error")
            if isinstance(err, dict):  # Vercel; its messages name keys and ids, never values
                return f"{err.get('code')}: {str(err.get('message'))[:300]}"
            return str(body.get("message") or "")[:300]
        return ""


def get_text(url: str, timeout: float = 30) -> tuple[int, str]:
    req = urllib.request.Request(url, headers={"User-Agent": "riskpredict-deploy"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, resp.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as exc:
        return exc.code, ""
    except (urllib.error.URLError, TimeoutError, ConnectionError):
        return 0, ""


def wait_healthy(url: str, timeout: float = 4 * 60) -> dict:
    """``/healthz`` answers 200 ``ok`` without credentials (so Deployment Protection is off),
    then ``/v1/health`` for the version. Neither touches the database."""
    start = time.monotonic()
    last = 0
    while time.monotonic() - start < timeout:
        code, body = get_text(url + HEALTH_PATH)
        last = code
        if code == 200:
            try:
                if json.loads(body).get("status") == "ok":
                    code2, body2 = get_text(url + "/v1/health")
                    return json.loads(body2) if code2 == 200 else {}
            except ValueError:
                pass
        if code in (401, 403):
            raise DeployError(f"{url}{HEALTH_PATH} answered {code}: Vercel Deployment Protection is still on; "
                              "the API and the webhooks must be public")  # fmt: skip
        time.sleep(10)
    raise DeployError(f"{url}{HEALTH_PATH} did not answer ok within {timeout / 60:.0f} minutes (last HTTP {last})")


# --- Vercel -------------------------------------------------------------------------------


def vercel() -> Api:
    """The Vercel API, scoped to VERCEL_TEAM_ID or else the token's default team (Hobby
    accounts are a personal team, e.g. "<name>'s projects"), so every call and the CLI
    work in one place."""
    api = Api(VERCEL_API, env("VERCEL_TOKEN", required=True), "Vercel")
    team = env("VERCEL_TEAM_ID")
    if not team:
        _, me = api.call("GET", "/v2/user")
        team = str(((me or {}).get("user") or {}).get("defaultTeamId") or "")
    if team:
        api.query = {"teamId": team}
    return api


def find_project(api: Api) -> dict | None:
    status, proj = api.call("GET", f"/v9/projects/{PROJECT_NAME}", ok404=True)
    return None if status == 404 else proj


PROJECT_SETTINGS = {
    "framework": "fastapi",
    # Public: the dashboard, Google Drive and Composio call this API without Vercel credentials.
    "ssoProtection": None,
    "passwordProtection": None,
    "buildCommand": None,
    "installCommand": None,
    "outputDirectory": None,
    "rootDirectory": None,
}


def ensure_project(api: Api) -> tuple[dict, bool]:
    proj = find_project(api)
    created = proj is None
    if created:
        _, proj = api.call("POST", "/v11/projects", {"name": PROJECT_NAME, "framework": "fastapi"})
        info(f"created Vercel project {PROJECT_NAME} ({proj['id']})")
    else:
        info(f"found Vercel project {PROJECT_NAME} ({proj['id']})")
    # Every run: framework preset, no build overrides, and no Deployment Protection.
    _, proj = api.call("PATCH", f"/v9/projects/{proj['id']}", PROJECT_SETTINGS)
    if proj.get("ssoProtection") or proj.get("passwordProtection"):
        raise DeployError("Vercel kept Deployment Protection on; turn it off under Project → Settings → "
                          "Deployment Protection (the API must be public)")  # fmt: skip
    return proj, created


def _env_rows(payload) -> list[dict]:
    if isinstance(payload, list):
        return payload
    return list((payload or {}).get("envs") or [])


def env_vars(api: Api, project_id: str, keys: tuple[str, ...] | set[str] | None = None) -> dict[str, dict]:
    """Production variables by key: {"id", "value", "type"}. Values of SECRET_KEYS (and of
    every key when ``keys`` is given) are masked before anything else happens."""
    _, payload = api.call("GET", f"/v10/projects/{project_id}/env?decrypt=true")
    out: dict[str, dict] = {}
    for row in _env_rows(payload):
        key = row.get("key")
        if not key or "production" not in (row.get("target") or []) or (keys is not None and key not in keys):
            continue
        value = row.get("value") or ""
        if row.get("type") in ("encrypted", "secret") and not row.get("decrypted", True):
            # the list did not decrypt it: read this one variable
            _, one = api.call("GET", f"/v1/projects/{project_id}/env/{row['id']}")
            value = (one or {}).get("value") or ""
        if key in SECRET_KEYS or keys is not None:
            mask(value)
        out[key] = {"id": row.get("id"), "value": value, "type": row.get("type")}
    return out


def put_env(api: Api, project_id: str, key: str, value: str) -> None:
    """Create or update one production variable, type ``encrypted`` (readable back by the worker job)."""
    api.call("POST", f"/v10/projects/{project_id}/env?upsert=true",
             {"key": key, "value": value, "type": "encrypted", "target": ["production"]})  # fmt: skip


def deploy_bundle(api: Api, proj: dict, region: str, *, check_size: bool) -> dict:
    """Build the clean bundle, check it, deploy it to production with the pinned CLI, wait until READY."""
    bundle = _bundle_module()
    with tempfile.TemporaryDirectory(prefix="riskpredict-vercel-") as tmp:
        out = bundle.build(Path(tmp) / "bundle", region)
        info(f"bundle: {len(bundle.file_list(out))} files, region {region}")
        size = {}
        if check_size:
            try:
                size = bundle.check(out, BUNDLE_GUARD_MB)
            except SystemExit as exc:  # the size guard or the ASGI smoke test
                raise DeployError(str(exc)) from None
            info(f"bundle size: {size['total_mb']} MB of Vercel's {size['limit_mb']} MB (largest: {', '.join(size['largest'])})")
            shutil.rmtree(out / ".check", ignore_errors=True)
        url = run_cli(out, proj)
    dep = wait_ready(api, url)
    dep["bundle_mb"] = size.get("total_mb", "")
    return dep


def _bundle_module():
    spec = importlib.util.spec_from_file_location("vercel_bundle", ROOT / "infra/vercel/bundle.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def run_cli(directory: Path, proj: dict) -> str:
    """``vercel deploy --prod`` from ``directory``; returns the deployment URL it prints."""
    npx = shutil.which("npx")
    if not npx:
        raise DeployError("npx is not available (actions/setup-node runs before this step)")
    child = {k: v for k, v in os.environ.items() if k not in SECRET_KEYS and not k.startswith(("DATABASE_URL", "GOOGLE_", "COMPOSIO_"))}
    child.update({"VERCEL_ORG_ID": proj.get("accountId") or "", "VERCEL_PROJECT_ID": proj["id"],
                  "VERCEL_TELEMETRY_DISABLED": "1", "CI": "1"})  # fmt: skip
    cmd = [npx, "--yes", VERCEL_CLI, "deploy", "--prod", "--yes", "--token", env("VERCEL_TOKEN", required=True)]
    proc = subprocess.run(cmd, cwd=directory, env=child, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                          timeout=20 * 60)  # fmt: skip
    for line in proc.stderr.splitlines()[-40:]:  # the CLI's progress and build log; no values in it
        info(f"  vercel: {line[:300]}")
    urls = re.findall(r"https://[a-z0-9.-]+\.vercel\.app", proc.stdout)
    if proc.returncode != 0 or not urls:
        raise DeployError(f"vercel deploy failed (exit {proc.returncode}); see the build log above")
    return urls[-1]


def wait_ready(api: Api, url: str, timeout: float = 15 * 60) -> dict:
    host = url.removeprefix("https://")
    start, last = time.monotonic(), ""
    while time.monotonic() - start < timeout:
        _, dep = api.call("GET", f"/v13/deployments/{host}")
        state = (dep or {}).get("readyState") or (dep or {}).get("status") or ""
        if state != last:
            info(f"deployment {dep.get('id')}: {state}")
            last = state
        if state == "READY" and (dep.get("aliasAssigned") or dep.get("alias")):
            return dep
        if state in ("ERROR", "CANCELED"):
            raise DeployError(f"deployment {dep.get('id')} ended as {state}; open it in the Vercel dashboard")
        time.sleep(5)
    raise DeployError(f"deployment {host} was not READY after {timeout / 60:.0f} minutes")


def deployment_url(proj: dict, dep: dict) -> str:
    aliases = list(dep.get("alias") or [])
    aliases += list(((proj.get("targets") or {}).get("production") or {}).get("alias") or [])
    return production_url(PROJECT_NAME, aliases)


# --- the app login role ---------------------------------------------------------------------


def _roles_module():
    spec = importlib.util.spec_from_file_location("create_login_roles", ROOT / "infra/scripts/create_login_roles.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def app_role_exists() -> bool:
    import psycopg

    roles = _roles_module()
    with psycopg.connect(env("DATABASE_URL_OWNER", required=True), autocommit=True, connect_timeout=20,
                         application_name="riskpredict-deploy") as conn:  # fmt: skip
        return roles.role_exists(conn, APP_ROLE)


def run_roles_script(*extra: str, password: str | None = None) -> None:
    cmd = [sys.executable, str(ROOT / "infra/scripts/create_login_roles.py"), "--dsn-env", "DATABASE_URL_OWNER",
           "--role", f"{APP_ROLE}:{APP_GROUP}", *extra]  # fmt: skip
    if password is not None:
        cmd.append("--password-stdin")
    proc = subprocess.run(cmd, input=(password + "\n") if password is not None else None, text=True)
    if proc.returncode != 0:
        raise DeployError(f"create_login_roles.py {' '.join(extra) or '(create)'} failed (exit {proc.returncode})")


# --- commands -----------------------------------------------------------------------------


def cmd_vercel_up(args) -> None:
    owner_dsn = env("DATABASE_URL_OWNER", required=True)
    groq = env("GROQ_API_KEY", required=True)
    region = vercel_region(neon_host(owner_dsn), args.region)
    api = vercel()
    proj, created = ensure_project(api)
    pid = proj["id"]
    current = {k: v["value"] for k, v in env_vars(api, pid).items()}

    # 1. The riskpredict_app login and DATABASE_URL_APP (kept only on Vercel). A login left
    #    by an earlier platform has a password nobody knows: no Vercel value → rotate.
    exists = app_role_exists()
    new_password = None
    if args.rotate_app_password or not exists or "DATABASE_URL_APP" not in current:
        reason = ("rotate requested" if args.rotate_app_password else
                  "role missing" if not exists else "Vercel has no DATABASE_URL_APP")  # fmt: skip
        new_password = mask(secrets.token_urlsafe(32))
        info(f"{APP_ROLE}: new password ({reason})")
        run_roles_script(*(["--rotate"] if exists else []), password=new_password)
    else:
        info(f"{APP_ROLE}: exists; keeping its password and the Vercel value")
        run_roles_script("--check")

    # 2. Environment of the project (production).
    desired = {
        "GROQ_API_KEY": groq,
        "WEB_ORIGIN": args.web_origin.rstrip("/"),
        # Vercel's edge sets x-real-ip itself; a client cannot choose it.
        "CLIENT_IP_HEADER": "x-real-ip",
    }
    for k in OPTIONAL:
        if env(k):
            desired[k] = env(k)
    if new_password:
        desired["DATABASE_URL_APP"] = mask(app_url(owner_dsn, new_password))
    if current.get("PUBLIC_API_URL"):
        desired["PUBLIC_API_URL"] = current["PUBLIC_API_URL"]
    generated = {k: mask(new_secret(k)) for k in GENERATED if k not in current}
    plan = plan_env(current, desired, generated)
    for k, v in plan.set.items():
        try:
            put_env(api, pid, k, v)
        except DeployError:
            if k == "DATABASE_URL_APP":
                raise DeployError("the role's password changed but Vercel kept the old DATABASE_URL_APP; "
                                  "run Deploy again with rotate_app_password=true") from None  # fmt: skip
            raise
    info("env set: " + (", ".join(sorted(plan.set)) or "none changed"))
    if plan.kept:
        info("env kept (generated once, never overwritten): " + ", ".join(sorted(plan.kept)))

    # 3. Deploy this commit's bundle; then make PUBLIC_API_URL the production address.
    dep = deploy_bundle(api, proj, region, check_size=True)
    _, proj = api.call("GET", f"/v9/projects/{pid}")
    url = deployment_url(proj, dep)
    redeployed = False
    if current.get("PUBLIC_API_URL") != url:
        put_env(api, pid, "PUBLIC_API_URL", url)
        info(f"PUBLIC_API_URL set to {url}; deploying again so the API sees it")
        dep = {**deploy_bundle(api, proj, region, check_size=False), "bundle_mb": dep.get("bundle_mb", "")}
        redeployed = True
    health = wait_healthy(url)
    output(project_id=pid, api_url=url, vercel_region=region, created=str(created).lower(),
           api_version=str(health.get("version", "unknown")), deployment_id=str(dep.get("id", "")),
           bundle_mb=str(dep.get("bundle_mb", "")), redeployed=str(redeployed).lower())  # fmt: skip


def cmd_vercel_origin(args) -> None:
    api = vercel()
    proj = find_project(api)
    if proj is None:
        raise DeployError(f"Vercel project {PROJECT_NAME} not found")
    origin = args.web_origin.rstrip("/")
    current = env_vars(api, proj["id"], {"WEB_ORIGIN", "PUBLIC_API_URL"})
    if current.get("WEB_ORIGIN", {}).get("value") == origin:
        output(origin_changed="false")
        return
    put_env(api, proj["id"], "WEB_ORIGIN", origin)
    info(f"WEB_ORIGIN changed to {origin}; deploying again")
    region = args.region or proj.get("serverlessFunctionRegion") or DEFAULT_REGION
    deploy_bundle(api, proj, region if region in VERCEL_REGIONS else DEFAULT_REGION, check_size=False)
    wait_healthy(current.get("PUBLIC_API_URL", {}).get("value") or f"https://{PROJECT_NAME}.vercel.app")
    output(origin_changed="true")


def cmd_vercel_destroy(args) -> None:
    api = vercel()
    proj = find_project(api)
    if proj is None:
        info(f"Vercel project {PROJECT_NAME}: already gone")
        output(vercel_removed="already gone")
        return
    api.call("DELETE", f"/v9/projects/{proj['id']}", ok404=True)
    info(f"Vercel project {PROJECT_NAME} ({proj['id']}): deleted, with its deployments and environment")
    output(vercel_removed=f"deleted ({proj['id']})")


def cmd_worker_run(args) -> None:
    """Read the worker's settings from the Vercel project, mask them, run ``python -m worker --once``
    with them as its environment (in memory: nothing is written to a file)."""
    if not env("VERCEL_TOKEN"):
        info("::notice title=Worker skipped::The VERCEL_TOKEN secret is not set, so the worker did not run.")
        return
    api = vercel()
    proj = find_project(api)
    if proj is None:
        info(f"::notice title=Worker skipped::The Vercel project {PROJECT_NAME} does not exist yet; run Deploy first.")
        return
    values = {k: v["value"] for k, v in env_vars(api, proj["id"], set(WORKER_KEYS)).items() if v["value"]}
    if "DATABASE_URL_APP" not in values:
        raise DeployError("the Vercel project has no DATABASE_URL_APP; run Deploy first")
    info("worker settings from Vercel: " + ", ".join(sorted(values)))
    child = {k: v for k, v in os.environ.items() if k not in WORKER_KEYS and k not in SECRET_KEYS and k != "VERCEL_TOKEN"}
    child.update(values)
    child["WORKER_ONCE_BUDGET_SECONDS"] = env("WORKER_ONCE_BUDGET_SECONDS") or "480"
    proc = subprocess.run([sys.executable, "-m", "worker", "--once"], cwd=ROOT, env=child)
    if proc.returncode != 0:
        raise DeployError(f"python -m worker --once exited with {proc.returncode}")


# --- Cloudflare Pages -----------------------------------------------------------------------


def cloudflare() -> tuple[Api, str]:
    acct = env("CLOUDFLARE_ACCOUNT_ID", required=True)
    return Api(f"{CF_API}/accounts/{acct}", env("CLOUDFLARE_API_TOKEN", required=True), "Cloudflare"), acct


def pages_url(project: dict) -> str:
    sub = project.get("subdomain") or ""
    if not sub:
        raise DeployError("Cloudflare did not return the Pages subdomain")
    return "https://" + sub.rstrip("/")


def cmd_pages_ensure(args) -> None:
    cf, _ = cloudflare()
    status, res = cf.call("GET", f"/pages/projects/{PAGES_PROJECT}", ok404=True)
    if status == 404:
        _, res = cf.call("POST", "/pages/projects", {"name": PAGES_PROJECT, "production_branch": "main"})
        info(f"created Cloudflare Pages project {PAGES_PROJECT}")
        created = "true"
    else:
        created = "false"
    project = res["result"]
    output(pages_project=project.get("name") or PAGES_PROJECT, pages_url=pages_url(project), pages_created=created)


def cmd_pages_destroy(args) -> None:
    cf, _ = cloudflare()
    path = f"/pages/projects/{PAGES_PROJECT}"
    status, _ = cf.call("GET", path, ok404=True)
    if status == 404:
        info(f"Cloudflare Pages project {PAGES_PROJECT}: already gone")
        output(pages_removed="already gone")
        return
    try:
        cf.call("DELETE", path, ok404=True)
    except DeployError:
        # A project with many deployments must be emptied first.
        info("deleting the project's deployments first")
        while True:
            _, res = cf.call("GET", f"{path}/deployments?per_page=25")
            ids = [d["id"] for d in (res or {}).get("result") or []]
            if not ids:
                break
            for i in ids:
                cf.call("DELETE", f"{path}/deployments/{i}?force=true", ok404=True)
        cf.call("DELETE", path, ok404=True)
    info(f"Cloudflare Pages project {PAGES_PROJECT}: deleted")
    output(pages_removed="deleted")


def cmd_drop_app_role(args) -> None:
    import psycopg
    from psycopg import sql

    roles = _roles_module()
    with psycopg.connect(env("DATABASE_URL_OWNER", required=True), autocommit=True, connect_timeout=20,
                         application_name="riskpredict-deploy") as conn:  # fmt: skip
        if not roles.role_exists(conn, APP_ROLE):
            info(f"{APP_ROLE}: already gone")
            output(role_removed="already gone")
            return
        problems = [p for p in roles.check_login_role(conn, APP_ROLE, APP_GROUP) if "owns" in p]
        if problems:
            raise DeployError(f"{APP_ROLE} owns objects; not dropping it (nothing in the data was changed)")
        try:
            conn.execute(sql.SQL("DROP ROLE {}").format(sql.Identifier(APP_ROLE)))
        except psycopg.Error as exc:
            raise DeployError(f"DROP ROLE {APP_ROLE} failed ({type(exc).__name__})") from None
    info(f"{APP_ROLE}: dropped (login only; tables and rows untouched)")
    output(role_removed="dropped")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    up = sub.add_parser("vercel-up")
    up.add_argument("--web-origin", required=True)
    up.add_argument("--rotate-app-password", action="store_true")
    up.add_argument("--region", default="auto", choices=["auto", *VERCEL_REGIONS])
    origin = sub.add_parser("vercel-origin")
    origin.add_argument("--web-origin", required=True)
    origin.add_argument("--region", default="")
    for name in ("vercel-destroy", "pages-ensure", "pages-destroy", "drop-app-role", "worker-run"):
        sub.add_parser(name)
    args = ap.parse_args(argv)
    fn = {"vercel-up": cmd_vercel_up, "vercel-origin": cmd_vercel_origin, "vercel-destroy": cmd_vercel_destroy,
          "pages-ensure": cmd_pages_ensure, "pages-destroy": cmd_pages_destroy, "drop-app-role": cmd_drop_app_role,
          "worker-run": cmd_worker_run}  # fmt: skip
    try:
        fn[args.cmd](args)
    except DeployError as exc:
        print(f"::error::{exc}", flush=True)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
