"""Sanctions screening: name normalisation, token-sort similarity, aliases, the threshold and
the country adjustment (risk-engine.md §5.2 ``sanctions_name``). Names here are made up."""

from __future__ import annotations

from dataclasses import replace

import pytest

pytest.importorskip("rapidfuzz")

from engine import DEFAULT_CONFIG, assess  # noqa: E402
from engine.names import NameIndex, alias_names, normalise_name, similarity  # noqa: E402
from engine_fixtures import ent, event, ofac, snap  # noqa: E402


def screen(entities, events, config=DEFAULT_CONFIG):
    return [a for a in assess(entities, snap(events), base_currency=None, config=config) if a.category == "sanctions"]


@pytest.mark.parametrize(
    "raw, norm",
    [
        ("Acme Trading LLC", "acme trading"),
        ("ACME TRADING L.L.C.", "acme trading"),
        ("Müller & Söhne GmbH", "muller and sohne"),
        ("O'Neil Shipping Co., Ltd.", "oneil shipping"),
        ("Al-Noor General Trading FZE", "al noor general trading fze"),
        ("  OOO   Promsnab  ", "promsnab"),
        ("S.A.", "sa"),  # every token is a legal form: kept
        ("Ætna Straße", "aetna strasse"),
        ("", ""),
    ],
)
def test_normalise_name(raw, norm):
    assert normalise_name(raw) == norm


@pytest.mark.parametrize(
    "a, b, sim",
    [
        ("Acme Trading LLC", "ACME TRADING L.L.C.", 1.0),
        ("Trading Acme", "Acme Trading", 1.0),  # token order does not matter
        ("Acme Trading", "Acme Tradin", 0.957),
        ("Acme Trading", "Acma Trading", 0.917),
    ],
)
def test_similarity(a, b, sim):
    assert similarity(a, b) == pytest.approx(sim, abs=1e-3)


def test_exact_match_after_normalisation():
    sup = ent("acme", "supplier", name="Acme Trading LLC")
    [a] = screen([sup], [ofac("100:designation:2026-11-23", "ACME TRADING L.L.C.")])
    ev = a.evidence[0]
    assert ev.match_type == "sanctions_name" and ev.match_confidence == 1.0
    assert ev.snapshot["matched_name"] == "ACME TRADING L.L.C." and ev.snapshot["similarity"] == 1.0
    assert a.title == "Sanctions designation may match Acme Trading LLC"
    assert a.fingerprint.endswith("|sanctions|sanctions_name:acme trading")


def test_near_miss_typo_matches_with_lower_confidence():
    sup = ent("acme", "supplier", name="Acme Trading")
    [a] = screen([sup], [ofac("1", "Acme Tradin")])
    assert a.evidence[0].match_confidence == pytest.approx(0.7 + 0.3 * (0.9565 - 0.92) / 0.08, abs=2e-3)


@pytest.mark.parametrize(
    "ours, listed",
    [
        ("Acme Trading", "Acma Trading"),  # 0.917: one letter in a short name is below 0.92
        ("Global Shipping Ltd", "Global Shipping Lines"),  # extra word
        ("Northern Star Mining", "Southern Star Mining"),
        ("ABC", "ABD"),
        ("Sun", "Sunrise Holdings"),
        ("Acme Trading", "Acme Trading International Holdings"),
    ],
)
def test_false_positives_are_rejected(ours, listed):
    assert screen([ent("s", "supplier", name=ours)], [ofac("1", listed)]) == []


def test_short_names_are_never_screened():
    assert screen([ent("s", "supplier", name="AB Ltd")], [ofac("1", "AB")]) == []


def test_aliases_are_compared_in_any_shape():
    sup = ent("s", "supplier", name="Blue Lagoon Maritime")
    for aliases in (["Blue Lagoon Maritime Co"], [{"name": "BLUE LAGOON MARITIME"}], "Blue Lagoon Maritime S.A."):
        [a] = screen([sup], [ofac("1", "Ocean Pearl Shipping", aliases=aliases)])
        assert a.evidence[0].snapshot["matched_on"] == "alias"
        assert a.evidence[0].match_value == "Ocean Pearl Shipping"  # the listed primary name
    assert alias_names([{"full_name": "X Y"}, 3, None, {"other": "z"}]) == ["X Y"]


