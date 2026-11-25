"""Column mapping: target fields, validation, suggestions and applying a mapping to rows.

Normalisation follows the ``MappingField`` rules in openapi.yaml and reuses
``jobs/codes.py``. A value that cannot be normalised is left out and reported as
a problem; it is never guessed (contracts decision 11).

Suggestions come from Groq when it is configured and reachable; otherwise, or
for columns the model skipped, from the deterministic header heuristics below.
Either way the user confirms the mapping before any entity is written.
"""

from __future__ import annotations

import hashlib
import json
import re
import unicodedata
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from typing import Any, Callable, Iterable

from api.core import engine as engine_iface
from api.core.config import Settings
from api.core.groq import GroqUnavailable, chat_json
from jobs import codes

ENTITY_KINDS = ("supplier", "site", "customer", "route", "port", "currency", "commodity", "software")
ALL = ENTITY_KINDS
LOCATED = ("supplier", "site", "customer", "route", "port")
POINT = ("supplier", "site", "customer", "port")

# field → (label, kinds, multi)
FIELDS: dict[str, tuple[str, tuple[str, ...], bool]] = {
    "name": ("Name", ALL, False),
    "kind": ("Entity kind", ALL, False),
    "row_key": ("Row key (stable id)", ALL, False),
    "country": ("Country", LOCATED, False),
    "countries": ("Countries", LOCATED, True),
    "city": ("City", POINT, False),
    "address": ("Address", POINT, False),
    "lat": ("Latitude", POINT, False),
    "lon": ("Longitude", POINT, False),
    "port": ("Port (UN/LOCODE)", ("supplier", "site", "route", "port"), False),
    "ports": ("Ports (UN/LOCODE)", ("supplier", "site", "route", "port"), True),
    "route_origin_port": ("Route origin port", ("route",), False),
    "route_destination_port": ("Route destination port", ("route",), False),
    "via_chokepoints": ("Via chokepoints", ("route",), True),
    "currency": ("Currency (ISO 4217)", ("supplier", "customer", "currency"), False),
    "currencies": ("Currencies (ISO 4217)", ("supplier", "customer", "currency"), True),
    "hs_code": ("HS code", ("supplier", "commodity"), True),
    "commodity": ("Commodity", ("supplier", "commodity"), True),
    "software": ("Software (purl or CPE 2.3)", ("software",), True),
    "annual_spend": ("Annual spend", ("supplier",), False),
    "spend_currency": ("Spend currency", ("supplier",), False),
    "revenue_share": ("Revenue share", ("customer",), False),
    "criticality": ("Criticality", ALL, False),
    "notes": ("Notes", ALL, False),
    "ignore": ("Ignore", ALL, False),
}
REPEATABLE = {"countries", "country", "ports", "port", "currencies", "currency", "hs_code", "commodity", "software",
              "via_chokepoints", "notes", "ignore"}  # fmt: skip
DEFAULT_SPLIT = ",;|"
MAX_PROBLEMS = 500


def field_infos() -> list[dict]:
    return [{"field": f, "label": lbl, "kinds": list(kinds), "multi": multi} for f, (lbl, kinds, multi) in FIELDS.items()]


# --- validation --------------------------------------------------------------------


