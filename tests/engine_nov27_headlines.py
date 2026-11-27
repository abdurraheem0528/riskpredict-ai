"""Real headlines from the live workspace on 27 Nov 2026, as test rows for engine 1.3.4 (§5.10 A–G)
and 1.3.5 (§5.10 C: bans, restrictions, curbs; blockades).

The headlines, tag categories and GDELT ``countries`` (first mention first) are those the live
API returned as evidence on 27 Nov (engine 1.3.3). The 1.3.5 rows are real headlines of 26–27 Nov:
NewsData stories the live check after 1.3.4 found dropped, a Bloomberg story of 26 Nov, and
trade-tagged stories that must stay out. The suppliers are test data modelled on the
live workspace's; the one URL host given (moderntokyotimes.com) is the outlet's own name, and
wrp.org.uk is the outlet the trailing segment names. ``CONSTRUCTED`` holds the one row that is
not a real headline. Nothing here is fetched.

Only the public engine API is used, so the same rows can be run against an older engine:

    PYTHONPATH=<checkout>:tests python tests/engine_nov27_headlines.py
"""

from __future__ import annotations

import sys
from dataclasses import dataclass, replace
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:  # pragma: no cover - when run as a script
    sys.path.append(str(ROOT))

from engine import DEFAULT_CONFIG, assess  # noqa: E402
from engine_fixtures import ent, event, snap, tag  # noqa: E402

# Supplier per country (test data; the three named in the brief carry the live names).
SUPPLIERS = {
    "DEU": ent("h-deu", "supplier", name="Rhein Sensorik GmbH", countries=["DEU"], hs_codes=["9031"],
               commodities=["Industrial sensors"], criticality="critical",
               attrs={"notes": "Pressure and position sensors for the test rigs"}),
    "IND": ent("h-ind", "supplier", name="Coromandel Power Modules", countries=["IND"], hs_codes=["8504"],
               commodities=["Power modules"], criticality="critical"),
    "JPN": ent("h-jpn", "supplier", name="Kanto Precision Capacitors", countries=["JPN"], hs_codes=["8532"],
               commodities=["Capacitors"], criticality="critical"),
    "MYS": ent("h-mys", "supplier", name="Penang Semicon Assembly", countries=["MYS"], hs_codes=["8542"],
               commodities=["Chip packaging and test"], criticality="critical"),
    "CHN": ent("h-chn", "supplier", name="Pearl River PCB Works", countries=["CHN"], hs_codes=["8534"],
               commodities=["Printed circuit boards"], criticality="critical"),
    "TWN": ent("h-twn", "supplier", name="Formosa Microcircuits", countries=["TWN"], hs_codes=["8542"],
               commodities=["Microcontrollers"], criticality="critical"),
    "KOR": ent("h-kor", "supplier", name="Hanbit Memory Works", countries=["KOR"], hs_codes=["8542"],
               commodities=["DRAM modules"], criticality="critical"),
    "NLD": ent("h-nld", "supplier", name="Brabant Precision Optics", countries=["NLD"], hs_codes=["9002"],
               commodities=["Optical lenses"], criticality="critical"),
    "USA": ent("h-usa", "supplier", name="Ohio Valley Fasteners", countries=["USA"], hs_codes=["7318"],
               commodities=["Steel fasteners"], criticality="critical"),
}  # fmt: skip

TAGS = {"conflict": "conflict", "cyber": "cyber_incident", "labour": "labour_action", "financial": "financial_stress",
        "shipping": "port_shipping_disruption", "trade": "trade_policy", "sanctions": "sanctions",
        "hazard": "natural_hazard"}  # fmt: skip


@dataclass(frozen=True)
class Row:
    expect: str  # "drop" (must no longer count), "keep" (must still count) or "either"
    category: str  # key of TAGS
    country: str  # the supplier tested
    countries: tuple[str, ...] | None  # GDELT countries, first mention first; None: not a GDELT file article
    title: str
    fixes: str = ""  # the fix that drops it (A–F)
    url: str | None = None


