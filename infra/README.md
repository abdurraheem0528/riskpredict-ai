# infra

`migrations/` holds plain SQL migrations, applied in filename order by
`python -m jobs.migrate` (each file once, recorded in `feeds.schema_migrations`).
Deploy/Destroy workflows arrive in a later step.
