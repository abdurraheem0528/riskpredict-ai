"""City positions (GeoNames cities15000) and port display names (engine/places.py).

The suppliers are the fictional rows of the live test's sheet; test data only.
"""

from __future__ import annotations

import gzip
import json
from pathlib import Path

import pytest

pytest.importorskip("rapidfuzz")  # the repository-wide job does not install the engine's dependencies

from api.core import mapping  # noqa: E402
from api.core import serialize as ser  # noqa: E402
from engine import FeedSnapshot, assess, places  # noqa: E402
from engine.geo import haversine_km  # noqa: E402
from engine_fixtures import NOW, ent, quake  # noqa: E402


ROOT = Path(__file__).resolve().parent.parent
HEADER = "Supplier ID,Supplier,Country,City,Main port,Products,HS code,Currency,Annual spend (USD),Criticality,Notes"
SHEET = [
    "SUP-001,Formosa Microcircuits,Taiwan,Hsinchu,TWKHH,Microcontrollers;power ICs,8542,TWD,4200000,High,",
    "SUP-002,Pearl River PCB Works,China,Shenzhen,CNYTN,Printed circuit boards,8534,CNY,1800000,Medium,",
    "SUP-003,Rhein Sensorik GmbH,Germany,Dortmund,DEHAM,Sensors,9031,EUR,1200000,Medium,",
    "SUP-004,Hanbit Display,South Korea,Gumi,KRPUS,Display panels,8524,KRW,3100000,High,",
    "SUP-005,Penang Semicon Assembly,Malaysia,Bayan Lepas,MYPEN,Chip packaging,8542,MYR,2600000,High,",
]


def apply(lines=SHEET, header=HEADER):
    cols = header.split(",")
    raw = [mapping.RawRow(i + 2, None, dict(zip(cols, line.split(",")))) for i, line in enumerate(lines)]
    spec = mapping.heuristic_suggest([{"name": h, "samples": []} for h in cols])
    return mapping.apply(spec, raw, chokepoints=lambda v: None)


# --- the gazetteer ------------------------------------------------------------------------------


@pytest.mark.parametrize("city, country, lat, lon", [
    ("Hsinchu", "TWN", 24.80, 120.97), ("Dortmund", "DEU", 51.51, 7.47), ("Gumi", "KOR", 36.11, 128.34),
    ("Bayan Lepas", "MYS", 5.30, 100.26), ("Shenzhen", "CHN", 22.55, 114.07),
])  # fmt: skip
def test_the_live_test_cities_resolve_to_the_city(city, country, lat, lon):
    c = places.city_position(city, [country])
    assert c is not None and c.name == city
    assert haversine_km(c.lat, c.lon, lat, lon) < 2


def test_a_city_is_matched_only_within_its_countries():
    assert places.city_position("Hsinchu", ["JPN"]) is None
    assert places.city_position("Dortmund", ["AUT", "CHE"]) is None
    assert places.city_position("Dortmund", ["AUT", "DEU"]).country == "DE"  # in order, the first that has it
    assert places.city_position("Hsinchu", []) is None
    assert places.city_position("", ["TWN"]) is None
    assert places.city_position("Atlantis", ["GRC"]) is None


def test_names_are_matched_as_keys_and_on_latin_alternates():
    assert places.city_position("hsinchu city", ["TWN"]).name == "Hsinchu"
    assert places.city_position("Hsin-chu", ["TW"]).name == "Hsinchu"
    assert places.city_position("München", ["DEU"]).name == "Munich"
    assert places.city_position("Saigon", ["VNM"]).name == "Ho Chi Minh City"
    assert places.city_position("Bayan Lepas, Penang", ["MYS"]).name == "Bayan Lepas"


def test_a_citys_own_name_beats_a_larger_citys_alternate_name():
    # "Gumi" is also an alternate spelling elsewhere; the city called Gumi wins in Korea,
    # and among cities of the same name the larger one wins.
    assert places.city_position("Gumi", ["KOR"]).population > 400_000
    assert places.city_position("Kawasaki", ["JPN"]).population > 1_000_000


def test_the_gazetteer_file_is_small_and_attributed():
    path = ROOT / "engine/data/cities15000.tsv.gz"
    assert path.stat().st_size < 2_000_000
    with gzip.open(path, "rt", encoding="utf-8") as fh:
        text = fh.read()
    assert "GeoNames" in text.splitlines()[0] and "CC BY 4.0" in text.splitlines()[0]
    rows = [ln for ln in text.splitlines() if ln and not ln.startswith("#")]
    assert len(rows) == 34006
    assert all(len(r.split("\t")) == 6 for r in rows)


# --- placement at sync time -------------------------------------------------------------------


def test_rows_are_placed_at_their_city_and_keep_their_port():
    out = {e.name: e for e in apply().entities}
    expect = {
        "Formosa Microcircuits": ("Hsinchu", ["TWKHH"]), "Pearl River PCB Works": ("Shenzhen", ["CNYTN"]),
        "Rhein Sensorik GmbH": ("Dortmund", ["DEHAM"]), "Hanbit Display": ("Gumi", ["KRPUS"]),
        "Penang Semicon Assembly": ("Bayan Lepas", ["MYPEN"]),
    }  # fmt: skip
    for name, (city, ports) in expect.items():
        e = out[name]
        c = places.city_position(city, e.countries)
        assert (e.lat, e.lon) == (c.lat, c.lon), name
        assert e.ports == ports and e.city == city
    f = out["Formosa Microcircuits"]
    assert haversine_km(f.lat, f.lon, 22.617, 120.25) > 200  # not at the port of Kaohsiung


