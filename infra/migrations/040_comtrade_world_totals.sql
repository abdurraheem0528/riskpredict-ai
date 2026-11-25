-- 040: one-off cleanup of Comtrade rows stored before the query pinned every breakdown.
--
-- Until now the daily Comtrade call left breakdownMode, partner2Code, customsCode and
-- motCode unset, so the API answered in "plus" mode: one row per second partner,
-- customs procedure and transport mode, ~100,000 rows that hit the free tier's cap.
-- Those rows were stored under the same `<ISO3>:<HS>` key, so a stored value can be
-- one breakdown (e.g. sea freight only) rather than the World total, and the
-- `<ISO3>:<HS>:<partner>` form the parser also accepted would never age out: retention
-- (jobs/prune.py) always keeps the newest 60 points of every key.
--
-- The connector now asks for World totals only and drops anything else, so this
-- deletes every stored comtrade row (nothing reads them yet: the risk engine does not
-- use comtrade.*, and they are never scored for anomalies) and clears the source's
-- last success, so the fetch step that runs right after this migration refills them
-- with one call (~9,600 rows) instead of waiting up to 24 hours. Runs once.

DELETE FROM feeds.series WHERE source = 'comtrade';

UPDATE feeds.source_health SET last_success_at = NULL WHERE source = 'comtrade';
