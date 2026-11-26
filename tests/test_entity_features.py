"""Route features on /v1/map/entities carry their declared chokepoints (api/core/serialize.py).

The dashboard draws a route's sea lane through them in this order, so the order is the route's
own, each chokepoint once, as ids (a name or alias is resolved), and unknown values are left out.
"""

from __future__ import annotations

import uuid

from api.core import serialize as ser


def row(kind: str, attrs: dict | None) -> dict:
    return {
        "id": uuid.uuid4(), "kind": kind, "name": "Test lane", "countries": ["CHN", "NLD"], "ports": ["CNYTN", "NLRTM"],
        "city": None, "criticality": None, "open_risk_count": 0, "max_score": None, "lat": None, "lon": None,
        "attrs": attrs,
    }  # fmt: skip


def test_a_route_lists_its_chokepoints_in_its_own_order():
    f = ser.entity_feature(row("route", {"chokepoints": ["suez", "bab_el_mandeb"]}))
    assert f["properties"]["chokepoints"] == ["suez", "bab_el_mandeb"]
    f = ser.entity_feature(row("route", {"chokepoints": ["bab_el_mandeb", "suez"]}))
    assert f["properties"]["chokepoints"] == ["bab_el_mandeb", "suez"]


def test_names_and_aliases_resolve_to_ids_once_and_unknown_values_drop():
    attrs = {"chokepoints": ["Bab el-Mandeb", "Suez Canal", "suez", "Atlantis", "", None]}
    assert ser.entity_feature(row("route", attrs))["properties"]["chokepoints"] == ["bab_el_mandeb", "suez"]


def test_a_route_without_chokepoints_has_an_empty_list():
    assert ser.entity_feature(row("route", None))["properties"]["chokepoints"] == []
    assert ser.entity_feature(row("route", {"chokepoints": "suez"}))["properties"]["chokepoints"] == []


def test_other_kinds_carry_none():
    for kind in ("supplier", "site", "port", "customer"):
        assert "chokepoints" not in ser.entity_feature(row(kind, {"chokepoints": ["suez"]}))["properties"]