def validate_spec(spec: Any, column_names: Iterable[str], *, suggestion: bool = False) -> tuple[dict, list[dict]]:
    """Return the cleaned MappingSpec and field errors (JSON pointers into the body)."""
    errors: list[dict] = []
    names = set(column_names)
    if not isinstance(spec, dict):
        return {}, [{"field": "", "message": "must be an object"}]
    kind = spec.get("entity_kind")
    if kind is not None and kind not in ENTITY_KINDS:
        errors.append({"field": "/entity_kind", "message": f"must be one of {', '.join(ENTITY_KINDS)} or null"})
        kind = None
    cols = spec.get("columns")
    if not isinstance(cols, list) or not cols:
        errors.append({"field": "/columns", "message": "at least one column is required"})
        cols = []
    out_cols: list[dict] = []
    seen_columns: set[str] = set()
    used: dict[str, int] = {}
    for i, c in enumerate(cols):
        p = f"/columns/{i}"
        if not isinstance(c, dict):
            errors.append({"field": p, "message": "must be an object"})
            continue
        col, fld = c.get("column"), c.get("field")
        if not isinstance(col, str) or col not in names:
            errors.append({"field": f"{p}/column", "message": "not a column of this source"})
            continue
        if col in seen_columns:
            errors.append({"field": f"{p}/column", "message": "column mapped twice"})
            continue
        if fld not in FIELDS:
            errors.append({"field": f"{p}/field", "message": "unknown field"})
            continue
        if fld not in REPEATABLE and used.get(fld):
            errors.append({"field": f"{p}/field", "message": f"only one column can be {fld}"})
            continue
        seen_columns.add(col)
        used[fld] = used.get(fld, 0) + 1
        item: dict[str, Any] = {"column": col, "field": fld}
        split = c.get("split")
        if split is not None:
            if not isinstance(split, str) or not 1 <= len(split) <= 5:
                errors.append({"field": f"{p}/split", "message": "1 to 5 separator characters"})
            else:
                item["split"] = split
        if suggestion:
            conf = c.get("confidence")
            if isinstance(conf, (int, float)) and not isinstance(conf, bool):
                item["confidence"] = round(min(1.0, max(0.0, float(conf))), 2)
            reason = c.get("reason")
            if isinstance(reason, str) and reason.strip():
                item["reason"] = _clean_text(reason)[:200]
        out_cols.append(item)
    if not suggestion:
        if not used.get("name"):
            errors.append({"field": "/columns", "message": "exactly one column must be mapped to name"})
        if kind is None and not used.get("kind"):
            errors.append({"field": "/entity_kind", "message": "set entity_kind, or map a column to kind"})
        if bool(used.get("lat")) != bool(used.get("lon")):
            errors.append({"field": "/columns", "message": "map both lat and lon, or neither"})
    return {"entity_kind": kind, "columns": out_cols}, errors


def _clean_text(s: str) -> str:
    return re.sub(r"\s+", " ", "".join(ch for ch in s if ch.isprintable())).strip()


# --- heuristics ---------------------------------------------------------------------


def _header_key(h: str) -> str:
    s = unicodedata.normalize("NFKD", h).casefold()
    s = re.sub(r"[^a-z0-9%]+", " ", s)
    return f" {s.strip()} "


# Ordered: the first match wins, so specific rules come before general ones.
_RULES: list[tuple[str, str, str]] = [
    (r" (lat|latitude) ", "lat", "header says latitude"),
    (r" (lon|lng|long|longitude) ", "lon", "header says longitude"),
    (r" (origin|from port|pol|port of loading|load port|loading port) ", "route_origin_port", "header names the origin port"),
    (r" (destination|to port|pod|port of discharge|discharge port|dest) ", "route_destination_port", "header names the destination port"),
    (r" (chokepoints?|via) ", "via_chokepoints", "header names chokepoints"),
    (r" (purl|cpe|package|packages|library|dependency|software) ", "software", "header names software packages"),
    (r" (unlocode|locode|ports) ", "ports", "header names ports"),
    (r" port ", "port", "header names a port"),
    (r" countries ", "countries", "header names countries"),
    (r" (country|nation|iso3|country code) ", "country", "header names a country"),
    (r" (city|town) ", "city", "header names a city"),
    (r" (address|street) ", "address", "header names an address"),
    (r" (spend currency|currency of spend|spend ccy) ", "spend_currency", "header names the spend currency"),
    (r" (spend|annual spend|purchase value|purchases|purchase volume) ", "annual_spend", "header names spend"),
    (r" (currencies) ", "currencies", "header names currencies"),
    (r" (currency|ccy) ", "currency", "header names a currency"),
    (r" (revenue share|share of revenue|% of revenue|revenue %|revenue pct) ", "revenue_share", "header names revenue share"),
    (r" (hs|hs code|hs6|hts|tariff code|tariff) ", "hs_code", "header names an HS code"),
    (r" (commodity|commodities|material|materials|raw material) ", "commodity", "header names a commodity"),
    (r" (criticality|critical|priority|tier|importance) ", "criticality", "header names criticality"),
    (r" (kind|entity type|type) ", "kind", "header names the entity kind"),
    (r" (id|key|code|record id|supplier id|vendor id|customer id|site id|number|no) ", "row_key", "header looks like an id"),
    (r" (notes?|comments?|remarks|description) ", "notes", "header looks like notes"),
    (r" (name|supplier|vendor|company|customer|client|site|facility|plant|warehouse|route|lane|partner) ", "name", "header looks like a name"),
]  # fmt: skip

