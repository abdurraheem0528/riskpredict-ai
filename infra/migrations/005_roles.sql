-- 005: NOLOGIN group roles, and their access to the public `feeds` schema.
--
--   rp_feeds    the GitHub Actions jobs: read/write `feeds`, nothing in `biz`
--   rp_app      the API and the worker: read `feeds`, read/write `biz` under RLS
--   rp_definer  owns the few SECURITY DEFINER lookups in `biz` (007); no login,
--               no members, never granted to a login role
--
-- Login roles (with passwords) are created outside the repository and only get
-- membership in one of these groups: see docs/database-roles.md and
-- infra/scripts/create_login_roles.py.
--
-- Roles are cluster-wide while migrations are per database, so this file is
-- written to be safe to run again (the roles may already exist).
-- Requires PostgreSQL 16 or later (Neon's default is 17).

DO $$
DECLARE
    r text;
BEGIN
    FOREACH r IN ARRAY ARRAY['rp_feeds', 'rp_app', 'rp_definer'] LOOP
        IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = r) THEN
            EXECUTE format('CREATE ROLE %I NOLOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION NOBYPASSRLS', r);
        ELSE
            -- Re-assert the attributes in case someone changed them by hand.
            EXECUTE format('ALTER ROLE %I NOLOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION NOBYPASSRLS', r);
        END IF;
    END LOOP;
END
$$;

-- `feeds`: the jobs write it, the app only reads it.
GRANT USAGE ON SCHEMA feeds TO rp_feeds, rp_app;
GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA feeds TO rp_feeds;
GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA feeds TO rp_feeds;
GRANT SELECT ON ALL TABLES IN SCHEMA feeds TO rp_app;

-- The worker may add watch terms derived from a workspace's entities (package
-- names for OSV, supplier queries for GDELT). Insert only, never update/delete,
-- and the row carries no workspace id: see docs/contracts/risk-engine.md.
GRANT INSERT (kind, term, scope, origin) ON feeds.watch_terms TO rp_app;

-- Tables a later migration adds to `feeds` (e.g. 004 if it lands after this
-- file) get the same access, as long as the same role runs the migrations.
-- If 004 is applied first, the GRANT ... ON ALL TABLES above already covers it.
ALTER DEFAULT PRIVILEGES IN SCHEMA feeds GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO rp_feeds;
ALTER DEFAULT PRIVILEGES IN SCHEMA feeds GRANT USAGE, SELECT ON SEQUENCES TO rp_feeds;
ALTER DEFAULT PRIVILEGES IN SCHEMA feeds GRANT SELECT ON TABLES TO rp_app;
