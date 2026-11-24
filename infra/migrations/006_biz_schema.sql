-- 006: the `biz` schema — each business's own data, isolated by row-level security.
--
-- Ships empty: no seed rows, no demo data. Every table that belongs to a
-- workspace has `workspace_id` and a policy keyed on
-- current_setting('app.workspace_id', true). The two identity tables (users,
-- sessions) belong to a person, not a workspace, and are keyed on
-- current_setting('app.user_id', true) instead. See docs/contracts/README.md
-- ("Database session context") for how the API and worker set both.
--
-- Rows from different workspaces can never reference each other: child tables
-- use composite foreign keys (workspace_id, parent_id).

CREATE SCHEMA biz;
REVOKE ALL ON SCHEMA biz FROM PUBLIC;
-- Functions are executable by PUBLIC by default, and a per-schema default
-- privilege cannot take that away, so every migration that adds a function to
-- biz ends with REVOKE EXECUTE ... FROM PUBLIC (a test checks this).

-- --- session context -----------------------------------------------------------
-- NULL when the setting is missing or empty, so policies then match nothing.
-- A value that is not a uuid raises an error rather than matching nothing.
CREATE FUNCTION biz.current_workspace_id() RETURNS uuid
    LANGUAGE sql STABLE PARALLEL SAFE
    AS $$ SELECT nullif(current_setting('app.workspace_id', true), '')::uuid $$;

CREATE FUNCTION biz.current_user_id() RETURNS uuid
    LANGUAGE sql STABLE PARALLEL SAFE
    AS $$ SELECT nullif(current_setting('app.user_id', true), '')::uuid $$;

-- true when every element of `arr` matches the regular expression `re`.
CREATE FUNCTION biz.all_match(arr text[], re text) RETURNS boolean
    LANGUAGE sql IMMUTABLE PARALLEL SAFE
    AS $$ SELECT coalesce(bool_and(x ~ re), true) FROM unnest(arr) AS x $$;

-- Append-only tables reject UPDATE even from their owner.
CREATE FUNCTION biz.reject_update() RETURNS trigger
    LANGUAGE plpgsql
    AS $$ BEGIN RAISE EXCEPTION '% is append-only', TG_TABLE_NAME USING ERRCODE = 'insufficient_privilege'; END $$;

-- --- tenancy and identity -------------------------------------------------------

CREATE TABLE biz.workspaces (
    id              uuid        PRIMARY KEY,          -- chosen by the API (gen_random_uuid() works too)
    name            text        NOT NULL CHECK (length(name) BETWEEN 1 AND 120),
    base_currency   text        CHECK (base_currency ~ '^[A-Z]{3}$'),  -- ISO 4217; NULL until the user sets it
    alert_min_score real        NOT NULL DEFAULT 60 CHECK (alert_min_score BETWEEN 0 AND 100),
    -- Opt-in: may the worker add watch terms derived from this workspace's entities
    -- (package names → OSV, critical supplier names → GDELT)? Those queries leave
    -- the platform, so the default is no.
    share_watch_terms boolean   NOT NULL DEFAULT false,
    created_at      timestamptz NOT NULL DEFAULT now(),
    updated_at      timestamptz NOT NULL DEFAULT now()
);

-- Sign-in is our own: email + argon2id hash. No third-party identity provider.
CREATE TABLE biz.users (
    id                   uuid        PRIMARY KEY,
    email                text        NOT NULL CHECK (length(email) BETWEEN 3 AND 254 AND email ~ '^[^@\s]+@[^@\s]+$'),
    password_hash        text        NOT NULL CHECK (password_hash LIKE '$argon2id$%'),  -- PHC string
    display_name         text        CHECK (length(display_name) <= 120),
    failed_sign_in_count integer     NOT NULL DEFAULT 0 CHECK (failed_sign_in_count >= 0),
    locked_until         timestamptz,
    last_sign_in_at      timestamptz,
    disabled_at          timestamptz,
    created_at           timestamptz NOT NULL DEFAULT now(),
    updated_at           timestamptz NOT NULL DEFAULT now()
);
CREATE UNIQUE INDEX users_email_key ON biz.users (lower(email));