def test_country_overlap_and_conflict_adjust_confidence():
    listed = "Delta Petrochem Trading"
    near = "Delta Petrochem Tradng"  # sim ≈ 0.977 → c ≈ 0.914
    base = screen([ent("s", "supplier", name=near)], [ofac("1", listed)])[0].evidence[0].match_confidence
    same = screen([ent("s", "supplier", name=near, countries=["ARE"])], [ofac("1", listed, countries=["ARE"])])
    other = screen([ent("s", "supplier", name=near, countries=["DEU"])], [ofac("1", listed, countries=["IRN"])])
    one_sided = screen([ent("s", "supplier", name=near)], [ofac("1", listed, countries=["IRN"])])
    assert same[0].evidence[0].match_confidence == 1.0  # +0.1, clamped
    assert other[0].evidence[0].match_confidence == pytest.approx(base - 0.2, abs=1e-3)
    assert one_sided[0].evidence[0].match_confidence == base  # only one side has countries: no change


def test_weak_match_in_a_different_country_is_dropped():
    # sim ≈ 0.957 → c ≈ 0.84; −0.2 for disjoint countries → 0.64 (kept). At the threshold: 0.7 − 0.2 = 0.5 (kept),
    # just below the threshold nothing matches at all; a custom penalty shows the drop below 0.5.
    cfg = replace(DEFAULT_CONFIG, sanctions_country_penalty=0.4)
    out = screen([ent("s", "supplier", name="Acme Trading", countries=["DEU"])],
                 [ofac("1", "Acme Tradin", countries=["IRN"])], config=cfg)  # fmt: skip
    assert out == []


def test_customers_are_screened_but_sites_are_not():
    cust = ent("c", "customer", name="Acme Trading LLC")
    site = ent("p", "site", name="Acme Trading LLC")
    out = screen([cust, site], [ofac("1", "Acme Trading")])
    assert [a.entity_id for a in out] == [cust.id]


def test_update_signal_and_recency():
    sup = ent("s", "supplier", name="Acme Trading")
    [a] = screen([sup], [ofac("1", "Acme Trading", type_="sanction.update", age_h=720)])
    assert a.evidence[0].signal == 0.8 and a.evidence[0].recency == 0.5
    assert a.title == "Sanctions update may match Acme Trading"
    assert screen([sup], [ofac("1", "Acme Trading", age_h=24 * 121)]) == []  # 4 half-lives = 120 days


def test_full_list_entries_do_not_decay():
    sup = ent("s", "supplier", name="Acme Trading")
    old = 24 * 900  # designated years ago, still on the list
    for ev in (ofac("1", "Acme Trading", type_="sanction.listed", age_h=old),
               ofac("2", "Acme Trading", age_h=old, full_list=True)):  # fmt: skip
        [a] = screen([sup], [ev])
        assert a.evidence[0].recency == 1.0


def test_removal_cancels_earlier_designations_of_the_same_entity():
    sup = ent("s", "supplier", name="Acme Trading")
    des = ofac("77:designation:2026-11-01", "Acme Trading", entity_id="77", age_h=24 * 20)
    rem = event("ofac", "77:removal:2026-11-20", "sanction.removal", age_h=24 * 4,
                attrs={"entity_id": "77", "name": "Acme Trading"})  # fmt: skip
    assert screen([sup], [des, rem]) == []
    later = ofac("77:designation:2026-11-22", "Acme Trading", entity_id="77", age_h=24 * 2)  # re-designated
    assert len(screen([sup], [des, rem, later])) == 1
    other = ofac("78:designation:2026-11-01", "Acme Trading", entity_id="78", age_h=24 * 20)
    assert len(screen([sup], [other, rem])) == 1  # a removal of another OFAC entity changes nothing


def test_several_listings_of_one_name_are_one_risk():
    sup = ent("s", "supplier", name="Acme Trading")
    [a] = screen([sup], [ofac("1", "ACME TRADING"), ofac("2", "Acme Trading LLC", type_="sanction.update")])
    assert len(a.evidence) == 2


def test_name_index_search_order_is_stable():
    idx = NameIndex()
    for i, n in enumerate(["Acme Trading", "Acme Tradin", "ACME TRADING", "Zeta"]):
        idx.add(n, i)
    assert [r for r, _, _ in idx.search("acme trading", 0.92)] == [0, 2, 1]


