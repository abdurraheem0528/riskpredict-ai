"""infra/vercel/bundle.py: what goes into the Vercel function and what stays out.
(The size, cold-import and ASGI checks run in CI's ``vercel-bundle`` job.)"""

from __future__ import annotations

import importlib.util
import json
import tomllib
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture(scope="module")
def b():
    spec = importlib.util.spec_from_file_location("vercel_bundle", ROOT / "infra/vercel/bundle.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_bundle_holds_only_the_api_runtime(b, tmp_path):
    out = b.build(tmp_path / "bundle", "fra1")
    files = b.file_list(out)
    assert {"app.py", "pyproject.toml", "vercel.json", ".python-version", "pkg/api/main.py", "pkg/api/events.py",
            "pkg/engine/core.py", "pkg/engine/data/chokepoints.json", "pkg/worker/sync.py",
            "pkg/jobs/analyse/anomalies.py", "pkg/jobs/data/fips_countries.tsv"} <= set(files)  # fmt: skip
    for f in files:
        assert not f.startswith(("web/", "design/", "docs/", "tests/", "infra/", "api/", "pkg/jobs/connectors")), f
        assert "__pycache__" not in f and not f.endswith(("Dockerfile", "requirements-dev.txt")), f
    assert "pkg/jobs/analyse/tagging.py" not in files and "pkg/worker/main.py" not in files
    cfg = json.loads((out / "vercel.json").read_text())
    assert cfg["regions"] == ["fra1"] and cfg["functions"]["app.py"]["maxDuration"] == 300
    project = tomllib.loads((out / "pyproject.toml").read_text())
    assert project["tool"]["vercel"]["entrypoint"] == "app:app"
    deps = [d.split("==")[0].split("[")[0].lower() for d in project["project"]["dependencies"]]
    assert "fastapi" in deps and "psycopg" in deps and "uvicorn" not in deps
    assert not {"torch", "gliner", "gliner2", "transformers"} & set(deps)  # the model stack stays in jobs/


def test_every_import_of_the_api_is_in_the_bundle(b, tmp_path):
    """Import the app from the bundle alone (no repository on sys.path) in a fresh interpreter."""
    pytest.importorskip("fastapi")  # the repository-wide CI job does not install the API
    import subprocess
    import sys

    out = b.build(tmp_path / "bundle")
    code = ("import sys; sys.path = [p for p in sys.path if p and 'riskpredict' not in p]; sys.path.insert(0, '.');"
            " import app; import api.events, worker.passes; print(type(app.app).__name__)")  # fmt: skip
    env = {"DATABASE_URL_APP": "postgresql://nobody@127.0.0.1:9/none", "PATH": "/usr/bin:/bin"}
    proc = subprocess.run([sys.executable, "-c", code], cwd=out, env=env, capture_output=True, text=True)
    assert proc.returncode == 0, proc.stderr[-500:]
    assert proc.stdout.strip() == "FastAPI"