CREATE TABLE biz.memberships (
    workspace_id uuid        NOT NULL REFERENCES biz.workspaces (id) ON DELETE CASCADE,
    user_id      uuid        NOT NULL REFERENCES biz.users (id) ON DELETE CASCADE,
    role         text        NOT NULL CHECK (role IN ('owner', 'admin', 'editor', 'viewer')),
    created_at   timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (workspace_id, user_id)
);
CREATE INDEX memberships_user_idx ON biz.memberships (user_id);

-- One row per refresh token. Rotation creates a new row in the same family and
-- sets rotated_at on the old one; presenting a rotated token revokes the family.
CREATE TABLE biz.sessions (
    id                 uuid        PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id            uuid        NOT NULL REFERENCES biz.users (id) ON DELETE CASCADE,
    family_id          uuid        NOT NULL,
    refresh_token_hash bytea       NOT NULL UNIQUE CHECK (length(refresh_token_hash) = 32),  -- sha256 of the token
    created_at         timestamptz NOT NULL DEFAULT now(),
    expires_at         timestamptz NOT NULL,
    last_used_at       timestamptz,
    rotated_at         timestamptz,
    revoked_at         timestamptz,
    revoked_reason     text        CHECK (revoked_reason IN ('sign_out', 'reuse_detected', 'password_changed', 'admin')),
    user_agent         text        CHECK (length(user_agent) <= 400),
    CHECK (expires_at > created_at)
);
CREATE INDEX sessions_user_idx ON biz.sessions (user_id);
CREATE INDEX sessions_family_idx ON biz.sessions (family_id);

-- --- connected tools ------------------------------------------------------------

-- provider 'google_sheets': we hold the Google refresh token, AES-256-GCM
--   encrypted by the app (never plaintext here); Drive `drive.file` scope.
-- provider 'composio:<app>' (e.g. composio:airtable): Composio holds the tokens;
--   we keep only its connected-account id.
CREATE TABLE biz.connections (
    id                       uuid        PRIMARY KEY DEFAULT gen_random_uuid(),
    workspace_id             uuid        NOT NULL REFERENCES biz.workspaces (id) ON DELETE CASCADE,
    provider                 text        NOT NULL CHECK (provider = 'google_sheets' OR provider ~ '^composio:[a-z0-9_]{1,60}$'),
    display_name             text        NOT NULL DEFAULT '' CHECK (length(display_name) <= 200),
    status                   text        NOT NULL DEFAULT 'pending'
        CHECK (status IN ('pending', 'active', 'needs_reauth', 'error', 'revoked')),
    status_reason            text        CHECK (length(status_reason) <= 500),   -- safe to show; never a token
    account_label            text        CHECK (length(account_label) <= 254),   -- e.g. the Google account email
    -- google_sheets only
    oauth_state_hash         bytea       CHECK (length(oauth_state_hash) = 32),  -- sha256 of the pending OAuth `state`
    refresh_token_ciphertext bytea,                                              -- 12-byte nonce || ciphertext || 16-byte tag
    refresh_token_key_id     text        CHECK (length(refresh_token_key_id) <= 40), -- which TOKEN_ENCRYPTION_KEYS entry
    scopes                   text[]      NOT NULL DEFAULT '{}',
    -- Drive changes.watch channel (google_sheets only)
    drive_page_token         text,                                               -- changes.list cursor
    watch_channel_id         text        UNIQUE,
    watch_resource_id        text,
    watch_token_hash         bytea       CHECK (length(watch_token_hash) = 32),  -- sha256 of the X-Goog-Channel-Token we set
    watch_expires_at         timestamptz,
    watch_renew_after        timestamptz,                                        -- worker renews once now() passes this
    watch_last_renewed_at    timestamptz,
    watch_failures           integer     NOT NULL DEFAULT 0 CHECK (watch_failures >= 0),
    -- composio:<app> only
    composio_connected_account_id text   UNIQUE,
    composio_auth_config_id  text,
    created_by               uuid        REFERENCES biz.users (id) ON DELETE SET NULL,
    created_at               timestamptz NOT NULL DEFAULT now(),
    updated_at               timestamptz NOT NULL DEFAULT now(),
    UNIQUE (workspace_id, id),
    CHECK (provider = 'google_sheets' OR (refresh_token_ciphertext IS NULL AND watch_channel_id IS NULL)),
    CHECK (provider <> 'google_sheets' OR composio_connected_account_id IS NULL),
    CHECK ((refresh_token_ciphertext IS NULL) = (refresh_token_key_id IS NULL)),
    CHECK (status <> 'active' OR provider <> 'google_sheets' OR refresh_token_ciphertext IS NOT NULL),
    CHECK (status <> 'active' OR provider = 'google_sheets' OR composio_connected_account_id IS NOT NULL),
    CHECK ((watch_channel_id IS NULL) = (watch_expires_at IS NULL))
);
CREATE INDEX connections_workspace_idx ON biz.connections (workspace_id);
CREATE INDEX connections_watch_renew_idx ON biz.connections (watch_renew_after) WHERE watch_channel_id IS NOT NULL;

