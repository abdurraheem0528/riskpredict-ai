"""The `biz` schema, its row-level security and the rp_* roles, against a real Postgres.

Runs only when ``TEST_DATABASE_URL`` points at a throwaway database whose user
can create roles (CI's postgres:16 service user is a superuser). Login roles
for rp_app and rp_feeds are created here with random passwords through
``infra/scripts/create_login_roles.py`` and dropped at the end. All rows below
are test fixtures; the migrations themselves insert nothing into `biz`.
"""

from __future__ import annotations

import importlib.util
import os
import secrets
import shutil
import uuid
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path

import psycopg
import pytest
from conftest import reset_database
from psycopg import errors, sql
from psycopg.conninfo import conninfo_to_dict, make_conninfo

from jobs import db, migrate
from jobs.models import Event, FetchResult, Status

DSN = os.environ.get("TEST_DATABASE_URL")
pytestmark = pytest.mark.skipif(not DSN, reason="TEST_DATABASE_URL not set")
NOW = datetime(2026, 11, 24, 12, tzinfo=timezone.utc)
ROOT = Path(__file__).resolve().parent.parent

_spec = importlib.util.spec_from_file_location("create_login_roles", ROOT / "infra/scripts/create_login_roles.py")
roles_script = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(roles_script)

APP_LOGIN, FEEDS_LOGIN = "test_rp_app_login", "test_rp_feeds_login"
WORKSPACE_TABLES = [
    "workspaces", "memberships", "connections", "sources", "column_mappings", "entities", "risks",
    "risk_evidence", "risk_history", "scenarios", "alerts", "audit_log", "webhook_events",
]  # fmt: skip
ALL_TABLES = sorted([*WORKSPACE_TABLES, "users", "sessions"])


# --- connections ------------------------------------------------------------------


class Dsn(str):
    """A connection string that never shows its password in test output."""

    def __repr__(self) -> str:
        return f"<dsn for {conninfo_to_dict(self).get('user')}>"


def login_dsn(user: str, password: str) -> Dsn:
    params = conninfo_to_dict(DSN)
    params.update(user=user, password=password)
    return Dsn(make_conninfo(**params))


@pytest.fixture(scope="module")
def logins():
    """Create the two test login roles once per module; drop them afterwards."""
    reset_database(DSN)
    migrate.apply(DSN)
    pw = {APP_LOGIN: secrets.token_urlsafe(24), FEEDS_LOGIN: secrets.token_urlsafe(24)}
    with psycopg.connect(DSN, autocommit=True) as conn:
        for name in pw:
            conn.execute(sql.SQL("DROP ROLE IF EXISTS {}").format(sql.Identifier(name)))
        assert roles_script.ensure_login_role(conn, APP_LOGIN, "rp_app", pw[APP_LOGIN]) == "created"
        assert roles_script.ensure_login_role(conn, FEEDS_LOGIN, "rp_feeds", pw[FEEDS_LOGIN]) == "created"
    yield {"app": login_dsn(APP_LOGIN, pw[APP_LOGIN]), "feeds": login_dsn(FEEDS_LOGIN, pw[FEEDS_LOGIN])}
    reset_database(DSN)
    with psycopg.connect(DSN, autocommit=True) as conn:
        for name in pw:
            conn.execute(sql.SQL("DROP ROLE IF EXISTS {}").format(sql.Identifier(name)))


@pytest.fixture
def fresh(logins):
    """Empty, freshly migrated schemas for each test (the login roles stay)."""
    reset_database(DSN)
    migrate.apply(DSN)
    return logins


@contextmanager
def tx(dsn: str, workspace: uuid.UUID | str | None = None, user: uuid.UUID | str | None = None):
    """One transaction with the session context the API sets (transaction-local)."""
    with psycopg.connect(dsn) as conn:
        if workspace is not None:
            conn.execute("SELECT set_config('app.workspace_id', %s, true)", (str(workspace),))
        if user is not None:
            conn.execute("SELECT set_config('app.user_id', %s, true)", (str(user),))
        yield conn