def _c(s: str | None) -> tuple[str, ...] | None:
    return None if s is None else tuple(s.split(","))


ROWS: tuple[Row, ...] = tuple(Row(x[0], x[1], x[2], _c(x[3]), *x[4:]) for x in [
    # --- must no longer count ---------------------------------------------------------------------
    ("drop", "conflict", "DEU", "DEU,KWT,ITA,USA,UKR,IRN,YEM,PAK,GBR,SAU",
     "Two Italians Killed in Kuwait; Iran Claims Hormuz Mine Strikes", "A"),
    ("drop", "conflict", "DEU", "DEU,UKR,AUS,RUS,USA,LBY,ITA,NOR", "Russia accuses Ukraine of trying to widen conflict zone",
     "A"),
    ("drop", "conflict", "DEU", "DEU,LBY,USA,FRA,RUS,UKR,ITA,BEL,GBR,CHN",
     "Stop the escalation toward World War III! Mobilize the international working class!", "A"),
    ("drop", "conflict", "DEU", None,
     "Germany: INSA poll shows 58 percent of citizens demand early elections amid fiscal crisis", "C"),
    ("drop", "conflict", "MYS", "MYS,THA,DZA,AUS,USA,SGP,GBR,MAR,MMR,NLD",
     "All-time great war film with 96% rating leaves Netflix on December 1 - last chance to see", "A"),
    ("drop", "conflict", "CHN", "CHN,RUS,ITA,ISR,LBY,UKR,IRN,GBR", "Strike back to stop the drive to war", "A"),
    ("drop", "conflict", "TWN", "TWN,JPN,CHN,USA", "Retired Navy petty officer sentenced to 6 years in China spying case",
     "A"),
    ("drop", "conflict", "JPN", "EGY,YEM,JPN,USA,IRN,PAK,SAU,TUR,ISR,RUS",
     "Yemen Crisis: Houthi Forward Gains near Aden – Modern Tokyo Times", "B",
     "https://moderntokyotimes.com/yemen-crisis-houthi-forward-gains-near-aden/"),
    ("drop", "conflict", "IND", "IND,BRA,FRA,SRB", "The Revolt Against Oligarchy | Countercurrents", "A"),
    ("drop", "conflict", "IND", "IND", "M/S Reliance Chemotax Industries Ltd vs Union Of India on 26 November, 2026", "D"),
    ("drop", "conflict", "IND", "IND", "M/S. Reliance Chemotex Industries Ltd vs Union Of India on 26 November, 2026",
     "D"),
    ("drop", "conflict", "IND", "IND", "M/S Mittal Pigments Private Limited vs Union Of India on 26 November, 2026", "D"),
    ("drop", "conflict", "IND", "IND,PAK", "26/11 Attacks: 18 Years Later, Remembering the Night That Changed Mumbai", "D"),
    ("drop", "conflict", "IND", "IND", "26/11 victim's brother recalls horror of Mumbai attacks", "D"),
    ("drop", "conflict", "IND", "IND",
     "'A terrorist will always remain a terrorist': 26/11 victim's brother recalls horror of Mumbai attacks", "D"),
    ("drop", "conflict", "IND", "IND,PAK",
     "15 years on, lawyers who defended 26/11 terrorist Ajmal Kasab yet to receive fees", "D"),
    ("drop", "conflict", "IND", "IND,ISR,CAN,PAK",
     "Indian mission in Canada marks 26/11 anniversary, calls for united fight against terrorism", "D"),
    ("drop", "conflict", "IND", "PAK,IND,ISR,ARG,USA",
     "Argentina's envoy remembers 26/11 attack, expresses solidarity with people of India", "D"),
    ("drop", "conflict", "IND", "USA,IND,PAK",
     "'US Stands With India In Fight Against Terrorism': Sergio Gor On 26/11 Anniversary", "D"),
    ("drop", "conflict", "IND", "IND,JPN", "You cannot blackmail govt: DK Shivakumar tells protesting ASHA workers", "C"),
    ("drop", "conflict", "IND", "IND",
     "CPI(M) leader P. Rajeeve urges police to register case against Keralam CM over remarks at Youth Congress event",
     "C"),
    ("drop", "cyber", "JPN", "JPN,FRA",
     "FIFA hackers had access to 'business and personal information,' organization says - National", "B, E"),
    ("drop", "cyber", "IND", "IND",
     "Hyderabad: Cyber Crime Police Arrest Two for Impersonation, Circulating Obscene Photos", "E"),
    ("drop", "cyber", "IND", "IND", "Interstate Cyber Fraud Syndicate Impersonating Hotels Busted By Delhi Police", "E"),
    ("drop", "cyber", "IND", "IND,MYS",
     "Indian police rescue 164 foreign nationals from alleged cybercrime, human trafficking network", "E"),
    ("drop", "cyber", "IND", "IND,USA,MYS,IDN", "Noida cybercrime raid: Police rescue 164 foreigners", "E"),
    ("drop", "cyber", "IND", "IND",
     "Delhi Police bust fake Instagram shopping, Amazon delivery fraud module; two arrested, Rs 1.04 lakh refunded",
     "E"),
    ("drop", "labour", "CHN", "CHN,RUS,UKR,AUS,USA,LBY", "Russia 'preparing new mass strike soon': Zelenskiy", "A, C"),
    ("drop", "labour", "IND", "IND,LKA,BGD,RUS,USA,CHN,IRN",
     "Sri Lankan union leader recalls how the unions helped bring the JVP/NPP government to power", "A"),
    ("drop", "labour", "IND", "IND,PHL,AUS,USA,ISR,URY,CAN,DEU,BRA,GBR",
     "Make Amazon Pay Day 2026: Amazon workers and supporters to strike and protest in over 20 countries as union wins "
     "first North American collective agreement", "A"),
    ("drop", "labour", "DEU", "DEU,CHN,PRT,BEL,NLD", "Strike Wave Over Pay Across Europe! - Workers Revolutionary Party",
     "A", "https://wrp.org.uk/news/strike-wave-over-pay-across-europe/"),
    ("drop", "financial", "CHN", "CHN,RUS,VEN,ESP", "Venezuelan Socialists Reveal Country Is $237 Billion in Debt", "A"),
    ("drop", "financial", "JPN", "JPN,CHN,LKA,GBR",
     "Wake-up call to economists in stagnant economy: Take market case to public", "A"),
    ("drop", "financial", "DEU", "DEU,GBR", "Pound-to-Euro Forecast: GBP Slides as ECB Turns Hawkish", "C"),
    ("drop", "shipping", "CHN", "CHN,GUM,TWN,KOR,USA,PLW,AUS,JPN,PHL",
     "Experts question feasibility of U.S. 1st island chain withdrawal", "A"),
    ("drop", "shipping", "KOR", "KOR,USA,TWN,PLW,AUS,JPN,PHL,CHN,GUM",
     "Experts question feasibility of U.S. 1st island chain withdrawal", "A"),
    ("drop", "shipping", "KOR", "KOR,SYC,IND,CHN,MOZ,USA,ARE,ITA,GBR,TUR",
     "India says attacks on maritime shipping and seafarers unacceptable, calls for global response", "A"),
    ("drop", "shipping", "DEU", "DEU,USA,IND,CYP,CHN,FRA,RUS,JPN,GBR,UKR",
     "Shipping industry must brace for global shift in trade routes, economist warns", "A"),
    ("drop", "shipping", "DEU", "DEU,FRA,CYP,IRL", "Port of Cork to Boulogne-sur-Mer ferry company to cease operations", "A"),
    ("drop", "shipping", "IND", "IND",
     "Kerala's Disappearing Coastline: The Impact on Coastal Communities and Livelihoods", "C"),
    ("drop", "shipping", "CHN", "CHN,MYS,BRN", "'Diplomacy key to South China Sea disputes'", "C"),
    ("drop", "trade", "IND", "IND,CHN,BEL,USA,VNM", "Trump's new tariffs aim to prise global trade from China's grip", "A"),
    ("drop", "trade", "CHN", "CHN,JPN,ARG,KOR,USA,LBR,GBR,PAN,BRA,IDN",
     "Weakening shipping carbon rules could strip poorer nations of transition finance", "A"),
    ("drop", "trade", "CHN", "CHN,VUT,AUS,FJI,SLB", "Solomons signals strategic shift as Canberra treaty looms", "A"),
    ("drop", "trade", "CHN", "IDN,CHN,BRN,VNM,MYS,PHL",
     "Indonesia's Natuna compromise with China: A point of no return - Academia", "C"),
    ("drop", "trade", "CHN", None, "Chinese firms commit $681m to Philippine expansion, targeting 7,290 jobs", "C"),
    ("drop", "trade", "JPN", None, "Japan services producer prices jump 4.2% in October, firming the case for a BOJ hike",
     "C"),
    ("drop", "sanctions", "IND", None,
     "COVID-19 and its Two-Folded Impact vis a vis India's Export Restriction on Drugs", "F"),
    # --- must still count ----------------------------------------------------------------------------
    ("keep", "labour", "KOR", "KOR", "Bread and milk: Strike disrupts regular meals at nearly 1 in 3 schools"),
    ("keep", "labour", "KOR", "KOR", "Unionized rail workers launch work-to-rule campaign, 4 train services canceled"),
    ("keep", "labour", "DEU", "DEU", "German Union Ver.di Calls For Strike At Amazon Sites On Black Friday"),
    ("keep", "cyber", "TWN", None, "Taiwan weathers wave of cyberattacks ahead of Saturday's elections"),
    ("keep", "conflict", "CHN", "PHL,CHN,BRN,TTO", "Chinese military patrols moving closer to Philippines, AFP says"),
    ("keep", "conflict", "CHN", "TWN,JPN,CHN,USA", "Retired Navy petty officer sentenced to 6 years in China spying case"),
    ("keep", "conflict", "IND", "IND",
     "In Manipur, a glass of drinking water comes at a price: Women trek through forests, villagers risk ambushes to "
     "repair pipelines amid conflict and drought"),
    ("keep", "shipping", "TWN", "CHN,TWN,TGO", "Taiwan detains China-linked cargo ship over severed undersea cable"),
    ("keep", "shipping", "CHN", "CHN,TWN,TGO", "Taiwan detains China-linked cargo ship over severed undersea cable"),
    ("keep", "shipping", "IND", "KOR,SYC,IND,CHN,MOZ,USA,ARE,ITA,GBR,TUR",
     "India says attacks on maritime shipping and seafarers unacceptable, calls for global response"),
    ("keep", "trade", "CHN", "CHN,HKG,USA,SGP,VNM,BEL",
     "Analysis-Trump's new tariffs aim to prise global trade from China's grip"),
    ("keep", "trade", "CHN", None, "US to hike tariffs on China EU Brazil and others over subsidised factories"),
    ("keep", "trade", "NLD", None, "Dutch Government Lobbies Against US Chip Export Bill"),
    ("keep", "sanctions", "CHN", None,
     "Nvidia's Most Advanced AI Chips Reach Chinese Military Alumnus And Blacklisted Firm Via US Companies: Report"),
    ("keep", "sanctions", "USA", None, "China to blacklist US defence firms"),
    # --- either way is fine --------------------------------------------------------------------------
    ("either", "conflict", "IND", None,
     "\"Terrorism knows no borders\": Israel backs India's fight against terrorism on 26/11"),
    ("either", "conflict", "DEU", None,
     "ForMin Neculaescu: I have requested a consistent German presence in Romania, including military capabilities"),
    ("either", "hazard", "IND", None, "India seizes Nepal flood crisis to rebuild influence"),
    # --- engine 1.3.5 (§5.10 C): a ban, a restriction or a curb is a trade-policy cue ------------------
    # NewsData stories 1.3.4 dropped from "Trade policy news in Netherlands affecting Atlas Eindhoven
    # Assembly Plant" / "… Atlas Rotterdam Distribution Centre" and "… in China affecting Pearl River
    # PCB Works" (worker run of 27 Nov, 17:06 UTC); Bloomberg, 26 Nov.
    ("keep", "trade", "NLD", None,
     "Netherlands Seeks to Block US Bill Restricting China’s Access to Chipmaking Tools"),
    ("keep", "trade", "CHN", None,
     "Netherlands Seeks to Block US Bill Restricting China’s Access to Chipmaking Tools"),
    ("keep", "trade", "CHN", None, "China extends pause on tighter mineral restrictions"),
    ("keep", "trade", "NLD", None, "Netherlands to Fight Rollout of Proposed US Curbs on ASML"),
    # must stay out: "deal(s)" and "duties" alone are no trade-policy cue (the last two were "either" in 1.3.4)
    ("drop", "trade", "CHN", None,
     "Ten Smart Cities for $50 Billion: What’s Behind Indonesia’s Deals With China", "C"),
    ("drop", "trade", "CHN", None, "Carney courts Chinese investment, but deals could come with real risks", "C"),
    ("drop", "trade", "JPN", None, "Japan Weighs Safety Duties for AI Firms, With Penalties for Defying Orders", "C"),
    ("either", "trade", "CHN", None,
     "Nvidia Chip Sellers Scramble to Find Customers After Government Tightens China Remote Access Rules"),
    ("either", "trade", "CHN", None, "Investigation Reveals Nvidia Chips Flowing To Chinese-Owned Firms"),
])  # fmt: skip

