"""The keyed connectors (FRED, NVD, EIA, Comtrade, OTX, Tiingo, NewsData,
Currents), API-key handling and redaction, and the full OFAC list connector.

No network: every response is a saved fixture served by httpx.MockTransport
(see tests/fixtures/README.md for which fixtures are real responses and which
were built from the provider's documentation). The keys used here are fakes.
"""

import io
import json
import zipfile
from datetime import date, datetime, timedelta, timezone

import httpx
import pytest
from conftest import fixture_bytes, fixture_json, query_of

from jobs import run
from jobs.connectors import CONNECTORS, comtrade, currents, eia, fred, news, newsdata, nvd, ofac_sdn, otx, tiingo
from jobs.connectors.base import DailyBudget, KeyedConnector, RunContext, WatchTerm
from jobs.http import SourceError, make_client, redact, secret_values
from jobs.models import SanctionEntry, Status

NOW = datetime(2026, 11, 24, 12, tzinfo=timezone.utc)
FAKE_KEY = "fake-key-1234567890abcdef"
KEYED = {n: c for n, c in CONNECTORS.items() if issubclass(c, KeyedConnector)}
TERMS = [WatchTerm("gdelt_query", f'("term {i}" OR "other {i}")') for i in range(10)]


def ctx(state=None, terms=TERMS, now=NOW):
    return RunContext(now=now, watch_terms=list(terms), sleep=lambda s: None, state=state or {})


def client_for(handler):
    return make_client(transport=httpx.MockTransport(handler))


def no_network(request):  # pragma: no cover - failing is the point
    raise AssertionError(f"unexpected request to {request.url.host}")


@pytest.fixture
def no_keys(monkeypatch):
    for name in KEYED:
        monkeypatch.delenv(f"{name.upper()}_API_KEY", raising=False)


# --- keys --------------------------------------------------------------------


def test_every_keyed_source_names_its_secret():
    assert {c.key_env() for c in KEYED.values()} == {
        "FRED_API_KEY", "NVD_API_KEY", "EIA_API_KEY", "COMTRADE_API_KEY",
        "OTX_API_KEY", "TIINGO_API_KEY", "NEWSDATA_API_KEY", "CURRENTS_API_KEY",
    }  # fmt: skip


@pytest.mark.parametrize("name", sorted(set(KEYED) - {"nvd"}))
def test_missing_key_is_skipped_without_any_request(name, no_keys):
    result = KEYED[name]().run(client_for(no_network), ctx())
    assert result.status == Status.SKIPPED and result.error == "no key" and result.record_count == 0


def test_blank_key_counts_as_missing(monkeypatch):
    monkeypatch.setenv("FRED_API_KEY", "   ")
    assert fred.FredConnector().run(client_for(no_network), ctx()).status == Status.SKIPPED


def test_nvd_runs_without_a_key(no_keys):
    seen = []

    def handler(request):
        seen.append(request)
        return httpx.Response(200, content=fixture_bytes("nvd_cves_pub_window.json"))

    result = nvd.NvdConnector().run(client_for(handler), ctx())
    assert result.status == Status.PARTIAL  # 1230 results, only one 3-row page served each time
    assert "apikey" not in {k.lower() for k in seen[0].headers}
    assert result.notes["keyed"] == 0


# Where each provider gets the key: header name, or None for the query string.
KEY_PLACES = {
    "fred": ("query", "api_key"),
    "eia": ("query", "api_key"),
    "nvd": ("header", "apiKey"),
    "comtrade": ("header", "Ocp-Apim-Subscription-Key"),
    "otx": ("header", "X-OTX-API-KEY"),
    "tiingo": ("header", "Authorization"),
    "newsdata": ("header", "X-ACCESS-KEY"),
    "currents": ("header", "Authorization"),
}
FIXTURE_FOR = {
    "api.stlouisfed.org": "fred_observations_doc.json",
    "api.eia.gov": "eia_spot_prices.json",
    "services.nvd.nist.gov": "nvd_cve_log4shell.json",
    "comtradeapi.un.org": "comtrade_world_classic.json",
    "otx.alienvault.com": "otx_pulses_shape.json",
    "newsdata.io": "newsdata_latest_shape.json",
    "api.currentsapi.services": "currents_search_doc.json",
}