-- A connected sheet tab or table.
CREATE TABLE biz.sources (
    id                  uuid        PRIMARY KEY DEFAULT gen_random_uuid(),
    workspace_id        uuid        NOT NULL,
    connection_id       uuid        NOT NULL,
    kind                text        NOT NULL CHECK (kind IN ('google_sheet', 'composio_table')),
    external_id         text        NOT NULL CHECK (length(external_id) BETWEEN 1 AND 200),  -- spreadsheet id / base id
    external_table_id   text        NOT NULL DEFAULT '' CHECK (length(external_table_id) <= 200), -- sheet gid / table id
    name                text        NOT NULL CHECK (length(name) BETWEEN 1 AND 300),
    url                 text,
    status              text        NOT NULL DEFAULT 'new'
        CHECK (status IN ('new', 'mapping_suggested', 'active', 'paused', 'error')),
    status_reason       text        CHECK (length(status_reason) <= 500),
    header_row          integer     NOT NULL DEFAULT 1 CHECK (header_row >= 1),
    columns             jsonb       NOT NULL DEFAULT '[]',  -- [{"index":0,"name":"Supplier","samples":["Acme",...]}], ≤5 samples
    row_count           integer     CHECK (row_count >= 0),
    content_hash        text,                               -- sha256 of the normalised rows last synced
    sync_cursor         jsonb       NOT NULL DEFAULT '{}',  -- provider-specific (e.g. Airtable offset)
    last_synced_at      timestamptz,
    last_change_at      timestamptz,                        -- last webhook/poll that said it changed
    created_by          uuid        REFERENCES biz.users (id) ON DELETE SET NULL,
    created_at          timestamptz NOT NULL DEFAULT now(),
    updated_at          timestamptz NOT NULL DEFAULT now(),
    UNIQUE (workspace_id, id),
    UNIQUE (workspace_id, connection_id, external_id, external_table_id),
    FOREIGN KEY (workspace_id, connection_id) REFERENCES biz.connections (workspace_id, id) ON DELETE CASCADE,
    CHECK (jsonb_typeof(columns) = 'array')
);

-- The AI's suggestion (Groq) and the user's confirmed mapping, versioned per source.
-- Both `suggestion` and `mapping` follow the MappingSpec schema in openapi.yaml.
CREATE TABLE biz.column_mappings (
    id                 uuid        PRIMARY KEY DEFAULT gen_random_uuid(),
    workspace_id       uuid        NOT NULL,
    source_id          uuid        NOT NULL,
    version            integer     NOT NULL CHECK (version >= 1),
    status             text        NOT NULL CHECK (status IN ('suggested', 'confirmed', 'superseded', 'rejected')),
    suggestion         jsonb,                    -- MappingSpec + per-column confidence/reason, as the model returned it (validated)
    suggestion_model   text,                     -- e.g. groq:llama-3.3-70b-versatile; NULL if the user mapped by hand
    suggested_at       timestamptz,
    mapping            jsonb,                    -- MappingSpec as confirmed by the user
    confirmed_by       uuid        REFERENCES biz.users (id) ON DELETE SET NULL,
    confirmed_at       timestamptz,
    created_at         timestamptz NOT NULL DEFAULT now(),
    UNIQUE (workspace_id, id),
    UNIQUE (source_id, version),
    FOREIGN KEY (workspace_id, source_id) REFERENCES biz.sources (workspace_id, id) ON DELETE CASCADE,
    CHECK (status <> 'confirmed' OR (mapping IS NOT NULL AND confirmed_at IS NOT NULL)),
    CHECK (suggestion IS NOT NULL OR mapping IS NOT NULL)
);
CREATE UNIQUE INDEX column_mappings_one_confirmed ON biz.column_mappings (source_id) WHERE status = 'confirmed';

