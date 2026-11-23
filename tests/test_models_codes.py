from datetime import datetime, timezone

import pytest

from jobs import codes
from jobs.models import Event, SeriesPoint

NOW = datetime(2026, 11, 23, tzinfo=timezone.utc)


def test_event_normalises_and_dedupes():
    ev = Event(
        source="s",
        raw_id=1,
        type="t",
        title="  x ",
        published_at=NOW,
        countries=["IRN", "IRN", ""],
        software=("a:b", "a:b"),
    )
    assert ev.raw_id == "1" and ev.title == "x"
    assert ev.countries == ("IRN",) and ev.software == ("a:b",)


@pytest.mark.parametrize(
    "kwargs",
    [
        {"countries": ["IR"]},  # alpha-2 is not accepted
        {"countries": ["XKX"]},  # Kosovo has no ISO 3166-1 code
        {"ports": ["JP TRG"]},  # LOCODEs are stored without the space
        {"currencies": ["XBT"]},  # not ISO 4217
        {"severity": 1.5},
        {"severity": float("nan")},
        {"published_at": datetime(2026, 1, 1)},  # naive datetime
        {"lat": 10.0},  # lat without lon
    ],
)
def test_event_rejects_invalid(kwargs):
    with pytest.raises((ValueError, TypeError)):
        Event(source="s", raw_id="1", type="t", title="x", **kwargs)


def test_series_point_rejects_nan():
    with pytest.raises(ValueError):
        SeriesPoint(source="s", series="a", key="b", ts=NOW, value=float("nan"))


@pytest.mark.parametrize(
    "name,expected",
    [
        # spellings seen in the live OFAC / PortWatch / USGS / GDACS data
        ("Russia", "RUS"),
        ("Korea, North", "PRK"),
        ("Burma", "MMR"),
        ("Congo, Democratic Republic of the", "COD"),
        ("Cote d Ivoire", "CIV"),
        ("Taiwan Province of China", "TWN"),
        ("Timor Leste", "TLS"),
        ("St.  Vincent and the Grenadines", "VCT"),
        ("São Tomé and Príncipe", "STP"),
        ("Türkiye", "TUR"),
        ("United Arab Emirates", "ARE"),
        ("IDN", "IDN"),
        ("ID", "IDN"),
        ("Kosovo", None),
        ("Region: Somewhere", None),
        ("", None),
    ],
)
def test_country_alpha3(name, expected):
    assert codes.country_alpha3(name) == expected


def test_locode():
    assert codes.locode("JP TRG") == "JPTRG"
    assert codes.locode("mzpol") == "MZPOL"
    assert codes.locode("XX ABC") is None  # XX is not a country
    assert codes.locode("JP T") is None
    assert codes.locode(None) is None
