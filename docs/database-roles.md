# Database roles

Everything lives in one Neon Postgres database with two schemas:

| Schema | Holds | Written by |
|---|---|---|
| `feeds` | public feed data only (events, series, tags, anomalies, health) | GitHub Actions jobs |
| `biz` | each business's own data, isolated by row-level security | API and worker |

## Group roles (created by migration `005_roles.sql`)

All three are `NOLOGIN`, `NOSUPERUSER`, `NOBYPASSRLS`, and own no tables.

| Role | Used by | `feeds` | `biz` |
|---|---|---|---|
| `rp_feeds` | GitHub Actions jobs (`jobs/`) | SELECT, INSERT, UPDATE, DELETE | no access (not even `USAGE` on the schema) |
| `rp_app` | `api` and `worker` | SELECT; INSERT on `watch_terms (kind, term, scope, origin)` only | SELECT/INSERT/UPDATE/DELETE per table, always under RLS; `risk_history` and `audit_log` are SELECT + INSERT only |
| `rp_definer` | nobody logs in as it | – | owns the five `SECURITY DEFINER` lookups in `007_biz_lookups.sql` |

The tables are owned by the role that runs the migrations (on Neon: `neondb_owner`).
`FORCE ROW LEVEL SECURITY` is set on every `biz` table, so even the owner is filtered
unless it has `BYPASSRLS`. Note that `neondb_owner` *does* have `BYPASSRLS`, which is
one more reason the API must never use it.

## Login roles (created outside the repository)

Passwords are never stored in the repository. Create the login roles **with SQL**,
not in the Neon Console, CLI or API: roles made there join `neon_superuser`, which
has `BYPASSRLS`, `CREATEROLE` and read/write on every table, so RLS would not apply.

`infra/scripts/create_login_roles.py` does this safely. It connects with the owner
connection string, reads each password from an environment variable or stdin,
creates the role as a plain `LOGIN` member of one group, and then checks it:

```bash
export DATABASE_URL_MIGRATE='<the neondb_owner connection string>'
```

```bash
read -rs RP_PASSWORD_RISKPREDICT_JOBS && export RP_PASSWORD_RISKPREDICT_JOBS
```

```bash
read -rs RP_PASSWORD_RISKPREDICT_APP && export RP_PASSWORD_RISKPREDICT_APP
```

```bash
python infra/scripts/create_login_roles.py --role riskpredict_jobs:rp_feeds --role riskpredict_app:rp_app
```

* **Passwords:** Neon requires at least 60 bits of entropy and only accepts them in
  plain text inside `CREATE ROLE ... PASSWORD`, so the script sends them that way over
  TLS. It stores nothing and prints only the role name and what it did. Generate one
  with `python -c "import secrets; print(secrets.token_urlsafe(24))"` into your
  password manager; the script asks for at least 16 characters.
* **Existing roles** keep their password unless you pass `--rotate`.
* **Checks** (also on their own with `--check`): the role can log in, has no
  `SUPERUSER`/`BYPASSRLS`/`CREATEROLE`/`CREATEDB`/`REPLICATION`, is a member of its
  group and of nothing else (so not `neon_superuser`, `pg_read_all_data`,
  `pg_write_all_data`), and owns no tables. Any problem exits with status 1.
* **Other ways in:** `--password-stdin` reads one password per line in `--role` order;
  without either, it prompts when run in a terminal.

Then build the connection strings from the Neon host and database name:

| Secret / env var | Role | Used by |
|---|---|---|
| `DATABASE_URL_OWNER` (GitHub secret) | `neondb_owner` | the "Apply migrations" steps of `feeds.yml` only |
| `DATABASE_URL_MIGRATE` (local env var) | `neondb_owner` | this script, run by hand (same string as `DATABASE_URL_OWNER`; `--dsn-env` picks another variable). To migrate by hand, run `python -m jobs.migrate` with `DATABASE_URL_FEEDS` set to the owner string |
| `DATABASE_URL_FEEDS` | `riskpredict_jobs` (member of `rp_feeds`) | fetch, prune and analyse jobs |
| `DATABASE_URL_APP` | `riskpredict_app` (member of `rp_app`) | `api` and `worker` |

Use the **pooled** Neon host (`-pooler`) for `DATABASE_URL_APP`; the API sets its
session context per transaction, which works through PgBouncer's transaction mode.

### Switching the jobs to the `rp_feeds` login (order matters)

`python -m jobs.migrate` needs owner rights (it creates schemas, roles and
functions), and a member of `rp_feeds` cannot run it: Postgres checks `CREATE` on
the schema even for `CREATE TABLE IF NOT EXISTS`. So `feeds.yml` gives the two
"Apply migrations" steps their own secret, **`DATABASE_URL_OWNER`**, and every other
step (fetch, prune, analyse) keeps **`DATABASE_URL_FEEDS`**:

```yaml
DATABASE_URL_FEEDS: ${{ secrets.DATABASE_URL_OWNER || secrets.DATABASE_URL_FEEDS }}  # migrate steps only
```

While `DATABASE_URL_OWNER` is unset the migrate steps fall back to
`DATABASE_URL_FEEDS`, so today's setup (the owner string in `DATABASE_URL_FEEDS`)
keeps working unchanged. To switch over:

1. **Merge, and let one `feeds.yml` run finish.** It still uses the owner via
   `DATABASE_URL_FEEDS`, so it applies any pending migrations (005 and later, e.g.
   `030_sanctions_entries`). Check the "Apply migrations" step log ends in
   `applied: ...` or `applied: none`.
