# infra

* `migrations/` holds plain SQL migrations, applied in filename order by
  `python -m jobs.migrate` (each file once, recorded in `feeds.schema_migrations`;
  gaps in the numbering are fine). `001`–`003` are the public `feeds` schema;
  `005`–`007` add the rp_* roles and the `biz` schema with row-level security.
  `010` lets `rp_app` delete handled `biz.webhook_events` rows (the worker prunes them
  after 30 days, per workspace under RLS).
* `scripts/create_login_roles.py` creates the login roles for `rp_feeds` / `rp_app`
  with passwords from env or stdin. See [`docs/database-roles.md`](../docs/database-roles.md).

Deploy/Destroy workflows arrive in a later step.
