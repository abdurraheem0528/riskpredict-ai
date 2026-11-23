"""OFAC Sanctions List Service — the latest published change set (delta file).

Docs: https://ofac.treasury.gov/sanctions-list-service
``/changes/latest`` redirects to the most recent delta XML; each entity in it
becomes one event (designation, update or removal). Countries come from the
entity's addresses, nationality / citizenship and vessel flag. Personal
details such as birth dates are not kept. OFAC publishes no severity.
"""

from __future__ import annotations

from datetime import timedelta

import httpx
from defusedxml import ElementTree as ET

from jobs import codes
from jobs.connectors.base import Batch, Connector, RunContext, parse_date, parse_iso, safe_build
from jobs.http import SourceError, get
from jobs.models import Event

URL = "https://sanctionslistservice.ofac.treas.gov/changes/latest"
COUNTRY_FEATURES = {"Nationality Country", "Citizenship Country", "Vessel Flag"}
ACTIONS = {"add": "designation", "update": "update", "modify": "update", "delete": "removal", "remove": "removal"}


def _ns(root) -> str:
    return root.tag.split("}")[0] + "}" if root.tag.startswith("{") else ""


def _text(el) -> str | None:
    return el.text.strip() if el is not None and el.text and el.text.strip() else None


def _primary_first(elements, n: str) -> list:
    """Order elements so the one flagged ``isPrimary=true`` comes first."""
    return sorted(elements, key=lambda el: _text(el.find(f"{n}isPrimary")) != "true")


def _primary_name(ent, n: str) -> str | None:
    for nm in _primary_first(ent.findall(f"{n}names/{n}name"), n):
        for tr in _primary_first(nm.findall(f"{n}translations/{n}translation"), n):
            full = _text(tr.find(f"{n}formattedFullName"))
            if full:
                return full
    return None


def parse(xml: bytes, notes: dict[str, int] | None = None) -> list[Event]:
    notes = notes if notes is not None else {}
    try:
        root = ET.fromstring(xml)
    except ET.ParseError as exc:
        raise SourceError("OFAC delta file is not valid XML") from exc
    n = _ns(root)
    published = parse_iso(_text(root.find(f"{n}publicationInfo/{n}datePublished")))
    pub_type = _text(root.find(f"{n}publicationInfo/{n}publicationType"))
    events = []
    for ent in root.iterfind(f"{n}entities/{n}entity"):
        eid = ent.get("id")
        if not eid:
            continue
        action = (ent.get("action") or "").lower() or "unknown"
        entity_type = _text(ent.find(f"{n}generalInfo/{n}entityType"))
        lists = [
            (_text(sl), sl.get("datePublished")) for sl in ent.iterfind(f"{n}sanctionsLists/{n}sanctionsList")
        ]
        programs = [_text(p) for p in ent.iterfind(f"{n}sanctionsPrograms/{n}sanctionsProgram") if _text(p)]
        name = _primary_name(ent, n)
        country_names = [_text(c) for c in ent.iterfind(f"{n}addresses/{n}address/{n}country")]
        for f in ent.iterfind(f"{n}features/{n}feature"):
            if _text(f.find(f"{n}type")) in COUNTRY_FEATURES:
                country_names.append(_text(f.find(f"{n}value")))
        countries = [c for c in (codes.country_alpha3(x) for x in country_names if x) if c]
        unmatched = sum(1 for x in country_names if x and not codes.country_alpha3(x))
        if unmatched:
            notes["unmatched_country_names"] = notes.get("unmatched_country_names", 0) + unmatched
        list_date = next((parse_date(d) for _, d in lists if d), None)
        verb = ACTIONS.get(action, action)
        list_names = [ln for ln, _ in lists if ln]
        title = f"OFAC {verb}: {name or 'entity ' + eid}"
        if programs:
            title += f" [{', '.join(programs)}]"
        when = list_date or published
        ev = safe_build(
            Event,
            notes,
            source="ofac",
            raw_id=f"{eid}:{action}:{when.date().isoformat() if when else 'undated'}",
            type=f"sanction.{verb}",
            title=title,
            published_at=when,
            countries=countries,
            attrs={
                k: v
                for k, v in {
                    "entity_id": eid,
                    "action": action,
                    "entity_type": entity_type,
                    "name": name,
                    "programs": programs or None,
                    "lists": list_names or None,
                    "publication_type": pub_type,
                }.items()
                if v
            },
        )
        if ev:
            events.append(ev)
    return events


class OfacConnector(Connector):
    name = "ofac"
    min_interval = timedelta(hours=1)

    def fetch(self, client: httpx.Client, ctx: RunContext) -> Batch:
        batch = Batch()
        batch.events = parse(get(client, URL, sleep=ctx.sleep).content, batch.notes)
        return batch
