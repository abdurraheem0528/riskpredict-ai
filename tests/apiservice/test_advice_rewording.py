"""Advice written under an older prompt is worded again (risk-engine.md §8, ADVICE_VERSION).

On the worker's runs, open risks whose Groq advice has another ``advice_version`` (or none) join
the advice queue after the run's own changes, highest score first, within ``ADVICE_PER_RUN``.
Their old text stays until a new one replaces it. Every advice text, Groq's or the template's,
is stored with the version, so up-to-date advice is never asked for again. All rows are test
fixtures.
"""

from __future__ import annotations

import dataclasses
import json
import uuid

import httpx
import pytest

pytest.importorskip("fastapi")
pytest.importorskip("rapidfuzz")  # the real engine scores here

from apiservice_fixtures import *  # noqa: E402,F401,F403  (fixtures)
from apiservice_helpers import groq_reply, sign_up  # noqa: E402

NEW = "Confirm stock with the supplier.\nLine up an alternative source.\nCheck the delivery dates you promised."


class Groq:
    """The Groq chat endpoint: answers with ``reply`` (or ``status``) and records who was asked."""

    def __init__(self, fake, reply: str = NEW, status: int = 200):
        self.reply, self.status, self.asked = reply, status, []
        fake.on("POST", "api.groq.com/openai/v1/chat/completions", self)

    def __call__(self, req: httpx.Request) -> httpx.Response:
        body = json.loads(req.content)
        self.asked.append(body["messages"][1]["content"].splitlines()[0])  # 'Entity: supplier "…"'
        if self.status != 200:
            return httpx.Response(self.status, json={"error": {"message": "rate limit"}})
        return httpx.Response(200, json=groq_reply(self.reply))


def add_supplier(owner_db, ws: str, ids: dict, name: str, criticality: str | None) -> None:
    owner_db.execute(
        "INSERT INTO biz.entities (id, workspace_id, kind, name, countries, lat, lon, criticality, source_id,"
        " mapping_id, source_row_key, source_row_hash) VALUES (%s, %s, 'supplier', %s, '{JPN}', 34.6, 135.5, %s,"
        " %s, %s, %s, 'h')",
        (uuid.uuid4(), ws, name, criticality, ids["source"], ids["mapping"], f"rec-{name}"),
    )  # fmt: skip


@pytest.fixture
def arranged(client, owner_db):
    """Four suppliers in Japan rated low, unrated (medium), high and critical, and a quake in Japan:
    one natural-hazard risk each, scores 13.5, 27, 40.5 and 54 (L 0.6 by country × I 0.9 × weight)."""
    from api.core import groq
    from test_api_isolation import seed_workspace

    groq.reset_limits()
    me = sign_up(client)
    ws = me["workspace_id"]
    ids = seed_workspace(owner_db, ws, "t")  # "Secret supplier t", criticality not set
    owner_db.execute("DELETE FROM biz.risks")
    owner_db.execute("UPDATE biz.sources SET status = 'paused', last_synced_at = now()")  # nothing to sync
    for crit in ("low", "high", "critical"):
        add_supplier(owner_db, ws, ids, f"Supplier {crit}", crit)
    owner_db.execute("INSERT INTO feeds.events (source, raw_id, type, title, countries, severity) VALUES"
                     " ('gdacs', 'EQ-1', 'hazard.earthquake', 'Quake', '{JPN}', 1.0)")  # fmt: skip
    return ws, ids


def advice(owner_db) -> dict[str, dict]:
    rows = owner_db.execute(
        "SELECT e.name, r.score, r.advice, r.advice_source, r.advice_version FROM biz.risks r"
        " JOIN biz.entities e ON e.id = r.entity_id WHERE r.status = 'open'"
    ).fetchall()  # fmt: skip
    return {r["name"]: r for r in rows}


def run(settings) -> None:
    """One run-once worker: the database pass scores a workspace whose feeds moved, or whose risks
    have older Groq advice, then words the advice."""
    from worker.main import Worker

    assert Worker(settings).run_once() == 0


