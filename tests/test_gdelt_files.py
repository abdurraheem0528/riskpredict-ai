"""GDELT: the 15-minute GKG file path, the one-call DOC fallback and the
FIPS → ISO table, on saved real files served through httpx.MockTransport."""

import io
import time
import zipfile
from datetime import datetime, timezone

import httpx
import pytest
from conftest import fixture_bytes

from jobs import codes
from jobs.connectors import gdelt, news
from jobs.connectors.base import RunContext, WatchTerm
from jobs.http import make_client
from jobs.models import Status
from jobs.watch_defaults import DEFAULT_TERMS, GDELT_THEME_QUERIES

NOW = datetime(2026, 11, 16, 16, 40, tzinfo=timezone.utc)
LATEST = 20261116163000  # newest window in gdelt_lastupdate.txt

# Terms that hit the saved rows through different fields.
TERMS = [
    WatchTerm("gdelt_query", "theme:CYBER_ATTACK"),  # GKG theme code
    WatchTerm("gdelt_query", '("russian engines" OR "engine co")'),  # URL slug / organisation name
    WatchTerm("gdelt_query", '(blockade OR "gaza strip")'),  # theme name / location name
]


def gkg_zip(name="20190301001500.gkg.csv", fixture="gdelt_gkg_trimmed.csv") -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr(name, fixture_bytes(fixture))
    return buf.getvalue()


def matchers(terms=TERMS):
    return [gdelt.parse_query(t.term) for t in terms]


def client_for(handler):
    return make_client(transport=httpx.MockTransport(handler))