-- --- the business's things ------------------------------------------------------

-- One row of a source = one entity. Match keys are normalised by the sync
-- (same rules as jobs/codes.py) so the risk engine compares codes, not text.
CREATE TABLE biz.entities (
    id                uuid        PRIMARY KEY DEFAULT gen_random_uuid(),
    workspace_id      uuid        NOT NULL,
    kind              text        NOT NULL CHECK (kind IN
        ('supplier', 'site', 'customer', 'route', 'port', 'currency', 'commodity', 'software')),
    name              text        NOT NULL CHECK (length(name) BETWEEN 1 AND 300),
    -- match keys
    countries         text[]      NOT NULL DEFAULT '{}' CHECK (biz.all_match(countries, '^[A-Z]{3}$')),       -- ISO 3166-1 alpha-3
    ports             text[]      NOT NULL DEFAULT '{}' CHECK (biz.all_match(ports, '^[A-Z]{2}[A-Z2-9]{3}$')), -- UN/LOCODE, no space
    currencies        text[]      NOT NULL DEFAULT '{}' CHECK (biz.all_match(currencies, '^[A-Z]{3}$')),      -- ISO 4217
    hs_codes          text[]      NOT NULL DEFAULT '{}' CHECK (biz.all_match(hs_codes, '^[0-9]{2}([0-9]{2}([0-9]{2})?)?$')),
    commodities       text[]      NOT NULL DEFAULT '{}',                                                       -- free text, lower case
    software          text[]      NOT NULL DEFAULT '{}' CHECK (biz.all_match(software, '^(pkg:|cpe:2\.3:)')),  -- purl or CPE 2.3
    lat               double precision CHECK (lat BETWEEN -90 AND 90),
    lon               double precision CHECK (lon BETWEEN -180 AND 180),
    city              text        CHECK (length(city) <= 200),
    address           text        CHECK (length(address) <= 500),
    -- exposure
    annual_spend      numeric(18, 2) CHECK (annual_spend >= 0),
    spend_currency    text        CHECK (spend_currency ~ '^[A-Z]{3}$'),
    revenue_share     numeric(5, 4) CHECK (revenue_share BETWEEN 0 AND 1),
    criticality       text        CHECK (criticality IN ('low', 'medium', 'high', 'critical')),
    attrs             jsonb       NOT NULL DEFAULT '{}',   -- mapped extras, e.g. {"notes": "..."}; never match keys
    -- lineage back to the source row
    source_id         uuid        NOT NULL,
    mapping_id        uuid        NOT NULL,
    source_row_key    text        NOT NULL CHECK (length(source_row_key) BETWEEN 1 AND 200), -- Airtable record id, or the mapped key column, or "row:<n>"
    source_row_number integer     CHECK (source_row_number >= 1),
    source_row_hash   text        NOT NULL,                -- sha256 of the raw row, to skip unchanged rows
    created_at        timestamptz NOT NULL DEFAULT now(),
    updated_at        timestamptz NOT NULL DEFAULT now(),
    UNIQUE (workspace_id, id),
    UNIQUE (workspace_id, source_id, source_row_key),
    FOREIGN KEY (workspace_id, source_id) REFERENCES biz.sources (workspace_id, id) ON DELETE CASCADE,
    FOREIGN KEY (workspace_id, mapping_id) REFERENCES biz.column_mappings (workspace_id, id),
    CHECK ((lat IS NULL) = (lon IS NULL)),
    CHECK (annual_spend IS NULL OR spend_currency IS NOT NULL)
);
CREATE INDEX entities_kind_idx ON biz.entities (workspace_id, kind);
CREATE INDEX entities_countries_idx ON biz.entities USING gin (countries);
CREATE INDEX entities_ports_idx ON biz.entities USING gin (ports);
CREATE INDEX entities_software_idx ON biz.entities USING gin (software);

-- --- risks ----------------------------------------------------------------------

