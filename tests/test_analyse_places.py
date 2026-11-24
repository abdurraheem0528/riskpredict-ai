"""Exact place-name → ISO 3166-1 alpha-3 matching from the vendored table."""

import pycountry
import pytest

from jobs.analyse import places


@pytest.mark.parametrize(
    "place, code",
    [
        ("China", "CHN"),
        ("china", "CHN"),
        ("  Germany ", "DEU"),
        ("Russian Federation", "RUS"),  # ISO short name
        ("Russia", "RUS"),  # alias
        ("U.S.", "USA"),
        ("the U.S.", "USA"),
        ("United States", "USA"),
        ("UK", "GBR"),
        ("UAE", "ARE"),
        ("the Netherlands", "NLD"),
        ("Côte d'Ivoire", "CIV"),
        ("Cote d’Ivoire", "CIV"),
        ("Türkiye", "TUR"),
        ("Republic of Korea", "KOR"),
        ("Taiwan", "TWN"),
        ("Gaza", "PSE"),
        ("St. Lucia", "LCA"),
        ("Guinea-Bissau", "GNB"),
        ("Democratic Republic of the Congo", "COD"),
    ],
)
def test_exact_names_and_aliases(place, code):
    assert places.country_code(place) == code


@pytest.mark.parametrize(
    "place",
    [
        None,
        "",
        "Texas",  # a state, not a country
        "Port of Rotterdam",
        "Red Sea",
        "Strait of Hormuz",
        "Shanghai",
        "Chinese",  # demonym
        "China Sea",  # no substring matching
        "Germany's",
        "Georgia",  # country or US state: refused
        "Congo",  # two countries: refused
        "Korea",  # not an exact name
        "Holland",  # a region of the Netherlands
        "America",
        "IN",  # bare codes are not accepted unless listed as an alias
        "Europe",
    ],
)
def test_no_guessing(place):
    assert places.country_code(place) is None


def test_table_codes_are_iso_and_cover_every_country():
    table = places.table()
    codes = set(table.values())
    assert codes <= {c.alpha_3 for c in pycountry.countries}
    for c in pycountry.countries:
        assert table[places.normalise(c.name)] == c.alpha_3, c.name


def test_normalise():
    assert places.normalise("The  U.S.A.") == "usa"
    assert places.normalise("Bosnia-Herzegovina") == "bosnia herzegovina"
    assert places.normalise("São Tomé and Príncipe") == "sao tome and principe"