def serve(request):
    if request.url.host == "api.tiingo.com":
        return httpx.Response(200, content=fixture_bytes("tiingo_eod_doc.json"))
    return httpx.Response(200, content=fixture_bytes(FIXTURE_FOR[request.url.host]))


@pytest.mark.parametrize("name", sorted(KEYED))
def test_key_is_sent_where_the_provider_expects_it(name, monkeypatch):
    monkeypatch.setenv(f"{name.upper()}_API_KEY", FAKE_KEY)
    seen = []

    def handler(request):
        seen.append(request)
        return serve(request)

    result = KEYED[name]().run(client_for(handler), ctx())
    assert result.status in (Status.OK, Status.PARTIAL), result.error
    assert seen
    place, field = KEY_PLACES[name]
    for request in seen:
        if place == "query":
            assert query_of(request)[field] == FAKE_KEY
            assert FAKE_KEY not in json.dumps(dict(request.headers))
        else:
            assert FAKE_KEY in request.headers[field]
            assert FAKE_KEY not in str(request.url)


@pytest.mark.parametrize("name", sorted(KEYED))
def test_key_never_appears_in_errors(name, monkeypatch):
    monkeypatch.setenv(f"{name.upper()}_API_KEY", FAKE_KEY)
    for status in (401, 500, 429):
        result = KEYED[name]().run(client_for(lambda r, s=status: httpx.Response(s, text=f"bad key {FAKE_KEY}")), ctx())
        assert result.status in (Status.ERROR, Status.THROTTLED, Status.PARTIAL)
        assert FAKE_KEY not in (result.error or "")
        assert "api_key=" not in (result.error or "").replace("api_key=***", "")


def test_redaction():
    env = {"FRED_API_KEY": FAKE_KEY, "DATABASE_URL_FEEDS": "postgresql://u:pw123456@h/db", "OTHER": "x" * 20}
    assert secret_values(env)[0] in (FAKE_KEY, "postgresql://u:pw123456@h/db")
    text = f"GET https://api.stlouisfed.org/x?series_id=DFF&api_key={FAKE_KEY}&file_type=json via postgresql://u:pw123456@h/db"
    out = redact(text, env)
    assert FAKE_KEY not in out and "pw123456" not in out and "series_id=DFF" in out
    assert redact("https://h/p?token=abc&apikey=def&subscription-key=ghi", {}) == (
        "https://h/p?token=***&apikey=***&subscription-key=***"
    )
    assert redact(None) is None


def test_source_error_messages_are_redacted(monkeypatch):
    monkeypatch.setenv("EIA_API_KEY", FAKE_KEY)
    assert FAKE_KEY not in str(SourceError(f"boom {FAKE_KEY}"))


def test_redirects_are_not_followed_for_keyed_requests(monkeypatch):
    monkeypatch.setenv("OTX_API_KEY", FAKE_KEY)
    hosts = []

    def handler(request):
        hosts.append(request.url.host)
        return httpx.Response(302, headers={"Location": "https://elsewhere.example/steal"})

    result = otx.OtxConnector().run(client_for(handler), ctx())
    assert result.status == Status.ERROR and "redirect" in result.error
    assert hosts == ["otx.alienvault.com"]


def test_dry_run_with_keys_prints_no_key_and_no_records(monkeypatch, capsys):
    for name in KEYED:
        monkeypatch.setenv(f"{name.upper()}_API_KEY", FAKE_KEY)
    monkeypatch.setattr(run, "make_client", lambda: make_client(transport=httpx.MockTransport(serve)))
    monkeypatch.setattr("time.sleep", lambda s: None)
    assert run.main(["--sources", ",".join(KEYED), "--dry-run"]) == 0
    out = capsys.readouterr().out
    assert FAKE_KEY not in out and "api_key" not in out
    for leaked in ("log4j", "Bitcoin", "Example pulse", "Cushing", "Electronic integrated"):
        assert leaked not in out


