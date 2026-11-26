-- 050: state that used to live in the API's memory, for a serverless API (Vercel)
-- where each request may land on a different short-lived instance.
--
--   biz.events        live updates for the dashboard's polling (GET /v1/events);
--                     replaces the in-memory SSE buffer, same event names and payloads
--   biz.worker_state  per-workspace bookkeeping of the run-once worker and the
--                     inline passes (feeds seen, scoring owed, events pruned, a lease)
--   biz.rate_limits   fixed-window counters shared by every API instance, reachable
--                     only through biz.rate_hit() (a sixth SECURITY DEFINER function)
--
-- Nothing is seeded. See docs/database-roles.md for the rules followed here.

-- --- events ----------------------------------------------------------------------
-- `tx` is the writing transaction's id. GET /v1/events returns only rows whose
-- transaction is older than every transaction still running, so an event that
-- commits late with a lower id is never skipped by a client that already moved on.
CREATE TABLE biz.events (
    id           bigint      GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    workspace_id uuid        NOT NULL REFERENCES biz.workspaces (id) ON DELETE CASCADE,
    event        text        NOT NULL CHECK (event IN ('risk.created', 'risk.updated', 'risk.resolved', 'alert.created',
                                                       'source.sync_started', 'source.synced', 'source.sync_failed',
                                                       'mapping.suggested', 'connection.updated', 'feeds.updated')),
    data         jsonb       NOT NULL DEFAULT '{}',
    tx           xid8        NOT NULL DEFAULT pg_current_xact_id(),
    created_at   timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX events_workspace_id_idx ON biz.events (workspace_id, id);

-- --- worker bookkeeping ----------------------------------------------------------
CREATE TABLE biz.worker_state (
    workspace_id          uuid        PRIMARY KEY REFERENCES biz.workspaces (id) ON DELETE CASCADE,
    -- max(feeds.source_health.last_run_at) when this workspace was last scored for the feeds
    feeds_seen_at         timestamptz,
    -- entities changed but the engine did not run (time budget, error): the next worker run scores
    score_pending         boolean     NOT NULL DEFAULT false,
    -- highest biz.events id the worker deleted (24 h retention); a poll from below it resyncs
    events_pruned_through bigint      NOT NULL DEFAULT 0,
    -- a webhook pass holds this for its time budget, so a burst of notifications runs once
    lease_until           timestamptz,
    updated_at            timestamptz NOT NULL DEFAULT now()
);

DO $$
DECLARE
    t text;
BEGIN
    FOREACH t IN ARRAY ARRAY['events', 'worker_state'] LOOP
        EXECUTE format('ALTER TABLE biz.%I ENABLE ROW LEVEL SECURITY', t);
        EXECUTE format('ALTER TABLE biz.%I FORCE ROW LEVEL SECURITY', t);
        EXECUTE format(
            'CREATE POLICY tenant ON biz.%I USING (workspace_id = biz.current_workspace_id()) '
            'WITH CHECK (workspace_id = biz.current_workspace_id())', t);
    END LOOP;
END
$$;

GRANT SELECT, INSERT, DELETE ON biz.events TO rp_app;
GRANT SELECT ON SEQUENCE biz.events_id_seq TO rp_app;  -- last_value: a cursor from a reset database resyncs
GRANT SELECT, INSERT, UPDATE ON biz.worker_state TO rp_app;

-- --- shared rate limits ------------------------------------------------------------
-- key_hash is HMAC-SHA256 of e.g. "signin-acct:<email>" with a key derived from
-- JWT_SIGNING_KEY: no email or IP address is stored in clear. rp_app has no
-- privilege on the table at all; only rp_definer (through biz.rate_hit) does.
CREATE TABLE biz.rate_limits (
    key_hash       bytea       PRIMARY KEY CHECK (octet_length(key_hash) = 32),
    window_start   timestamptz NOT NULL,
    window_seconds integer     NOT NULL CHECK (window_seconds BETWEEN 1 AND 86400),
    hits           integer     NOT NULL CHECK (hits >= 0)
);
ALTER TABLE biz.rate_limits ENABLE ROW LEVEL SECURITY;
ALTER TABLE biz.rate_limits FORCE ROW LEVEL SECURITY;
CREATE POLICY definer_all ON biz.rate_limits TO rp_definer USING (true) WITH CHECK (true);
GRANT SELECT, INSERT, UPDATE, DELETE ON biz.rate_limits TO rp_definer;

-- Count one hit for p_key in a fixed window of p_window_seconds. Returns 0 when the
-- hit is within p_limit, else the seconds until the window ends (for Retry-After).
-- About one call in a hundred also deletes counters whose window ended a day ago.
CREATE FUNCTION biz.rate_hit(p_key bytea, p_limit integer, p_window_seconds integer)
    RETURNS integer
    LANGUAGE plpgsql VOLATILE SECURITY DEFINER
    SET search_path = pg_catalog, pg_temp
    AS $$
DECLARE
    v_hits  integer;
    v_start timestamptz;
BEGIN
    IF p_key IS NULL OR octet_length(p_key) <> 32 OR p_limit IS NULL OR p_limit < 1
       OR p_window_seconds IS NULL OR p_window_seconds NOT BETWEEN 1 AND 86400 THEN
        RAISE EXCEPTION 'invalid rate limit arguments' USING ERRCODE = 'invalid_parameter_value';
    END IF;
    INSERT INTO biz.rate_limits AS r (key_hash, window_start, window_seconds, hits)
    VALUES (p_key, now(), p_window_seconds, 1)
    ON CONFLICT (key_hash) DO UPDATE SET
        hits           = CASE WHEN r.window_start + make_interval(secs => r.window_seconds) <= now()
                              THEN 1 ELSE r.hits + 1 END,
        window_start   = CASE WHEN r.window_start + make_interval(secs => r.window_seconds) <= now()
                              THEN now() ELSE r.window_start END,
        window_seconds = CASE WHEN r.window_start + make_interval(secs => r.window_seconds) <= now()
                              THEN EXCLUDED.window_seconds ELSE r.window_seconds END
    RETURNING r.hits, r.window_start INTO v_hits, v_start;
    IF random() < 0.01 THEN
        DELETE FROM biz.rate_limits WHERE window_start + make_interval(secs => window_seconds) < now() - interval '1 day';
    END IF;
    IF v_hits > p_limit THEN
        RETURN greatest(1, ceil(extract(epoch FROM v_start + make_interval(secs => p_window_seconds) - now()))::integer);
    END IF;
    RETURN 0;
END
$$;

-- Hand it to rp_definer, exactly as 007 does for the other five lookups.
DO $$
BEGIN
    IF NOT (SELECT rolsuper FROM pg_roles WHERE rolname = current_user)
       AND NOT pg_has_role(current_user, 'rp_definer', 'SET') THEN
        EXECUTE format('GRANT rp_definer TO %I WITH INHERIT FALSE, SET TRUE', current_user);
    END IF;
END
$$;
GRANT CREATE ON SCHEMA biz TO rp_definer;
ALTER FUNCTION biz.rate_hit(bytea, integer, integer) OWNER TO rp_definer;
REVOKE CREATE ON SCHEMA biz FROM rp_definer;

REVOKE EXECUTE ON FUNCTION biz.rate_hit(bytea, integer, integer) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION biz.rate_hit(bytea, integer, integer) TO rp_app;