-- One row per (entity, category, driver) — `fingerprint` is the engine's stable key.
CREATE TABLE biz.risks (
    id                  uuid        PRIMARY KEY DEFAULT gen_random_uuid(),
    workspace_id        uuid        NOT NULL,
    entity_id           uuid        NOT NULL,
    fingerprint         text        NOT NULL CHECK (length(fingerprint) BETWEEN 1 AND 200),
    category            text        NOT NULL CHECK (category IN (
        'natural_hazard', 'logistics', 'geopolitical', 'sanctions', 'trade_policy',
        'cyber', 'financial', 'labour', 'other')),
    title               text        NOT NULL CHECK (length(title) BETWEEN 1 AND 300),  -- engine template, not AI
    explanation         text        NOT NULL DEFAULT '',                               -- engine template, not AI
    likelihood          real        NOT NULL CHECK (likelihood BETWEEN 0 AND 1),
    impact              real        NOT NULL CHECK (impact BETWEEN 0 AND 1),
    score               real        GENERATED ALWAYS AS (round((100 * likelihood * impact)::numeric, 1)::real) STORED,
    factors             jsonb       NOT NULL DEFAULT '{}',  -- RiskFactors in risk-engine.md
    -- where to draw it
    countries           text[]      NOT NULL DEFAULT '{}' CHECK (biz.all_match(countries, '^[A-Z]{3}$')),
    ports               text[]      NOT NULL DEFAULT '{}' CHECK (biz.all_match(ports, '^[A-Z]{2}[A-Z2-9]{3}$')),
    lat                 double precision CHECK (lat BETWEEN -90 AND 90),
    lon                 double precision CHECK (lon BETWEEN -180 AND 180),
    -- advice: Groq words it, it never changes a number
    advice              text,
    advice_source       text        CHECK (advice_source IN ('groq', 'template')),
    advice_model        text,
    advice_generated_at timestamptz,
    -- lifecycle
    status              text        NOT NULL DEFAULT 'open'
        CHECK (status IN ('open', 'acknowledged', 'mitigating', 'resolved', 'dismissed')),
    status_changed_at   timestamptz NOT NULL DEFAULT now(),
    status_changed_by   uuid        REFERENCES biz.users (id) ON DELETE SET NULL,  -- NULL = the engine
    status_note         text        CHECK (length(status_note) <= 2000),
    first_seen_at       timestamptz NOT NULL DEFAULT now(),
    last_seen_at        timestamptz NOT NULL DEFAULT now(),   -- last engine run that still found evidence
    last_scored_at      timestamptz NOT NULL DEFAULT now(),
    engine_version      text        NOT NULL,
    UNIQUE (workspace_id, id),
    UNIQUE (workspace_id, fingerprint),
    FOREIGN KEY (workspace_id, entity_id) REFERENCES biz.entities (workspace_id, id) ON DELETE CASCADE,
    CHECK ((lat IS NULL) = (lon IS NULL)),
    CHECK ((advice IS NULL) = (advice_source IS NULL))
);
CREATE INDEX risks_score_idx ON biz.risks (workspace_id, status, score DESC);
CREATE INDEX risks_entity_idx ON biz.risks (workspace_id, entity_id);
CREATE INDEX risks_countries_idx ON biz.risks USING gin (countries);