# --- parsers -----------------------------------------------------------------


def test_fred_documented_example():
    notes = {}
    payload = fixture_json("fred_observations_doc.json")
    points = fred.parse(payload, "GNPCA", notes)
    assert [(p.ts.year, p.value) for p in points][:2] == [(1929, 1065.9), (1930, 975.5)]
    assert all(p.series == "fred.GNPCA" and p.key == "USA" for p in points)
    payload["observations"].append({"date": "1934-01-01", "value": "."})  # FRED's missing-value marker
    assert len(fred.parse(payload, "DFF", notes)) == 5 and notes["missing_values"] == 1
    assert fred.parse(payload, "DFF")[0].unit == fred.SERIES["DFF"][1]
    with pytest.raises(SourceError):
        fred.parse({"error_code": 400}, "DFF")


def test_nvd_cpe_cvss_and_kev():
    (ev,) = nvd.parse(fixture_json("nvd_cve_log4shell.json"), 7.0)
    assert ev.raw_id == "CVE-2021-44228" and ev.type == "cyber.vulnerability"
    assert ev.severity == 1.0 and ev.attrs["cvss_version"] == "3.1" and ev.attrs["cvss_source"] == "nvd"
    assert ev.software[:2] == ("Apache Software Foundation:Apache Log4j2", "apache:log4j")  # CNA, then apps
    assert "Apache Software Foundation:Apache Log4j2" in ev.software
    assert ev.attrs["cpes"][0] == "cpe:2.3:a:apache:log4j"
    assert ev.attrs["cisa_kev"] is True and "CWE-502" in ev.attrs["cwes"]
    assert ev.url == "https://nvd.nist.gov/vuln/detail/CVE-2021-44228"
    assert ev.published_at == datetime(2021, 12, 10, 10, 15, 9, 143000, tzinfo=timezone.utc)
    assert len(ev.title) <= 300 and ev.title.startswith("CVE-2021-44228: Apache Log4j2")


def test_nvd_threshold_cna_scores_and_purls():
    notes = {}
    events = nvd.parse(fixture_json("nvd_cves_pub_window.json"), 7.0, notes)
    assert [e.raw_id for e in events] == ["CVE-2026-12487"]  # 7.5; the others score 2.9 and 4.6
    assert notes["below_threshold"] == 2
    zyxel = events[0]
    assert zyxel.software == ("Zyxel:EE5301-00 customized firmware",) and zyxel.attrs["cvss_source"] == "cna"
    everything = {e.raw_id: e for e in nvd.parse(fixture_json("nvd_cves_pub_window.json"), 0.0)}
    adminneo = everything["CVE-2026-130338"]
    assert adminneo.severity == pytest.approx(0.46)  # v3.1 preferred over the CNA's v4.0 (2.1)
    assert adminneo.attrs["purls"] == ["pkg:composer/adminneo-org/adminneo"]
    assert nvd.cpe_parts(r"cpe:2.3:a:vendor\:x:prod:1:*:*:*:*:*:*:*")[3] == "vendor:x"


def test_nvd_window_advances_and_resumes(monkeypatch):
    conn = nvd.NvdConnector()
    start, end, index = conn.window(ctx())
    assert end == NOW and end - start == timedelta(days=1) and index == 0  # first run: the last day
    prev = int((NOW - timedelta(hours=2)).timestamp())
    start, end, _ = conn.window(ctx({"nvd": {"window_end": prev}}))
    assert start.timestamp() == prev and end == NOW
    state = {"nvd": {"pending_start": prev - 100, "pending_end": prev, "next_index": 2000, "window_end": prev - 100}}
    assert conn.window(ctx(state)) == (
        datetime.fromtimestamp(prev - 100, tz=timezone.utc), datetime.fromtimestamp(prev, tz=timezone.utc), 2000
    )

    pages = []

    def handler(request):
        pages.append(int(query_of(request)["startIndex"]))
        body = fixture_json("nvd_cves_pub_window.json")
        body["totalResults"] = 6
        body["startIndex"] = pages[-1]
        return httpx.Response(200, json=body)

    monkeypatch.setenv("NVD_API_KEY", FAKE_KEY)
    result = conn.run(client_for(handler), ctx({"nvd": {"window_end": prev}}))
    assert pages == [0, 3] and result.status == Status.OK
    assert result.notes["window_end"] == int(NOW.timestamp()) and "next_index" not in result.notes