def test_the_rows_own_position_wins_and_an_unknown_city_stays_unplaced():
    header = "Supplier,Country,City,Main port,Latitude,Longitude"
    res = apply(["Own,Taiwan,Hsinchu,TWKHH,25.0,121.5", "Nowhere,Taiwan,Atlantis,TWKHH,,",
                 "Wrong country,Japan,Hsinchu,JPTYO,,", "No country,,Hsinchu,TWKHH,,"], header)  # fmt: skip
    out = {e.name: e for e in res.entities}
    assert (out["Own"].lat, out["Own"].lon) == (25.0, 121.5)
    assert out["Nowhere"].lat is None and out["Wrong country"].lat is None
    assert out["No country"].lat == pytest.approx(24.8036)  # the port's country, when the row names none


def test_hazard_radius_measures_from_the_city():
    e = next(x for x in apply().entities if x.name == "Formosa Microcircuits")
    formosa = ent("formosa", countries=e.countries, ports=e.ports, lat=e.lat, lon=e.lon, hs_codes=e.hs_codes)
    near_city = quake("q-hsinchu", 24.85, 121.05, magnitude=6.0, countries=["TWN"])
    near_port = quake("q-kaohsiung", 22.55, 120.2, magnitude=6.0, countries=["TWN"])
    hit = assess([formosa], FeedSnapshot(now=NOW, events=(near_city,), anomalies=()), base_currency=None)
    assert len(hit) == 1 and hit[0].evidence[0].match_type == "geo_radius"
    assert int(hit[0].evidence[0].match_value.split()[0]) < 15  # "10 km": measured from Hsinchu
    assert hit[0].title == "Earthquake near formosa"
    miss = assess([formosa], FeedSnapshot(now=NOW, events=(near_port,), anomalies=()), base_currency=None)
    assert miss == []  # 250 km from Hsinchu: outside the quake's radius, and position beats country


# --- port names -------------------------------------------------------------------------------


def test_port_names_follow_the_dashboards_rule():
    # The same five as web/tests/unit/lib.test.ts, from the same ports.json.
    codes = ["TWKHH", "MYPEN", "AEJEA", "CLSAI", "VNHPH"]
    assert [places.port_name(c) for c in codes] == ["Kaohsiung", "Penang", "Jebel Ali", "San Antonio", "Haiphong"]
    raw = ["Tokyo Ko", "Kao-Hsiung", "Chennai (Madras)", "Chennai (ex Madras)", "Penang (Georgetown)", "Yantian Pt",
           "Tegal, Java", "Hualien Hsien", "Busan"]  # fmt: skip
    assert [places.clean_port_name(r) for r in raw] == [
        "Tokyo", "Kao-Hsiung", "Chennai", "Chennai", "Penang", "Yantian", "Tegal", "Hualien", "Busan"]  # fmt: skip
    assert places.port_name("XXXXX") is None and places.port_label("XXXXX") == "XXXXX"


def test_the_port_table_matches_the_dashboards():
    web = ROOT / "web/public/map/ports.json"
    if not web.exists():  # pragma: no cover - a checkout without web/
        pytest.skip("no web/public/map/ports.json")
    ports = json.loads(web.read_text(encoding="utf-8"))
    assert set(places._ports()) == set(ports)
    for code, p in ports.items():
        assert places.port_name(code) == places.clean_port_name(p[3] if len(p) > 3 and p[3] else p[2]), code


def test_place_lines():
    assert places.place_label("Hsinchu", ["TWN"], ["TWKHH"]) == "Hsinchu, Taiwan"
    assert places.place_label(None, ["TWN"], ["TWKHH"]) == "Kaohsiung, Taiwan"
    assert places.place_label(None, ["MYS"], ["MYPEN"]) == "Penang, Malaysia"
    assert places.place_label(None, ["TWN", "JPN", "CHN"], []) == "Taiwan and Japan +1"
    assert places.place_label("Bayan Lepas", [], ["MYPEN"]) == "Bayan Lepas, Malaysia"
    assert places.place_label(None, [], []) is None


def test_the_api_serves_the_place_lines():
    row = {"city": "Hsinchu", "countries": ["TWN"], "ports": ["TWKHH"]}
    assert ser.entity_place(row) == "Hsinchu, Taiwan"
    risk = {"entity_city": None, "countries": ["ARE"], "ports": ["AEJEA"]}
    assert ser.risk_place(risk) == "Jebel Ali, United Arab Emirates"
    risk = {"entity_city": "Gumi", "entity_countries": ["KOR"], "entity_ports": ["KRPUS"], "countries": ["KOR"],
            "ports": ["KRPUS"]}  # fmt: skip
    assert ser.risk_place(risk) == "Gumi, South Korea"


def test_titles_name_the_port():
    from engine_fixtures import event

    sup = ent("pearl", countries=["CHN"], ports=["CNYTN"])
    ev = event("portwatch", "pd-1", "port.disruption", countries=["CHN"], ports=["CNYTN"], severity=0.7)
    out = assess([sup], FeedSnapshot(now=NOW, events=(ev,), anomalies=()), base_currency=None)
    assert out and "the port of Yantian" in out[0].title
    assert "the port of Yantian" in out[0].explanation
    assert out[0].evidence[0].match_value == "CNYTN"
