"""Default watch terms: generic, public topics — no business data.

They are seeded into ``feeds.watch_terms`` by migrations 002 (phrases, OSV
packages) and 041 (GDELT GKG themes) and used directly by ``--dry-run`` (which
has no database). Keep them in sync; a test checks it.
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

# GDELT GKG theme terms (migration 041), one per risk category. Every code is in
# GDELT's published theme list, http://data.gdeltproject.org/api/v2/guides/LOOKUP-GKGTHEMES.TXT
# (checked 2026-11-25; a test keeps a copy of the codes used). GKG themes are
# assigned from keywords anywhere in an article's text, so a single broad theme
# (ARMEDCONFLICT alone is ~220 articles per 15-minute file, mostly unrelated
# politics) is ANDed with a second, narrower one. Volumes per 15-minute file are
# from four real GKG files (2,074-3,415 articles each); all ten together ~15
# (5-23), about 30 per 30-minute run, far under gdelt.MAX_EVENTS (1,500).
# Only the GKG file path reads these terms: the DOC fallback cannot send AND-ed
# terms, and NewsData and Currents skip ``theme:``. No GKG theme exists for embargoes or export controls: those
# are covered by SANCTIONS and by trade disputes with non-tariff measures.
GDELT_THEME_QUERIES = [
    # cyber attacks (~2)
    "theme:CYBER_ATTACK (theme:SOC_RANSOM OR theme:WB_2457_CYBER_CRIME)",
    # maritime incidents and piracy (~3)
    "theme:MARITIME_INCIDENT (theme:MARITIME_PIRACY OR theme:MANMADE_DISASTER_STRICKEN_VESSEL)",
    # port and shipping disruption (~1.5)
    "theme:WB_167_PORTS (theme:STRIKE OR theme:BLOCKADE OR theme:CLOSURE)",
    # strikes and labour action (~1.5)
    "theme:STRIKE (theme:ECON_UNIONS OR theme:TAX_FNCACT_WORKERS OR theme:TAX_FNCACT_UNION_MEMBERS)",
    # sanctions and embargoes (~1)
    "theme:SANCTIONS (theme:ECON_TRADE_DISPUTE OR theme:WB_2290_OIL_AND_GAS_EXPORT"
    " OR theme:WB_778_NON_TARIFF_MEASURES)",
    # natural disasters that hit ports or power (~5; GDACS, USGS and EONET carry the hazards themselves)
    "(theme:NATURAL_DISASTER_EARTHQUAKE OR theme:NATURAL_DISASTER_FLOOD OR theme:NATURAL_DISASTER_FLOODING"
    " OR theme:NATURAL_DISASTER_CYCLONE OR theme:NATURAL_DISASTER_HURRICANE OR theme:NATURAL_DISASTER_TYPHOON)"
    " (theme:WB_167_PORTS OR theme:MANMADE_DISASTER_POWER_DISRUPTION)",
    # armed conflict at sea, at ports or blocking transport (~2)
    "theme:ARMEDCONFLICT (theme:MARITIME_INCIDENT OR theme:BLOCKADE)"
    " (theme:WB_167_PORTS OR theme:CRISISLEX_C04_LOGISTICS_TRANSPORT)",
    # supply chain disruption: shortages with logistics trouble in a supply chain or at ports (~0.3)
    "theme:SHORTAGE theme:CRISISLEX_C04_LOGISTICS_TRANSPORT (theme:WB_1353_PHARMACEUTICAL_SUPPLY_CHAIN"
    " OR theme:WB_194_AGRICULTURAL_SUPPLY_CHAIN OR theme:WB_167_PORTS)",
    # export controls and trade restrictions (~0.5)
    "theme:ECON_TRADE_DISPUTE (theme:SANCTIONS OR theme:WB_778_NON_TARIFF_MEASURES)",
    # currency crisis (~0.5)
    "theme:EPU_CATS_SOVEREIGN_DEBT_CURRENCY_CRISES theme:ECON_CENTRALBANK"
    " (theme:ECON_CURRENCY_EXCHANGE_RATE OR theme:ECON_CURRENCY_RESERVES)",
]

OSV_PACKAGES = [
    ("PyPI", "requests"),
    ("PyPI", "django"),
    ("npm", "lodash"),
    ("npm", "axios"),
    ("Maven", "org.apache.logging.log4j:log4j-core"),
    ("Go", "golang.org/x/net"),
]

# In seeding order: migration 002's rows, then 041's (ids follow this order, and
# GDELT attributes an article to the first term it matches, so phrases come first).
DEFAULT_TERMS: list[WatchTerm] = (
    [WatchTerm("gdelt_query", q) for q in GDELT_QUERIES]
    + [WatchTerm("osv_package", name, eco) for eco, name in OSV_PACKAGES]
    + [WatchTerm("gdelt_query", q) for q in GDELT_THEME_QUERIES]
)