def test_eia_real_response():
    points = eia.parse(fixture_json("eia_spot_prices.json"))
    assert [(p.key, p.ts.date(), p.value) for p in points][0] == ("RWTC", date(2026, 11, 17), 89.53)
    assert points[0].unit == "$/BBL" and points[0].series == "eia.spot_price"
    assert points[0].attrs["product"] == "WTI Crude Oil" and "area" not in points[0].attrs
    assert "api_key=K" in eia.query("petroleum/pri/spt", ("RWTC",), "K")
    with pytest.raises(SourceError):
        eia.parse({"error": "x"})


def test_comtrade_real_response():
    notes = {}
    points = comtrade.parse(fixture_json("comtrade_world_classic.json"), notes)
    assert [(p.series, p.key, p.value) for p in points] == [
        ("comtrade.import_value", "JPN:2709", 71811869887.915),
        ("comtrade.import_value", "JPN:8542", 23161378835.289),
        ("comtrade.import_value", "IND:2709", 141532322252.516),
        ("comtrade.import_value", "IND:8542", 23445487573.734),
    ]
    assert points[0].ts == datetime(2024, 1, 1, tzinfo=timezone.utc) and points[0].unit == "USD"
    assert points[0].attrs["description"].startswith("Petroleum oils") and notes == {}
    world = fixture_json("comtrade_world_classic.json")
    world["data"][0]["reporterISO"] = "E19"  # an aggregate, no ISO 3166 code
    assert len(comtrade.parse(world, notes)) == 3 and notes["dropped_aggregate_reporter"] == 1
    # A partner = China reply: bilateral rows are not World totals and are never stored.
    notes = {}
    assert comtrade.parse(fixture_json("comtrade_preview.json"), notes) == []
    assert notes["dropped_breakdown"] == 2
    with pytest.raises(SourceError):
        comtrade.parse({"error": "Rate limit is exceeded", "data": None})


def test_comtrade_asks_for_world_totals_only():
    q = query_of(httpx.Request("GET", comtrade.query(ctx())))
    assert q["period"] == "2025,2024" and q["includeDesc"] == "true"
    # Partner World, and every Comtrade+ breakdown pinned to its total (else "plus" mode
    # returns one row per second partner, customs procedure and transport mode).
    assert (q["partnerCode"], q["partner2Code"], q["customsCode"], q["motCode"]) == ("0", "0", "C00", "0")
    assert q["breakdownMode"] == "classic" and q["flowCode"] == "M,X"
    assert all(c.isdigit() for c in q["cmdCode"].split(",")) and len(q["cmdCode"].split(",")) == 12
    assert "reporterCode" not in q  # every reporter, still one call
    # At most ~250 reporters x 12 codes x 2 flows x 2 years: far under the 100,000 cap.
    assert 250 * len(comtrade.HS_CODES) * len(comtrade.FLOWS) * len(comtrade.periods(ctx())) < comtrade.MAX_RECORDS / 8


def _plus_mode_reply():
    """A "plus" reply: the World total plus its transport-mode, customs and 2nd-partner parts."""
    total = fixture_json("comtrade_world_classic.json")["data"][1]  # JPN imports of 8542
    total["primaryValue"] = 1000.0
    parts = [
        {**total, "motCode": 2100, "primaryValue": 600.0},  # sea
        {**total, "motCode": 1000, "primaryValue": 400.0},  # air
        {**total, "customsCode": "C01", "primaryValue": 900.0},
        {**total, "partner2Code": 156, "partner2ISO": "CHN", "primaryValue": 50.0},
        {**total, "partnerCode": 156, "partnerISO": "CHN", "primaryValue": 300.0},
    ]
    return {"count": 1 + len(parts), "data": [*parts[:2], total, *parts[2:], dict(total, primaryValue=1.0)]}