# Constructed for the tests (not a real headline, as tests/fixtures/README.md marks constructed
# controls): a blockade with no other shipping cue word, so only the 1.3.5 words let it count.
CONSTRUCTED: tuple[Row, ...] = (
    Row("keep", "shipping", "TWN", None, "Taiwan warns China could impose a blockade"),
)

# Copies that differ only by outlet name or wire tag: each group is one report (G).
COPY_GROUPS = (
    ("US plans tariff hikes on China, EU and others | The Arkansas Democrat-Gazette",
     "US plans tariff hikes on China, EU and others | Northwest Arkansas Democrat-Gazette"),
    ("Analysis-Trump's new tariffs aim to prise global trade from China's grip",
     "Trump's new tariffs aim to prise global trade from China's grip"),
    ("US to hike tariffs on China, EU, Brazil and others over subsidized factories | News, Sports, Jobs",
     "US to hike tariffs on China, EU, Brazil, and others over subsidized factories"),
)  # fmt: skip

# Matching is what is tested, not the minimum score, so every linked item shows.
CONFIG = replace(DEFAULT_CONFIG, min_score=0.0)


def item(row: Row, raw_id: str = "h1"):
    """The feed row: a GDELT file article when it has countries, else an RSS item (NewsData)."""
    source = "gdelt" if row.countries is not None else "newsdata"
    t = tag(TAGS[row.category], 0.8)  # the tag's own countries: those it found named in the title
    return event(source, raw_id, "news", title=row.title, tag=t, countries=row.countries or (), url=row.url)


def reaches(row: Row) -> bool:
    """Does the item reach the supplier of ``row.country`` (any category, any match)?"""
    sup = SUPPLIERS[row.country]
    out = assess([sup], snap([item(row)]), base_currency=None, config=CONFIG)
    return any(e.event_raw_id == "h1" for a in out if a.entity_id == sup.id for e in a.evidence)


if __name__ == "__main__":  # pragma: no cover - prints the verdicts of the engine on sys.path
    import engine

    print("engine", engine.ENGINE_VERSION)
    for r in ROWS + CONSTRUCTED:
        mark = " (constructed)" if r in CONSTRUCTED else ""
        print(f"{r.expect:6} {'counts' if reaches(r) else 'dropped':7} {r.category:9} {r.country} {r.title[:90]}{mark}")