def count(conn, table: str) -> int:
    return conn.execute(sql.SQL("SELECT count(*) FROM biz.{}").format(sql.Identifier(table))).fetchone()[0]


# --- fixture rows (test data only; written as rp_app, the way the API would) ------


def make_workspace(app_dsn: str, label: str) -> dict:
    ids = {k: uuid.uuid4() for k in ("ws", "user", "conn", "source", "mapping", "entity", "risk")}
    ws, user = ids["ws"], ids["user"]
    with tx(app_dsn, user=user) as c:
        c.execute(
            "INSERT INTO biz.users (id, email, password_hash) VALUES (%s, %s, %s)",
            (user, f"{label}@example.test", "$argon2id$v=19$m=19456,t=2,p=1$c2FsdA$aGFzaA"),
        )
        c.execute(
            "INSERT INTO biz.sessions (user_id, family_id, refresh_token_hash, expires_at) VALUES (%s, %s, %s, %s)",
            (user, uuid.uuid4(), secrets.token_bytes(32), NOW + timedelta(days=30)),
        )
    with tx(app_dsn, workspace=ws, user=user) as c:
        c.execute("INSERT INTO biz.workspaces (id, name) VALUES (%s, %s)", (ws, f"Workspace {label}"))
        c.execute("INSERT INTO biz.memberships (workspace_id, user_id, role) VALUES (%s, %s, 'owner')", (ws, user))
        c.execute(
            "INSERT INTO biz.connections (id, workspace_id, provider, status, refresh_token_ciphertext,"
            " refresh_token_key_id, watch_channel_id, watch_token_hash, watch_expires_at)"
            " VALUES (%s, %s, 'google_sheets', 'active', %s, 'k1', %s, %s, %s)",
            (ids["conn"], ws, secrets.token_bytes(40), f"chan-{label}", secrets.token_bytes(32), NOW + timedelta(days=6)),
        )
        c.execute(
            "INSERT INTO biz.sources (id, workspace_id, connection_id, kind, external_id, name)"
            " VALUES (%s, %s, %s, 'google_sheet', %s, 'Suppliers')",
            (ids["source"], ws, ids["conn"], f"sheet-{label}"),
        )
        c.execute(
            "INSERT INTO biz.column_mappings (id, workspace_id, source_id, version, status, mapping, confirmed_at)"
            " VALUES (%s, %s, %s, 1, 'confirmed', '{}', now())",
            (ids["mapping"], ws, ids["source"]),
        )
        c.execute(
            "INSERT INTO biz.entities (id, workspace_id, kind, name, countries, source_id, mapping_id,"
            " source_row_key, source_row_hash) VALUES (%s, %s, 'supplier', 'Test supplier', '{JPN}', %s, %s, 'row:2', 'h')",
            (ids["entity"], ws, ids["source"], ids["mapping"]),
        )
        c.execute(
            "INSERT INTO biz.risks (id, workspace_id, entity_id, fingerprint, category, title, likelihood, impact,"
            " engine_version) VALUES (%s, %s, %s, 'fp', 'natural_hazard', 'Test risk', 0.5, 0.8, 'test')",
            (ids["risk"], ws, ids["entity"]),
        )
        c.execute(
            "INSERT INTO biz.risk_evidence (workspace_id, risk_id, kind, event_source, event_raw_id, match_type,"
            " match_value, match_confidence, signal, recency, contribution)"
            " VALUES (%s, %s, 'event', 'usgs', 'x', 'country', 'JPN', 1, 0.5, 1, 0.5)",
            (ws, ids["risk"]),
        )
        c.execute(
            "INSERT INTO biz.risk_history (workspace_id, risk_id, likelihood, impact, status, reason)"
            " VALUES (%s, %s, 0.5, 0.8, 'open', 'created')",
            (ws, ids["risk"]),
        )
        c.execute(
            "INSERT INTO biz.scenarios (workspace_id, name, inputs, status, results) VALUES (%s, 's', '{}', 'completed', '{}')",
            (ws,),
        )
        c.execute(
            "INSERT INTO biz.alerts (workspace_id, risk_id, kind, severity, title, dedupe_key)"
            " VALUES (%s, %s, 'risk_new', 'warning', 't', %s)",
            (ws, ids["risk"], f"risk_new:{ids['risk']}"),
        )
        c.execute(
            "INSERT INTO biz.audit_log (workspace_id, actor_type, actor_user_id, action) VALUES (%s, 'user', %s, 'workspace.created')",
            (ws, user),
        )
        c.execute(
            "INSERT INTO biz.webhook_events (workspace_id, connection_id, provider, delivery_id, event_type)"
            " VALUES (%s, %s, 'google_drive', %s, 'change')",
            (ws, ids["conn"], f"chan-{label}:1"),
        )
    return ids