def test_comtrade_keeps_only_the_world_total():
    notes = {}
    points = comtrade.parse(_plus_mode_reply(), notes)
    assert [(p.key, p.value) for p in points] == [("JPN:8542", 1000.0)]
    assert notes["dropped_breakdown"] == 5 and notes["duplicate_keys"] == 1


def test_comtrade_run_reports_partial_only_at_the_cap(monkeypatch):
    monkeypatch.setenv("COMTRADE_API_KEY", FAKE_KEY)
    calls = []

    def handler(count):
        def reply(request):
            calls.append(query_of(request))
            return httpx.Response(200, json={**_plus_mode_reply(), "count": count})

        return reply

    result = comtrade.ComtradeConnector().run(client_for(handler(6)), ctx())
    assert result.status == Status.OK and result.error is None and len(result.series) == 1
    assert result.notes["records_in_response"] == 6 and result.notes["budget_used"] == 1
    capped = comtrade.ComtradeConnector().run(client_for(handler(comtrade.MAX_RECORDS)), ctx())
    assert capped.status == Status.PARTIAL and "100000 record maximum" in capped.error
    assert "5 were not World totals" in capped.error
    assert len(calls) == 2  # one call per run


def test_comtrade_respects_the_daily_budget(monkeypatch):
    monkeypatch.setenv("COMTRADE_API_KEY", FAKE_KEY)
    full = {"comtrade": {"budget_day": 20261124, "budget_used": comtrade.DAILY_LIMIT}}
    result = comtrade.ComtradeConnector().run(client_for(no_network), ctx(full))
    assert result.status == Status.SKIPPED and "budget" in result.error


def test_otx_tlp_countries_and_cves():
    notes = {}
    events = otx.parse(fixture_json("otx_pulses_shape.json"), notes)
    assert [e.raw_id for e in events] == ["000000000000000000000001", "000000000000000000000003"]
    assert notes["skipped_tlp"] == 1 and notes["unmatched_country_names"] == 1  # "Atlantis"
    first = events[0]
    assert first.type == "cyber.threat_pulse" and first.countries == ("DEU", "USA")
    assert first.attrs["cves"] == ["CVE-2021-44228"] and first.attrs["attack_ids"] == ["T1486"]
    assert first.attrs["malware_families"] == ["ExampleLocker"] and first.attrs["indicator_count"] == 2
    assert "indicators" not in first.attrs and first.severity is None


def test_otx_paginates_and_keeps_its_window(monkeypatch):
    monkeypatch.setenv("OTX_API_KEY", FAKE_KEY)
    since = int((NOW - timedelta(hours=1)).timestamp())
    calls = []

    def handler(request):
        calls.append(query_of(request))
        body = fixture_json("otx_pulses_shape.json")
        body["next"] = "https://otx.alienvault.com/api/v1/pulses/subscribed?page=2" if len(calls) == 1 else None
        return httpx.Response(200, json=body)

    result = otx.OtxConnector().run(client_for(handler), ctx({"otx": {"modified_since": since}}))
    assert [c["page"] for c in calls] == ["1", "2"]
    assert calls[0]["modified_since"] == (NOW - timedelta(hours=1)).strftime("%Y-%m-%dT%H:%M:%S")
    assert result.notes["modified_since"] == int(NOW.timestamp()) and result.status == Status.OK


