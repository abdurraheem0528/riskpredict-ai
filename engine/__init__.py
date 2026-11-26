"""RiskPredict risk engine: entities + public feeds → scored risks (docs/contracts/risk-engine.md).

Deterministic: ``score = 100 × likelihood × impact`` from fixed rules. No LLM,
no randomness, no network. See ``engine/README.md`` for each rule with an example.
"""

from engine.advice import advice_messages, needs_new_advice, template_advice
from engine.config import DEFAULT_CONFIG, EngineConfig
from engine.core import ENGINE_VERSION, assess
from engine.db import EngineContextError, load_entities, load_feeds, run_workspace, write_risks
from engine.models import (
    Anomaly,
    Assessment,
    Entity,
    EventTag,
    Evidence,
    FeedEvent,
    FeedSnapshot,
    RiskChange,
    RunResult,
)
from engine.places import city_position, place_label, port_name
from engine.reference import chokepoint_id, chokepoints
from engine.scenario import (
    EntityOverride,
    FxShock,
    HazardShock,
    SanctionsShock,
    ScenarioError,
    ScenarioInputs,
    run_scenario,
    scenario_results,
)
from engine.watch import watch_terms_for

__all__ = [
    "ENGINE_VERSION", "EngineConfig", "DEFAULT_CONFIG",
    "Entity", "FeedEvent", "EventTag", "Anomaly", "FeedSnapshot", "Evidence", "Assessment",
    "RunResult", "RiskChange", "ScenarioInputs", "HazardShock", "FxShock", "SanctionsShock", "EntityOverride",
    "ScenarioError", "EngineContextError",
    "assess", "load_entities", "load_feeds", "write_risks", "run_workspace", "run_scenario", "scenario_results",
    "watch_terms_for", "advice_messages", "template_advice", "needs_new_advice",
    "chokepoints", "chokepoint_id", "city_position", "port_name", "place_label",
]  # fmt: skip
