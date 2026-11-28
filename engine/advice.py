"""Advice wording (risk-engine.md §8). Groq only words it; nothing here changes a number.

``advice_messages`` builds the chat messages for the worker's Groq call (with where the
trouble is and, for a chokepoint, its usual detour, so the model names no other place);
``template_advice`` is the deterministic fallback; ``needs_new_advice`` says when existing
advice is stale; ``ADVICE_VERSION`` names the prompt the advice was written under.
"""

from __future__ import annotations

import math
from collections.abc import Mapping

from engine.models import Assessment, Entity

# The prompt and the checks the advice is written under (§8). The worker stores it with every
# advice text and re-words open risks whose Groq advice has another version (or none), so a
# change to SYSTEM_PROMPT, the user message or the worker's checks is a bump. It changes no
# number, so it is not an ENGINE_VERSION bump.
ADVICE_VERSION = "2"

SYSTEM_PROMPT = (
    "You write risk-mitigation advice for a business risk dashboard. "
    "Reply in plain text with exactly 3 steps the business can take, one step per line, "
    "with no numbering, bullets or headings. "
    "Each step is one sentence of at most 16 words and starts with a verb. "
    "Name no place, route, port or company unless it appears in the message. "
    "If a step suggests rerouting, name only the route given as the usual detour; "
    "if the message gives none or says there is none, do not name a route. "
    "Do not add any new numbers, percentages or probabilities. Do not include links. "
    "Do not restate the score. Do not invent facts beyond the risk described."
)

# Match types whose value is a place; for a currency, commodity, software or company match the
# risk texts already name the thing, and no Place: line is sent.
PLACE_MATCH_TYPES = ("country", "port", "chokepoint", "geo_radius")

