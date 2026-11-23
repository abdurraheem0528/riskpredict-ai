"""Registry of keyless public-source connectors, keyed by CLI name."""

from __future__ import annotations

from jobs.connectors.base import Connector
from jobs.connectors.cisa_kev import CisaKevConnector
from jobs.connectors.eonet import EonetConnector
from jobs.connectors.epss import EpssConnector
from jobs.connectors.frankfurter import FrankfurterConnector
from jobs.connectors.gdacs import GdacsConnector
from jobs.connectors.gdelt import GdeltConnector
from jobs.connectors.ofac import OfacConnector
from jobs.connectors.osv import OsvConnector
from jobs.connectors.portwatch import PortWatchConnector
from jobs.connectors.usgs import UsgsConnector
from jobs.connectors.worldbank import WorldBankConnector

CONNECTORS: dict[str, type[Connector]] = {
    c.name: c
    for c in (
        GdeltConnector,
        CisaKevConnector,
        EpssConnector,
        OsvConnector,
        PortWatchConnector,
        GdacsConnector,
        UsgsConnector,
        EonetConnector,
        FrankfurterConnector,
        WorldBankConnector,
        OfacConnector,
    )
}

__all__ = ["CONNECTORS", "Connector"]