def test_tiingo_eod_and_fx(monkeypatch):
    (p,) = tiingo.parse_eod(fixture_json("tiingo_eod_doc.json"), "spy")
    assert (p.series, p.key, p.value, p.unit) == ("tiingo.close", "SPY", 157.92, "USD")
    # FX price rows carry date, ticker, open, high, low, close (documented fields; values arbitrary)
    fx_rows = [{"date": "2026-11-20T00:00:00.000Z", "ticker": "usdtry", "open": 1.0, "high": 1.0, "low": 1.0, "close": 1.0}]
    (fx,) = tiingo.parse_fx(fx_rows, "usdtry")
    assert (fx.series, fx.key, fx.unit) == ("tiingo.fx_close", "USD/TRY", "TRY per USD")
    with pytest.raises(SourceError):
        tiingo.parse_eod({"detail": "Error"}, "spy")

    monkeypatch.setenv("TIINGO_API_KEY", FAKE_KEY)

    def handler(request):
        if "/fx/" in request.url.path:
            return httpx.Response(404, json={"detail": "not found"})
        return httpx.Response(200, content=fixture_bytes("tiingo_eod_doc.json"))

    result = tiingo.TiingoConnector().run(client_for(handler), ctx())
    assert result.status == Status.OK  # uncovered symbols are counted, not an error
    assert result.notes["symbols_unavailable"] == len(tiingo.FX_PAIRS)
    assert result.notes["budget_used"] == len(tiingo.TICKERS) + len(tiingo.FX_PAIRS)

    replies = iter([httpx.Response(200, content=fixture_bytes("tiingo_eod_doc.json"))] + [httpx.Response(429)] * 50)
    throttled = tiingo.TiingoConnector().run(client_for(lambda r: next(replies)), ctx())
    assert throttled.status == Status.PARTIAL and throttled.notes["budget_used"] == 2  # stopped at the 429


def test_newsdata_shape_and_paid_fields():
    notes = {}
    (ev,) = newsdata.parse(fixture_json("newsdata_latest_shape.json"), "q", notes)
    assert ev.type == "news" and ev.countries == ()  # publisher country is not the event location
    assert ev.attrs["publisher_country"] == ["united kingdom"] and ev.attrs["sentiment"] == "negative"
    assert ev.published_at == datetime(2026, 11, 23, 8, 15, tzinfo=timezone.utc)
    assert "content" not in ev.attrs and notes["skipped_duplicate_or_no_url"] == 1
    assert ev.raw_id == news.url_id("https://example.com/news/port-strike-example")
    with pytest.raises(SourceError):
        newsdata.parse({"status": "error", "results": {"message": "x", "code": "Unauthorized"}}, "q")


def test_currents_documented_example():
    events = currents.parse(fixture_json("currents_search_doc.json"), "q")
    assert [e.title for e in events] == ["Investors are taking a gamble on Bitcoin", "City council approves new transit plan"]
    assert events[0].published_at == datetime(2026, 9, 7, 14, 22, 8, tzinfo=timezone.utc)
    assert events[0].attrs["domain"] == "example.com" and events[0].countries == ()


@pytest.mark.parametrize("cls,limit", [(newsdata.NewsDataConnector, newsdata.PER_RUN), (currents.CurrentsConnector, currents.PER_RUN)])
def test_news_queries_rotate_and_stay_in_budget(cls, limit, monkeypatch):
    monkeypatch.setenv(cls.key_env(), FAKE_KEY)
    sent = []

    def handler(request):
        q = query_of(request)
        sent.append(q.get("q") or q.get("query"))
        return serve(request)

    first = cls().run(client_for(handler), ctx())
    assert sent == [t.term for t in TERMS[:limit]]
    assert first.notes["term_offset"] == limit % len(TERMS) and first.notes["budget_used"] == limit
    sent.clear()
    cls().run(client_for(handler), ctx({cls.name: first.notes}))
    assert sent[0] == TERMS[limit % len(TERMS)].term  # continues where the last run stopped
    # GDELT-only operators are skipped, and no terms means no request at all
    rot = news.Rotation(ctx(terms=[WatchTerm("gdelt_query", "theme:ECON_BANKRUPTCY")]), cls.name, limit)
    assert rot.terms == [] and rot.unsupported == 1
    assert cls().run(client_for(no_network), ctx(terms=[])).status == Status.SKIPPED
    spent = {cls.name: {"budget_day": 20261124, "budget_used": 10_000}}
    assert cls().run(client_for(no_network), ctx(spent)).status == Status.ERROR