-- The feeds rows behind a risk. Keys point at feeds.events / feeds.anomalies,
-- deliberately without a foreign key: feed retention may delete the row, so a
-- snapshot of what was shown is kept here.
CREATE TABLE biz.risk_evidence (
    id               bigint      GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    workspace_id     uuid        NOT NULL,
    risk_id          uuid        NOT NULL,
    kind             text        NOT NULL CHECK (kind IN ('event', 'anomaly')),
    -- kind = 'event': feeds.events (source, raw_id)
    event_source     text,
    event_raw_id     text,
    -- kind = 'anomaly': feeds.anomalies (series, key, ts, method)
    anomaly_series   text,
    anomaly_key      text,
    anomaly_ts       timestamptz,
    anomaly_method   text        CHECK (anomaly_method IN ('robust_z', 'isolation_forest')),
    match_type       text        NOT NULL CHECK (match_type IN (
        'country', 'geo_radius', 'port', 'chokepoint', 'currency', 'commodity',
        'software', 'sanctions_name', 'company_name')),
    match_value      text        NOT NULL,      -- the key that matched, e.g. 'JPN', 'NLRTM', 'PyPI:requests'
    match_confidence real        NOT NULL CHECK (match_confidence BETWEEN 0 AND 1),
    signal           real        NOT NULL CHECK (signal BETWEEN 0 AND 1),
    recency          real        NOT NULL CHECK (recency BETWEEN 0 AND 1),
    contribution     real        NOT NULL CHECK (contribution BETWEEN 0 AND 1),  -- signal × recency × match_confidence
    snapshot         jsonb       NOT NULL DEFAULT '{}',  -- EvidenceSnapshot in risk-engine.md
    first_linked_at  timestamptz NOT NULL DEFAULT now(),
    last_linked_at   timestamptz NOT NULL DEFAULT now(),
    UNIQUE NULLS NOT DISTINCT (risk_id, kind, event_source, event_raw_id,
                               anomaly_series, anomaly_key, anomaly_ts, anomaly_method),
    FOREIGN KEY (workspace_id, risk_id) REFERENCES biz.risks (workspace_id, id) ON DELETE CASCADE,
    CHECK (kind <> 'event' OR (event_source IS NOT NULL AND event_raw_id IS NOT NULL)),
    CHECK (kind <> 'anomaly' OR (anomaly_series IS NOT NULL AND anomaly_key IS NOT NULL
                                 AND anomaly_ts IS NOT NULL AND anomaly_method IS NOT NULL))
);
CREATE INDEX risk_evidence_risk_idx ON biz.risk_evidence (workspace_id, risk_id);
CREATE INDEX risk_evidence_event_idx ON biz.risk_evidence (event_source, event_raw_id) WHERE kind = 'event';

-- Append-only: one row whenever a risk's score moves by ≥ 1 point or its status changes.
CREATE TABLE biz.risk_history (
    id             bigint      GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    workspace_id   uuid        NOT NULL,
    risk_id        uuid        NOT NULL,
    recorded_at    timestamptz NOT NULL DEFAULT now(),
    likelihood     real        NOT NULL CHECK (likelihood BETWEEN 0 AND 1),
    impact         real        NOT NULL CHECK (impact BETWEEN 0 AND 1),
    score          real        GENERATED ALWAYS AS (round((100 * likelihood * impact)::numeric, 1)::real) STORED,
    status         text        NOT NULL CHECK (status IN ('open', 'acknowledged', 'mitigating', 'resolved', 'dismissed')),
    reason         text        NOT NULL CHECK (reason IN ('created', 'rescored', 'status_changed', 'resolved_by_engine', 'reopened')),
    changed_by     uuid        REFERENCES biz.users (id) ON DELETE SET NULL,
    engine_version text,
    FOREIGN KEY (workspace_id, risk_id) REFERENCES biz.risks (workspace_id, id) ON DELETE CASCADE
);
CREATE INDEX risk_history_risk_idx ON biz.risk_history (workspace_id, risk_id, recorded_at DESC);
CREATE TRIGGER risk_history_append_only BEFORE UPDATE ON biz.risk_history
    FOR EACH ROW EXECUTE FUNCTION biz.reject_update();

-- --- what-if, alerts, audit, webhooks -------------------------------------------

CREATE TABLE biz.scenarios (
    id             uuid        PRIMARY KEY DEFAULT gen_random_uuid(),
    workspace_id   uuid        NOT NULL REFERENCES biz.workspaces (id) ON DELETE CASCADE,
    name           text        NOT NULL CHECK (length(name) BETWEEN 1 AND 200),
    description    text        CHECK (length(description) <= 2000),
    inputs         jsonb       NOT NULL,              -- ScenarioInputs in openapi.yaml
    status         text        NOT NULL DEFAULT 'running' CHECK (status IN ('running', 'completed', 'failed')),
    results        jsonb,                             -- ScenarioResults in openapi.yaml
    error          text        CHECK (length(error) <= 500),
    engine_version text,
    created_by     uuid        REFERENCES biz.users (id) ON DELETE SET NULL,
    created_at     timestamptz NOT NULL DEFAULT now(),
    completed_at   timestamptz,
    UNIQUE (workspace_id, id),
    CHECK (status <> 'completed' OR results IS NOT NULL)
);

