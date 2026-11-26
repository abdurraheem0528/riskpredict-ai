"""Deploy / destroy helpers for .github/workflows/deploy.yml and destroy.yml.

Runs inside GitHub Actions only (never on the owner's computer). Standard library
only, apart from psycopg for the role checks. Every secret this script creates is
announced to the runner with ``::add-mask::`` before it is used anywhere, and no
secret value is ever printed, written to $GITHUB_OUTPUT or to a file.

    python infra/scripts/deploy.py pages-ensure                 # Cloudflare Pages project, prints its URL
    python infra/scripts/deploy.py render-up --web-origin URL   # role + Render service + env + deploy
    python infra/scripts/deploy.py render-origin --web-origin URL
    python infra/scripts/deploy.py render-destroy
    python infra/scripts/deploy.py pages-destroy
    python infra/scripts/deploy.py drop-app-role

Environment (only what each command needs):
    RENDER_API_KEY, CLOUDFLARE_API_TOKEN, CLOUDFLARE_ACCOUNT_ID, DATABASE_URL_OWNER,
    GROQ_API_KEY, the optional Google / Composio secrets, GITHUB_REPOSITORY, GITHUB_SHA.

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
import secrets
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
RENDER_API = "https://api.render.com/v1"
CF_API = "https://api.cloudflare.com/client/v4"

SERVICE_NAME = os.environ.get("RENDER_SERVICE_NAME", "riskpredict-api")
PAGES_PROJECT = os.environ.get("PAGES_PROJECT_NAME", "riskpredict")
APP_ROLE, APP_GROUP = "riskpredict_app", "rp_app"
DOCKERFILE = "./infra/render/Dockerfile"
PORT = "10000"
HEALTH_PATH = "/healthz"

# Generated once, when the variable is missing on Render, and never overwritten:
# rotating TOKEN_ENCRYPTION_KEYS would make the stored Google refresh tokens unreadable.
GENERATED = ("JWT_SIGNING_KEY", "TOKEN_ENCRYPTION_KEYS", "INTERNAL_TOKEN")
# Copied from GitHub secrets / variables when they are set; skipped (left as they are on
# Render) when empty, so a later run of Deploy adds them.
OPTIONAL = (
    "GOOGLE_OAUTH_CLIENT_ID", "GOOGLE_OAUTH_CLIENT_SECRET", "GOOGLE_PICKER_API_KEY", "GOOGLE_CLOUD_PROJECT_NUMBER",
    "COMPOSIO_API_KEY", "COMPOSIO_WEBHOOK_SECRET", "COMPOSIO_AUTH_CONFIGS", "COMPOSIO_TRIGGER_SLUGS",
    "GROQ_MAPPING_MODEL", "GROQ_ADVICE_MODEL", "WORKER_DB_INTERVAL_SECONDS",
)  # fmt: skip
SECRET_KEYS = {"DATABASE_URL_APP", "GROQ_API_KEY", *GENERATED, "GOOGLE_OAUTH_CLIENT_SECRET", "GOOGLE_PICKER_API_KEY",
               "COMPOSIO_API_KEY", "COMPOSIO_WEBHOOK_SECRET"}  # fmt: skip

# Render serves web services through Cloudflare and its own load balancer, and keeps
# any X-Forwarded-For the client sent, appending to it. Uvicorn walks the header from
# the right, skipping trusted proxies, so the first untrusted entry is the client.
# ("*" would make uvicorn take the leftmost entry, which the client chooses.)
PRIVATE_RANGES = ["10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16", "100.64.0.0/10", "127.0.0.1", "::1", "fc00::/7"]
CLOUDFLARE_FALLBACK = [
    "173.245.48.0/20", "103.21.244.0/22", "103.22.200.0/22", "103.31.4.0/22", "141.101.64.0/18", "108.162.192.0/18",
    "190.93.240.0/20", "188.114.96.0/20", "197.234.240.0/22", "198.41.128.0/17", "162.158.0.0/15", "104.16.0.0/13",
    "104.24.0.0/14", "172.64.0.0/13", "131.0.72.0/22", "2400:cb00::/32", "2606:4700::/32", "2803:f800::/32",
    "2405:b500::/32", "2405:8100::/32", "2a06:98c0::/29", "2c0f:f248::/32",
]  # fmt: skip

# Neon region (from the host) → closest Render region (oregon, ohio, virginia, frankfurt, singapore).
NEON_TO_RENDER = {
    "us-east-1": "virginia", "us-east-2": "ohio", "us-west-2": "oregon", "eu-central-1": "frankfurt",
    "eu-west-2": "frankfurt", "eu-west-1": "frankfurt", "ap-southeast-1": "singapore",
    "ap-southeast-2": "singapore", "ap-northeast-1": "singapore", "ap-south-1": "singapore",
    "sa-east-1": "virginia", "ca-central-1": "virginia",
    # Azure
    "eastus2": "virginia", "eastus": "virginia", "westus3": "oregon", "germanywestcentral": "frankfurt",
}  # fmt: skip
DEFAULT_REGION = "ohio"


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


def render_region(host: str, override: str = "") -> str:
    if override and override != "auto":
        return override
    return NEON_TO_RENDER.get(neon_region(host) or "", DEFAULT_REGION)


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
    if name == "INTERNAL_TOKEN":
        return secrets.token_urlsafe(32)
    raise ValueError(name)


def forwarded_allow_ips(cloudflare: list[str]) -> str:
    return ",".join(PRIVATE_RANGES + (cloudflare or CLOUDFLARE_FALLBACK))


@dataclass
class EnvPlan:
    set: dict[str, str] = field(default_factory=dict)  # key → value to PUT
    kept: list[str] = field(default_factory=list)  # present on Render and left alone


def plan_env(current: dict[str, str], desired: dict[str, str], generated: dict[str, str]) -> EnvPlan:
    """``desired`` always wins when it differs; ``generated`` only fills keys Render lacks."""
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


# --- HTTP -------------------------------------------------------------------------------


class Api:
    def __init__(self, base: str, token: str, name: str):
        self.base, self.token, self.name = base, token, name

    def call(self, method: str, path: str, body: dict | None = None, *, ok404: bool = False, tries: int = 5):
        url = path if path.startswith("http") else self.base + path
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
        # env-var paths carry a key name only; strip query strings
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
            return str(body.get("message") or "")[:300]
        return ""


def get_text(url: str, timeout: float = 20) -> tuple[int, str]:
    req = urllib.request.Request(url, headers={"User-Agent": "riskpredict-deploy"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, resp.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as exc:
        return exc.code, ""
    except (urllib.error.URLError, TimeoutError, ConnectionError):
        return 0, ""


# --- Render -------------------------------------------------------------------------------


def render() -> Api:
    return Api(RENDER_API, env("RENDER_API_KEY", required=True), "Render")


def render_owner(api: Api) -> str:
    wanted = env("RENDER_OWNER_ID")
    _, rows = api.call("GET", "/owners?limit=100")
    owners = [r["owner"] for r in rows or []]
    if wanted:
        if not any(o["id"] == wanted for o in owners):
            raise DeployError("RENDER_OWNER_ID is not a workspace this API key can use")
        return wanted
    if len(owners) == 1:
        return owners[0]["id"]
    names = ", ".join(f"{o.get('name')} ({o['id']})" for o in owners)
    raise DeployError(f"the Render API key sees {len(owners)} workspaces; set the repository variable "
                      f"RENDER_OWNER_ID to one of: {names}")  # fmt: skip


def find_service(api: Api, owner_id: str | None = None) -> dict | None:
    q = {"name": SERVICE_NAME, "type": "web_service", "limit": "100"}
    if owner_id:
        q["ownerId"] = owner_id
    _, rows = api.call("GET", "/services?" + urllib.parse.urlencode(q))
    found = [r["service"] for r in rows or [] if r.get("service", {}).get("name") == SERVICE_NAME]
    if len(found) > 1:
        raise DeployError(f"{len(found)} Render services are named {SERVICE_NAME}; delete the extra ones")
    return found[0] if found else None


def service_url(svc: dict) -> str:
    url = (svc.get("serviceDetails") or {}).get("url") or ""
    if not url:
        raise DeployError("Render did not return the service URL")
    return url.rstrip("/")


def env_vars(api: Api, sid: str) -> dict[str, str]:
    out: dict[str, str] = {}
    cursor = None
    while True:
        q = {"limit": "100"} | ({"cursor": cursor} if cursor else {})
        _, rows = api.call("GET", f"/services/{sid}/env-vars?" + urllib.parse.urlencode(q))
        rows = rows or []
        for r in rows:
            ev = r.get("envVar") or {}
            key, value = ev.get("key"), ev.get("value") or ""
            if key:
                if key in SECRET_KEYS:
                    mask(value)
                out[key] = value
        if len(rows) < 100:
            return out
        cursor = rows[-1].get("cursor")


def put_env(api: Api, sid: str, key: str, value: str) -> None:
    api.call("PUT", f"/services/{sid}/env-vars/{urllib.parse.quote(key)}", {"value": value})


def trigger_deploy(api: Api, sid: str, commit: str) -> str:
    body: dict = {"clearCache": "do_not_clear"}
    if commit:
        body["commitId"] = commit
    status, dep = api.call("POST", f"/services/{sid}/deploys", body)
    if dep and dep.get("id"):
        return dep["id"]
    # 202 (queued behind a running deploy) has no body: take the newest deploy.
    time.sleep(5)
    _, rows = api.call("GET", f"/services/{sid}/deploys?limit=5")
    for r in rows or []:
        d = r.get("deploy") or {}
        if not commit or (d.get("commit") or {}).get("id") == commit:
            return d["id"]
    raise DeployError(f"Render accepted the deploy (HTTP {status}) but it could not be found")


def wait_deploy(api: Api, sid: str, deploy_id: str, timeout: float = 30 * 60) -> None:
    start = time.monotonic()
    last = ""
    while time.monotonic() - start < timeout:
        _, d = api.call("GET", f"/services/{sid}/deploys/{deploy_id}")
        status = (d or {}).get("status", "")
        if status != last:
            info(f"deploy {deploy_id}: {status} ({(time.monotonic() - start) / 60:.0f} min)")
            last = status
        if status == "live":
            return
        if status in ("build_failed", "update_failed", "pre_deploy_failed", "canceled", "deactivated"):
            raise DeployError(f"deploy {deploy_id} ended as {status}; open the service's Events/Logs in the Render dashboard")
        time.sleep(15)
    raise DeployError(f"deploy {deploy_id} was not live after {timeout / 60:.0f} minutes")


def wait_healthy(url: str, timeout: float = 6 * 60) -> dict:
    start = time.monotonic()
    while time.monotonic() - start < timeout:
        code, body = get_text(url + HEALTH_PATH)
        if code == 200:
            try:
                if json.loads(body).get("status") == "ok":
                    code2, body2 = get_text(url + "/v1/health")
                    return json.loads(body2) if code2 == 200 else {}
            except ValueError:
                pass
        time.sleep(10)
    raise DeployError(f"{url}{HEALTH_PATH} did not answer ok within {timeout / 60:.0f} minutes")


def cloudflare_ranges() -> list[str]:
    code, body = get_text(CF_API + "/ips")
    try:
        result = json.loads(body)["result"] if code == 200 else {}
        ranges = list(result.get("ipv4_cidrs") or []) + list(result.get("ipv6_cidrs") or [])
        return ranges or CLOUDFLARE_FALLBACK
    except (ValueError, KeyError, TypeError, AttributeError):
        return CLOUDFLARE_FALLBACK


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


def cmd_render_up(args) -> None:
    owner_dsn = env("DATABASE_URL_OWNER", required=True)
    groq = env("GROQ_API_KEY", required=True)
    host = neon_host(owner_dsn)
    region = render_region(host, args.region)
    api = render()
    owner_id = render_owner(api)
    svc = find_service(api, owner_id)
    current = env_vars(api, svc["id"]) if svc else {}
    if svc:
        plan = (svc.get("serviceDetails") or {}).get("plan")
        if plan != "free":
            raise DeployError(f"the Render service {SERVICE_NAME} is on plan {plan!r}, not free; refusing to touch it")

    # 1. The riskpredict_app login and DATABASE_URL_APP (kept only on Render).
    exists = app_role_exists()
    new_password = None
    if args.rotate_app_password or not exists or "DATABASE_URL_APP" not in current:
        reason = ("rotate requested" if args.rotate_app_password else
                  "role missing" if not exists else "Render has no DATABASE_URL_APP")  # fmt: skip
        new_password = mask(secrets.token_urlsafe(32))
        info(f"{APP_ROLE}: new password ({reason})")
        run_roles_script(*(["--rotate"] if exists else []), password=new_password)
    else:
        info(f"{APP_ROLE}: exists; keeping its password and the Render value")
        run_roles_script("--check")
    database_url_app = mask(app_url(owner_dsn, new_password)) if new_password else None

    # 2. Environment for the service.
    desired = {
        "GROQ_API_KEY": groq,
        "WEB_ORIGIN": args.web_origin.rstrip("/"),
        "API_INTERNAL_URL": f"http://127.0.0.1:{PORT}",
        "PORT": PORT,
        "INTERNAL_LOOPBACK_ONLY": "true",
        "FORWARDED_ALLOW_IPS": forwarded_allow_ips(cloudflare_ranges()),
    }
    for k in OPTIONAL:
        if env(k):
            desired[k] = env(k)
    if database_url_app:
        desired["DATABASE_URL_APP"] = database_url_app
    generated = {k: mask(new_secret(k)) for k in GENERATED if k not in current}

    created = False
    first_deploy = None
    if svc is None:
        body = {
            "type": "web_service",
            "name": SERVICE_NAME,
            "ownerId": owner_id,
            "repo": f"https://github.com/{env('GITHUB_REPOSITORY', required=True)}",
            "branch": "main",
            "autoDeployTrigger": "off",
            "envVars": [{"key": k, "value": v} for k, v in {**desired, **generated}.items()],
            "serviceDetails": {
                "runtime": "docker",
                "plan": "free",
                "region": region,
                "healthCheckPath": HEALTH_PATH,
                "numInstances": 1,
                "envSpecificDetails": {"dockerfilePath": DOCKERFILE, "dockerContext": "."},
            },
        }
        _, res = api.call("POST", "/services", body)
        svc, first_deploy = res["service"], res.get("deployId")
        created = True
        info(f"created Render service {svc['id']} ({region}, free)")
        if first_deploy:
            # PUBLIC_API_URL is only known now; cancel the automatic first build and
            # deploy once with every variable in place.
            try:
                api.call("POST", f"/services/{svc['id']}/deploys/{first_deploy}/cancel", {})
            except DeployError as exc:
                info(f"could not cancel the first automatic deploy ({exc}); waiting for it instead")
                try:
                    wait_deploy(api, svc["id"], first_deploy)
                except DeployError:
                    pass
    else:
        info(f"found Render service {svc['id']} ({(svc.get('serviceDetails') or {}).get('region')})")
        if (svc.get("serviceDetails") or {}).get("region") != region:
            info(f"note: the service's region cannot change; the Neon host suggests {region}")
        api.call("PATCH", f"/services/{svc['id']}", {
            "autoDeployTrigger": "off",
            "branch": "main",
            "serviceDetails": {"healthCheckPath": HEALTH_PATH,
                               "envSpecificDetails": {"dockerfilePath": DOCKERFILE, "dockerContext": "."}},
        })  # fmt: skip

    sid, url = svc["id"], service_url(svc)
    desired["PUBLIC_API_URL"] = url
    # A new service got every other variable in its create request.
    plan = EnvPlan(set={"PUBLIC_API_URL": url}) if created else plan_env(current, desired, generated)
    for k, v in plan.set.items():
        try:
            put_env(api, sid, k, v)
        except DeployError:
            if k == "DATABASE_URL_APP":
                raise DeployError("the role's password changed but Render kept the old DATABASE_URL_APP; "
                                  "run Deploy again with rotate_app_password=true") from None  # fmt: skip
            raise
    info("env set: " + (", ".join(sorted(plan.set)) or "none changed"))
    if plan.kept:
        info("env kept (generated once, never overwritten): " + ", ".join(sorted(plan.kept)))

    # 3. Deploy this commit and wait until it serves /healthz.
    deploy_id = trigger_deploy(api, sid, env("GITHUB_SHA"))
    wait_deploy(api, sid, deploy_id)
    health = wait_healthy(url)
    output(service_id=sid, api_url=url, render_region=(svc.get("serviceDetails") or {}).get("region") or region,
           created=str(created).lower(), schema_version=str(health.get("schema_version", "unknown")),
           api_version=str(health.get("version", "unknown")), deploy_id=deploy_id)  # fmt: skip


def cmd_render_origin(args) -> None:
    api = render()
    svc = find_service(api, render_owner(api))
    if svc is None:
        raise DeployError(f"Render service {SERVICE_NAME} not found")
    origin = args.web_origin.rstrip("/")
    current = env_vars(api, svc["id"])
    if current.get("WEB_ORIGIN") == origin:
        output(origin_changed="false")
        return
    put_env(api, svc["id"], "WEB_ORIGIN", origin)
    info(f"WEB_ORIGIN changed to {origin}; redeploying")
    deploy_id = trigger_deploy(api, svc["id"], env("GITHUB_SHA"))
    wait_deploy(api, svc["id"], deploy_id)
    wait_healthy(service_url(svc))
    output(origin_changed="true")


def cmd_render_destroy(args) -> None:
    api = render()
    svc = find_service(api)
    if svc is None:
        info(f"Render service {SERVICE_NAME}: already gone")
        output(render_removed="already gone")
        return
    api.call("DELETE", f"/services/{svc['id']}", ok404=True)
    info(f"Render service {SERVICE_NAME} ({svc['id']}): deleted")
    output(render_removed=f"deleted ({svc['id']})")


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
    up = sub.add_parser("render-up")
    up.add_argument("--web-origin", required=True)
    up.add_argument("--rotate-app-password", action="store_true")
    up.add_argument("--region", default="auto", choices=["auto", "oregon", "ohio", "virginia", "frankfurt", "singapore"])
    sub.add_parser("render-origin").add_argument("--web-origin", required=True)
    for name in ("render-destroy", "pages-ensure", "pages-destroy", "drop-app-role"):
        sub.add_parser(name)
    args = ap.parse_args(argv)
    fn = {"render-up": cmd_render_up, "render-origin": cmd_render_origin, "render-destroy": cmd_render_destroy,
          "pages-ensure": cmd_pages_ensure, "pages-destroy": cmd_pages_destroy, "drop-app-role": cmd_drop_app_role}  # fmt: skip
    try:
        fn[args.cmd](args)
    except DeployError as exc:
        print(f"::error::{exc}", flush=True)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