def test_older_groq_advice_is_worded_again_highest_score_first(arranged, owner_db, settings, fake_http):
    import engine
    from uuid import UUID

    from worker.main import Worker

    ws, ids = arranged
    g = Groq(fake_http)
    w = Worker(settings)
    w.db.open()
    try:
        w.workspace_pass(UUID(ws), score=True)
    finally:
        w.db.close()
    first = advice(owner_db)
    assert sorted(r["score"] for r in first.values()) == [13.5, 27.0, 40.5, 54.0]
    assert {(r["advice"], r["advice_source"], r["advice_version"]) for r in first.values()} == {
        (NEW, "groq", engine.ADVICE_VERSION)
    }
    assert len(g.asked) == 4

    # Advice from before this change (no version) and from an older prompt ("1").
    owner_db.execute("UPDATE biz.risks SET advice = 'Old wording, route through the Gulf of Aden.',"
                     " advice_version = CASE WHEN score > 30 THEN NULL ELSE '1' END")  # fmt: skip
    # A new supplier: its new risk is this run's own change, worded before any older advice.
    add_supplier(owner_db, ws, ids, "Supplier new", "medium")
    g.asked.clear()
    two = dataclasses.replace(settings, advice_per_run=2)

    run(two)  # the feeds did not move; the older advice alone makes the worker score
    assert g.asked == ['Entity: supplier "Supplier new"', 'Entity: supplier "Supplier critical"']
    now = advice(owner_db)
    assert (now["Supplier new"]["advice"], now["Supplier new"]["advice_version"]) == (NEW, engine.ADVICE_VERSION)
    assert (now["Supplier critical"]["advice"], now["Supplier critical"]["advice_version"]) == (NEW, engine.ADVICE_VERSION)
    for name in ("Supplier high", "Secret supplier t", "Supplier low"):  # kept, never blanked
        assert now[name]["advice"] == "Old wording, route through the Gulf of Aden.", name
        assert now[name]["advice_source"] == "groq" and now[name]["advice_version"] in (None, "1")

    g.asked.clear()
    run(two)  # the next two by score: 40.5, then 27
    assert g.asked == ['Entity: supplier "Supplier high"', 'Entity: supplier "Secret supplier t"']
    g.asked.clear()
    run(two)
    assert g.asked == ['Entity: supplier "Supplier low"']
    assert {r["advice_version"] for r in advice(owner_db).values()} == {engine.ADVICE_VERSION}

    # Up to date: nothing is asked again, and with the feeds still where they were nothing is scored.
    g.asked.clear()
    run(two)
    assert g.asked == []


def test_older_advice_is_kept_while_groq_is_unavailable(arranged, owner_db, settings, fake_http):
    import engine

    from uuid import UUID

    from worker.main import Worker

    ws, _ = arranged
    g = Groq(fake_http)
    w = Worker(settings)
    w.db.open()
    try:
        w.workspace_pass(UUID(ws), score=True)
    finally:
        w.db.close()
    owner_db.execute("UPDATE biz.risks SET advice = 'Old wording.', advice_version = NULL")
    g.status = 429
    g.asked.clear()
    run(settings)
    assert len(g.asked) >= 1
    rows = advice(owner_db).values()
    assert {(r["advice"], r["advice_source"], r["advice_version"]) for r in rows} == {("Old wording.", "groq", None)}

    # Back, but its reply fails the checks (four lines): the template replaces the older wording,
    # with the version, so it is not asked for again.
    from api.core import groq

    groq.reset_limits()
    g.status, g.reply = 200, "Call them.\nOrder early.\nCheck terms.\nTell the team."
    g.asked.clear()
    run(settings)
    assert len(g.asked) == 4
    rows = advice(owner_db).values()
    assert {(r["advice_source"], r["advice_version"]) for r in rows} == {("template", engine.ADVICE_VERSION)}
    assert all(r["advice"].startswith("Contact ") for r in rows)  # the natural-hazard supplier template
    g.asked.clear()
    run(settings)
    assert g.asked == []


def test_a_request_does_not_word_older_advice_again(arranged, owner_db, settings, fake_http):
    """Inside an API request (api/work.py) only the run's own changes are worded: re-wording waits
    for the worker, so a request does not spend its time budget on Groq calls."""
    from uuid import UUID

    from worker import passes, sync

    from api.core.db import Database

    ws, _ = arranged
    g = Groq(fake_http)
    db = Database(settings.database_url, max_size=1, max_idle=settings.db_max_idle_seconds)
    db.open()
    try:
        ctx = sync.Ctx(settings, db)
        passes.workspace_pass(ctx, UUID(ws), webhooks=False, connections=False, sources=False, score=True)
        assert len(g.asked) == 4
        owner_db.execute("UPDATE biz.risks SET advice_version = NULL")
        g.asked.clear()
        passes.workspace_pass(ctx, UUID(ws), webhooks=False, connections=False, sources=False, score=True)
        assert g.asked == []
    finally:
        db.close()


def test_valid_advice_holds_a_reply_to_three_short_steps():
    from worker.scoring import valid_advice

    msgs = [{"content": "Score 54.0"}]
    words = lambda n, w="check": " ".join([w] * n)  # noqa: E731
    good = "Ask your forwarder about rerouting around the Cape of Good Hope.\nOrder early.\nCheck contract terms."
    assert valid_advice(good, msgs)
    assert valid_advice("- Ask your forwarder about delays.\n- Order early.\n\n- Check terms.", msgs)  # bullets, a gap
    assert valid_advice(f"{words(20)}\n{words(20)}\n{words(20)}", msgs)  # 3 × 20 = 60: at the limits
    assert not valid_advice("Call them.\nOrder early.\nCheck terms.\nTell the team.", msgs)  # 4 lines
    assert not valid_advice(f"{words(21)}\nOrder early.", msgs)  # a 21-word line
    # 61 words in all (with at most 3 lines of at most 20 words a reply cannot pass 60; this backs that up)
    assert not valid_advice(f"{words(20)}\n{words(20)}\n{words(21)}", msgs)
    assert not valid_advice("Ask the carriers about delays, read the advisory, and check every contract you hold with "
                            "them today. Order early. Check terms now.", msgs)  # one paragraph is one line: 22 words  # fmt: skip
    # the rules from before still hold
    assert not valid_advice("See https://example.com for details.", msgs)
    assert not valid_advice("There is a 37% chance of delay.", msgs)
    assert valid_advice("Score 54.0 is high; review stock.", msgs)