CREATE TABLE biz.alerts (
    id           uuid        PRIMARY KEY DEFAULT gen_random_uuid(),
    workspace_id uuid        NOT NULL REFERENCES biz.workspaces (id) ON DELETE CASCADE,
    risk_id      uuid,
    connection_id uuid,
    kind         text        NOT NULL CHECK (kind IN (
        'risk_new', 'risk_escalated', 'sanctions_match', 'connection_error', 'source_sync_failed')),
    severity     text        NOT NULL CHECK (severity IN ('info', 'warning', 'critical')),
    title        text        NOT NULL CHECK (length(title) BETWEEN 1 AND 300),
    body         text        NOT NULL DEFAULT '',
    dedupe_key   text        NOT NULL,                 -- e.g. 'risk_new:<risk id>'
    created_at   timestamptz NOT NULL DEFAULT now(),
    read_at      timestamptz,
    read_by      uuid        REFERENCES biz.users (id) ON DELETE SET NULL,
    UNIQUE (workspace_id, dedupe_key),
    FOREIGN KEY (workspace_id, risk_id) REFERENCES biz.risks (workspace_id, id) ON DELETE SET NULL (risk_id),
    FOREIGN KEY (workspace_id, connection_id) REFERENCES biz.connections (workspace_id, id) ON DELETE SET NULL (connection_id)
);
CREATE INDEX alerts_unread_idx ON biz.alerts (workspace_id, created_at DESC) WHERE read_at IS NULL;

