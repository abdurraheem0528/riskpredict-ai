"""Risk-engine runs per workspace, then alerts, SSE events and advice.

The engine (``engine/``, session C) is called only through the interface in
``docs/contracts/risk-engine.md``: ``load_entities → load_feeds → assess →
write_risks`` inside one transaction that has ``app.workspace_id`` set (this is
what ``run_workspace`` does; the steps are called one by one so the worker keeps
the assessments for the advice step). Nothing here computes a score.

Groq only words advice (contracts decision 9). Its reply is checked: at most
1,200 characters, no links, and no digits that were not in the prompt. Otherwise,
or when Groq is down or out of quota, the engine's template text is stored.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any
from uuid import UUID

from api.core import engine as engine_iface
from api.core import serialize as ser
from api.core.groq import GroqUnavailable, chat
from worker.sync import Ctx, create_alert

log = logging.getLogger("riskpredict.worker.scoring")
URL_RE = re.compile(r"(https?://|www\.|\b[a-z0-9-]+\.(com|org|net|io|gov)\b)", re.I)
NUM_RE = re.compile(r"\d+(?:[.,]\d+)*")


@dataclass
class RunSummary:
    workspace_id: UUID
    engine_version: str
    counts: dict
    alerts: int
    advice_groq: int
    advice_template: int


def _ts(v: Any) -> str:
    if isinstance(v, datetime):
        return ser.ts(v if v.tzinfo else v.replace(tzinfo=timezone.utc)) or ""
    return str(v)


def evidence_key(ev: Any) -> str:
    g = (lambda n: ev.get(n)) if isinstance(ev, dict) else (lambda n: getattr(ev, n, None))
    if g("kind") == "event":
        return f"event:{g('event_source')}:{g('event_raw_id')}"
    return f"anomaly:{g('anomaly_series')}|{g('anomaly_key')}|{_ts(g('anomaly_ts'))}|{g('anomaly_method')}"


def valid_advice(text: str, messages: list[dict]) -> bool:
    if not text or len(text) > 1200 or URL_RE.search(text):
        return False
    given = " ".join(str(m.get("content", "")) for m in messages)
    return all(n in given for n in NUM_RE.findall(text))


def _alerts_for(change: Any, risk: dict, threshold: float) -> list[tuple[str, str, str, str]]:
    """(kind, severity, title, dedupe key) for one RiskChange (risk-engine.md §9)."""
    kind = getattr(change, "change", "")
    new, old = getattr(change, "new_score", None), getattr(change, "old_score", None)
    rid = risk["id"]
    sev = "critical" if (new or 0) >= 80 else "warning"
    out = []
    if kind == "created" and risk["category"] == "sanctions":
        out.append(("sanctions_match", "critical", f"Possible sanctions match: {risk['title']}", f"sanctions_match:{rid}"))
    elif kind == "created" and (new or 0) >= threshold:
        out.append(("risk_new", sev, f"New risk: {risk['title']}", f"risk_new:{rid}"))
    elif kind in ("rescored", "reopened") and (new or 0) >= threshold and (old is None or old < threshold):
        out.append(("risk_escalated", sev, f"Risk escalated: {risk['title']}",
                    f"risk_escalated:{rid}:{datetime.now(timezone.utc):%Y-%m-%d}"))  # fmt: skip
    return out


SSE_FOR = {"created": "risk.created", "rescored": "risk.updated", "reopened": "risk.updated", "resolved": "risk.resolved"}


def run_workspace(ctx: Ctx, ws: UUID, *, now: datetime | None = None) -> RunSummary | None:
    eng = engine_iface.load()
    if eng is None:
        return None
    now = now or datetime.now(timezone.utc)
    with ctx.db.workspace_tx(ws) as conn:
        wsrow = conn.execute(
            "SELECT base_currency, alert_min_score, share_watch_terms FROM biz.workspaces WHERE id = %s", (ws,)
        ).fetchone()
        if wsrow is None:
            return None
        before = {r["id"]: r for r in conn.execute("SELECT id, score, advice IS NOT NULL AS has_advice FROM biz.risks").fetchall()}
        old_keys: dict[UUID, set[str]] = {}
        for r in conn.execute(
            "SELECT * FROM (SELECT ev.*, row_number() OVER (PARTITION BY risk_id ORDER BY contribution DESC, id) AS n"
            " FROM biz.risk_evidence ev) x WHERE n <= 5"
        ).fetchall():
            old_keys.setdefault(r["risk_id"], set()).add(ser.evidence_key(r))

        entities = eng.load_entities(conn)
        feeds = eng.load_feeds(conn, entities, now=now)
        assessments = eng.assess(entities, feeds, base_currency=wsrow["base_currency"])
        result = eng.write_risks(conn, ws, assessments, now=now)

        if wsrow["share_watch_terms"]:
            terms = list(eng.watch_terms_for(entities))
            if terms:
                with conn.cursor() as cur:
                    cur.executemany(
                        "INSERT INTO feeds.watch_terms (kind, term, scope, origin) VALUES (%s, %s, %s, 'workspace')"
                        " ON CONFLICT (kind, scope, term) DO NOTHING",
                        [(k, t, s) for k, t, s in terms],
                    )

        changes = list(getattr(result, "changes", ()) or ())
        ids = [c.risk_id for c in changes]
        risks = {r["id"]: r for r in conn.execute(ser.RISK_SELECT + " WHERE r.id = ANY(%s)", (ids,)).fetchall()} if ids else {}
        alerts = 0
        for c in changes:
            risk = risks.get(c.risk_id)
            if risk is None:
                continue
            body = ser.risk(risk)
            kind = getattr(c, "change", "")
            if kind == "rescored" and abs((c.new_score or 0) - (c.old_score or 0)) < 1:
                pass
            elif kind in SSE_FOR:
                ctx.outbox.add(ws, SSE_FOR[kind], {"risk": body, "previous_score": c.old_score})
            for akind, sev, title, key in _alerts_for(c, risk, float(wsrow["alert_min_score"])):
                create_alert(conn, ctx.outbox, ws, akind, sev, title,
                             f"{risk['entity_name']}: score {ser.r1(risk['score'])}", key, risk_id=risk["id"])  # fmt: skip
                alerts += 1

        # Which risks need (new) advice wording.
        by_fp = {a.fingerprint: a for a in assessments}
        ents = {e.id: e for e in entities}
        todo = []
        for c in changes:
            if getattr(c, "change", "") not in ("created", "rescored", "reopened"):
                continue
            a = by_fp.get(c.fingerprint)
            entity = ents.get(a.entity_id) if a else None
            if a is None or entity is None:
                continue
            prev = before.get(c.risk_id)
            old_score = prev["score"] if prev and prev["has_advice"] else None
            new_keys = {evidence_key(ev) for ev in list(a.evidence)[:5]}
            if eng.needs_new_advice(old_score, a.score, old_keys.get(c.risk_id, set()), new_keys):
                todo.append((c.risk_id, a, entity))
        # Risks that exist but never got advice (e.g. Groq was down last time).
        missing = [rid for rid, r in before.items() if not r["has_advice"]]
        if missing:
            fps = {r["id"]: r["fingerprint"] for r in conn.execute(
                "SELECT id, fingerprint FROM biz.risks WHERE id = ANY(%s) AND status IN ('open', 'acknowledged', 'mitigating')",
                (missing,)).fetchall()}  # fmt: skip
            queued = {t[0] for t in todo}
            for rid, fp in fps.items():
                a = by_fp.get(fp)
                if rid not in queued and a is not None and ents.get(a.entity_id) is not None:
                    todo.append((rid, a, ents[a.entity_id]))
    ctx.flush()

    groq_n = tmpl_n = 0
    for i, (rid, a, entity) in enumerate(todo):
        text, source, model = None, "template", None
        if i < ctx.settings.advice_per_run and ctx.settings.groq_api_key and ctx.settings.groq_advice_model:
            messages = eng.advice_messages(a, entity)
            try:
                reply = chat(ctx.settings, ctx.settings.groq_advice_model, messages, max_tokens=400).strip()
                if valid_advice(reply, messages):
                    text, source, model = reply, "groq", f"groq:{ctx.settings.groq_advice_model}"
            except GroqUnavailable as exc:
                log.info("advice falls back to the template (%s)", exc)
        if text is None:
            text = eng.template_advice(a, entity)
        if source == "groq":
            groq_n += 1
        else:
            tmpl_n += 1
        with ctx.db.workspace_tx(ws) as conn:
            conn.execute(
                "UPDATE biz.risks SET advice = %s, advice_source = %s, advice_model = %s, advice_generated_at = now()"
                " WHERE id = %s",
                (text, source, model, rid),
            )
    counts = dict(getattr(result, "counts", {}) or {})
    log.info("engine run: %s; %d alerts; advice %d groq / %d template", counts, alerts, groq_n, tmpl_n)
    return RunSummary(ws, str(getattr(result, "engine_version", "")), counts, alerts, groq_n, tmpl_n)
