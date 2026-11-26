"""Smoke test for the combined Render image (infra/render/Dockerfile), run by CI.

    python infra/render/smoke.py --image riskpredict-render:ci

Needs a throwaway Postgres 16 superuser DSN in ``SMOKE_DATABASE_URL`` (CI: a
service container) and Docker. It runs the image the way Render's free plan does
(``--memory 512m --cpus 0.1``, ``PORT=10000``) and checks:

* ``GET /healthz`` answers, and how long the start took at 0.1 CPU;
* the worker inside the container long-polls the API (``/internal/work``);
* SSE ``/v1/stream`` works through start.sh: a feed update seen by the worker's
  database pass arrives on the stream as ``feeds.updated`` (worker → API → client);
* a what-if run (the engine inside the API process) completes;
* memory: idle and peak (``docker stats`` samples and the cgroup's own peak);
* killing the worker stops the container (so Render restarts it).

Every value here is a throwaway test value; nothing is printed but counts, statuses
and sizes. Results go to stdout and, in Actions, to the job summary.
"""

from __future__ import annotations

import argparse
import base64
import json
import os
import re
import secrets
import statistics
import subprocess
import sys
import threading
import time
from pathlib import Path

import httpx
import psycopg
from psycopg.conninfo import conninfo_to_dict, make_conninfo

ROOT = Path(__file__).resolve().parents[2]
NAME = "rp-render-smoke"
PORT = 10000
BASE = f"http://127.0.0.1:{PORT}"
LOGIN = "smoke_app_login"
LIMIT_MB = 512
BUDGET_MB = 400  # "well under 512": fail above this


def mib(text: str) -> float:
    """'123.4MiB / 512MiB' → 123.4"""
    m = re.match(r"\s*([\d.]+)\s*([KMG]i?B)", text)
    if not m:
        return float("nan")
    value, unit = float(m.group(1)), m.group(2)
    return value * {"KiB": 1 / 1024, "KB": 1 / 1024, "MiB": 1, "MB": 1, "GiB": 1024, "GB": 1024}[unit]


class Sampler(threading.Thread):
    def __init__(self) -> None:
        super().__init__(daemon=True)
        self.samples: list[tuple[float, float]] = []
        self.stop = threading.Event()

    def run(self) -> None:
        while not self.stop.is_set():
            out = subprocess.run(["docker", "stats", "--no-stream", "--format", "{{.MemUsage}}", NAME],
                                 capture_output=True, text=True)  # fmt: skip
            if out.returncode == 0 and out.stdout.strip():
                self.samples.append((time.monotonic(), mib(out.stdout)))
            self.stop.wait(0.5)

    def since(self, t0: float) -> list[float]:
        return [v for t, v in self.samples if t >= t0]


def cgroup_peak_mb() -> float | None:
    for path in ("/sys/fs/cgroup/memory.peak", "/sys/fs/cgroup/memory/memory.max_usage_in_bytes"):
        out = subprocess.run(["docker", "exec", NAME, "cat", path], capture_output=True, text=True)
        if out.returncode == 0 and out.stdout.strip().isdigit():
            return int(out.stdout.strip()) / 1024 / 1024
    return None


def logs() -> str:
    out = subprocess.run(["docker", "logs", NAME], capture_output=True, text=True)
    return out.stdout + out.stderr