2. **Create the login role** from your own machine, with the owner connection string
   in an environment variable (it is never typed on the command line). The script's
   default variable is `DATABASE_URL_MIGRATE`; `--dsn-env` points it at another one:

   ```bash
   read -rs DATABASE_URL_OWNER && export DATABASE_URL_OWNER
   ```

   ```bash
   read -rs RP_PASSWORD_RISKPREDICT_JOBS && export RP_PASSWORD_RISKPREDICT_JOBS
   ```

   ```bash
   python infra/scripts/create_login_roles.py --dsn-env DATABASE_URL_OWNER --role riskpredict_jobs:rp_feeds
   ```

   It prints only the role name, what it did and the checks (no `SUPERUSER`,
   `BYPASSRLS` or `CREATEROLE`; member of `rp_feeds` only; owns no tables).
3. **Add the owner secret first:** repository *Settings → Secrets and variables →
   Actions → New repository secret*, name `DATABASE_URL_OWNER`, value the
   `neondb_owner` connection string (the one `DATABASE_URL_FEEDS` holds today).
4. **Then replace `DATABASE_URL_FEEDS`** with the `riskpredict_jobs` string: same
   Neon host and database, `postgresql://riskpredict_jobs:<password>@<host>/<db>?sslmode=require`.
   Use the direct (non-`-pooler`) host; the jobs open short connections and don't
   need PgBouncer.
5. **Verify.** Run `feeds.yml` by hand (*Actions → feeds → Run workflow*, sources
   `usgs`, force on). Expect: "Apply migrations" prints `applied: none` (it ran as
   the owner), "Fetch feeds" shows `usgs ok`, "Prune old rows" prints the database
   size, and the `analyse` job succeeds. Then confirm the role from your machine:

   ```bash
   python infra/scripts/create_login_roles.py --dsn-env DATABASE_URL_OWNER --role riskpredict_jobs:rp_feeds --check
   ```

   If "Fetch feeds" fails with `database: could not read state (InsufficientPrivilege)`,
   the login is not in `rp_feeds`; if "Apply migrations" fails the same way, the
   `DATABASE_URL_OWNER` secret is missing or misspelt (the fallback then used the
   `rp_feeds` login).

To undo, delete `DATABASE_URL_OWNER` and put the owner string back into
`DATABASE_URL_FEEDS`.

### Self-hosting (`docker compose`)

The compose Postgres user is a superuser and runs the migrations. Create the login
roles the same way, with `DATABASE_URL_MIGRATE` pointing at the compose database, and
give the `api`/`worker` services the `rp_app` login. Never give the services the
superuser connection string.

## Session context (how RLS knows the workspace)

Every `biz` policy is keyed on two transaction-local settings:

```sql
SELECT set_config('app.workspace_id', '<uuid>', true),   -- true = this transaction only
       set_config('app.user_id',      '<uuid>', true);
```

* With neither set, every `biz` table returns no rows and accepts no writes.
* With only `app.user_id`, a user sees their own `users`, `sessions` and
  `memberships` rows, plus (read-only) the workspaces they belong to.
* **Always use `set_config(..., true)` inside a transaction, never plain `SET`.** A
  session-level setting would stay on a pooled connection and leak into the next
  request.

The API derives both from the verified access token and the `X-Workspace-Id` header,
*after* checking the membership. RLS is a second line of defence against a query that
forgets its `WHERE workspace_id = ...`. It does not replace the API's own checks: any
`rp_app` connection can set any workspace id.

## The five cross-workspace lookups

These are the only ways an `rp_app` connection can see across workspaces, for the
moments before a workspace is known. Each is a `SECURITY DEFINER` SQL function owned
by `rp_definer`, has `search_path` pinned, and returns only the columns listed.

| Function | Returns | Used for |
|---|---|---|
| `biz.auth_user_by_email(email)` | user_id, password_hash, disabled, locked_until, failed_sign_in_count | sign-in; adding a member by email |
| `biz.auth_session_by_refresh_hash(sha256)` | session_id, user_id, family_id, expires_at, rotated_at, revoked_at | `POST /v1/auth/refresh` |
| `biz.route_drive_channel(channel_id)` | workspace_id, connection_id, watch_token_hash, watch_resource_id | Drive push notifications |
| `biz.route_composio_account(connected_account_id)` | workspace_id, connection_id, provider | Composio triggers |
| `biz.worker_workspace_ids()` | workspace_id | the worker's per-workspace loop |

## Rules for later migrations

* Number new files from the next free number. `jobs.migrate` applies any file it
  has not recorded yet, in filename order, so gaps are fine. A lower number that lands
  later (e.g. `004`) is applied on the next run. Migrations must therefore not depend
  on files with a higher number.
* A new `biz` table needs `workspace_id`, `ENABLE` + `FORCE ROW LEVEL SECURITY`, a
  policy, and explicit grants to `rp_app`. There are deliberately no default
  privileges in `biz`. `tests/test_biz_db.py` fails if a table lacks forced RLS.
* A new `biz` function must end with `REVOKE EXECUTE ... FROM PUBLIC` (tested).
* A new `feeds` table gets `rp_feeds`/`rp_app` access automatically through default
  privileges, as long as the same owner role runs the migration.
* Postgres 16 or later is required: the migrations use `UNIQUE NULLS NOT DISTINCT`,
  `ON DELETE SET NULL (column)` and `GRANT ... WITH SET TRUE`.
