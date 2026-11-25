"""Software keys: purl → OSV ``<ecosystem>:<package>``, CPE 2.3 → ``vendor:product`` (§5.2).

Versions are never compared: the feeds hold no affected version ranges.
"""

from __future__ import annotations

import re
from urllib.parse import unquote

# purl type → OSV ecosystem
PURL_ECOSYSTEMS = {
    "pypi": "PyPI",
    "npm": "npm",
    "maven": "Maven",
    "golang": "Go",
    "cargo": "crates.io",
    "nuget": "NuGet",
    "gem": "RubyGems",
    "composer": "Packagist",
}


def pep503(name: str) -> str:
    return re.sub(r"[-_.]+", "-", name).lower()


def purl_to_osv(purl: str) -> tuple[str, str] | None:
    """``pkg:pypi/Jinja2@3.1`` → ``('PyPI', 'jinja2')``; ``None`` for other ecosystems."""
    if not purl or not purl.startswith("pkg:"):
        return None
    body = purl[4:].split("#", 1)[0].split("?", 1)[0]
    body = body.split("@", 1)[0].strip("/")
    if "/" not in body:
        return None
    ptype, rest = body.split("/", 1)
    eco = PURL_ECOSYSTEMS.get(ptype.lower())
    if not eco or not rest:
        return None
    parts = [unquote(p) for p in rest.split("/") if p]
    if not parts:
        return None
    if eco == "PyPI":
        name = pep503(parts[-1])
    elif eco == "Maven":
        if len(parts) < 2:
            return None
        name = f"{parts[0]}:{parts[1]}"
    elif eco == "npm":
        name = "/".join(parts[-2:]) if len(parts) >= 2 and parts[-2].startswith("@") else parts[-1]
    elif eco in ("Go", "Packagist"):
        name = "/".join(parts)
    else:
        name = parts[-1]
    return eco, name


def osv_key(ecosystem: str, package: str) -> str:
    """Comparable form of an OSV ``event.software`` entry (PyPI names PEP 503-normalised)."""
    return f"{ecosystem}:{pep503(package) if ecosystem == 'PyPI' else package}"


def _squash(s: str) -> str:
    return re.sub(r"[_\s]+", "", unquote(s).replace("\\", "")).casefold()


def cpe_vendor_product(cpe: str) -> str | None:
    """``cpe:2.3:a:microsoft:exchange_server:2019:…`` → ``microsoft:exchangeserver``."""
    if not cpe or not cpe.startswith("cpe:2.3:"):
        return None
    parts = re.split(r"(?<!\\):", cpe)
    if len(parts) < 5 or parts[3] in ("*", "-", "") or parts[4] in ("*", "-", ""):
        return None
    return f"{_squash(parts[3])}:{_squash(parts[4])}"


def vendor_product_key(value: str) -> str | None:
    """KEV ``vendorProject:product`` (e.g. ``Microsoft:Exchange Server``) in the same form."""
    if not value or ":" not in value:
        return None
    vendor, product = value.split(":", 1)
    if not vendor.strip() or not product.strip():
        return None
    return f"{_squash(vendor)}:{_squash(product)}"


def entity_keys(software: tuple[str, ...]) -> list[tuple[str, str, str]]:
    """``(kind, comparable key, display value)`` for each purl / CPE of an entity.

    ``kind`` is ``osv`` (compared with OSV ``event.software``) or ``cpe``
    (compared with KEV/NVD ``vendor:product``).
    """
    out = []
    for s in software:
        osv = purl_to_osv(s)
        if osv:
            out.append(("osv", osv_key(*osv), f"{osv[0]}:{osv[1]}"))
            continue
        vp = cpe_vendor_product(s)
        if vp:
            out.append(("cpe", vp, vp))
    return out
