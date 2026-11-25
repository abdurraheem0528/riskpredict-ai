"""Advice wording (risk-engine.md §8). Groq only words it; nothing here changes a number.

``advice_messages`` builds the chat messages for the worker's Groq call;
``template_advice`` is the deterministic fallback; ``needs_new_advice`` says
when existing advice is stale.
"""

from __future__ import annotations

import math

from engine.models import Assessment, Entity

SYSTEM_PROMPT = (
    "You write short risk-mitigation advice for a business risk dashboard. "
    "Reply in plain text, at most 120 words, with 2-3 concrete mitigation steps the business can take. "
    "Do not add any new numbers, percentages or probabilities. Do not include links. "
    "Do not restate the score. Do not invent facts beyond the risk described."
)

TEMPLATES = {
    "natural_hazard": (
        "Contact {name} to confirm its status and any damage or delays. Check safety stock and lead times for "
        "what depends on it, and line up an alternative source or site in case operations stay disrupted."
    ),
    "logistics": (
        "Ask your forwarder about delays and rerouting options for shipments involving {name}. Bring forward or "
        "split urgent orders, and review buffer stock for goods that move through the affected port or route."
    ),
    "geopolitical": (
        "Monitor the situation around {name} and confirm with local contacts whether operations or deliveries "
        "are affected. Review contingency suppliers or routes, and check staff travel and safety guidance."
    ),
    "sanctions": (
        "Pause new orders and payments to {name} until compliance has reviewed the possible match against the "
        "sanctions list. Compare identifiers such as address, registration number and ownership, and record the "
        "outcome of the review."
    ),
    "trade_policy": (
        "Check whether the policy change affects tariffs, quotas or licences for goods linked to {name}. Ask "
        "your customs broker about classification and timing, and review contract terms for cost pass-through."
    ),
    "cyber": (
        "Find where {name} is deployed and apply the vendor's patch or mitigation, starting with internet-facing "
        "systems. Watch logs for signs of exploitation and confirm affected suppliers have patched too."
    ),
    "financial": (
        "Review open orders, invoices and contracts in the affected currency or market for {name}. Consider "
        "hedging or renegotiating payment terms, and check the financial health of counterparties exposed to it."
    ),
    "labour": (
        "Ask {name} how the labour action affects production or deliveries and for how long. Bring forward "
        "critical orders where possible and prepare an alternative supplier or carrier."
    ),
    "other": (
        "Read the recent coverage about {name} and confirm with your contact there whether it affects your "
        "business. Note any follow-up actions with the owner of this relationship."
    ),
}


def advice_messages(risk: Assessment, entity: Entity) -> list[dict]:
    """Chat messages for Groq. Only the entity kind and name, the risk texts, up to 5
    evidence titles and dates, and L, I and score go in; nothing else from the workspace."""
    lines = [
        f"Entity: {entity.kind} \"{entity.name}\"",
        f"Category: {risk.category}",
        f"Risk: {risk.title}",
        f"Explanation: {risk.explanation}",
        f"Likelihood: {risk.likelihood:.3f}; impact: {risk.impact:.3f}; score: {risk.score:.1f}",
        "Evidence:",
    ]
    for ev in risk.evidence[:5]:
        when = (ev.snapshot.get("published_at") or "")[:10]
        lines.append(f"- {ev.snapshot.get('title', '')}" + (f" ({when})" if when else ""))
    lines.append("Write the mitigation advice.")
    return [{"role": "system", "content": SYSTEM_PROMPT}, {"role": "user", "content": "\n".join(lines)}]


def template_advice(risk: Assessment, entity: Entity) -> str:
    """Deterministic per-category advice, used when Groq is unavailable or its reply fails validation."""
    return TEMPLATES.get(risk.category, TEMPLATES["other"]).format(name=entity.name)


def score_band(score: float) -> int:
    """0–20 → 0, 20–40 → 1, …, 80–100 → 4."""
    return min(4, max(0, math.floor(score / 20.0)))


def needs_new_advice(old_score: float | None, new_score: float, old_evidence: set[str], new_evidence: set[str]) -> bool:
    """True when there is no advice yet, the score moved into another 20-point band, or the
    top-5 evidence keys changed (pass the top-5 key sets)."""
    if old_score is None:
        return True
    return score_band(old_score) != score_band(new_score) or set(old_evidence) != set(new_evidence)