# Deterministic advice by risk category × entity kind (§8). Each kind is a different thing to
# act on: a supplier or customer is a company you can ask, a site is your own location, a
# route or port is how goods move, a currency or commodity is a market, software is a product
# you run. Only software gets patching advice. No digits (the Groq check allows none either).
# The dashboard shows one step per sentence: at most 3 sentences of at most 20 words each
# (``{name}`` counted as one word), the limits of Groq's reply (worker ``valid_advice``).
TEMPLATES: dict[str, dict[str, str]] = {
    "natural_hazard": {
        "supplier": "Contact {name} to confirm its status and any damage or delays. Check safety stock and lead times "
                    "for what you buy from it. Line up an alternative source in case its operations stay disrupted.",
        "customer": "Contact {name} to ask whether the event affects its operations or its orders with you. Review "
                    "open orders and deliveries to it, and expect changes to demand or delivery dates.",
        "site": "Confirm the safety of staff at {name} and check the site and its equipment for damage. Follow your "
                "continuity plan, and line up another site or supplier for work that cannot wait.",
        "route": "Ask your forwarder whether shipments on {name} are delayed or being rerouted. Review buffer stock "
                 "for goods on this route and agree on alternatives for urgent orders.",
        "port": "Ask your forwarder about closures or congestion at {name}. Check which shipments are due through it "
                "and whether they can move through another port.",
        "currency": "A natural disaster can move exchange rates. Review open orders, invoices and contracts in {name}, "
                    "and consider hedging larger payments until the situation settles.",
        "commodity": "Check whether the event affects the supply or price of {name}. Ask your suppliers about "
                     "allocation and lead times, and review stock for the products that depend on it.",
        "software": "Ask the vendor or hosting provider of {name} whether the event affects its service. Check that "
                    "backups and failover work for the systems that depend on it.",
    },
    "logistics": {
        "supplier": "Ask {name} and your forwarder about delays and rerouting for its shipments. Bring forward or "
                    "split urgent orders, and review buffer stock for what you buy from it.",
        "customer": "Tell {name} early about possible delays to its deliveries and agree on priorities. Ask your "
                    "forwarder about rerouting options and update the dates you promised.",
        "site": "Check which inbound and outbound shipments for {name} are affected, and ask your forwarder about "
                "rerouting. Review stock at the site for materials that could run short.",
        "route": "Ask your forwarder about delays and rerouting options for shipments on {name}. Bring forward or "
                 "split urgent orders, and review buffer stock for goods on this route.",
        "port": "Ask your forwarder about delays at {name} and whether shipments can move through another port. "
                "Bring forward urgent orders and review buffer stock for goods that pass through it.",
        "currency": "Shipping delays can shift when you pay or get paid in {name}. Review open orders and payment "
                    "dates in it, and keep enough of it on hand for payments that come due.",
        "commodity": "Ask your suppliers whether shipping delays affect deliveries of {name}. Review stock and lead "
                     "times, and consider ordering early or from a closer source.",
        "software": "Ask the vendor of {name} whether the disruption delays hardware, licences or services you are "
                    "waiting for. Plan around the delay.",
    },
    "geopolitical": {
        "supplier": "Monitor the situation around {name} and ask it whether operations or deliveries are affected. "
                    "Review alternative suppliers for what you buy from it, and check travel and safety guidance for "
                    "staff who visit.",
        "customer": "Monitor the situation around {name} and ask whether its orders or payments to you are affected. "
                    "Review credit terms and open orders, and check whether export rules apply.",
        "site": "Confirm the safety of staff at {name} and follow official travel and security guidance. Review your "
                "continuity plan and whether work can move to another site.",
        "route": "Ask your forwarder whether shipments on {name} face delays, rerouting or higher insurance costs. "
                 "Review buffer stock and alternative routes for urgent goods.",
        "port": "Ask your forwarder whether {name} is operating normally and whether shipments face delays or extra "
                "checks. Plan another port for urgent goods.",
        "currency": "Political events can move {name} sharply. Review open orders, invoices and contracts in it, and "
                    "consider hedging or shorter payment terms.",
        "commodity": "Check whether the situation affects the supply or price of {name}. Ask your suppliers about "
                     "allocation and lead times, and consider securing stock early.",
        "software": "Check whether the situation affects the vendor of {name}, its support or where your data is "
                    "hosted. Review export rules and plan a replacement in case the vendor becomes unavailable.",
    },
    "sanctions": {  # sanctions news or measures; a list *name* match is SANCTIONS_NAME below
        "supplier": "Check whether the new sanctions affect {name}: its country, owners, banks or the goods you buy "
                    "from it. Ask compliance before new orders or payments, and record what you checked.",
        "customer": "Check whether the new sanctions restrict selling to {name}, including export controls on your "
                    "goods. Ask compliance before new orders, and review how it pays you.",
        "site": "Check whether the new sanctions affect {name}: its suppliers, banks and service providers. Ask "
                "compliance which transactions need a review.",
        "route": "Check whether the sanctions restrict carriers, vessels, ports or goods on {name}. Ask your "
                 "forwarder to confirm the carriers and ports on this route are not affected. Keep the checks on "
                 "file.",
        "port": "Check whether the sanctions apply to {name}, its operators or ships calling there. Ask your "
                "forwarder to confirm shipments through it are allowed, and keep the checks on file.",
        "currency": "Check whether the sanctions restrict payments in {name} or the banks that clear them. Ask your "
                    "bank which payments need extra checks, and plan another way to pay if needed.",
        "commodity": "Check whether the sanctions restrict buying or importing {name} from the countries involved. "
                     "Ask suppliers to confirm the origin of what they ship, and keep the records.",
        "software": "Check whether the sanctions restrict using or getting support for {name}, or affect its vendor. "
                    "Ask compliance, and plan a replacement in case updates or licences stop.",
    },
    "trade_policy": {
        "supplier": "Check whether the policy change affects tariffs, quotas or licences for what you buy from {name}. "
                    "Ask your customs broker about classification and timing, and review contract terms for cost "
                    "pass-through.",
        "customer": "Check whether the policy change affects tariffs or licences for what you sell to {name}. Agree "
                    "with it who bears new costs, and review contract terms and delivery timing.",
        "site": "Check whether the policy change affects goods that {name} imports or exports. Ask your customs broker "
                "about classification and timing, and review sourcing for affected inputs.",
        "route": "Check whether the policy change adds tariffs, checks or licences for goods on {name}. Ask your "
                 "customs broker about timing and plan for longer clearance.",
        "port": "Ask your customs broker whether the policy change adds checks or delays for goods cleared at {name}. "
                "Plan for longer clearance times.",
        "currency": "Trade policy changes can move {name}. Review open orders and contracts in it, and consider "
                    "hedging larger payments.",
        "commodity": "Check whether the policy change affects tariffs, quotas or licences for {name}. Ask your customs "
                     "broker about classification and timing, and compare costs from other origins.",
        "software": "Check whether export controls or the policy change restrict buying, using or getting support for "
                    "{name}. Ask the vendor, and plan alternatives in case licences change.",
    },
    "cyber": {
        "supplier": "Ask {name} whether the incident affects its systems, its deliveries or data it holds about you. "
                    "Check the data and system access you share with it, and limit it until it confirms it is safe. "
                    "Plan for delays to its orders.",
        "customer": "Ask {name} whether the incident affects its systems, its orders or its payments to you. Treat "
                    "changed bank details or unusual requests in its name with care, and review the data you share.",
        "site": "Have your IT or security team check the systems at {name} for signs of the incident. Make sure "
                "backups work and that staff know how to report suspicious messages.",
        "route": "Ask your forwarder and carriers on {name} whether the incident affects booking, tracking or customs "
                 "systems. Plan for delays and confirm shipment details through a second channel.",
        "port": "Ask your forwarder whether the incident affects systems at {name}, such as terminal booking or "
                "customs. Plan for delays to shipments through it.",
        "currency": "Ask your bank whether the incident affects payment systems for {name}. Confirm large payments "
                    "through a second channel and watch for fraudulent payment requests.",
        "commodity": "Ask your suppliers of {name} whether the incident affects their production or deliveries. Plan "
                     "for delays and review stock.",
        "software": "Find where {name} is deployed and apply the vendor's patch or mitigation, starting with "
                    "internet-facing systems. Watch logs for signs of exploitation and confirm affected suppliers "
                    "have patched too.",
    },
    "financial": {
        "supplier": "Review open orders, invoices and contracts with {name}, including what you pay it in another "
                    "currency. Consider hedging or renegotiating payment terms, and check its financial health before "
                    "large prepayments.",
        "customer": "Review open invoices and credit terms for {name}, including what it pays you in another currency. "
                    "Consider hedging, shorter payment terms or credit insurance, and watch for late payments.",
        "site": "Review the costs of {name} paid in the affected currency or market, such as wages, rent and local "
                "suppliers. Consider hedging and update the budget.",
        "route": "Check whether freight rates, surcharges or carrier finances on {name} are affected. Review your "
                 "carrier contracts and keep an alternative carrier in mind.",
        "port": "Check whether port fees, surcharges or the finances of operators at {name} are affected. Review the "
                "contract with your forwarder.",
        "currency": "Review open orders, invoices and contracts in {name}. Consider hedging larger payments or "
                    "agreeing prices in another currency, and update budgets for the new rate.",
        "commodity": "Check how the price move in {name} affects your costs and contracts. Consider fixing prices or "
                     "hedging larger volumes, and review which products depend on it.",
        "software": "Check the financial health of the vendor of {name} and what happens to support and licences if "
                    "it fails. Keep your data exportable and know your alternatives.",
    },
    "labour": {
        "supplier": "Ask {name} how the labour action affects production or deliveries and for how long. Bring forward "
                    "critical orders where possible and prepare an alternative supplier or carrier.",
        "customer": "Ask {name} how the labour action affects its operations and its orders with you. Adjust delivery "
                    "plans and expect changes to demand.",
        "site": "Talk to local management at {name} about the labour action and how long it may last. Plan for lower "
                "output and move urgent work elsewhere where possible.",
        "route": "Ask your forwarder whether strikes at ports or carriers on {name} delay shipments. Bring forward "
                 "urgent orders and consider other routes or carriers.",
        "port": "Ask your forwarder how the labour action at {name} affects cargo handling and how long it may last. "
                "Consider another port for urgent shipments.",
        "currency": "Widespread labour action can weigh on {name}. Review open orders and payments in it and watch "
                    "the exchange rate.",
        "commodity": "Ask your suppliers whether the labour action affects production or deliveries of {name}. Review "
                     "stock and consider ordering early.",
        "software": "Ask the vendor of {name} whether the labour action affects its support or service, and plan for "
                    "slower responses.",
    },
    "other": {
        **{k: "Read the recent coverage about {name} and confirm with your contact there whether it affects your "
              "business. Note any follow-up actions with the owner of this relationship."
           for k in ("supplier", "customer", "site")},
        **{k: "Read the recent coverage about {name} and check whether it affects your business. Note any "
              "follow-up actions with whoever looks after it in your team."
           for k in ("route", "port", "currency", "commodity", "software")},
    },
}  # fmt: skip

