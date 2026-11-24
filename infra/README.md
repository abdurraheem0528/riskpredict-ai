# infra

* `migrations/` holds plain SQL migrations, applied in filename order by
  `python -m jobs.migrate` (each file once, recorded in `feeds.schema_migrations`;
  gaps in the numbering are fine). `001`–`003` are the public `feeds` schema;
  `005`–`007` add the rp_* roles and the `biz` schema with row-level security.
* `scripts/create_login_roles.py` creates the login roles for `rp_feeds` / `rp_app`
  with passwords from env or stdin. See [`docs/database-roles.md`](../docs/database-roles.md).

Deploy/Destroy workflows arrive in a later step.
