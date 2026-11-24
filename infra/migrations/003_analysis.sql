-- 003: analysis results derived from the public feeds (step 2).
-- Nothing here overwrites a source field: tags and anomalies live in their own
-- tables and can be dropped and recomputed at any time.

-- Entities and an event type for events that carry text, one row per event per model.
CREATE TABLE feeds.event_tags (
    source          text        NOT NULL,
    raw_id          text        NOT NULL,
    model           text        NOT NULL,              -- e.g. fastino/gliner2-base-v1
    model_version   text        NOT NULL,              -- model revision + label-schema hash
    input_md5       text        NOT NULL,              -- md5 of the title that was tagged
    entities        jsonb       NOT NULL DEFAULT '{}', -- {"companies": [...], "places": [...], "commodities": [...]}
    event_type      text        NOT NULL CHECK (event_type IN (
                        'conflict', 'protest', 'sanctions', 'trade_policy', 'port_shipping_disruption',
                        'natural_hazard', 'cyber_incident', 'labour_action', 'financial_stress', 'other')),
    type_confidence real        CHECK (type_confidence IS NULL OR type_confidence BETWEEN 0 AND 1),
    type_source     text        NOT NULL DEFAULT 'model' CHECK (type_source IN ('model', 'source')),
    tagged_at       timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (source, raw_id, model),
    FOREIGN KEY (source, raw_id) REFERENCES feeds.events (source, raw_id) ON DELETE CASCADE
);

CREATE INDEX event_tags_type_idx ON feeds.event_tags (event_type, tagged_at DESC);
CREATE INDEX event_tags_entities_idx ON feeds.event_tags USING gin (entities jsonb_path_ops);

-- Points flagged as unusual against their own trailing history.
CREATE TABLE feeds.anomalies (
    series      text             NOT NULL,  -- feeds.series.series, or gdelt.articles for news volume
    key         text             NOT NULL,  -- feeds.series.key, or the GDELT watch term
    ts          timestamptz      NOT NULL,  -- the flagged observation (midnight UTC for daily data)
    method      text             NOT NULL CHECK (method IN ('robust_z', 'isolation_forest')),
    value       double precision NOT NULL,  -- observed value
    baseline    double precision NOT NULL,  -- what the trailing window expected, same unit as value
    score       double precision NOT NULL,  -- robust z, or isolation-forest anomaly score (0-1)
    attrs       jsonb            NOT NULL DEFAULT '{}', -- window, points, threshold, transform
    detected_at timestamptz      NOT NULL DEFAULT now(),
    PRIMARY KEY (series, key, ts, method)
);

CREATE INDEX anomalies_ts_idx ON feeds.anomalies (ts DESC);

-- Like feeds.source_health, for analysis jobs (subject '*') and for each series
-- checked for spikes (subject 'series|key'), including "insufficient_history".
CREATE TABLE feeds.analysis_status (
    analysis        text        NOT NULL,   -- 'tagging' | 'anomalies'
    subject         text        NOT NULL,   -- '*' for the job, else e.g. 'fx.rate|EUR/USD'
    status          text        NOT NULL
        CHECK (status IN ('ok', 'empty', 'partial', 'insufficient_history', 'skipped', 'error')),
    last_run_at     timestamptz NOT NULL,
    last_success_at timestamptz,
    last_error      text,
    history_points  integer,                -- points in the trailing window
    needed_points   integer,                -- minimum required to score
    notes           jsonb       NOT NULL DEFAULT '{}',
    PRIMARY KEY (analysis, subject)
);