# A sanctions-list name match (rule ``sanctions_name``; suppliers and customers only).
SANCTIONS_NAME = {
    "supplier": "Pause new orders and payments to {name} until compliance has reviewed the possible match against the "
                "sanctions list. Compare identifiers such as address, registration number and ownership, and record "
                "the outcome of the review.",
    "customer": "Pause new orders, deliveries and payments involving {name} until compliance has reviewed the possible "
                "match against the sanctions list. Compare identifiers such as address, registration number and "
                "ownership, and record the outcome of the review.",
}  # fmt: skip


def _matched(risk: Assessment) -> tuple[str, str] | None:
    """(match type, value) of the risk's strongest item, as ``factors.explain.matched`` gives it
    (what the API's ``Risk.where`` reads), else the first evidence item's."""
    explain = risk.factors.get("explain") if isinstance(risk.factors, Mapping) else None
    m = explain.get("matched") if isinstance(explain, Mapping) else None
    if isinstance(m, Mapping) and m.get("match_type") and m.get("match_value"):
        return str(m["match_type"]), str(m["match_value"])
    if risk.evidence:
        return risk.evidence[0].match_type, risk.evidence[0].match_value
    return None


def place_lines(risk: Assessment, entity: Entity) -> list[str]:
    """``Place: <where the trouble is>`` (the label the API shows as ``Risk.where.label``) and,
    for a chokepoint, ``Usual detour: <its detour from chokepoints.json>``, or nothing."""
    from engine.places import where_label
    from engine.reference import chokepoint_by_id

    m = _matched(risk)
    if m is None or m[0] not in PLACE_MATCH_TYPES:
        return []
    mt, mv = m
    label = where_label(mt, mv, city=entity.city, countries=risk.countries, ports=risk.ports, name=entity.name)
    out = [f"Place: {label}"] if label else []
    cp = chokepoint_by_id(mv) if mt == "chokepoint" else None
    if cp is not None and cp.detour:
        out.append(f"Usual detour: {cp.detour}")
    return out


