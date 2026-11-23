"""Each connector's parser against a real, saved response."""

from datetime import datetime, timezone

import pytest
from conftest import fixture_bytes, fixture_json

from jobs.connectors import cisa_kev, eonet, epss, frankfurter, gdacs, gdelt, ofac, osv, portwatch, usgs, worldbank
from jobs.http import SourceError, Throttled


def test_gdelt_articles():
    events = gdelt.parse_articles(fixture_json("gdelt_artlist.json"), "q1")
    assert len(events) == 1
    ev = events[0]
    assert ev.source == "gdelt" and ev.type == "news"
    assert ev.url == "https://newsonjapan.com/article/151486.php"
    assert ev.published_at == datetime(2026, 11, 23, 3, 30, tzinfo=timezone.utc)
    # publisher country is metadata, not the event location
    assert ev.countries == () and ev.attrs["sourcecountry"] == "Japan"
    assert len(ev.raw_id) == 32


def test_gdelt_throttle_text_is_recognised():
    body = fixture_bytes("gdelt_throttle.txt").decode()
    assert gdelt.is_throttle_text(body)
    with pytest.raises(Throttled):
        gdelt.decode(body)
    assert gdelt.decode("") == {}
    with pytest.raises(SourceError):
        gdelt.decode("<html>oops</html>")


def test_cisa_kev():
    events = cisa_kev.parse(fixture_json("cisa_kev_trimmed.json"))
    assert len(events) == 5
    by_id = {e.raw_id: e for e in events}
    cisco = by_id["CVE-2026-76466"]
    assert cisco.type == "cyber.exploited_vulnerability"
    assert cisco.software == ("Cisco:Expressway Series",)
    assert cisco.published_at == datetime(2026, 11, 20, tzinfo=timezone.utc)
    assert cisco.severity is None  # KEV publishes no score
    assert cisco.attrs["catalog_version"] == "2026.11.23"
    assert by_id["CVE-2021-27104"].attrs["known_ransomware_use"] == "Known"


def test_epss_threshold_and_metadata():
    text = fixture_bytes("epss_scores_trimmed.csv").decode()
    notes: dict = {}
    points = epss.parse_csv(text, 0.5, notes)
    assert [p.key for p in points] == ["CVE-1999-0016", "CVE-1999-0046", "CVE-1999-0067"]
    assert points[0].value == pytest.approx(0.96429)
    assert points[0].attrs == {"percentile": 0.99882, "model_version": "v2026.06.15"}
    assert points[0].ts == datetime(2026, 11, 23, 12, 0, 23, tzinfo=timezone.utc)
    assert notes["rows_in_file"] == 7
    assert len(epss.parse_csv(text, 0.0)) == 7
    with pytest.raises(SourceError):
        epss.parse_csv("cve,epss,percentile\n", 0.5)


def test_osv_with_cvss_severity():
    events = osv.parse(fixture_json("osv_jinja2_trimmed.json"), "PyPI", "jinja2")
    by_id = {e.raw_id: e for e in events}
    assert set(by_id) == {"GHSA-462w-v97r-4m45", "GHSA-cpwx-vrp4-4pq7", "GHSA-h5c8-rqwp-cp95", "PYSEC-2014-8"}
    sandbox = by_id["GHSA-462w-v97r-4m45"]
    # CVSS:3.0/AV:N/AC:L/PR:N/UI:N/S:C/C:H/I:N/A:N has base score 8.6
    assert sandbox.severity == pytest.approx(0.86)
    assert sandbox.software == ("PyPI:jinja2",)
    assert sandbox.url == "https://osv.dev/vulnerability/GHSA-462w-v97r-4m45"
    assert "CVE-2019-10906" in sandbox.attrs["aliases"]
    # only a CVSS v4 vector: CVSS:4.0/AV:L/AC:L/AT:P/PR:L/UI:P/VC:H/VI:H/VA:H/... = 5.4
    assert by_id["GHSA-cpwx-vrp4-4pq7"].severity == pytest.approx(0.54)
    # no vector published -> no severity, and the id stands in for the missing summary
    assert by_id["PYSEC-2014-8"].severity is None
    assert by_id["PYSEC-2014-8"].title == "PYSEC-2014-8"


def _ports_index():
    rows = [f["attributes"] for n in (1, 2) for f in fixture_json(f"portwatch_ports_page{n}.json")["features"]]
    return portwatch.PortIndex(rows)


def test_portwatch_disruptions():
    index = _ports_index()
    assert index.locode == {"port137": "MZBEW", "port784": "MZMNC", "port896": "MZPOL", "port769": "KMYVA"}
    rows = [f["attributes"] for f in fixture_json("portwatch_disruptions.json")["features"]]
    events = {e.raw_id: e for e in portwatch.parse_disruptions(rows, index)}
    kenneth = events["TC-1000559"]
    assert kenneth.title == "KENNETH-19"
    assert kenneth.countries == ("MOZ", "COM")
    assert kenneth.ports == ("MZPOL", "KMYVA")
    assert kenneth.severity == 1.0  # RED
    assert kenneth.published_at == datetime(2019, 4, 23, tzinfo=timezone.utc)
    assert (kenneth.lat, kenneth.lon) == pytest.approx((-12.3, 40.1))
    assert events["FL-1000227"].ports == ("MZBEW", "MZMNC", "MZPOL")