def wait_for(what: str, check, timeout: float, every: float = 1.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            result = check()
        except Exception:
            result = None
        if result:
            return result
        time.sleep(every)
    print(f"FAIL: timed out waiting for {what}", file=sys.stderr)
    print("--- container log (statuses only) ---\n" + logs()[-6000:], file=sys.stderr)
    raise SystemExit(1)


def prepare_db(owner_dsn: str) -> str:
    sys.path.insert(0, str(ROOT))
    from jobs import migrate

    migrate.apply(owner_dsn)
    password = secrets.token_urlsafe(24)
    with psycopg.connect(owner_dsn, autocommit=True) as conn:
        conn.execute(f"DROP ROLE IF EXISTS {LOGIN}")
    subprocess.run([sys.executable, str(ROOT / "infra/scripts/create_login_roles.py"), "--dsn-env", "SMOKE_DATABASE_URL",
                    "--role", f"{LOGIN}:rp_app", "--password-stdin"], input=password + "\n", text=True, check=True)  # fmt: skip
    with psycopg.connect(owner_dsn, autocommit=True) as conn:
        # One feed-health row, so the worker's first database pass records "feeds as of".
        conn.execute("INSERT INTO feeds.source_health (source, status, last_run_at, last_success_at)"
                     " VALUES ('usgs', 'ok', now() - interval '1 hour', now() - interval '1 hour')"
                     " ON CONFLICT (source) DO UPDATE SET last_run_at = EXCLUDED.last_run_at")  # fmt: skip
    parts = conninfo_to_dict(owner_dsn)
    parts.update(user=LOGIN, password=password)
    return make_conninfo(**parts)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--image", required=True)
    args = ap.parse_args()
    owner_dsn = os.environ.get("SMOKE_DATABASE_URL")
    if not owner_dsn:
        print("SMOKE_DATABASE_URL is not set", file=sys.stderr)
        return 2

    env = dict(os.environ)
    env.update(
        DATABASE_URL_APP=prepare_db(owner_dsn),
        JWT_SIGNING_KEY=base64.b64encode(secrets.token_bytes(32)).decode(),
        TOKEN_ENCRYPTION_KEYS="k1:" + base64.b64encode(secrets.token_bytes(32)).decode(),
        INTERNAL_TOKEN=secrets.token_urlsafe(32),
        WEB_ORIGIN="http://localhost:5173",
        PUBLIC_API_URL=BASE,
        WORKER_DB_INTERVAL_SECONDS="45",
    )
    names = ["DATABASE_URL_APP", "JWT_SIGNING_KEY", "TOKEN_ENCRYPTION_KEYS", "INTERNAL_TOKEN", "WEB_ORIGIN",
             "PUBLIC_API_URL", "WORKER_DB_INTERVAL_SECONDS"]  # fmt: skip
    subprocess.run(["docker", "rm", "-f", NAME], capture_output=True)
    run = ["docker", "run", "-d", "--name", NAME, "--network", "host", "--memory", f"{LIMIT_MB}m",
           "--memory-swap", f"{LIMIT_MB}m", "--cpus", "0.1", "-e", f"PORT={PORT}"]  # fmt: skip
    for n in names:
        run += ["-e", n]  # value from this process's environment, never on the command line
    sampler = Sampler()
    t_start = time.monotonic()
    subprocess.run(run + [args.image], env=env, check=True, capture_output=True)
    sampler.start()
    results: dict[str, object] = {}
    try:
        wait_for("/healthz", lambda: httpx.get(f"{BASE}/healthz", timeout=5).json() == {"status": "ok"}, 240)
        results["start_seconds"] = round(time.monotonic() - t_start, 1)
        print(f"healthz ok after {results['start_seconds']} s at 0.1 CPU")
        wait_for("the worker's first database pass", lambda: "database pass:" in logs(), 240)
        wait_for("the worker's long poll", lambda: "/internal/work 200" in logs(), 120)
        print("worker started and long-polls the API inside the container")

        time.sleep(20)  # settle
        t_idle = time.monotonic()
        time.sleep(10)
        idle = sampler.since(t_idle) or [float("nan")]
        results["idle_mb"] = round(statistics.median(idle), 1)

        c = httpx.Client(base_url=BASE, timeout=60)
        r = c.post("/v1/auth/sign-up", json={"email": f"smoke-{secrets.token_hex(4)}@example.test",
                                             "password": "correct horse battery", "workspace_name": "Smoke"})  # fmt: skip
        assert r.status_code == 201, r.status_code
        me = r.json()
        h = {"Authorization": f"Bearer {me['access_token']}", "X-Workspace-Id": me["workspace_id"]}
        ticket = c.post("/v1/stream/ticket", headers=h).json()["ticket"]

        seen: list[str] = []
        connected = threading.Event()

        def listen() -> None:
            with httpx.stream("GET", f"{BASE}/v1/stream", params={"ticket": ticket}, timeout=httpx.Timeout(10, read=200)) as s:
                assert s.status_code == 200, s.status_code
                for line in s.iter_lines():
                    if line.startswith(": connected"):
                        connected.set()
                    if line.startswith("event:"):
                        seen.append(line.split(":", 1)[1].strip())
                        if seen[-1] == "feeds.updated":
                            return

        th = threading.Thread(target=listen, daemon=True)
        th.start()
        wait_for("the SSE stream to open", connected.is_set, 30)
        print("SSE stream open through start.sh")

        t_busy = time.monotonic()
        # A what-if run: the engine (numpy, scikit-learn, rapidfuzz) inside the API process.
        r = c.post("/v1/scenarios", headers=h, json={"name": "smoke", "inputs": {"shocks": [
            {"type": "event", "category": "logistics", "severity": 0.8, "countries": ["DEU"]}]}})  # fmt: skip
        assert r.status_code == 201, r.status_code
        results["scenario_status"] = r.json().get("status")
        # A feed update: the worker's next database pass re-scores (the engine inside the
        # worker process) and pushes feeds.updated to the API, which streams it to us.
        with psycopg.connect(owner_dsn, autocommit=True) as conn:
            conn.execute("UPDATE feeds.source_health SET last_run_at = now(), last_success_at = now() WHERE source = 'usgs'")
        th.join(150)
        if "feeds.updated" not in seen:
            print(f"FAIL: feeds.updated not received over SSE (saw {seen})", file=sys.stderr)
            print(logs()[-6000:], file=sys.stderr)
            return 1
        results["sse"] = "feeds.updated received (worker → API → stream)"
        print(results["sse"])
        wait_for("the worker's engine run", lambda: logs().count("database pass:") >= 2, 60)
        time.sleep(5)
        busy = sampler.since(t_busy) or [float("nan")]
        results["busy_max_mb"] = round(max(busy), 1)
        results["sampled_peak_mb"] = round(max(v for _, v in sampler.samples), 1)
        peak = cgroup_peak_mb()
        results["cgroup_peak_mb"] = round(peak, 1) if peak is not None else "n/a"

        # The worker dies → the container must stop.
        subprocess.run(["docker", "exec", NAME, "python", "-c",
                        "import os, signal\n"
                        "for p in os.listdir('/proc'):\n"
                        "    if p.isdigit():\n"
                        "        try: cmd = open(f'/proc/{p}/cmdline','rb').read().split(b'\\0')\n"
                        "        except OSError: continue\n"
                        "        if cmd[:3] == [b'python', b'-m', b'worker']: os.kill(int(p), signal.SIGKILL)\n"],
                       check=True)  # fmt: skip
        t_kill = time.monotonic()

        def stopped() -> bool:
            out = subprocess.run(["docker", "inspect", "-f", "{{.State.Running}}", NAME], capture_output=True, text=True)
            return out.stdout.strip() == "false"

        wait_for("the container to stop after the worker died", stopped, 60)
        results["exit_after_worker_kill_seconds"] = round(time.monotonic() - t_kill, 1)
    finally:
        sampler.stop.set()
        subprocess.run(["docker", "rm", "-f", NAME], capture_output=True)

    peak_values = [v for v in (results.get("cgroup_peak_mb"), results.get("sampled_peak_mb")) if isinstance(v, float)]
    worst = max(peak_values) if peak_values else float("nan")
    ok = worst < BUDGET_MB
    lines = [
        "### Combined image (API + worker) at Render free limits: 512 MB, 0.1 CPU",
        "",
        "| Check | Result |",
        "|---|---|",
        f"| Start to `/healthz` ok | {results['start_seconds']} s |",
        f"| Idle memory (median, `docker stats`) | {results['idle_mb']} MiB |",
        f"| Peak while busy (what-if + worker engine run, `docker stats`) | {results['busy_max_mb']} MiB |",
        f"| Peak since start (`docker stats` / cgroup) | {results['sampled_peak_mb']} / {results['cgroup_peak_mb']} MiB |",
        f"| What-if run | {results['scenario_status']} |",
        f"| SSE `/v1/stream` | {results['sse']} |",
        f"| Worker killed → container stopped | after {results['exit_after_worker_kill_seconds']} s |",
        f"| Budget | {'OK' if ok else 'OVER'}: peak {worst:.0f} MiB, budget {BUDGET_MB} MiB of {LIMIT_MB} |",
    ]
    text = "\n".join(lines)
    print(text)
    print(json.dumps(results))
    if os.environ.get("GITHUB_STEP_SUMMARY"):
        with open(os.environ["GITHUB_STEP_SUMMARY"], "a", encoding="utf-8") as f:
            f.write(text + "\n")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