def advice_messages(risk: Assessment, entity: Entity) -> list[dict]:
    """Chat messages for Groq. Only the entity kind and name, the category, where the trouble
    is (and a chokepoint's usual detour), the risk texts, up to 5 evidence titles and dates, and
    L, I and score go in; nothing else from the workspace."""
    lines = [
        f"Entity: {entity.kind} \"{entity.name}\"",
        f"Category: {risk.category}",
        *place_lines(risk, entity),
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


def _name_match(risk: Assessment) -> bool:
    """Driven by a sanctions-list name match: the fingerprint follows the strongest match (§5.7)."""
    parts = risk.fingerprint.split("|", 2)
    return len(parts) == 3 and parts[2].startswith("sanctions_name:")


def template_advice(risk: Assessment, entity: Entity) -> str:
    """Deterministic advice by category and entity kind, used when Groq is unavailable or its
    reply fails validation. A sanctions-list name match gets the compliance-review text."""
    if risk.category == "sanctions" and entity.kind in SANCTIONS_NAME and _name_match(risk):
        text = SANCTIONS_NAME[entity.kind]
    else:
        by_kind = TEMPLATES.get(risk.category, TEMPLATES["other"])
        text = by_kind.get(entity.kind) or TEMPLATES["other"]["route"]
    return text.replace("{name}", entity.name)


def score_band(score: float) -> int:
    """0–20 → 0, 20–40 → 1, …, 80–100 → 4."""
    return min(4, max(0, math.floor(score / 20.0)))


def needs_new_advice(old_score: float | None, new_score: float, old_evidence: set[str], new_evidence: set[str]) -> bool:
    """True when there is no advice yet, the score moved into another 20-point band, or the
    top-5 evidence keys changed (pass the top-5 key sets)."""
    if old_score is None:
        return True
    return score_band(old_score) != score_band(new_score) or set(old_evidence) != set(new_evidence)