_KIND_HINTS = [
    (r" (supplier|vendor|manufacturer) ", "supplier"),
    (r" (customer|client) ", "customer"),
    (r" (site|plant|facility|warehouse|factory|office) ", "site"),
    (r" (route|lane|origin|destination) ", "route"),
    (r" (purl|cpe|package|software|library|dependency) ", "software"),
    (r" (commodity|material) ", "commodity"),
]


def _sample_field(samples: list[str]) -> str | None:
    vals = [s for s in samples if s.strip()]
    if len(vals) < 2:
        return None
    if all(s.strip().startswith(("pkg:", "cpe:2.3:")) for s in vals):
        return "software"
    if all(codes.locode(s) for s in vals):
        return "port"
    if all(codes.country_alpha3(s) for s in vals) and not all(re.fullmatch(r"\d+", s.strip()) for s in vals):
        return "country"
    return None


def heuristic_suggest(columns: list[dict]) -> dict:
    out: list[dict] = []
    used: set[str] = set()
    kind_votes: dict[str, int] = {}
    for col in columns:
        name = col["name"]
        key = _header_key(name)
        for pat, kind in _KIND_HINTS:
            if re.search(pat, key):
                kind_votes[kind] = kind_votes.get(kind, 0) + 1
        field_name, reason, conf = "ignore", "no rule matched", 0.2
        for pat, fld, why in _RULES:
            if re.search(pat, key):
                field_name, reason, conf = fld, why, 0.6
                break
        if field_name == "ignore" and (by_sample := _sample_field(col.get("samples") or [])):
            field_name, reason, conf = by_sample, "sample values look like this", 0.5
        if field_name not in REPEATABLE and field_name in used:
            field_name, reason, conf = "ignore", "another column already has this field", 0.2
        used.add(field_name)
        out.append({"column": name, "field": field_name, "confidence": conf, "reason": reason})
    if "name" not in used:
        # The first mostly-text column is the likeliest name.
        for item, col in zip(out, columns):
            vals = [s for s in col.get("samples") or [] if s.strip()]
            if item["field"] in ("ignore", "notes") and vals and not any(re.fullmatch(r"[\d.,%\s-]+", v) for v in vals):
                item.update(field="name", confidence=0.3, reason="first text column")
                break
    kind = None
    if kind_votes:
        kind = max(sorted(kind_votes), key=lambda k: kind_votes[k])
    if "route_origin_port" in used and "route_destination_port" in used:
        kind = "route"
    if "kind" in used:
        kind = None
    return {"entity_kind": kind, "columns": out}


# --- Groq suggestion ----------------------------------------------------------------

_SYSTEM = """You map spreadsheet columns to fields of a supply-chain risk platform.
Reply with one JSON object: {"entity_kind": <kind or null>, "columns": [{"column": <header exactly as given>,
"field": <field>, "split": <optional separator characters>, "confidence": <0..1>, "reason": <short reason>}]}.
Entity kinds: supplier, site, customer, route, port, currency, commodity, software. Use null for entity_kind only
when a column holds the kind per row (then map it to "kind").
Fields: name, kind, row_key, country, countries, city, address, lat, lon, port, ports, route_origin_port,
route_destination_port, via_chokepoints, currency, currencies, hs_code, commodity, software, annual_spend,
spend_currency, revenue_share, criticality, notes, ignore.
Exactly one column is "name". Map every column; use "ignore" for columns that fit no field.
Use only the headers and samples given. Do not invent columns."""


