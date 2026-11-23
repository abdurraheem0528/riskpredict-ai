"""HTTP behaviour (throttling, pagination, failure reporting) on real fixtures,
served through httpx.MockTransport so no network is used."""

import gzip
from datetime import datetime, timezone

import httpx
from conftest import fixture_bytes, query_of

from jobs.connectors.base import RunContext, WatchTerm
from jobs.connectors.epss import EpssConnector
from jobs.connectors.gdelt import GdeltConnector
from jobs.connectors.osv import OsvConnector
from jobs.connectors.portwatch import PortWatchConnector
from jobs.connectors.usgs import UsgsConnector
from jobs.connectors.worldbank import WorldBankConnector
from jobs.http import make_client
from jobs.models import Status

NOW = datetime(2026, 11, 23, 20, tzinfo=timezone.utc)


class FakeClock:
    def __init__(self):
        self.t = 1000.0
        self.slept: list[float] = []

    def __call__(self):
        return self.t

    def sleep(self, s):
        self.slept.append(s)
        self.t += s


def ctx_with(clock=None, terms=()):
    return RunContext(now=NOW, watch_terms=list(terms), sleep=clock.sleep if clock else (lambda s: None))


def client_for(handler):
    return make_client(transport=httpx.MockTransport(handler))


GDELT_TERMS = [WatchTerm("gdelt_query", "a"), WatchTerm("gdelt_query", "b")]


def test_gdelt_backs_off_on_429_then_succeeds():
    clock = FakeClock()
    replies = iter(
        [
            httpx.Response(429, content=fixture_bytes("gdelt_throttle.txt")),
            httpx.Response(200, content=fixture_bytes("gdelt_throttle.txt")),  # throttle text with HTTP 200
            httpx.Response(200, content=fixture_bytes("gdelt_artlist.json")),
            httpx.Response(200, content=fixture_bytes("gdelt_artlist.json")),
        ]
    )
    conn = GdeltConnector(clock=clock)
    result = conn.run(client_for(lambda r: next(replies)), ctx_with(clock, GDELT_TERMS))
    assert result.status == Status.OK
    assert len(result.events) == 2  # same article for both queries; merged later by the writer
    assert clock.slept[:2] == [15.0, 30.0]  # exponential back-off
    assert 5.5 in clock.slept  # min gap between the two queries


def test_gdelt_stops_after_repeated_connection_failures():
    clock = FakeClock()
    calls = []

    def handler(request):
        calls.append(request)
        raise httpx.ConnectError("reset")

    terms = [WatchTerm("gdelt_query", q) for q in "abcde"]
    result = GdeltConnector(clock=clock).run(client_for(handler), ctx_with(clock, terms))
    assert result.status == Status.ERROR
    assert len(calls) == 2  # circuit breaker: no hammering a refusing host
    assert result.notes["queries_not_run"] == 3
    assert result.error == "ConnectError contacting api.gdeltproject.org"


def test_gdelt_all_throttled():
    clock = FakeClock()
    result = GdeltConnector(clock=clock, budget=60).run(
        client_for(lambda r: httpx.Response(429, content=fixture_bytes("gdelt_throttle.txt"))),
        ctx_with(clock, GDELT_TERMS),
    )
    assert result.status == Status.THROTTLED and result.events == []


def test_gdelt_without_terms_is_skipped():
    result = GdeltConnector().run(client_for(lambda r: httpx.Response(500)), ctx_with())
    assert result.status == Status.SKIPPED


def test_portwatch_paginates_and_reports_partial():
    seen_offsets = []

    def handler(request):
        path = request.url.path
        q = query_of(request)
        if "PortWatch_ports_database" in path:
            seen_offsets.append(q["resultOffset"])
            page = 1 if q["resultOffset"] == "0" else 2
            return httpx.Response(200, content=fixture_bytes(f"portwatch_ports_page{page}.json"))
        if "portwatch_disruptions_database" in path:
            return httpx.Response(200, content=fixture_bytes("portwatch_disruptions.json"))
        # The trade layer answering like the token-protected layers do.
        return httpx.Response(200, content=fixture_bytes("portwatch_token_required.json"))

    conn = PortWatchConnector()
    result = conn.run(client_for(handler), ctx_with())
    assert seen_offsets == ["0", "1000"]
    assert result.status == Status.PARTIAL
    assert result.error == "ArcGIS error 499 from services9.arcgis.com"
    assert len(result.events) == 3 and result.series == []
    assert result.notes["ports_indexed"] == 4


def test_worldbank_error_payload_is_reported():
    def handler(request):
        return httpx.Response(200, content=fixture_bytes("worldbank_error.json"))

    result = WorldBankConnector().run(client_for(handler), ctx_with())
    assert result.status == Status.ERROR and result.series == []
    assert result.error.startswith("World Bank API error")


def test_epss_gzip_download():
    raw = gzip.compress(fixture_bytes("epss_scores_trimmed.csv"))
    result = EpssConnector(min_score=0.5).run(client_for(lambda r: httpx.Response(200, content=raw)), ctx_with())
    assert result.status == Status.OK and len(result.series) == 3


def test_osv_queries_each_package():
    bodies = []

    def handler(request):
        bodies.append(request.content)
        return httpx.Response(200, content=fixture_bytes("osv_jinja2_trimmed.json"))

    terms = [WatchTerm("osv_package", "jinja2", "PyPI"), WatchTerm("osv_package", "flask", "PyPI")]
    result = OsvConnector().run(client_for(handler), ctx_with(terms=terms))
    assert result.status == Status.OK
    assert len(bodies) == 2 and b'"ecosystem":"PyPI"' in bodies[0].replace(b" ", b"")
    assert result.notes == {"packages": 2, "packages_failed": 0}


def test_http_errors_become_status_not_exceptions():
    result = UsgsConnector().run(client_for(lambda r: httpx.Response(404)), ctx_with())
    assert result.status == Status.ERROR
    assert result.error == "HTTP 404 from earthquake.usgs.gov"
    assert result.events == []


def test_server_errors_are_retried():
    replies = iter([httpx.Response(503), httpx.Response(200, content=fixture_bytes("usgs_4.5_day.geojson"))])
    result = UsgsConnector().run(client_for(lambda r: next(replies)), ctx_with())
    assert result.status == Status.OK and len(result.events) == 21


def test_unexpected_payload_reports_only_exception_class():
    result = UsgsConnector().run(
        client_for(lambda r: httpx.Response(200, json={"features": "secret-looking text"})), ctx_with()
    )
    assert result.status == Status.ERROR
    assert "secret" not in (result.error or "")