@pytest.fixture
def two(fresh):
    return fresh, make_workspace(fresh["app"], "a"), make_workspace(fresh["app"], "b")


# --- migrations ----------------------------------------------------------------------


def test_migrations_apply_from_empty_and_rerun_is_a_noop(fresh):
    assert migrate.apply(DSN) == []
    assert migrate.apply(DSN, status_only=True) == []
    with psycopg.connect(DSN) as conn:
        tables = {r[0] for r in conn.execute("SELECT tablename FROM pg_tables WHERE schemaname = 'biz'")}
        assert tables == set(ALL_TABLES)
        # Ships empty: no seed or demo rows anywhere in biz.
        assert {t: count(conn, t) for t in ALL_TABLES} == {t: 0 for t in ALL_TABLES}
        # 005 creates cluster-wide roles, so its SQL must survive running again as is.
        conn.execute((migrate.MIGRATIONS_DIR / "005_roles.sql").read_text())


def test_gap_in_numbering_and_a_late_004(fresh, tmp_path):
    """001-003 and 005+ apply with no 004; a 004 that lands later is applied on the
    next run, and tables it adds to feeds get rp_feeds/rp_app access."""
    reset_database(DSN)
    src = tmp_path / "migrations"
    src.mkdir()
    for p in migrate.migration_files():
        if not p.name.startswith("004"):
            shutil.copy(p, src / p.name)
    first = migrate.apply(DSN, directory=src)
    assert "005_roles" in first and not any(v.startswith("004") for v in first)
    (src / "004_late_probe.sql").write_text("CREATE TABLE feeds.late_probe (id int PRIMARY KEY);")
    assert migrate.apply(DSN, directory=src) == ["004_late_probe"]
    assert migrate.apply(DSN, directory=src) == []
    with psycopg.connect(DSN) as conn:
        grants = conn.execute(
            "SELECT grantee, string_agg(privilege_type, ',' ORDER BY privilege_type)"
            " FROM information_schema.role_table_grants"
            " WHERE table_schema = 'feeds' AND table_name = 'late_probe' AND grantee LIKE 'rp_%' GROUP BY 1 ORDER BY 1"
        ).fetchall()
    assert grants == [("rp_app", "SELECT"), ("rp_feeds", "DELETE,INSERT,SELECT,UPDATE")]