def groq_suggest(settings: Settings, columns: list[dict]) -> dict:
    """Ask Groq; raises GroqUnavailable. Sends only headers and ≤5 samples per column."""
    payload = [{"column": c["name"], "samples": [str(s)[:80] for s in (c.get("samples") or [])[:5]]} for c in columns]
    data = chat_json(
        settings,
        settings.groq_mapping_model,
        [{"role": "system", "content": _SYSTEM}, {"role": "user", "content": json.dumps({"columns": payload})}],
    )
    spec, _errors = validate_spec(data, [c["name"] for c in columns], suggestion=True)
    return spec


def suggest(settings: Settings, columns: list[dict]) -> tuple[dict, str]:
    """A suggestion and the ``model`` label: ``groq:<model>``, or ``heuristic`` as the fallback."""
    heur = heuristic_suggest(columns)
    try:
        ai = groq_suggest(settings, columns)
    except GroqUnavailable:
        return heur, "heuristic"
    if not ai["columns"]:
        return heur, "heuristic"
    # Keep one `name`; fill columns the model left out with the heuristics.
    by_col = {c["column"]: c for c in ai["columns"]}
    merged = []
    has_name = False
    for h in heur["columns"]:
        c = by_col.get(h["column"], h)
        if c["field"] == "name":
            if has_name:
                c = {**c, "field": "ignore"}
            has_name = True
        merged.append(c)
    return {"entity_kind": ai.get("entity_kind") or heur.get("entity_kind"), "columns": merged}, f"groq:{settings.groq_mapping_model}"


# --- applying a mapping -----------------------------------------------------------------


