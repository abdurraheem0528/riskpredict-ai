"""Registry of public-source connectors, keyed by CLI name.

The keyed ones (``KeyedConnector``) read ``<NAME>_API_KEY`` and report
``skipped`` ("no key") without it; ``nvd`` also runs keyless at a lower rate.
"""

from __future__ import annotations

from jobs.connectors.base import Connector
from jobs.connectors.cisa_kev import CisaKevConnector
from jobs.connectors.comtrade import ComtradeConnector
from jobs.connectors.currents import CurrentsConnector
from jobs.connectors.eia import EiaConnector
from jobs.connectors.eonet import EonetConnector
from jobs.connectors.epss import EpssConnector
from jobs.connectors.frankfurter import FrankfurterConnector
from jobs.connectors.fred import FredConnector
from jobs.connectors.gdacs import GdacsConnector
from jobs.connectors.gdelt import GdeltConnector
from jobs.connectors.newsdata import NewsDataConnector
from jobs.connectors.nvd import NvdConnector
from jobs.connectors.ofac import OfacConnector
from jobs.connectors.ofac_sdn import OfacSdnConnector
from jobs.connectors.osv import OsvConnector
from jobs.connectors.otx import OtxConnector
from jobs.connectors.portwatch import PortWatchConnector
from jobs.connectors.tiingo import TiingoConnector
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
        # Full sanctions lists (keyless, daily).
        OfacSdnConnector,
        # Keyed sources: skipped ("no key") until <NAME>_API_KEY is set.
        NvdConnector,  # also runs keyless
        FredConnector,
        EiaConnector,
        ComtradeConnector,
        OtxConnector,
        TiingoConnector,
        NewsDataConnector,
        CurrentsConnector,
    )
}

__all__ = ["CONNECTORS", "Connector"]
