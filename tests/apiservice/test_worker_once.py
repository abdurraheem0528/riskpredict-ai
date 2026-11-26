"""``python -m worker --once``: the run-once GitHub Actions job (.github/workflows/worker.yml).

One database pass (webhooks, scoring when the feeds moved or a request left
``score_pending``, housekeeping and the 24 h event retention), then the
re-read, then exit. Its memory of what it has seen lives in ``biz.worker_state``,
because every run is a fresh process. All rows are test fixtures.
"""

from __future__ import annotations

import logging
import uuid

import pytest

pytest.importorskip("fastapi")

from apiservice_fixtures import *  # noqa: E402,F401,F403  (fixtures)
from apiservice_helpers import event_names, sign_up  # noqa: E402


def feeds_ran(db) -> None:
    db.execute("INSERT INTO feeds.source_health (source, status, last_run_at, last_success_at)"
               " VALUES ('usgs', 'ok', clock_timestamp(), clock_timestamp())"
               " ON CONFLICT (source) DO UPDATE SET last_run_at = EXCLUDED.last_run_at")  # fmt: skip


def arrange(client, owner_db) -> str:
    from test_api_isolation import seed_workspace

    me = sign_up(client)
    ws = me["workspace_id"]
    seed_workspace(owner_db, ws, "o")
    # paused: the webhook below marks nothing due and the re-read skips it, so no tool is called
    owner_db.execute("UPDATE biz.sources SET status = 'paused', last_synced_at = now()")
    owner_db.execute("DELETE FROM biz.risks")
    owner_db.execute("INSERT INTO feeds.events (source, raw_id, type, title, countries, severity, lat, lon)"
                     " VALUES ('usgs', 'once-1', 'hazard.earthquake', 'M6 near Osaka', '{JPN}', 0.9, 34.65, 135.45)")  # fmt: skip
    feeds_ran(owner_db)
    # a Composio trigger delivery that no request got round to (its time budget ran out)
    cid = owner_db.execute("SELECT id FROM biz.connections").fetchone()["id"]
    owner_db.execute("INSERT INTO biz.webhook_events (workspace_id, connection_id, provider, delivery_id, event_type)"
                     " VALUES (%s, %s, 'composio', 'left-over', 'AIRTABLE_RECORD_CHANGED')", (ws, cid))  # fmt: skip
    # an event from two days ago
    owner_db.execute("INSERT INTO biz.events (workspace_id, event, data, created_at)"
                     " VALUES (%s, 'source.synced', '{}', now() - interval '2 days')", (ws,))  # fmt: skip
    return ws


def test_once_does_one_pass_remembers_it_and_exits(client, owner_db, settings, fake_http, caplog):
    from worker.main import Worker

    ws = arrange(client, owner_db)
    caplog.set_level(logging.INFO, logger="riskpredict")
    assert Worker(settings).run_once() == 0

    assert owner_db.execute("SELECT status FROM biz.webhook_events").fetchone()["status"] == "processed"
    risks = owner_db.execute("SELECT category FROM biz.risks").fetchall()
    assert [r["category"] for r in risks] == ["natural_hazard"]
    state = owner_db.execute("SELECT * FROM biz.worker_state WHERE workspace_id = %s", (ws,)).fetchone()
    seen = owner_db.execute("SELECT max(last_run_at) AS t FROM feeds.source_health").fetchone()["t"]
    assert state["feeds_seen_at"] == seen and state["score_pending"] is False
    assert state["events_pruned_through"] > 0  # the two-day-old event is gone
    names = event_names(owner_db, ws)
    assert "risk.created" in names and "feeds.updated" not in names  # first run: nothing to compare with
    assert "database pass: 1 workspaces, 1 done, 1 scored, 0 left for the next run" in caplog.text

    # A second run (a new process) with the feeds unchanged scores nothing and writes nothing.
    before = len(event_names(owner_db, ws))
    assert Worker(settings).run_once() == 0
    assert len(event_names(owner_db, ws)) == before
    assert "1 done, 0 scored" in caplog.text
    assert [r for r in fake_http.calls if r.url.host != "api.groq.com"] == []  # only the advice wording

    # The feeds moved on: the next run re-scores and tells the dashboard.
    feeds_ran(owner_db)
    assert Worker(settings).run_once() == 0
    assert event_names(owner_db, ws)[-1] == "feeds.updated"


def test_score_pending_is_honoured_without_new_feeds(client, owner_db, settings, fake_http):
    from worker.main import Worker

    ws = arrange(client, owner_db)
    assert Worker(settings).run_once() == 0
    owner_db.execute("DELETE FROM biz.risks")
    owner_db.execute("UPDATE biz.worker_state SET score_pending = true")  # a request ran out of time
    assert Worker(settings).run_once() == 0
    assert owner_db.execute("SELECT count(*) AS n FROM biz.risks").fetchone()["n"] == 1
    assert owner_db.execute("SELECT score_pending FROM biz.worker_state WHERE workspace_id = %s", (ws,)).fetchone()[
        "score_pending"] is False  # fmt: skip


def test_an_exhausted_budget_leaves_work_for_the_next_run(client, owner_db, settings, fake_http, caplog):
    import dataclasses

    from worker.main import Worker

    arrange(client, owner_db)
    caplog.set_level(logging.INFO, logger="riskpredict")
    assert Worker(dataclasses.replace(settings, worker_once_budget_seconds=0)).run_once() == 0
    assert "0 done, 0 scored, 1 left for the next run" in caplog.text
    assert owner_db.execute("SELECT status FROM biz.webhook_events").fetchone()["status"] == "received"


def test_main_needs_the_database_url_and_logs_no_values(monkeypatch, caplog, settings, owner_db):
    from worker.main import main

    monkeypatch.delenv("DATABASE_URL_APP", raising=False)
    assert main(["--once"]) == 2
    monkeypatch.setenv("DATABASE_URL_APP", settings.database_url)
    monkeypatch.setenv("TOKEN_ENCRYPTION_KEYS", "k1:not-base64!")
    assert main(["--once"]) == 2  # a configuration error names the variable, never its value
    assert "not-base64" not in caplog.text and "TOKEN_ENCRYPTION_KEYS" in caplog.text
    monkeypatch.delenv("TOKEN_ENCRYPTION_KEYS")
    assert main(["--once"]) == 0
    assert settings.database_url not in caplog.text
    assert str(uuid.UUID(int=0)) not in caplog.text