@dataclass
class RawRow:
    row_number: int | None  # 1-based row in the sheet, None for Airtable
    record_id: str | None  # Airtable record id
    values: dict[str, str]

    def digest(self) -> str:
        return hashlib.sha256(json.dumps(self.values, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


@dataclass
class EntityRow:
    kind: str
    name: str
    row_key: str
    row_number: int | None
    row_hash: str
    countries: list[str] = field(default_factory=list)
    ports: list[str] = field(default_factory=list)
    currencies: list[str] = field(default_factory=list)
    hs_codes: list[str] = field(default_factory=list)
    commodities: list[str] = field(default_factory=list)
    software: list[str] = field(default_factory=list)
    lat: float | None = None
    lon: float | None = None
    city: str | None = None
    address: str | None = None
    annual_spend: Decimal | None = None
    spend_currency: str | None = None
    revenue_share: Decimal | None = None
    criticality: str | None = None
    attrs: dict[str, Any] = field(default_factory=dict)


@dataclass
class ApplyResult:
    entities: list[EntityRow]
    problems: list[dict]
    rows_read: int
    problem_count: int = 0


_KIND_ALIASES = {k: k for k in ENTITY_KINDS} | {k + "s": k for k in ENTITY_KINDS} | {
    "commodities": "commodity", "currencies": "currency", "softwares": "software", "vendor": "supplier",
    "vendors": "supplier",
}  # fmt: skip
_CRIT = {"low": "low", "medium": "medium", "med": "medium", "high": "high", "critical": "critical",
         "1": "low", "2": "medium", "3": "high", "4": "critical"}  # fmt: skip
_PURL = re.compile(r"^pkg:[a-z][a-z0-9.+-]*/[^\s]+$", re.I)
_CPE = re.compile(r"^cpe:2\.3:[aho*-]:[^:\s]+:[^:\s]+(:[^\s]*)?$", re.I)


def _split(value: str, seps: str) -> list[str]:
    parts = re.split("[" + re.escape(seps) + "]", value) if seps else [value]
    return [p.strip() for p in parts if p.strip()]


def _number(value: str) -> Decimal | None:
    s = value.strip().replace(" ", "").replace(" ", "").replace("_", "")
    s = re.sub(r"^[$€£¥]|[$€£¥]$", "", s)
    if re.fullmatch(r"\d{1,3}(,\d{3})+(\.\d+)?", s):
        s = s.replace(",", "")
    if not re.fullmatch(r"\d+(\.\d+)?", s):
        return None
    try:
        return Decimal(s)
    except InvalidOperation:
        return None


def _hs(value: str) -> str | None:
    s = re.sub(r"[.\s-]", "", value)
    if not s.isdigit():
        return None
    if len(s) in (2, 4, 6):
        return s
    if len(s) in (8, 10):  # national tariff lines start with the HS6 code
        return s[:6]
    return None


def chokepoint_resolver() -> Callable[[str], str | None]:
    table = engine_iface.chokepoints()
    index: dict[str, str] = {}
    for cp in table:
        for label in (cp.id, cp.name, *cp.aliases):
            index[_norm_label(label)] = cp.id
    return lambda v: index.get(_norm_label(v))


def _norm_label(s: str) -> str:
    s = unicodedata.normalize("NFKD", s).casefold()
    return re.sub(r"[^a-z0-9]+", "", s.replace("strait of", "").replace("the ", ""))


def apply(
    spec: dict,
    rows: list[RawRow],
    *,
    chokepoints: Callable[[str], str | None] | None = None,
    default_key: Callable[[RawRow], str] | None = None,
) -> ApplyResult:
    resolve_cp = chokepoints or chokepoint_resolver()
    problems: list[dict] = []
    count = 0

    def problem(row: RawRow, column: str, value: str, message: str) -> None:
        nonlocal count
        count += 1
        if len(problems) < MAX_PROBLEMS:
            problems.append(
                {"row_number": row.row_number or 0, "column": column, "value": str(value)[:100], "message": message}
            )

    entities: list[EntityRow] = []
    keys: set[str] = set()
    fixed_kind = spec.get("entity_kind")
    cols = spec.get("columns") or []
    for idx, row in enumerate(rows):
        e = EntityRow(kind=fixed_kind or "", name="", row_key="", row_number=row.row_number, row_hash=row.digest())
        origin = dest = None
        mid_ports: list[str] = []
        notes: list[str] = []
        lat = lon = None
        lat_col = lon_col = ""
        spend_col = ""
        for c in cols:
            col, fld = c["column"], c["field"]
            raw = (row.values.get(col) or "").strip()
            if fld == "ignore" or not raw:
                continue
            multi = FIELDS[fld][2]
            values = _split(raw, c.get("split") or DEFAULT_SPLIT) if multi else [raw]
            for v in values:
                if fld == "name":
                    e.name = _clean_text(v)[:300]
                elif fld == "kind":
                    k = _KIND_ALIASES.get(v.casefold())
                    if k:
                        e.kind = k
                    else:
                        problem(row, col, v, "not a known entity kind")
                elif fld == "row_key":
                    e.row_key = _clean_text(v)[:200]
                elif fld in ("country", "countries"):
                    a3 = codes.country_alpha3(v)
                    if a3:
                        if a3 not in e.countries:
                            e.countries.append(a3)
                    else:
                        problem(row, col, v, "not a known country")
                elif fld in ("port", "ports", "route_origin_port", "route_destination_port"):
                    lc = codes.locode(v)
                    if not lc:
                        problem(row, col, v, "not a UN/LOCODE")
                    elif fld == "route_origin_port":
                        origin = lc
                    elif fld == "route_destination_port":
                        dest = lc
                    elif lc not in mid_ports:
                        mid_ports.append(lc)
                elif fld == "via_chokepoints":
                    cp = resolve_cp(v)
                    if cp:
                        e.attrs.setdefault("chokepoints", [])
                        if cp not in e.attrs["chokepoints"]:
                            e.attrs["chokepoints"].append(cp)
                    else:
                        problem(row, col, v, "not a known chokepoint")
                elif fld in ("currency", "currencies"):
                    code = v.upper()
                    if codes.is_currency(code):
                        if code not in e.currencies:
                            e.currencies.append(code)
                    else:
                        problem(row, col, v, "not an ISO 4217 currency code")
                elif fld == "hs_code":
                    hs = _hs(v)
                    if hs:
                        if hs not in e.hs_codes:
                            e.hs_codes.append(hs)
                    else:
                        problem(row, col, v, "not an HS code (2, 4 or 6 digits)")
                elif fld == "commodity":
                    cv = _clean_text(v).casefold()[:100]
                    if cv and cv not in e.commodities:
                        e.commodities.append(cv)
                elif fld == "software":
                    sv = v.strip()
                    if _PURL.match(sv) or _CPE.match(sv):
                        if sv not in e.software:
                            e.software.append(sv[:300])
                    else:
                        problem(row, col, v, "not a purl or CPE 2.3 name")
                elif fld in ("lat", "lon"):
                    try:
                        num = float(v.replace(",", "."))
                    except ValueError:
                        num = None
                    limit = 90 if fld == "lat" else 180
                    if num is None or not -limit <= num <= limit:
                        problem(row, col, v, f"not a {'latitude' if fld == 'lat' else 'longitude'}")
                    elif fld == "lat":
                        lat, lat_col = num, col
                    else:
                        lon, lon_col = num, col
                elif fld == "city":
                    e.city = _clean_text(v)[:200]
                elif fld == "address":
                    e.address = _clean_text(v)[:500]
                elif fld == "annual_spend":
                    n = _number(v)
                    if n is None:
                        problem(row, col, v, "not a number")
                    else:
                        e.annual_spend, spend_col = n.quantize(Decimal("0.01")), col
                elif fld == "spend_currency":
                    code = v.upper()
                    if codes.is_currency(code):
                        e.spend_currency = code
                    else:
                        problem(row, col, v, "not an ISO 4217 currency code")
                elif fld == "revenue_share":
                    pct = v.endswith("%")
                    n = _number(v[:-1] if pct else v)
                    if n is not None and pct:
                        n = n / 100
                    if n is None or not 0 <= n <= 1:
                        problem(row, col, v, "not a share between 0 and 1 (or a percentage)")
                    else:
                        e.revenue_share = n.quantize(Decimal("0.0001"))
                elif fld == "criticality":
                    cr = _CRIT.get(v.casefold())
                    if cr:
                        e.criticality = cr
                    else:
                        problem(row, col, v, "not low, medium, high or critical")
                elif fld == "notes":
                    notes.append(_clean_text(v)[:1000])
        if notes:
            e.attrs["notes"] = " ".join(notes)[:2000]
        e.ports = ([origin] if origin else []) + [p for p in mid_ports if p not in (origin, dest)] + ([dest] if dest else [])
        if (lat is None) != (lon is None):
            problem(row, lat_col or lon_col, "", "latitude and longitude must both be present")
        elif lat is not None:
            e.lat, e.lon = lat, lon
        if e.annual_spend is not None and not e.spend_currency:
            problem(row, spend_col, str(e.annual_spend), "annual spend needs a spend currency")
            e.annual_spend = None
        if not e.name:
            problem(row, "", "", "missing name")
            continue
        if not e.kind:
            problem(row, "", "", "missing entity kind")
            continue
        if not e.row_key:
            e.row_key = default_key(row) if default_key else (row.record_id or f"row:{row.row_number or idx + 1}")
        if e.row_key in keys:
            problem(row, "", e.row_key, "duplicate row key; row skipped")
            continue
        keys.add(e.row_key)
        entities.append(e)
    return ApplyResult(entities=entities, problems=problems, rows_read=len(rows), problem_count=count)
