"""OFAC full sanctions lists — SDN and consolidated (non-SDN), enhanced XML.

Docs: https://ofac.treasury.gov/sanctions-list-service and the schema at
https://sanctionslistservice.ofac.treas.gov/api/PublicationPreview/exports/ENHANCED_XML.xsd

The Sanctions List Service publishes each list in full as a ZIP of the
"enhanced" XML (``SDN_ENHANCED.ZIP`` ~6.5 MB, ~105 MB unzipped;
``CONS_ENHANCED.ZIP``, much smaller). Both are read at most once a day
(``min_interval``), parsed as a stream so memory stays small, and stored in
``feeds.sanctions_entries`` as current state: each complete list replaces the
stored rows of that dataset (entities no longer listed are deleted), so the
table does not grow with time. The daily change set stays in the ``ofac``
connector (``feeds.events``).

Per entity only what name screening needs is kept: the primary name, every
other name and translation (``aliases``; names OFAC flags ``isLowQuality`` go
to ``weak_aliases``), countries (addresses, nationality, citizenship, vessel
flag; ISO alpha-3 via :mod:`jobs.codes`, unmatched names are counted, never
guessed), programmes and lists. Birth dates, ID numbers, addresses and remarks
are not stored. Keyless; OFAC publishes no rate limit for these files.
"""

from __future__ import annotations

import io
import zipfile
from datetime import timedelta

import httpx
from defusedxml import ElementTree as ET

from jobs import codes
from jobs.connectors.base import Batch, Connector, RunContext, parse_date, safe_build
from jobs.http import SourceError, get, host_of
from jobs.models import SanctionEntry, Status

BASE = "https://sanctionslistservice.ofac.treas.gov/api/PublicationPreview/exports/"
DATASETS = {
    "sdn": BASE + "SDN_ENHANCED.ZIP",
    "consolidated": BASE + "CONS_ENHANCED.ZIP",
}
# A complete list is far larger than this; a smaller one is treated as a broken
# or truncated download, stored without deleting anything (status partial).
MIN_ENTRIES = {"sdn": 5000, "consolidated": 50}
MAX_ZIP_BYTES = 60 * 2**20
MAX_XML_BYTES = 600 * 2**20  # zip-bomb guard (the SDN XML is ~105 MB)
COUNTRY_FEATURES = {"Nationality Country", "Citizenship Country", "Vessel Flag"}
FILE_TIMEOUT = httpx.Timeout(120.0, connect=15.0)


