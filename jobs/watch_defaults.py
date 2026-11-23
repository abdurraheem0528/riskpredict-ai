"""Default watch terms: generic, public topics — no business data.

They are seeded into ``feeds.watch_terms`` by migration 002 and used directly
by ``--dry-run`` (which has no database). Keep both in sync; a test checks it.
"""

from jobs.connectors.base import WatchTerm

GDELT_QUERIES = [
    '("port strike" OR "dockworkers strike" OR "port closure")',
    '("export ban" OR "export restrictions" OR "tariff increase")',
    '("supply chain disruption" OR "factory fire" OR "plant shutdown")',
    '("new sanctions" OR "sanctions imposed")',
    '("ransomware attack" OR "cyberattack" OR "data breach")',
    '("shipping disruption" OR "vessel attacked" OR "canal closed")',
    '("currency crisis" OR "devaluation" OR "capital controls")',
    '("chip shortage" OR "semiconductor shortage" OR "raw material shortage")',
]

OSV_PACKAGES = [
    ("PyPI", "requests"),
    ("PyPI", "django"),
    ("npm", "lodash"),
    ("npm", "axios"),
    ("Maven", "org.apache.logging.log4j:log4j-core"),
    ("Go", "golang.org/x/net"),
]

DEFAULT_TERMS: list[WatchTerm] = [WatchTerm("gdelt_query", q) for q in GDELT_QUERIES] + [
    WatchTerm("osv_package", name, eco) for eco, name in OSV_PACKAGES
]
