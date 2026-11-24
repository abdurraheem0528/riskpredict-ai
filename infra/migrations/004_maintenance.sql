-- 004: bookkeeping for maintenance tasks, e.g. the daily retention prune
-- (python -m jobs.prune), so it runs at most once a day.

CREATE TABLE feeds.maintenance (
    task        text        PRIMARY KEY,           -- e.g. 'prune'
    last_run_at timestamptz NOT NULL,
    notes       jsonb       NOT NULL DEFAULT '{}'  -- rows deleted per table, settings used
);
