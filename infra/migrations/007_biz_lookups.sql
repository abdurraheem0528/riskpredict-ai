-- 007: the only ways rp_app can see across workspaces — five narrow
-- SECURITY DEFINER lookups, needed before a workspace is known:
--
--   biz.auth_user_by_email(email)           sign-in, adding a member by email
--   biz.auth_session_by_refresh_hash(hash)  POST /v1/auth/refresh
--   biz.route_drive_channel(channel_id)     Drive push notification → workspace
--   biz.route_composio_account(account_id)  Composio trigger → workspace
--   biz.worker_workspace_ids()              the worker's per-workspace loop
--
-- They are owned by rp_definer, which has SELECT on four tables plus a
-- read-everything policy that applies to it alone. rp_definer has no login and
-- no members, so nobody can run arbitrary queries as it; it is reached only
-- through these functions. Each returns the few columns its caller needs.

GRANT USAGE ON SCHEMA biz TO rp_definer;
GRANT EXECUTE ON FUNCTION biz.current_workspace_id(), biz.current_user_id() TO rp_definer;
GRANT SELECT ON biz.users, biz.sessions, biz.connections, biz.workspaces TO rp_definer;

CREATE POLICY definer_read ON biz.users       FOR SELECT TO rp_definer USING (true);
CREATE POLICY definer_read ON biz.sessions    FOR SELECT TO rp_definer USING (true);
CREATE POLICY definer_read ON biz.connections FOR SELECT TO rp_definer USING (true);
CREATE POLICY definer_read ON biz.workspaces  FOR SELECT TO rp_definer USING (true);

CREATE FUNCTION biz.auth_user_by_email(p_email text)
    RETURNS TABLE (user_id uuid, password_hash text, disabled boolean,
                   locked_until timestamptz, failed_sign_in_count integer)
    LANGUAGE sql STABLE SECURITY DEFINER
    SET search_path = pg_catalog, pg_temp
    AS $$
        SELECT u.id, u.password_hash, u.disabled_at IS NOT NULL, u.locked_until, u.failed_sign_in_count
        FROM biz.users u
        WHERE lower(u.email) = lower(p_email)
    $$;

CREATE FUNCTION biz.auth_session_by_refresh_hash(p_hash bytea)
    RETURNS TABLE (session_id uuid, user_id uuid, family_id uuid, expires_at timestamptz,
                   rotated_at timestamptz, revoked_at timestamptz)
    LANGUAGE sql STABLE SECURITY DEFINER
    SET search_path = pg_catalog, pg_temp
    AS $$
        SELECT s.id, s.user_id, s.family_id, s.expires_at, s.rotated_at, s.revoked_at
        FROM biz.sessions s
        WHERE s.refresh_token_hash = p_hash
    $$;

-- The caller must still compare sha256(X-Goog-Channel-Token) with watch_token_hash.
CREATE FUNCTION biz.route_drive_channel(p_channel_id text)
    RETURNS TABLE (workspace_id uuid, connection_id uuid, watch_token_hash bytea, watch_resource_id text)
    LANGUAGE sql STABLE SECURITY DEFINER
    SET search_path = pg_catalog, pg_temp
    AS $$
        SELECT c.workspace_id, c.id, c.watch_token_hash, c.watch_resource_id
        FROM biz.connections c
        WHERE c.watch_channel_id = p_channel_id AND c.provider = 'google_sheets'
    $$;

CREATE FUNCTION biz.route_composio_account(p_connected_account_id text)
    RETURNS TABLE (workspace_id uuid, connection_id uuid, provider text)
    LANGUAGE sql STABLE SECURITY DEFINER
    SET search_path = pg_catalog, pg_temp
    AS $$
        SELECT c.workspace_id, c.id, c.provider
        FROM biz.connections c
        WHERE c.composio_connected_account_id = p_connected_account_id
    $$;

CREATE FUNCTION biz.worker_workspace_ids()
    RETURNS TABLE (workspace_id uuid)
    LANGUAGE sql STABLE SECURITY DEFINER
    SET search_path = pg_catalog, pg_temp
    AS $$ SELECT w.id FROM biz.workspaces w ORDER BY w.created_at, w.id $$;

-- Hand the functions to rp_definer. A non-superuser migration role (Neon's
-- neondb_owner) must be able to SET ROLE rp_definer to do so; as the role's
-- creator it holds ADMIN OPTION and can grant that to itself. Changing a
-- function's owner also needs CREATE on the schema for the new owner, which is
-- granted only for the duration of this transaction.
DO $$
BEGIN
    IF NOT (SELECT rolsuper FROM pg_roles WHERE rolname = current_user)
       AND NOT pg_has_role(current_user, 'rp_definer', 'SET') THEN
        EXECUTE format('GRANT rp_definer TO %I WITH INHERIT FALSE, SET TRUE', current_user);
    END IF;
END
$$;
GRANT CREATE ON SCHEMA biz TO rp_definer;
ALTER FUNCTION biz.auth_user_by_email(text) OWNER TO rp_definer;
ALTER FUNCTION biz.auth_session_by_refresh_hash(bytea) OWNER TO rp_definer;
ALTER FUNCTION biz.route_drive_channel(text) OWNER TO rp_definer;
ALTER FUNCTION biz.route_composio_account(text) OWNER TO rp_definer;
ALTER FUNCTION biz.worker_workspace_ids() OWNER TO rp_definer;
REVOKE CREATE ON SCHEMA biz FROM rp_definer;

REVOKE EXECUTE ON FUNCTION
    biz.auth_user_by_email(text), biz.auth_session_by_refresh_hash(bytea),
    biz.route_drive_channel(text), biz.route_composio_account(text), biz.worker_workspace_ids()
    FROM PUBLIC;
GRANT EXECUTE ON FUNCTION
    biz.auth_user_by_email(text), biz.auth_session_by_refresh_hash(bytea),
    biz.route_drive_channel(text), biz.route_composio_account(text), biz.worker_workspace_ids()
    TO rp_app;