def test_daily_budget_resets_each_utc_day():
    b = DailyBudget.load(ctx({"x": {"budget_day": 20261123, "budget_used": 99}}), "x", 100)
    assert b.used == 0 and b.take(100) and not b.take()
    notes = {}
    b.save(notes)
    assert notes == {"budget_day": 20261124, "budget_used": 100}


# --- OFAC full lists ----------------------------------------------------------


def zip_of(xml: bytes, name: str = "SDN_ENHANCED.XML") -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr(name, xml)
    return buf.getvalue()


def test_ofac_full_list_entries():
    notes = {}
    entries = ofac_sdn.read_zip(zip_of(fixture_bytes("ofac_enhanced_shape.xml")), "sdn", notes)
    by_id = {e.entity_id: e for e in entries}
    firm = by_id["900001"]
    assert firm.name == "EXAMPLE TRADING COMPANY LLC" and firm.entity_type == "Entity"
    assert set(firm.aliases) == {"FICTIONAL GLOBAL SHIPPING LTD", "شركة مثال للتجارة"}
    assert firm.weak_aliases == ("EXTRA",)  # isLowQuality
    assert firm.countries == ("ARE",) and notes["unmatched_country_names"] == 1  # "Exampleland"
    assert firm.programs == ("SDGT", "IFSR") and firm.lists == ("SDN List",) and firm.listed_on == date(2019, 5, 2)
    assert by_id["900002"].countries == ("RUS",)  # Nationality Country feature
    assert by_id["900003"].countries == ("PAN",) and by_id["900003"].entity_type == "Vessel"
    assert notes["sdn_entities_in_file"] == 3
    # Nothing personal is kept: no birth dates, no document numbers.
    assert "1970" not in repr(entries) and "000000-EXAMPLE" not in repr(entries)


def test_ofac_full_list_also_reads_the_real_delta_format():
    # The real delta file uses the same enhanced layout (another namespace).
    entries = ofac_sdn.parse_xml(fixture_bytes("ofac_delta_trimmed.xml"), "sdn")
    assert [e.name for e in entries] == ["H N B TRADING CO LLC", "KALHOR, Ali", "HEMMATI, Valiollah"]
    assert entries[1].aliases == ("علی کلهر",)  # bidi control marks removed
    assert entries[0].countries == ("ARE",)


def test_ofac_full_list_rejects_bad_downloads():
    with pytest.raises(SourceError):
        ofac_sdn.read_zip(b"not a zip", "sdn")
    with pytest.raises(SourceError):
        ofac_sdn.read_zip(zip_of(b"<sanctionsData><entities><entity", "x.XML"), "sdn")
    with pytest.raises(ValueError):
        SanctionEntry("ofac_sdn", "sdn", "1", "   ")


def test_ofac_full_list_replaces_only_complete_lists(monkeypatch):
    def handler(request):
        return httpx.Response(200, content=zip_of(fixture_bytes("ofac_enhanced_shape.xml")))

    monkeypatch.setattr(ofac_sdn, "MIN_ENTRIES", {"sdn": 3, "consolidated": 50})
    result = ofac_sdn.OfacSdnConnector().run(client_for(handler), ctx())
    assert result.status == Status.PARTIAL  # consolidated came back with 3 entries < 50
    assert result.replace_datasets == ("sdn",) and len(result.sanctions) == 6
    assert "nothing deleted" in result.error

    def broken(request):
        if "CONS_" in request.url.path:
            return httpx.Response(503)
        return handler(request)

    result = ofac_sdn.OfacSdnConnector().run(client_for(broken), ctx())
    assert result.status == Status.PARTIAL and result.replace_datasets == ("sdn",)
    result = ofac_sdn.OfacSdnConnector().run(client_for(lambda r: httpx.Response(503)), ctx())
    assert result.status == Status.ERROR and result.sanctions == []