def test_every_biz_table_has_forced_rls_and_rp_app_owns_nothing(fresh):
    with psycopg.connect(DSN) as conn:
        rows = conn.execute(
            "SELECT c.relname, c.relrowsecurity, c.relforcerowsecurity, pg_get_userbyid(c.relowner),"
            " (SELECT count(*) FROM pg_policy p WHERE p.polrelid = c.oid)"
            " FROM pg_class c WHERE c.relnamespace = 'biz'::regnamespace AND c.relkind IN ('r', 'p', 'v', 'm')"
        ).fetchall()
        assert sorted(r[0] for r in rows) == ALL_TABLES
        for name, rls, force, owner, policies in rows:
            assert rls and force and policies >= 1, name
            assert not owner.startswith("rp_"), name
        # every table with a workspace_id is keyed on app.workspace_id
        for t in WORKSPACE_TABLES:
            quals = conn.execute(
                "SELECT string_agg(coalesce(pg_get_expr(polqual, polrelid), '') || pg_get_expr(polwithcheck, polrelid), ' ')"
                " FROM pg_policy WHERE polrelid = %s::regclass",
                (f"biz.{t}",),
            ).fetchone()[0] or ""
            assert "current_workspace_id()" in quals, t
        assert "current_setting('app.workspace_id'" in conn.execute(
            "SELECT prosrc FROM pg_proc WHERE oid = 'biz.current_workspace_id'::regproc"
        ).fetchone()[0]
        # nothing in biz is executable by PUBLIC
        public_exec = conn.execute(
            "SELECT proname FROM pg_proc WHERE pronamespace = 'biz'::regnamespace"
            " AND has_function_privilege('public', oid, 'EXECUTE')"
        ).fetchall()
        assert public_exec == []
        attrs = conn.execute(
            "SELECT rolname, rolcanlogin, rolsuper, rolbypassrls FROM pg_roles WHERE rolname LIKE 'rp\\_%' ORDER BY 1"
        ).fetchall()
        assert attrs == [("rp_app", False, False, False), ("rp_definer", False, False, False), ("rp_feeds", False, False, False)]


# --- isolation -----------------------------------------------------------------------


def test_workspace_a_cannot_read_b(two):
    logins, a, b = two
    with tx(logins["app"], workspace=a["ws"], user=a["user"]) as c:
        for t in WORKSPACE_TABLES:
            ws_col = "id" if t == "workspaces" else "workspace_id"
            seen = {r[0] for r in c.execute(sql.SQL("SELECT {} FROM biz.{}").format(sql.Identifier(ws_col), sql.Identifier(t)))}
            assert seen == {a["ws"]}, t
        assert c.execute("SELECT id FROM biz.risks WHERE id = %s", (b["risk"],)).fetchall() == []
        # users: yourself and co-members only; sessions: only your own
        assert {r[0] for r in c.execute("SELECT id FROM biz.users")} == {a["user"]}
        assert {r[0] for r in c.execute("SELECT user_id FROM biz.sessions")} == {a["user"]}


def test_workspace_a_cannot_write_b(two):
    logins, a, b = two
    app = logins["app"]
    with tx(app, workspace=a["ws"], user=a["user"]) as c:
        assert c.execute("UPDATE biz.risks SET status = 'dismissed' WHERE id = %s", (b["risk"],)).rowcount == 0
        assert c.execute("DELETE FROM biz.entities WHERE id = %s", (b["entity"],)).rowcount == 0
        assert c.execute("UPDATE biz.workspaces SET name = 'x' WHERE id = %s", (b["ws"],)).rowcount == 0
        assert c.execute("DELETE FROM biz.memberships WHERE workspace_id = %s", (b["ws"],)).rowcount == 0
    with pytest.raises(errors.InsufficientPrivilege):  # new row violates row-level security policy
        with tx(app, workspace=a["ws"]) as c:
            c.execute("INSERT INTO biz.scenarios (workspace_id, name, inputs) VALUES (%s, 's', '{}')", (b["ws"],))
    with pytest.raises(errors.InsufficientPrivilege):  # moving a row into B fails WITH CHECK
        with tx(app, workspace=a["ws"]) as c:
            c.execute("UPDATE biz.alerts SET workspace_id = %s", (b["ws"],))
    with pytest.raises(errors.InsufficientPrivilege):  # adding yourself to B
        with tx(app, workspace=a["ws"], user=a["user"]) as c:
            c.execute("INSERT INTO biz.memberships (workspace_id, user_id, role) VALUES (%s, %s, 'owner')", (b["ws"], a["user"]))
    with pytest.raises(errors.ForeignKeyViolation):  # pointing at B's source from A
        with tx(app, workspace=a["ws"]) as c:
            c.execute(
                "INSERT INTO biz.entities (workspace_id, kind, name, source_id, mapping_id, source_row_key, source_row_hash)"
                " VALUES (%s, 'site', 'x', %s, %s, 'k', 'h')",
                (a["ws"], b["source"], b["mapping"]),
            )
    with tx(app, workspace=b["ws"]) as c:
        assert c.execute("SELECT status FROM biz.risks").fetchall() == [("open",)]
        assert c.execute("SELECT name FROM biz.workspaces").fetchall() == [("Workspace b",)]