def test_portwatch_trade_skips_aggregates():
    rows = [f["attributes"] for f in fixture_json("portwatch_trade.json")["features"]]
    assert {r["ISO3"] for r in rows} == {"MOZ", "COM", "WLD", "400"}
    points = portwatch.parse_trade(rows)
    assert {p.key for p in points} == {"MOZ", "COM"}
    moz = {p.series: p.value for p in points if p.key == "MOZ"}
    assert moz == {"portwatch.port_calls": 5, "portwatch.import_volume": 25308, "portwatch.export_volume": 55500}
    assert all(p.ts == datetime(2026, 11, 13, tzinfo=timezone.utc) for p in points)


def test_gdacs():
    events = gdacs.parse(fixture_json("gdacs_events4app_trimmed.json"))
    assert len(events) == 4
    flood = events[0]
    assert flood.raw_id == "FL-1104253-6" and flood.type == "hazard.flood"
    assert flood.countries == ("IDN",)
    assert flood.severity == pytest.approx(1 / 3)  # Green, alertscore 1
    assert flood.url == "https://www.gdacs.org/report.aspx?eventid=1104253&episodeid=6&eventtype=FL"
    assert (flood.lat, flood.lon) == (4.3685, 97.0253)
    quake = events[3]
    assert quake.type == "hazard.earthquake" and quake.countries == ()


def test_usgs():
    events = usgs.parse(fixture_json("usgs_4.5_day.geojson"))
    assert len(events) == 21
    first = events[0]
    assert first.raw_id == "us6000ucz2" and first.type == "hazard.earthquake"
    assert first.severity == pytest.approx(0.34)  # sig 340
    assert first.attrs["magnitude"] == 4.7 and first.attrs["depth_km"] == 103.295
    assert first.countries == ()  # "South Sandwich Islands region" names no country
    places = {e.attrs["place"]: e.countries for e in events}
    assert places["209 km SSE of Tobi Village, Palau"] == ("PLW",)
    assert places["52 km SW of Tyrma, Russia"] == ("RUS",)
    assert places["181 km NE of Lospalos, Timor Leste"] == ("TLS",)
    assert usgs.place_country("10 km N of Ridgecrest, CA") == "USA"


def test_eonet():
    events = eonet.parse(fixture_json("eonet_events.json"))
    assert len(events) == 5
    ice = events[0]
    assert ice.raw_id == "EONET_5359" and ice.type == "hazard.sea_lake_ice"
    assert ice.published_at == datetime(2021, 5, 28, tzinfo=timezone.utc)  # first observation
    assert ice.attrs["last_observed"] == "2026-11-20T00:00:00Z"
    assert (ice.lat, ice.lon) == (-57.66, -23.48)  # latest position
    fire = events[2]
    assert fire.type == "hazard.wildfire" and fire.attrs["magnitude_unit"] == "acres"
    assert all(e.severity is None and e.countries == () for e in events)


def test_frankfurter_latest_and_range():
    latest = frankfurter.parse(fixture_json("frankfurter_latest.json"))
    assert len(latest) == 29
    usd = next(p for p in latest if p.key == "EUR/USD")
    assert usd.series == "fx.rate" and usd.unit == "USD per EUR"
    assert usd.ts == datetime(2026, 11, 23, tzinfo=timezone.utc)
    rng = frankfurter.parse(fixture_json("frankfurter_range.json"))
    assert {p.ts.date().isoformat() for p in rng} == {"2026-11-19", "2026-11-20"}
    assert len(rng) == 58


def test_worldbank():
    page1 = fixture_json("worldbank_page1.json")
    assert page1[0]["pages"] == 67
    assert worldbank.parse(page1[1]) == []  # first page is all regional aggregates
    points = worldbank.parse(fixture_json("worldbank_last_page.json")[1], "% annual")
    assert points and all(len(p.key) == 3 for p in points)
    assert all(p.series == "wb.FP.CPI.TOTL.ZG" and p.unit == "% annual" for p in points)
    assert worldbank.period_start("2025") == datetime(2025, 1, 1, tzinfo=timezone.utc)
    assert worldbank.period_start("2024Q3") == datetime(2024, 7, 1, tzinfo=timezone.utc)
    assert worldbank.period_start("2024M11") == datetime(2024, 11, 1, tzinfo=timezone.utc)


def test_ofac_delta():
    events = ofac.parse(fixture_bytes("ofac_delta_trimmed.xml"))
    assert [e.raw_id for e in events] == ["59110:add:2026-11-23", "59120:add:2026-11-23", "59123:add:2026-11-23"]
    hnb = events[0]
    assert hnb.type == "sanction.designation"
    assert hnb.title == "OFAC designation: H N B TRADING CO LLC [IFSR, SDGT]"
    assert hnb.countries == ("ARE",)
    assert hnb.attrs["lists"] == ["SDN List"] and hnb.attrs["entity_type"] == "Entity"
    assert events[1].countries == ("IRN",)  # from "Nationality Country"
    # personal details are not carried over
    assert all("birth" not in str(e.attrs).lower() for e in events)
    with pytest.raises(SourceError):
        ofac.parse(b"<not-xml")
