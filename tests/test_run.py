"""The CLI end to end, with every source served from saved real responses."""

import gzip
import io
import zipfile
from datetime import datetime, timedelta, timezone

import httpx
import pytest
from conftest import fixture_bytes

from jobs import run
from jobs.connectors import CONNECTORS
from jobs.http import make_client

ROUTES = {
    "www.cisa.gov": "cisa_kev_trimmed.json",
    "api.osv.dev": "osv_jinja2_trimmed.json",
    "www.gdacs.org": "gdacs_events4app_trimmed.json",
    "earthquake.usgs.gov": "usgs_4.5_day.geojson",
    "eonet.gsfc.nasa.gov": "eonet_events.json",
    "api.frankfurter.dev": "frankfurter_range.json",
    "api.worldbank.org": "worldbank_last_page.json",
    "sanctionslistservice.ofac.treas.gov": "ofac_delta_trimmed.xml",
    "api.gdeltproject.org": "gdelt_artlist.json",
    "services.nvd.nist.gov": "nvd_cves_pub_window.json",
}


def xml_zip(name: str, fixture: str) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr(name, fixture_bytes(fixture))
    return buf.getvalue()


def gkg_zip() -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("20190301001500.gkg.csv", fixture_bytes("gdelt_gkg_trimmed.csv"))
    return buf.getvalue()


def handler(request: httpx.Request) -> httpx.Response:
    host = request.url.host
    if host == "sanctionslistservice.ofac.treas.gov" and "/exports/" in request.url.path:
        name = request.url.path.rsplit("/", 1)[-1].replace(".ZIP", ".XML")
        return httpx.Response(200, content=xml_zip(name, "ofac_enhanced_shape.xml"))
    if host == "data.gdeltproject.org":
        if request.url.path.endswith("lastupdate.txt"):
            return httpx.Response(200, content=fixture_bytes("gdelt_lastupdate.txt"))
        return httpx.Response(200, content=gkg_zip())
    if host == "epss.empiricalsecurity.com":
        return httpx.Response(200, content=gzip.compress(fixture_bytes("epss_scores_trimmed.csv")))
    if host == "services9.arcgis.com":
        path = request.url.path
        if "ports_database" in path:
            name = "portwatch_ports_page2.json"  # single final page
        elif "disruptions" in path:
            name = "portwatch_disruptions.json"
        else:
            name = "portwatch_trade.json"
        return httpx.Response(200, content=fixture_bytes(name))
    return httpx.Response(200, content=fixture_bytes(ROUTES[host]))


@pytest.fixture
def offline(monkeypatch):
    for name, cls in CONNECTORS.items():  # keyed sources: test the no-key path here
        monkeypatch.delenv(f"{name.upper()}_API_KEY", raising=False)
    monkeypatch.setattr(run, "make_client", lambda: make_client(transport=httpx.MockTransport(handler)))
    monkeypatch.setattr("time.sleep", lambda s: None)


def test_dry_run_all_sources_prints_counts_only(offline, capsys, monkeypatch):
    monkeypatch.delenv("DATABASE_URL_FEEDS", raising=False)
    assert run.main(["--sources", "all", "--dry-run"]) == 0
    out = capsys.readouterr().out
    for name in CONNECTORS:
        assert f"\n{name} " in out
    assert "error" not in out and "dry run, nothing written" in out
    # Record contents must never reach the (public) log.
    for leaked in (
        "H N B TRADING", "Tobi Village", "Jinja2 sandbox", "newsonjapan", "CVE-2026-76466", "Mozambique",
        "EXAMPLE TRADING", "Info-ZIP", "CVE-2026-12487",
    ):  # fmt: skip
        assert leaked not in out


def test_unknown_source_is_rejected(capsys):
    with pytest.raises(SystemExit) as exc:
        run.main(["--sources", "usgs,nope", "--dry-run"])
    assert exc.value.code == 2
    assert "unknown source(s): nope" in capsys.readouterr().err


def test_missing_database_url_exits_2(monkeypatch, capsys):
    monkeypatch.delenv("DATABASE_URL_FEEDS", raising=False)
    assert run.main(["--sources", "usgs"]) == 2
    assert "DATABASE_URL_FEEDS is not set" in capsys.readouterr().err


def test_bad_database_url_does_not_echo_it(monkeypatch, capsys):
    secret = "postgresql://user:hunter2@127.0.0.1:1/db"
    monkeypatch.setenv("DATABASE_URL_FEEDS", secret)
    assert run.main(["--sources", "usgs"]) == 1
    captured = capsys.readouterr()
    assert "hunter2" not in captured.out + captured.err


def test_is_due():
    now = datetime(2026, 11, 23, 12, tzinfo=timezone.utc)
    assert run.is_due("worldbank", None, now)
    assert not run.is_due("worldbank", now - timedelta(hours=2), now)
    assert run.is_due("worldbank", now - timedelta(hours=23, minutes=58), now)  # schedule slack
    assert run.is_due("usgs", now - timedelta(minutes=13), now)


# --- --analyse ---------------------------------------------------------------


def test_analyse_dry_run_tags_in_memory_and_prints_counts_only(offline, capsys, monkeypatch):
    from test_analyse_tagging import FakeModel

    monkeypatch.delenv("DATABASE_URL_FEEDS", raising=False)
    monkeypatch.setattr(run, "load_model", lambda: FakeModel())
    assert run.main(["--analyse", "--dry-run"]) == 0
    out = capsys.readouterr().out
    assert "\ntagging    ok" in out
    # Each step-1 fixture holds a few days at most, so nothing can be scored yet.
    assert "\nanomalies  insufficient_history" in out
    assert "dry run, nothing written" in out
    for leaked in ("Stock Market", "Indonesia", "H N B TRADING", "Tennessee", "EUR/USD", "MOZ", "CVE-2026-76466"):
        assert leaked not in out


def test_analyse_without_model_libraries_skips_tagging(offline, capsys, monkeypatch):
    import sys

    from jobs.analyse import tagging

    for name in ("torch", "gliner2"):
        monkeypatch.setitem(sys.modules, name, None)
    monkeypatch.setattr(run, "load_model", tagging.Gliner2Model)
    assert run.main(["--analyse", "--dry-run", "--analysis", "tagging"]) == 0
    out = capsys.readouterr().out
    assert "skipped" in out and "model libraries not installed" in out


def test_analyse_needs_database_url(monkeypatch, capsys):
    monkeypatch.delenv("DATABASE_URL_FEEDS", raising=False)
    assert run.main(["--analyse"]) == 2
    assert "DATABASE_URL_FEEDS is not set" in capsys.readouterr().err


def test_analyse_bad_database_url_does_not_echo_it(monkeypatch, capsys):
    monkeypatch.setenv("DATABASE_URL_FEEDS", "postgresql://user:hunter2@127.0.0.1:1/db")
    assert run.main(["--analyse"]) == 1
    captured = capsys.readouterr()
    assert "hunter2" not in captured.out + captured.err


def test_unknown_analysis_step_is_rejected(capsys):
    with pytest.raises(SystemExit):
        run.main(["--analyse", "--analysis", "nope"])
    assert "choose from: all, tagging, anomalies" in capsys.readouterr().err