def test_missing_workspace_setting_returns_nothing(two):
    logins, a, _ = two
    for ws in (None, ""):
        with tx(logins["app"], workspace=ws) as c:
            assert {t: count(c, t) for t in ALL_TABLES} == {t: 0 for t in ALL_TABLES}
            assert c.execute("UPDATE biz.risks SET status = 'dismissed'").rowcount == 0
    # a user id alone shows that user's own identity rows and nothing of any workspace's data
    with tx(logins["app"], user=a["user"]) as c:
        assert count(c, "users") == 1 and count(c, "sessions") == 1 and count(c, "memberships") == 1
        assert count(c, "workspaces") == 1  # listed through the membership, read-only
        for t in WORKSPACE_TABLES:
            if t not in ("workspaces", "memberships"):
                assert count(c, t) == 0, t
    with pytest.raises(errors.InvalidTextRepresentation):
        with tx(logins["app"], workspace="not-a-uuid") as c:
            count(c, "risks")


def test_settings_do_not_leak_between_transactions(two):
    logins, a, _ = two
    with psycopg.connect(logins["app"]) as conn:
        with conn.transaction():
            conn.execute("SELECT set_config('app.workspace_id', %s, true)", (str(a["ws"]),))
            assert count(conn, "risks") == 1
        assert count(conn, "risks") == 0


# --- roles -----------------------------------------------------------------------------


def test_rp_feeds_cannot_touch_biz(two):
    logins, a, _ = two
    for t in ALL_TABLES:
        with pytest.raises(errors.InsufficientPrivilege):
            with tx(logins["feeds"], workspace=a["ws"]) as c:
                c.execute(sql.SQL("SELECT 1 FROM biz.{} LIMIT 1").format(sql.Identifier(t)))
    with pytest.raises(errors.InsufficientPrivilege):
        with tx(logins["feeds"]) as c:
            c.execute("SELECT * FROM biz.worker_workspace_ids()")
    with pytest.raises(errors.InsufficientPrivilege):
        with tx(logins["feeds"]) as c:
            c.execute("CREATE TABLE biz.sneaky (id int)")
    # ...while the jobs' own write path still works as rp_feeds.
    ev = Event(source="usgs", raw_id="t1", type="hazard.earthquake", title="M 5.0 - test", countries=["JPN"])
    assert db.write_results(logins["feeds"], [FetchResult("usgs", Status.OK, events=[ev])], NOW) == {"usgs": 1}
    with tx(logins["feeds"]) as c:
        assert c.execute("DELETE FROM feeds.events WHERE source = 'usgs'").rowcount == 1  # retention can delete


