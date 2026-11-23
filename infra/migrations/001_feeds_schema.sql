-- 001: public feeds schema.
-- Holds only data published by public sources. Business data lives in the
-- separate `biz` schema (added in a later step, with row-level security).

CREATE SCHEMA IF NOT EXISTS feeds;

-- Discrete happenings: articles, advisories, hazards, sanctions actions...
CREATE TABLE feeds.events (
    source        text             NOT NULL,
    raw_id        text             NOT NULL,             -- id as given by the source
    type          text             NOT NULL,             -- e.g. hazard.earthquake, cyber.vulnerability
    title         text             NOT NULL DEFAULT '',
    url           text,
    published_at  timestamptz,
    countries     text[]           NOT NULL DEFAULT '{}', -- ISO 3166-1 alpha-3
    ports         text[]           NOT NULL DEFAULT '{}', -- UN/LOCODE
    commodities   text[]           NOT NULL DEFAULT '{}',
    currencies    text[]           NOT NULL DEFAULT '{}', -- ISO 4217
    software      text[]           NOT NULL DEFAULT '{}', -- "<vendor|ecosystem>:<product|package>"
    severity      real             CHECK (severity IS NULL OR severity BETWEEN 0 AND 1),
    lat           double precision CHECK (lat IS NULL OR lat BETWEEN -90 AND 90),
    lon           double precision CHECK (lon IS NULL OR lon BETWEEN -180 AND 180),
    attrs         jsonb            NOT NULL DEFAULT '{}', -- source-specific extras
    first_seen_at timestamptz      NOT NULL DEFAULT now(),
    updated_at    timestamptz      NOT NULL DEFAULT now(),
    PRIMARY KEY (source, raw_id)
);

CREATE INDEX events_published_idx ON feeds.events (published_at DESC);
CREATE INDEX events_type_idx      ON feeds.events (type, published_at DESC);
CREATE INDEX events_countries_idx ON feeds.events USING gin (countries);
CREATE INDEX events_ports_idx     ON feeds.events USING gin (ports);
CREATE INDEX events_software_idx  ON feeds.events USING gin (software);
CREATE INDEX events_currencies_idx ON feeds.events USING gin (currencies);
CREATE INDEX events_commodities_idx ON feeds.events USING gin (commodities);

-- Numeric time series: FX rates, port calls, EPSS scores, macro indicators...
CREATE TABLE feeds.series (
    series     text             NOT NULL,  -- measure, e.g. fx.rate
    key        text             NOT NULL,  -- subject, e.g. EUR/USD, ISO3, CVE id
    ts         timestamptz      NOT NULL,
    source     text             NOT NULL,
    value      double precision NOT NULL,
    unit       text,
    attrs      jsonb            NOT NULL DEFAULT '{}',
    updated_at timestamptz      NOT NULL DEFAULT now(),
    PRIMARY KEY (series, key, ts, source)
);

CREATE INDEX series_source_ts_idx ON feeds.series (source, ts DESC);

-- What to ask query-driven sources about (GDELT queries, OSV packages).
CREATE TABLE feeds.watch_terms (
    id         bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    kind       text        NOT NULL CHECK (kind IN ('gdelt_query', 'osv_package')),
    term       text        NOT NULL CHECK (length(term) BETWEEN 1 AND 500),
    scope      text        NOT NULL DEFAULT '', -- e.g. OSV ecosystem
    enabled    boolean     NOT NULL DEFAULT true,
    origin     text        NOT NULL DEFAULT 'default',
    created_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE (kind, scope, term)
);

-- One row per source: how its latest run went.
CREATE TABLE feeds.source_health (
    source               text        PRIMARY KEY,
    status               text        NOT NULL
        CHECK (status IN ('ok', 'empty', 'partial', 'throttled', 'error', 'skipped')),
    last_run_at          timestamptz NOT NULL,
    last_success_at      timestamptz,
    last_error           text,
    consecutive_failures integer     NOT NULL DEFAULT 0,
    records_last_run     integer     NOT NULL DEFAULT 0,
    records_changed      integer     NOT NULL DEFAULT 0,
    duration_ms          integer,
    notes                jsonb       NOT NULL DEFAULT '{}'
);
