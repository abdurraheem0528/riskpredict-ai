"""The CLI end to end, with every source served from saved real responses."""

import gzip
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
}


def handler(request: httpx.Request) -> httpx.Response:
    host = request.url.host
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
    for leaked in ("H N B TRADING", "Tobi Village", "Jinja2 sandbox", "newsonjapan", "CVE-2026-76466", "Mozambique"):
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