def test_rp_app_cannot_bypass_rls(two):
    logins, a, _ = two
    app = logins["app"]
    attempts = [
        "SET row_security = off; SELECT count(*) FROM biz.risks",
        "ALTER TABLE biz.risks DISABLE ROW LEVEL SECURITY",
        "ALTER TABLE biz.risks NO FORCE ROW LEVEL SECURITY",
        "DROP POLICY tenant ON biz.risks",
        "CREATE POLICY open ON biz.risks USING (true)",
        "SET ROLE rp_definer",
        "SET ROLE rp_feeds",
        "CREATE TABLE biz.sneaky (id int)",
        "CREATE FUNCTION biz.sneaky() RETURNS int LANGUAGE sql SECURITY DEFINER AS 'SELECT 1'",
        "UPDATE biz.audit_log SET action = 'x.y'",
        "DELETE FROM biz.audit_log",
        "UPDATE biz.risk_history SET status = 'resolved'",
        "INSERT INTO feeds.events (source, raw_id, type) VALUES ('x', '1', 't')",
        "UPDATE feeds.watch_terms SET enabled = false",
        "DELETE FROM feeds.watch_terms",
    ]
    for stmt in attempts:
        with pytest.raises((errors.InsufficientPrivilege, errors.ProgrammingError)) as exc:
            with tx(app, workspace=a["ws"]) as c:
                for part in stmt.split("; "):
                    c.execute(part)
        assert isinstance(exc.value, (errors.InsufficientPrivilege, errors.InvalidParameterValue)), stmt
    with psycopg.connect(DSN) as conn:
        assert roles_script.check_login_role(conn, APP_LOGIN, "rp_app") == []
        assert roles_script.check_login_role(conn, FEEDS_LOGIN, "rp_feeds") == []
        assert not conn.execute(
            "SELECT bool_or(r.rolbypassrls OR r.rolsuper) FROM pg_roles r WHERE pg_has_role(%s, r.oid, 'MEMBER')",
            (APP_LOGIN,),
        ).fetchone()[0]
    # Append-only holds even for the owner.
    with psycopg.connect(DSN) as conn:
        with pytest.raises(errors.InsufficientPrivilege):
            conn.execute("UPDATE biz.audit_log SET action = 'x.y'")


def test_rp_app_reads_feeds_and_may_only_add_watch_terms(two):
    logins, a, _ = two
    with tx(logins["app"], workspace=a["ws"]) as c:
        assert c.execute("SELECT count(*) FROM feeds.events").fetchone()[0] == 0
        c.execute("INSERT INTO feeds.watch_terms (kind, term, scope, origin) VALUES ('osv_package', 'flask', 'PyPI', 'workspace')")
        assert c.execute("SELECT count(*) FROM feeds.watch_terms WHERE origin = 'workspace'").fetchone()[0] == 1


def test_login_role_check_reports_dangerous_roles(fresh):
    name = "test_rp_bad_login"
    with psycopg.connect(DSN, autocommit=True) as conn:
        conn.execute(sql.SQL("DROP ROLE IF EXISTS {}").format(sql.Identifier(name)))
        conn.execute(sql.SQL("CREATE ROLE {} LOGIN BYPASSRLS IN ROLE rp_app, rp_feeds").format(sql.Identifier(name)))
        try:
            problems = roles_script.check_login_role(conn, name, "rp_app")
            assert f"{name}: has BYPASSRLS" in problems and f"{name}: also a member of rp_feeds" in problems
            # ensure_login_role strips the attributes again (memberships are left for a human to fix)
            assert roles_script.ensure_login_role(conn, name, "rp_app", None) == "unchanged"
            assert roles_script.check_login_role(conn, name, "rp_app") == [f"{name}: also a member of rp_feeds"]
            with pytest.raises(roles_script.RoleError):
                roles_script.ensure_login_role(conn, "test_rp_new_login", "rp_app", "short")
        finally:
            conn.execute(sql.SQL("DROP ROLE IF EXISTS {}").format(sql.Identifier(name)))


# --- the narrow cross-workspace lookups -------------------------------------------------


