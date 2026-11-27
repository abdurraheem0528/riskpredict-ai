"""Every constant the engine scores with (docs/contracts/risk-engine.md §5–§6).

``DEFAULT_CONFIG`` holds exactly the values in the contract. Changing any of
them can change a score, so it is an ``ENGINE_VERSION`` bump plus an update to
``risk-engine.md``.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Mapping


def _frozen(d: dict) -> Mapping:
    return MappingProxyType(dict(d))


# Legal-form tokens dropped before name comparison (§5.2 sanctions_name).
LEGAL_FORMS = frozenset(
    "ltd limited llc inc corp corporation co company gmbh ag sa sas srl spa bv nv plc jsc pjsc ooo oao zao "
    "llp lp pte pvt kk as ab oy".split()
)


@dataclass(frozen=True, slots=True)
class EngineConfig:
    # --- output ---------------------------------------------------------------------
    min_score: float = 5.0  # below this a risk is not kept (and an open one is resolved)
    max_evidence: int = 50  # evidence items per risk (likelihood uses exactly these)
    rescore_min_delta: float = 1.0  # history row 'rescored' only when the score moves this much
    # --- recency (§5.5) -------------------------------------------------------------
    cutoff_half_lives: float = 4.0  # older items are ignored ...
    max_age_days: float = 365.0  # ... and never older than this
    half_life_hours: Mapping[str, float] = field(
        default_factory=lambda: _frozen(
            {
                "natural_hazard": 72,
                "logistics": 168,
                "geopolitical": 120,
                "labour": 120,
                "trade_policy": 120,
                "other": 120,
                "sanctions": 720,
                "cyber": 720,  # KEV and tagged news
                "cyber.advisory": 2160,  # OSV (and NVD) advisories
                "financial": 120,  # fx and tagged news
                "financial.wb": 4380,  # World Bank annual indicators
            }
        )
    )
    # --- likelihood (§5.6) ------------------------------------------------------------
    likelihood_cap: float = 0.99
    # Engine 1.3.0: weighted noisy-OR, L = min(cap, 1 − Π_k (1 − decay^(k−1) × p_k)), the items
    # sorted strongest first. The strongest item counts in full (one strong item scores as
    # before); the 2nd counts half, the 3rd a quarter, … Plain noisy-OR (decay 1) let seven weak
    # country matches of 0.3 reach 0.92 ("92% likely"); with 0.5 they reach 0.49. Since
    # L ≤ Σ w_k p_k and the weights sum to < 1 / (1 − decay) = 2, the likelihood is always below
    # twice the strongest item's contribution: weak items cannot add up to "likely".
    # 1.0 restores the plain noisy-OR.
    likelihood_decay: float = 0.5
    # --- signal s (§5.4) --------------------------------------------------------------
    default_severity: float = 0.5  # gdacs / usgs / portwatch / osv without a severity
    eonet_major_signal: float = 0.5  # wildfire, volcano, severe_storm, flood
    eonet_minor_signal: float = 0.3
    eonet_major_types: frozenset[str] = frozenset({"wildfire", "volcano", "severe_storm", "flood"})
    kev_min_signal: float = 0.9
    ofac_designation_signal: float = 1.0
    ofac_update_signal: float = 0.8
    news_signal_base: float = 0.25
    news_signal_slope: float = 0.5
    news_other_signal: float = 0.15
    z_threshold: float = 3.5  # |z| here → signal 0.5
    z_full: float = 8.0  # |z| here → signal 1.0
    isolation_forest_factor: float = 0.8
    # --- match confidence c (§5.2) ----------------------------------------------------
    country_confidence: Mapping[str, float] = field(
        default_factory=lambda: _frozen({"site": 0.8, "supplier": 0.6, "customer": 0.5, "route": 0.5, "port": 0.5})
    )
    news_country_factor: float = 0.6
    # Engine 1.3.0 (§5.10): a news country match needs the country to be central (title, tag or
    # first located place); a sanctions one needs it to be the target of the measure, found in
    # the title within this many words of the sanctions word ("sanctions on Chinese chipmakers").
    sanctions_target_window: int = 5
    # Engine 1.3.4 (§5.10 A): the first place GDELT located is central only when neither the
    # headline nor the tag names a country and GDELT located at most this many countries.
    first_place_max_countries: int = 3
    geo_min_confidence: float = 0.05
    radius_km: Mapping[str, float] = field(
        default_factory=lambda: _frozen(
            {
                "tropical_cyclone": 300,
                "tsunami": 300,
                "drought": 400,
                "flood": 100,
                "volcano": 100,
                "wildfire": 50,
                "other": 100,
                "port.disruption": 50,
            }
        )
    )
    quake_base_km: float = 50.0  # R = clamp(50 × 2^(M − 5), 25, 400)
    quake_ref_magnitude: float = 5.0
    quake_min_km: float = 25.0
    quake_max_km: float = 400.0
    quake_default_km: float = 100.0  # magnitude missing
    chokepoint_text_factor: float = 0.6
    fx_direct_confidence: float = 1.0
    fx_cross_confidence: float = 0.9
    fx_history_days: int = 130  # 90-day window + 14 evaluation days + slack, for cross rates
    commodity_chapter_factor: float = 0.6
    commodity_text_factor: float = 0.8
    software_osv_confidence: float = 1.0
    software_cpe_confidence: float = 0.9  # KEV (and NVD) vendor:product
    sanctions_threshold: float = 0.92
    sanctions_base_confidence: float = 0.7
    sanctions_span: float = 0.3
    sanctions_country_bonus: float = 0.1
    sanctions_country_penalty: float = 0.2
    sanctions_min_confidence: float = 0.5
    name_min_length: int = 3  # normalised names shorter than this are never fuzzy-matched
    company_threshold: float = 0.95
    company_tag_factor: float = 0.7
    company_anomaly_confidence: float = 0.8
    legal_forms: frozenset[str] = LEGAL_FORMS
    # --- anchoring (§5.9, engine 1.2.0) -------------------------------------------------
    # A commodity, HS-chapter or currency match is *support*: it never creates a risk on its
    # own for an entity that has a place (supplier, customer, site, route, port). It only
    # strengthens a match of the same item on an anchor (country, port, position,
    # chokepoint, company name, sanctions name):  c = 1 − (1 − c_anchor)(1 − w × c_support).
    support_match_types: frozenset[str] = frozenset({"commodity", "currency"})
    support_weight: float = 0.5  # w
    # Except a market-wide move (an fx.rate anomaly, or an event of type market.*): it is
    # global for that currency or commodity, so it may create a risk alone, but such a risk
    # is capped below "High": its likelihood is lowered until 100 × L × I ≤ this.
    market_event_prefix: str = "market."
    market_only_score_cap: float = 50.0
    # A supplier, site or port with no declared route gets chokepoints inferred from its
    # port's country (engine/data/sea_lanes.json) or its position; those matches count at
    # this share of a declared route's confidence.
    inferred_chokepoint_factor: float = 0.5
    # --- impact (§6)------------------------------------------------------------------
    criticality_weights: Mapping[str, float] = field(
        default_factory=lambda: _frozen({"low": 0.25, "medium": 0.5, "high": 0.75, "critical": 1.0})
    )
    default_criticality_weight: float = 0.5
    full_exposure_share: float = 0.25
    category_multipliers: Mapping[str, float] = field(
        default_factory=lambda: _frozen(
            {
                "sanctions": 1.0,
                "natural_hazard": 0.9,
                "cyber": 0.9,
                "logistics": 0.8,
                "geopolitical": 0.7,
                "trade_policy": 0.6,
                "financial": 0.6,
                "labour": 0.6,
                "other": 0.4,
            }
        )
    )
    impact_min: float = 0.05
    impact_max: float = 1.0
    # --- watch terms (§7) -------------------------------------------------------------
    watch_gdelt_max: int = 5
    watch_name_min_length: int = 4
    # --- load bounds (Neon free tier) -------------------------------------------------
    max_events: int = 20000  # newest first; a run never reads more event rows than this
    max_anomalies: int = 5000
    max_sanctions_entries: int = 50000  # feeds.sanctions_entries rows (the full OFAC lists are ~18k)

    def half_life(self, key: str) -> float:
        return float(self.half_life_hours[key])

    def lookback_hours(self, key: str) -> float:
        return min(self.cutoff_half_lives * self.half_life(key), self.max_age_days * 24.0)


DEFAULT_CONFIG = EngineConfig()