-- Append-only. `details` never holds a token, password or row contents.
CREATE TABLE biz.audit_log (
    id           bigint      GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    workspace_id uuid        NOT NULL REFERENCES biz.workspaces (id) ON DELETE CASCADE,
    actor_type   text        NOT NULL CHECK (actor_type IN ('user', 'worker', 'webhook')),
    actor_user_id uuid       REFERENCES biz.users (id) ON DELETE SET NULL,
    action       text        NOT NULL CHECK (action ~ '^[a-z_]+\.[a-z_]+$'),  -- e.g. connection.created
    target_type  text,
    target_id    text,
    details      jsonb       NOT NULL DEFAULT '{}',
    created_at   timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX audit_log_ws_idx ON biz.audit_log (workspace_id, created_at DESC);
CREATE TRIGGER audit_log_append_only BEFORE UPDATE ON biz.audit_log
    FOR EACH ROW EXECUTE FUNCTION biz.reject_update();

-- Idempotency for inbound webhooks: insert with ON CONFLICT DO NOTHING first;
-- if nothing was inserted the delivery was already seen.
CREATE TABLE biz.webhook_events (
    id            bigint      GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    workspace_id  uuid        NOT NULL REFERENCES biz.workspaces (id) ON DELETE CASCADE,
    connection_id uuid,
    provider      text        NOT NULL CHECK (provider IN ('google_drive', 'composio')),
    delivery_id   text        NOT NULL CHECK (length(delivery_id) BETWEEN 1 AND 300),
    -- google_drive: '<X-Goog-Channel-ID>:<X-Goog-Message-Number>'; composio: the webhook-id header
    event_type    text        NOT NULL,             -- e.g. 'change' / 'sync' / a Composio trigger slug
    received_at   timestamptz NOT NULL DEFAULT now(),
    processed_at  timestamptz,
    status        text        NOT NULL DEFAULT 'received' CHECK (status IN ('received', 'processed', 'ignored', 'failed')),
    error         text        CHECK (length(error) <= 500),
    payload       jsonb       NOT NULL DEFAULT '{}',  -- minimal: ids and states, never tokens or row contents
    UNIQUE (workspace_id, provider, delivery_id),
    FOREIGN KEY (workspace_id, connection_id) REFERENCES biz.connections (workspace_id, id) ON DELETE SET NULL (connection_id)
);
CREATE INDEX webhook_events_received_idx ON biz.webhook_events (received_at);

-- --- row-level security ---------------------------------------------------------

-- Every table with a workspace_id: one policy for all commands.
DO $$
DECLARE
    t text;
BEGIN
    FOR t IN
        SELECT c.relname FROM pg_class c
        JOIN pg_namespace n ON n.oid = c.relnamespace
        JOIN pg_attribute a ON a.attrelid = c.oid AND a.attname = 'workspace_id' AND NOT a.attisdropped
        WHERE n.nspname = 'biz' AND c.relkind = 'r' AND c.relname <> 'memberships'
    LOOP
        EXECUTE format('ALTER TABLE biz.%I ENABLE ROW LEVEL SECURITY', t);
        EXECUTE format('ALTER TABLE biz.%I FORCE ROW LEVEL SECURITY', t);
        EXECUTE format(
            'CREATE POLICY tenant ON biz.%I USING (workspace_id = biz.current_workspace_id()) '
            'WITH CHECK (workspace_id = biz.current_workspace_id())', t);
    END LOOP;
END
$$;

ALTER TABLE biz.workspaces  ENABLE ROW LEVEL SECURITY;
ALTER TABLE biz.workspaces  FORCE ROW LEVEL SECURITY;
ALTER TABLE biz.memberships ENABLE ROW LEVEL SECURITY;
ALTER TABLE biz.memberships FORCE ROW LEVEL SECURITY;
ALTER TABLE biz.users       ENABLE ROW LEVEL SECURITY;
ALTER TABLE biz.users       FORCE ROW LEVEL SECURITY;
ALTER TABLE biz.sessions    ENABLE ROW LEVEL SECURITY;
ALTER TABLE biz.sessions    FORCE ROW LEVEL SECURITY;

-- Memberships: the current workspace's members, plus the current user's own
-- memberships (so GET /v1/workspaces can list every workspace they belong to).
-- Writes only within the current workspace.
CREATE POLICY read ON biz.memberships FOR SELECT
    USING (workspace_id = biz.current_workspace_id() OR user_id = biz.current_user_id());
CREATE POLICY write ON biz.memberships FOR INSERT
    WITH CHECK (workspace_id = biz.current_workspace_id());
CREATE POLICY change ON biz.memberships FOR UPDATE
    USING (workspace_id = biz.current_workspace_id()) WITH CHECK (workspace_id = biz.current_workspace_id());
CREATE POLICY remove ON biz.memberships FOR DELETE
    USING (workspace_id = biz.current_workspace_id() OR user_id = biz.current_user_id());

-- Workspaces: the current one, plus (read-only) those the current user is a member of.
CREATE POLICY tenant ON biz.workspaces
    USING (id = biz.current_workspace_id()) WITH CHECK (id = biz.current_workspace_id());
CREATE POLICY member_read ON biz.workspaces FOR SELECT
    USING (EXISTS (SELECT 1 FROM biz.memberships m
                   WHERE m.workspace_id = workspaces.id AND m.user_id = biz.current_user_id()));

-- Users: yourself, plus (read-only) the members of the current workspace.
CREATE POLICY self ON biz.users
    USING (id = biz.current_user_id()) WITH CHECK (id = biz.current_user_id());
CREATE POLICY coworker_read ON biz.users FOR SELECT
    USING (EXISTS (SELECT 1 FROM biz.memberships m
                   WHERE m.user_id = users.id AND m.workspace_id = biz.current_workspace_id()));

-- Sessions: only your own.
CREATE POLICY self ON biz.sessions
    USING (user_id = biz.current_user_id()) WITH CHECK (user_id = biz.current_user_id());

-- --- privileges -----------------------------------------------------------------
-- rp_app gets exactly what it needs per table; it owns nothing. There are
-- deliberately no default privileges in biz: a new table is invisible to
-- rp_app until its migration enables RLS and grants access (a test checks
-- that every table rp_app can reach has RLS enabled and forced).

GRANT USAGE ON SCHEMA biz TO rp_app;
REVOKE EXECUTE ON ALL FUNCTIONS IN SCHEMA biz FROM PUBLIC;
GRANT EXECUTE ON FUNCTION biz.current_workspace_id(), biz.current_user_id(), biz.all_match(text[], text) TO rp_app;

GRANT SELECT, INSERT, UPDATE, DELETE ON
    biz.workspaces, biz.users, biz.memberships, biz.sessions,
    biz.connections, biz.sources, biz.column_mappings, biz.entities,
    biz.risks, biz.risk_evidence, biz.scenarios, biz.alerts
    TO rp_app;
GRANT SELECT, INSERT ON biz.risk_history, biz.audit_log TO rp_app;   -- append-only
GRANT SELECT, INSERT, UPDATE ON biz.webhook_events TO rp_app;