def test_definer_lookups_return_only_what_they_name(two):
    logins, a, b = two
    with tx(logins["app"]) as c:  # no context at all, as at sign-in
        assert count(c, "users") == 0
        rows = c.execute("SELECT user_id, disabled FROM biz.auth_user_by_email('A@Example.test')").fetchall()
        assert rows == [(a["user"], False)]
        assert c.execute("SELECT * FROM biz.auth_user_by_email('nobody@example.test')").fetchall() == []
        assert c.execute("SELECT workspace_id, connection_id FROM biz.route_drive_channel('chan-b')").fetchall() == [
            (b["ws"], b["conn"])
        ]
        assert c.execute("SELECT * FROM biz.route_composio_account('missing')").fetchall() == []
        assert {r[0] for r in c.execute("SELECT workspace_id FROM biz.worker_workspace_ids()")} == {a["ws"], b["ws"]}
        assert c.execute("SELECT * FROM biz.auth_session_by_refresh_hash(%s)", (b"\0" * 32,)).fetchall() == []


# --- behaviour the contracts rely on ----------------------------------------------------


def test_score_is_100_l_i_and_workspace_delete_cascades(two):
    logins, a, b = two
    with tx(logins["app"], workspace=a["ws"]) as c:
        assert c.execute("SELECT score FROM biz.risks").fetchone()[0] == pytest.approx(40.0)
        assert c.execute("SELECT score FROM biz.risk_history").fetchone()[0] == pytest.approx(40.0)
        with pytest.raises(errors.GeneratedAlways):
            c.execute("UPDATE biz.risks SET score = 99")
    with tx(logins["app"], workspace=a["ws"]) as c:
        c.execute("UPDATE biz.risks SET likelihood = 0.25, impact = 0.5")
        assert c.execute("SELECT score FROM biz.risks").fetchone()[0] == pytest.approx(12.5)
    with tx(logins["app"], workspace=a["ws"], user=a["user"]) as c:
        assert c.execute("DELETE FROM biz.workspaces WHERE id = %s", (a["ws"],)).rowcount == 1
    with tx(DSN) as conn:  # superuser: sees every workspace, so only B's rows are left
        for t in WORKSPACE_TABLES:
            ws_col = "id" if t == "workspaces" else "workspace_id"
            left = {r[0] for r in conn.execute(sql.SQL("SELECT {} FROM biz.{}").format(sql.Identifier(ws_col), sql.Identifier(t)))}
            assert left == {b["ws"]}, t
        assert count(conn, "users") == 2  # people are not deleted with a workspace


def test_connection_and_key_constraints(two):
    logins, a, _ = two
    bad = [
        # composio connections never hold a Google token
        ("INSERT INTO biz.connections (workspace_id, provider, refresh_token_ciphertext, refresh_token_key_id)"
         " VALUES (%(ws)s, 'composio:airtable', '\\x00', 'k1')", errors.CheckViolation),
        ("INSERT INTO biz.connections (workspace_id, provider) VALUES (%(ws)s, 'dropbox')", errors.CheckViolation),
        ("INSERT INTO biz.connections (workspace_id, provider, status) VALUES (%(ws)s, 'google_sheets', 'active')",
         errors.CheckViolation),
        ("UPDATE biz.entities SET countries = '{JP}'", errors.CheckViolation),
        ("UPDATE biz.entities SET ports = '{NL RTM}'", errors.CheckViolation),
        ("UPDATE biz.entities SET software = '{requests}'", errors.CheckViolation),
        ("UPDATE biz.risks SET likelihood = 1.5", errors.CheckViolation),
    ]  # fmt: skip
    for stmt, exc in bad:
        with pytest.raises(exc):
            with tx(logins["app"], workspace=a["ws"]) as c:
                c.execute(stmt, {"ws": a["ws"]})
    with tx(logins["app"], workspace=a["ws"]) as c:
        c.execute("UPDATE biz.entities SET ports = '{NLRTM}', software = '{pkg:pypi/requests@2.32.3,cpe:2.3:a:x:y:*}'")
        c.execute("INSERT INTO biz.connections (workspace_id, provider, composio_connected_account_id, status)"
                  " VALUES (%s, 'composio:airtable', 'ca_123', 'active')", (a["ws"],))  # fmt: skip
