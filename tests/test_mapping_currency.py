"""The spend currency read from a column header ("Annual spend (USD)"). Test rows only."""

from __future__ import annotations

from decimal import Decimal

import pytest

from api.core import mapping

HEADER = "Supplier ID,Supplier,Country,City,Main port,Products,HS code,Currency,Annual spend (USD),Criticality,Notes"
ROW = "SUP-001,Formosa Microcircuits,Taiwan,Hsinchu,TWKHH,Microcontrollers;power ICs,8542,TWD,4200000,High,"


def rows(header: str = HEADER, *lines: str) -> list[mapping.RawRow]:
    cols = header.split(",")
    return [mapping.RawRow(i + 2, None, dict(zip(cols, line.split(",")))) for i, line in enumerate(lines or [ROW])]


def columns(header: str = HEADER) -> list[dict]:
    return [{"name": h, "samples": []} for h in header.split(",")]


@pytest.mark.parametrize("header, code", [
    ("Annual spend (USD)", "USD"), ("Annual spend [usd]", "USD"), ("Spend USD", "USD"), ("Spend in EUR", "EUR"),
    ("spend in eur", "EUR"), ("Spend €", "EUR"), ("Spend (£)", "GBP"), ("Spend US$", "USD"), ("Spend $", "USD"),
    ("Annual spend", None), ("Spend (USD) in EUR", None), ("Spend for ALL sites", None), ("Spend ¥", None),
])  # fmt: skip
def test_header_currency(header, code):
    assert mapping.header_currency(header) == code


def test_the_heuristics_read_it_and_say_so():
    spec = mapping.heuristic_suggest(columns())
    spend = next(c for c in spec["columns"] if c["column"] == "Annual spend (USD)")
    assert spend["field"] == "annual_spend" and spend["currency"] == "USD"
    assert spend["reason"].endswith("currency from the header: USD")
    assert next(c for c in spec["columns"] if c["column"] == "Currency")["field"] == "currency"  # TWD: not the spend
    assert next(c for c in spec["columns"] if c["column"] == "Products")["field"] == "commodity"


def test_apply_keeps_the_spend_with_the_header_currency():
    spec = mapping.heuristic_suggest(columns())
    res = mapping.apply(spec, rows(), chokepoints=lambda v: None)
    [e] = res.entities
    assert (e.annual_spend, e.spend_currency, e.currencies) == (Decimal("4200000.00"), "USD", ["TWD"])
    assert not [p for p in res.problems if "spend" in p["message"]]


def test_a_mapping_confirmed_before_this_change_reads_the_header_too():
    """The live workspace's confirmed mapping has no `currency`: apply() reads the header."""
    spec = {"entity_kind": "supplier", "columns": [{"column": "Supplier", "field": "name"},
                                                    {"column": "Annual spend (USD)", "field": "annual_spend"}]}  # fmt: skip
    [e] = mapping.apply(spec, rows(), chokepoints=lambda v: None).entities
    assert e.spend_currency == "USD"


def test_a_spend_currency_column_wins_and_no_currency_still_reports_the_problem():
    header = "Supplier,Annual spend (USD),Pay in"
    spec = {"entity_kind": "supplier", "columns": [{"column": "Supplier", "field": "name"},
                                                    {"column": "Annual spend (USD)", "field": "annual_spend"},
                                                    {"column": "Pay in", "field": "spend_currency"}]}  # fmt: skip
    [e] = mapping.apply(spec, rows(header, "Acme,100,EUR"), chokepoints=lambda v: None).entities
    assert e.spend_currency == "EUR"
    plain = {"entity_kind": "supplier", "columns": [{"column": "Supplier", "field": "name"},
                                                     {"column": "Spend", "field": "annual_spend"}]}  # fmt: skip
    res = mapping.apply(plain, rows("Supplier,Spend", "Acme,100"), chokepoints=lambda v: None)
    assert res.entities[0].annual_spend is None and res.problems[0]["message"] == "annual spend needs a spend currency"


def test_the_spec_validates_the_currency():
    names = ["Supplier", "Annual spend (USD)", "Country"]
    ok, errors = mapping.validate_spec({"entity_kind": "supplier", "columns": [
        {"column": "Supplier", "field": "name"}, {"column": "Annual spend (USD)", "field": "annual_spend", "currency": "USD"},
    ]}, names)  # fmt: skip
    assert not errors and ok["columns"][1]["currency"] == "USD"
    _, errors = mapping.validate_spec({"entity_kind": "supplier", "columns": [
        {"column": "Supplier", "field": "name"}, {"column": "Annual spend (USD)", "field": "annual_spend", "currency": "usd$"},
        {"column": "Country", "field": "country", "currency": "USD"},
    ]}, names)  # fmt: skip
    assert [e["message"] for e in errors] == ["not an ISO 4217 currency code", "only an annual_spend column has a currency"]


def test_rows_re_sync_once_after_the_rules_change():
    """The digest includes APPLY_RULES_VERSION, so a source's content hash changes once."""
    import hashlib
    import json

    r = rows()[0]
    old = hashlib.sha256(json.dumps(r.values, sort_keys=True, ensure_ascii=False).encode()).hexdigest()
    assert r.digest() != old and r.digest() == rows()[0].digest()