def _local(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def _child(el, name: str):
    for c in el:
        if _local(c.tag) == name:
            return c
    return None


def _children(el, name: str) -> list:
    return [c for c in el if _local(c.tag) == name] if el is not None else []


def _text(el) -> str | None:
    return el.text.strip() if el is not None and el.text and el.text.strip() else None


_BIDI = dict.fromkeys(map(ord, "\u200e\u200f\u202a\u202b\u202c\u202d\u202e\u2066\u2067\u2068\u2069"))


def _name_text(el) -> str | None:
    """A name without the bidi control marks OFAC wraps right-to-left names in."""
    t = _text(el)
    return (" ".join(t.translate(_BIDI).split()) or None) if t else None


def _is_true(el) -> bool:
    return (_text(el) or "").lower() == "true"


def _entity(ent, dataset: str, notes: dict[str, int]) -> SanctionEntry | None:
    eid = ent.get("id")
    if not eid:
        notes["dropped_malformed"] = notes.get("dropped_malformed", 0) + 1
        return None
    general = _child(ent, "generalInfo")
    entity_type = _text(_child(general, "entityType")) if general is not None else None

    primary: str | None = None
    strong: list[str] = []
    weak: list[str] = []
    names = _children(_child(ent, "names"), "name")
    # The primary name first, then its primary translation first.
    names.sort(key=lambda n: not _is_true(_child(n, "isPrimary")))
    for nm in names:
        low = _is_true(_child(nm, "isLowQuality"))
        translations = _children(_child(nm, "translations"), "translation")
        translations.sort(key=lambda t: not _is_true(_child(t, "isPrimary")))
        for tr in translations:
            full = _name_text(_child(tr, "formattedFullName"))
            if not full:
                continue
            if primary is None and _is_true(_child(nm, "isPrimary")):
                primary = full
            elif low:
                weak.append(full)
            else:
                strong.append(full)
    if primary is None and strong:
        primary = strong.pop(0)

    country_names = [_text(_child(a, "country")) for a in _children(_child(ent, "addresses"), "address")]
    for f in _children(_child(ent, "features"), "feature"):
        if _text(_child(f, "type")) in COUNTRY_FEATURES:
            country_names.append(_text(_child(f, "value")))
    countries = []
    for cn in country_names:
        if not cn:
            continue
        a3 = codes.country_alpha3(cn)
        if a3:
            countries.append(a3)
        else:
            notes["unmatched_country_names"] = notes.get("unmatched_country_names", 0) + 1

    lists_el = _children(_child(ent, "sanctionsLists"), "sanctionsList")
    list_dates = sorted(d for d in (parse_date(sl.get("datePublished")) for sl in lists_el) if d)
    return safe_build(
        SanctionEntry,
        notes,
        source="ofac_sdn",
        dataset=dataset,
        entity_id=eid,
        name=primary or "",
        entity_type=entity_type,
        aliases=strong,
        weak_aliases=weak,
        countries=countries,
        programs=[_text(p) for p in _children(_child(ent, "sanctionsPrograms"), "sanctionsProgram")],
        lists=[_text(sl) for sl in lists_el],
        listed_on=list_dates[0].date() if list_dates else None,
    )


def parse_xml(stream, dataset: str, notes: dict[str, int] | None = None) -> list[SanctionEntry]:
    """Stream-parse an enhanced-XML list (a file object or bytes)."""
    notes = notes if notes is not None else {}
    if isinstance(stream, (bytes, bytearray)):
        stream = io.BytesIO(stream)
    out: list[SanctionEntry] = []
    seen = 0
    root = None
    try:
        for event, el in ET.iterparse(stream, events=("start", "end")):
            if event == "start":
                if root is None:
                    root = el
                continue
            if _local(el.tag) != "entity":
                continue
            seen += 1
            entry = _entity(el, dataset, notes)
            if entry:
                out.append(entry)
            el.clear()
            # Drop finished entities from the tree so memory stays flat.
            if root is not None:
                for parent in root:
                    if _local(parent.tag) == "entities":
                        parent.clear()
    except ET.ParseError as exc:
        raise SourceError(f"OFAC {dataset} list is not valid XML") from exc
    notes[f"{dataset}_entities_in_file"] = seen
    return out


def read_zip(data: bytes, dataset: str, notes: dict[str, int] | None = None) -> list[SanctionEntry]:
    try:
        zf = zipfile.ZipFile(io.BytesIO(data))
    except zipfile.BadZipFile as exc:
        raise SourceError(f"OFAC {dataset} download is not a ZIP file") from exc
    xml_names = [i for i in zf.infolist() if i.filename.lower().endswith(".xml")]
    if len(xml_names) != 1:
        raise SourceError(f"OFAC {dataset} ZIP holds {len(xml_names)} XML files, expected 1")
    info = xml_names[0]
    if info.file_size > MAX_XML_BYTES:
        raise SourceError(f"OFAC {dataset} XML is larger than {MAX_XML_BYTES // 2**20} MB")
    with zf.open(info) as fh:
        return parse_xml(fh, dataset, notes)


class OfacSdnConnector(Connector):
    name = "ofac_sdn"
    min_interval = timedelta(hours=24)

    def fetch(self, client: httpx.Client, ctx: RunContext) -> Batch:
        batch = Batch()
        failed: list[str] = []
        last_error = None
        for dataset, url in DATASETS.items():
            try:
                resp = get(client, url, sleep=ctx.sleep, timeout=FILE_TIMEOUT)
                if len(resp.content) > MAX_ZIP_BYTES:
                    raise SourceError(f"OFAC {dataset} ZIP from {host_of(url)} is too large")
                entries = read_zip(resp.content, dataset, batch.notes)
            except SourceError as exc:
                failed.append(dataset)
                last_error = str(exc)
                continue
            batch.notes[f"{dataset}_entries"] = len(entries)
            batch.sanctions.extend(entries)
            if len(entries) >= MIN_ENTRIES[dataset]:
                batch.replace_datasets.append(dataset)
            else:
                failed.append(dataset)
                last_error = f"OFAC {dataset} list has only {len(entries)} entries; kept, nothing deleted"
        if failed:
            batch.status = Status.ERROR if not batch.sanctions else Status.PARTIAL
            batch.error = last_error
        return batch