class Feed:
    """data.gdeltproject.org (and the DOC API) as a recorded set of replies."""

    def __init__(self, lastupdate=200, files=200, doc=200):
        self.lastupdate, self.files, self.doc = lastupdate, files, doc
        self.urls: list[str] = []
        self.zip = gkg_zip

    def __call__(self, request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        self.urls.append(url)
        if request.url.host == "api.gdeltproject.org":
            if self.doc == 429:
                return httpx.Response(429, content=fixture_bytes("gdelt_throttle.txt"))
            return httpx.Response(self.doc, content=fixture_bytes("gdelt_artlist.json"))
        if url.endswith("lastupdate.txt"):
            if isinstance(self.lastupdate, Exception):
                raise self.lastupdate
            return httpx.Response(self.lastupdate, content=fixture_bytes("gdelt_lastupdate.txt"))
        status = self.files(url) if callable(self.files) else self.files
        return httpx.Response(status, content=self.zip() if status == 200 else b"")

    @property
    def file_urls(self):
        return [u for u in self.urls if u.endswith(".gkg.csv.zip")]


def run(feed, terms=TERMS, state=None, **kw):
    ctx = RunContext(now=NOW, watch_terms=list(terms), state=state or {})
    return gdelt.GdeltConnector(**kw).run(client_for(feed), ctx)


# --- parsing ------------------------------------------------------------------


def test_lastupdate_lists_the_three_files():
    listing = gdelt.parse_lastupdate(fixture_bytes("gdelt_lastupdate.txt").decode())
    assert listing["gkg"] == (6637099, "http://data.gdeltproject.org/gdeltv2/20261116163000.gkg.csv.zip")
    assert set(listing) == {"gkg", "export", "mentions"}
    assert gdelt.window_of(listing["gkg"][1]) == datetime(2026, 11, 16, 16, 30, tzinfo=timezone.utc)


def test_fixture_rows_have_the_gkg_21_layout():
    rows = fixture_bytes("gdelt_gkg_trimmed.csv").decode().splitlines()
    assert len(rows) == 5 and all(len(r.split("\t")) == gdelt.GKG_COLUMNS for r in rows)


def test_gkg_rows_matched_on_themes_urls_orgs_and_locations():
    notes = {}
    events = gdelt.read_gkg_zip(gkg_zip(), matchers(), notes)
    assert notes["rows_scanned"] == 5
    by_id = {e.attrs["gkg_id"]: e for e in events}
    assert set(by_id) == {
        "20190301001500-201", "20190301001500-910", "20190301001500-1825", "20190301001500-2133",
    }  # row -0 (a press release about sinks) matches nothing

    engines = by_id["20190301001500-201"]
    assert engines.url == "https://atwonline.com/manufacturers/irkut-build-fifth-mc-21-prototype-test-russian-engines"
    assert engines.raw_id == gdelt.url_id(engines.url)  # same id as the DOC API path
    assert engines.published_at == datetime(2019, 3, 1, 0, 15, tzinfo=timezone.utc)
    assert engines.attrs == {
        "query": '("russian engines" OR "engine co")', "domain": "atwonline.com", "language": "English",
        "tone": -1.21, "feed": "gkg", "gkg_id": "20190301001500-201",
    }
    assert engines.countries == ("RUS",)
    assert engines.title == ""  # rows before Sept 2019 carry no <PAGE_TITLE>

    wafa = by_id["20190301001500-1825"]
    assert wafa.attrs["query"] == '(blockade OR "gaza strip")'
    assert wafa.countries[:2] == ("ISR", "PSE")  # FIPS IS, GZ/WE -> ISO alpha-3, first mention first
    assert by_id["20190301001500-910"].attrs["query"] == "theme:CYBER_ATTACK"


def test_default_watch_terms_do_not_match_unrelated_rows():
    defaults = [t for t in DEFAULT_TERMS if t.kind == "gdelt_query"]
    assert all(m.supported for m in matchers(defaults))
    assert gdelt.read_gkg_zip(gkg_zip(), matchers(defaults), {}) == []


THEME_TERMS = [t for t in DEFAULT_TERMS if t.kind == "gdelt_query" and "theme:" in t.term]


def test_default_theme_terms_on_real_rows():
    """Seven real GKG rows (Apr 2019, AWS mirror): one per theme term below, a phrase hit, a miss."""
    notes = {}
    zipped = gkg_zip("20190410120000.gkg.csv", "gdelt_gkg_themes.csv")
    defaults = [t for t in DEFAULT_TERMS if t.kind == "gdelt_query"]
    events = {e.attrs["gkg_id"]: e for e in gdelt.read_gkg_zip(zipped, matchers(defaults), notes)}
    assert notes["rows_scanned"] == 7 and notes["matched_on_themes"] == 5
    expected = {
        "20190410180000-43": (0, ["CYBER_ATTACK", "SOC_RANSOM", "WB_2457_CYBER_CRIME"]),
        "20190410180000-2132": (1, ["MARITIME_INCIDENT", "MARITIME_PIRACY"]),
        "20190410120000-1727": (2, ["WB_167_PORTS", "STRIKE"]),  # Nigerian workers' strike
        "20190410121500-1923": (5, ["NATURAL_DISASTER_CYCLONE", "WB_167_PORTS"]),  # Cyclone Idai, Beira
        "20190410120000-1257": (6, ["ARMEDCONFLICT", "MARITIME_INCIDENT", "CRISISLEX_C04_LOGISTICS_TRANSPORT"]),
    }
    for gkg_id, (i, themes) in expected.items():
        ev = events[gkg_id]
        assert ev.attrs["query"] == GDELT_THEME_QUERIES[i] and ev.attrs["themes"] == themes
        assert ev.type == "news" and ev.countries  # typed by GLiNER2 later, like any article
    phrase = events["20190411030000-215"]  # Yahoo data-breach settlement: a phrase default
    assert phrase.attrs["query"].startswith('("ransomware attack"') and "themes" not in phrase.attrs
    assert "20190410121500-148" not in events and len(events) == 6


def test_theme_codes_are_in_gdelts_published_list():
    """Every ``theme:`` code in the defaults is in LOOKUP-GKGTHEMES.TXT (the used lines are saved)."""
    listed = {}
    for line in fixture_bytes("gdelt_gkg_themes_used.txt").decode().splitlines():
        code, count = line.split("\t")
        listed[code] = int(count)
    used = {t.value for term in THEME_TERMS for g in gdelt.parse_query(term.term).groups for t in g}
    assert used == set(listed) and len(listed) == 33 and all(n > 0 for n in listed.values())
    assert len(THEME_TERMS) == len(GDELT_THEME_QUERIES) == 10
    for term in THEME_TERMS:
        m = gdelt.parse_query(term.term)
        assert m.supported and all(t.kind == "theme" for g in m.groups for t in g)
        assert len(m.groups) >= 2  # never one broad theme on its own
        assert not news.supported(term.term)  # NewsData / Currents skip them


def test_theme_terms_and_or_negate():
    hay = gdelt.normalise("Port workers walk out")
    themes = frozenset({"WB_167_PORTS", "STRIKE", "TAX_FNCACT_WORKERS"})

    def m(q):
        return gdelt.parse_query(q)

    assert m("theme:WB_167_PORTS (theme:STRIKE OR theme:CLOSURE)").matches(hay, themes)
    assert not m("theme:WB_167_PORTS (theme:BLOCKADE OR theme:CLOSURE)").matches(hay, themes)
    assert m("theme:wb_167_ports theme:strike").matches(hay, themes)  # codes are case-insensitive
    assert not m("theme:STRIKE -theme:TAX_FNCACT_WORKERS").matches(hay, themes)
    assert not m("theme:STRIKE").matches(hay, frozenset({"STRIKES"}))  # exact codes, no prefixes
    q = m("theme:STRIKE (theme:CLOSURE OR theme:TAX_FNCACT_WORKERS) -theme:BLOCKADE")
    assert gdelt.matched_themes(q, themes) == ["STRIKE", "TAX_FNCACT_WORKERS"]


def test_volume_stays_under_the_run_cap(monkeypatch):
    """A run keeps at most MAX_EVENTS articles, whatever the terms match."""
    monkeypatch.setattr(gdelt, "MAX_EVENTS", 3)
    feed = Feed()
    feed.zip = lambda: gkg_zip("20190410120000.gkg.csv", "gdelt_gkg_themes.csv")
    defaults = [t for t in DEFAULT_TERMS if t.kind == "gdelt_query"]
    result = run(feed, terms=defaults)
    # 3 windows x 6 matches = 18, but each URL is one row: 6 unique, cut to 3.
    assert len(result.events) == 3 and result.notes["dropped_over_cap"] == 3 and result.notes["matched"] == 3


def test_query_syntax():
    hay = gdelt.normalise("Dockworkers strike closes Port of Antwerp | port-of-antwerp-strike | ECON STRIKE")
    themes = frozenset({"STRIKE", "MARITIME"})

    def hit(q):
        return gdelt.parse_query(q).matches(hay, themes)

    assert hit('("port strike" OR "dockworkers strike")')
    assert hit("antwerp strike")  # space = AND
    assert not hit("antwerp rotterdam")
    assert hit("theme:maritime") and not hit("theme:CYBER_ATTACK")
    assert not hit('"dockworkers strike" -antwerp')
    assert not hit("port strikes")  # whole words only
    assert not gdelt.parse_query("sourcecountry:france strike").supported


def test_page_title_and_language():
    extras = "<PAGE_LINKS>x</PAGE_LINKS><PAGE_TITLE>Grève au port d&#x27;Anvers &amp; ailleurs</PAGE_TITLE>"
    assert gdelt.page_title(extras) == "Grève au port d'Anvers & ailleurs"
    assert gdelt.page_title("<PAGE_AUTHORS>A</PAGE_AUTHORS>") == ""
    assert gdelt.language_of("") == "English"
    assert gdelt.language_of("srclc:fra;eng:GT-FRA 1.0") == "French"


def test_doc_query_ors_every_term():
    defaults = [t for t in DEFAULT_TERMS if t.kind == "gdelt_query"]
    q, skipped = gdelt.doc_api_query(matchers(defaults + [WatchTerm("gdelt_query", "theme:CYBER_ATTACK")]))
    assert skipped == len(GDELT_THEME_QUERIES)  # AND-ed theme terms cannot be ORed into one DOC query
    assert q.startswith('("port strike" OR "dockworkers strike" OR ') and q.endswith(" OR theme:CYBER_ATTACK)")
    assert q.count(" OR ") == 23  # 23 phrases from the 8 default terms, plus the theme
    assert gdelt.doc_api_query(matchers([WatchTerm("gdelt_query", "a (b OR c)")])) == (None, 1)


def test_fips_table_agrees_with_gdelts_own_country_names():
    """For every country-level location in the saved rows, GDELT's name resolves to the same code."""
    seen = 0
    for row in fixture_bytes("gdelt_gkg_trimmed.csv").decode().splitlines():
        for block in row.split("\t")[gdelt.GKG_V1LOCATIONS].split(";"):
            p = block.split("#")
            if len(p) > 2 and p[0] == "1" and codes.country_alpha3(p[1]):
                assert codes.fips_alpha3(p[2]) == codes.country_alpha3(p[1]), p[:3]
                seen += 1
    assert seen >= 5
    assert codes.fips_alpha3("GM") == "DEU" and codes.fips_alpha3("UK") == "GBR" and codes.fips_alpha3("RS") == "RUS"
    assert codes.fips_alpha3("KV") is None  # Kosovo: no ISO 3166-1 code, so no guess
    assert len(codes.fips_table()) > 240


# --- the connector ------------------------------------------------------------------


def test_first_run_reads_the_newest_windows():
    feed = Feed()
    result = run(feed)
    assert result.status == Status.OK
    assert feed.file_urls == [
        "http://data.gdeltproject.org/gdeltv2/20261116163000.gkg.csv.zip",
        "http://data.gdeltproject.org/gdeltv2/20261116161500.gkg.csv.zip",
        "http://data.gdeltproject.org/gdeltv2/20261116160000.gkg.csv.zip",
    ]
    assert len(result.events) == 4  # the same rows in every window: one event per URL
    assert result.notes["gkg_window"] == LATEST and result.notes["windows_read"] == 3
    assert not any("api.gdeltproject.org" in u for u in feed.urls)


def test_later_run_reads_only_new_windows():
    feed = Feed()
    result = run(feed, state={"gdelt": {"gkg_window": 20261116161500}})
    assert feed.file_urls == ["http://data.gdeltproject.org/gdeltv2/20261116163000.gkg.csv.zip"]
    assert result.status == Status.OK and result.notes["gkg_window"] == LATEST


def test_nothing_new_is_empty_and_keeps_the_window():
    feed = Feed()
    result = run(feed, state={"gdelt": {"gkg_window": LATEST}})
    assert result.status == Status.EMPTY and feed.file_urls == []
    assert result.notes["gkg_window"] == LATEST


def test_missing_older_window_is_partial():
    feed = Feed(files=lambda url: 404 if "161500" in url else 200)
    result = run(feed)
    assert result.status == Status.PARTIAL and result.error == "HTTP 404 from data.gdeltproject.org"
    assert len(result.events) == 4 and result.notes["windows_failed"] == 1


def test_size_cap(monkeypatch):
    monkeypatch.setattr(gdelt, "MAX_ZIP_BYTES", 1000)
    result = run(Feed(doc=200))
    assert result.status == Status.PARTIAL and "size cap" in result.error  # fell back to the DOC API


def test_record_cap(monkeypatch):
    monkeypatch.setattr(gdelt, "MAX_EVENTS", 2)
    result = run(Feed())
    assert len(result.events) == 2 and result.notes["dropped_over_cap"] == 2


def test_file_feed_down_falls_back_to_one_doc_call():
    feed = Feed(lastupdate=httpx.ConnectError("refused"))
    result = run(feed, state={"gdelt": {"gkg_window": 20261116161500}})
    doc_calls = [u for u in feed.urls if "api.gdeltproject.org" in u]
    assert len(doc_calls) == 1
    assert result.status == Status.PARTIAL
    assert result.error == "file feed: ConnectError contacting data.gdeltproject.org; used the DOC API"
    assert [e.attrs["feed"] for e in result.events] == ["doc"]
    assert result.notes["gkg_window"] == 20261116161500  # carried forward for the next run


def test_doc_fallback_stops_on_first_429():
    slept = []
    feed = Feed(lastupdate=503, doc=429)
    ctx = RunContext(now=NOW, watch_terms=list(TERMS), sleep=slept.append)
    t0 = time.monotonic()
    result = gdelt.GdeltConnector().run(client_for(feed), ctx)
    assert result.status == Status.THROTTLED and result.events == []
    assert result.error == "file feed: HTTP 503 from data.gdeltproject.org; DOC API: HTTP 429 from api.gdeltproject.org"
    assert len([u for u in feed.urls if "api.gdeltproject.org" in u]) == 1 and slept == []
    assert time.monotonic() - t0 < 5


def test_time_budget_drops_the_oldest_windows():
    ticks = iter(range(0, 1000, 10))  # every clock read advances 10 s
    result = run(Feed(), clock=lambda: next(ticks), budget=25)
    assert result.status == Status.PARTIAL and result.notes["windows_not_run"] >= 1
    assert result.notes["gkg_window"] == LATEST  # the newest window was read first


def test_without_terms_is_skipped():
    assert run(Feed(), terms=[]).status == Status.SKIPPED


@pytest.mark.parametrize("bad", [b"not a zip", b""])
def test_bad_zip_is_a_source_error(bad):
    with pytest.raises(gdelt.SourceError):
        gdelt.read_gkg_zip(bad, matchers(), {})