# --- full lists: feeds.sanctions_entries (migration 030) ------------------------------------------


def entry(entity_id="36001", name="ACME TRADING L.L.C.", *, dataset="sdn", aliases=(), countries=(), programs=("SDGT",),
          first_seen_h=24.0 * 400, listed_on=None):  # fmt: skip
    """A ``feeds.sanctions_entries`` row in the column order engine.db reads it (test data)."""
    from datetime import timedelta

    from engine_fixtures import NOW

    return ("ofac_sdn", dataset, entity_id, "Entity", name, list(aliases), list(countries), list(programs),
            ["SDN List"], listed_on, NOW - timedelta(hours=first_seen_h))  # fmt: skip


def test_full_list_entry_is_a_standing_listing():
    from datetime import date

    from engine.db import entry_event

    ev = entry_event(entry(countries=["ARE"], listed_on=date(2019, 3, 1)))
    assert (ev.source, ev.raw_id, ev.type) == ("ofac_sdn", "sdn:36001", "sanction.listed")
    assert ev.key == "event:ofac_sdn:sdn:36001"
    assert ev.title == "OFAC SDN list: ACME TRADING L.L.C. [SDGT]" and ev.published_at is None
    assert ev.attrs["entity_id"] == "36001" and ev.attrs["full_list"] is True and ev.attrs["listed_on"] == "2019-03-01"
    sup = ent("acme", "supplier", name="Acme Trading LLC", countries=["ARE"])
    [a] = screen([sup], [ev])
    e = a.evidence[0]
    assert a.title == "Sanctions listing may match Acme Trading LLC"
    # Listed 400 days ago and still on the list: no decay.
    assert (e.event_source, e.event_raw_id, e.recency, e.signal) == ("ofac_sdn", "sdn:36001", 1.0, 1.0)
    assert e.snapshot["sanctions_entity_id"] == "36001" and e.snapshot["programs"] == ["SDGT"]
    # Same driver as a delta designation of the same name: one risk with both as evidence.
    [both] = screen([sup], [ev, ofac("36001:designation:2026-11-20", "ACME TRADING L.L.C.", entity_id="36001")])
    assert both.fingerprint == a.fingerprint and len(both.evidence) == 2


def test_full_list_aliases_in_other_scripts_and_translations_match():
    from engine.db import entry_event

    aliases = ["Nordwind Holding AG", "НОРДВИНД ХОЛДИНГ"]
    ev = entry_event(entry(name="NORTHWIND HOLDINGS", aliases=aliases))
    [a] = screen([ent("n", "customer", name="Nordwind Holding")], [ev])
    assert a.evidence[0].snapshot["matched_on"] == "alias" and a.evidence[0].match_value == "NORTHWIND HOLDINGS"
    assert screen([ent("x", "supplier", name="Southwind Holdings")], [ev]) == []


def test_removal_delta_cancels_an_entry_until_it_is_listed_again():
    from engine.db import entry_event

    sup = ent("s", "supplier", name="Acme Trading")
    listed = entry_event(entry("77", "ACME TRADING", first_seen_h=24 * 30))
    rem = event("ofac", "77:removal:2026-11-20", "sanction.removal", age_h=24 * 4, attrs={"entity_id": "77"})
    assert screen([sup], [listed, rem]) == []  # removed after we first saw the entry
    relisted = entry_event(entry("77", "ACME TRADING", first_seen_h=24))  # deleted by a full read, then back
    assert len(screen([sup], [relisted, rem])) == 1


def test_sanctions_name_index_is_reused_only_for_the_same_rows():
    from engine import core
    from engine.db import entry_event

    rows = [entry_event(entry(str(i), f"Listed Company Number {i}")) for i in range(50)]
    rows.append(entry_event(entry("99", "ACME TRADING")))
    sup = ent("s", "supplier", name="Acme Trading")
    first = screen([sup], rows)
    index = core._SANCTIONS_INDEX["index"]
    assert screen([sup], rows) == first and core._SANCTIONS_INDEX["index"] is index  # same objects: reused
    assert screen([sup], rows[:-1]) == [] and core._SANCTIONS_INDEX["index"] is not index  # changed: rebuilt
    assert screen([sup], list(rows)) == first  # equal results after a rebuild
