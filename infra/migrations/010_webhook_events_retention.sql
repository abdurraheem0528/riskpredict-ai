-- 010: let the worker prune handled webhook deliveries.
--
-- biz.webhook_events keeps one row per Drive / Composio delivery for idempotency.
-- A busy sheet can send many notifications a day, so the worker deletes rows that
-- were handled (status processed / ignored / failed) more than 30 days ago, within
-- the current workspace only: the table's `tenant` policy (006) applies to DELETE
-- as to every other command. Nothing else changes; no rows are added.

GRANT DELETE ON biz.webhook_events TO rp_app;
