"""Apply plain-SQL migrations from ``infra/migrations`` in filename order.

    python -m jobs.migrate            # uses DATABASE_URL_FEEDS
    python -m jobs.migrate --status   # list applied / pending, change nothing

Each file runs once, in its own transaction, recorded in
``feeds.schema_migrations``. An advisory lock stops two runs racing.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

import psycopg

from jobs import db

MIGRATIONS_DIR = Path(__file__).resolve().parent.parent / "infra" / "migrations"
LOCK_ID = 0x5249534B  # "RISK"

BOOTSTRAP = """
CREATE SCHEMA IF NOT EXISTS feeds;
CREATE TABLE IF NOT EXISTS feeds.schema_migrations (
    version    text PRIMARY KEY,
    applied_at timestamptz NOT NULL DEFAULT now()
);
"""


def migration_files(directory: Path = MIGRATIONS_DIR) -> list[Path]:
    return sorted(p for p in directory.glob("*.sql") if p.is_file())


def apply(dsn: str, directory: Path = MIGRATIONS_DIR, status_only: bool = False) -> list[str]:
    """Return the versions applied (or pending, with ``status_only``)."""
    files = migration_files(directory)
    done: list[str] = []
    with db.connect(dsn) as conn:
        with conn.transaction():
            conn.execute("SELECT pg_advisory_xact_lock(%s)", (LOCK_ID,))
            conn.execute(BOOTSTRAP)
        applied = {r[0] for r in conn.execute("SELECT version FROM feeds.schema_migrations").fetchall()}
        for path in files:
            version = path.stem
            if version in applied:
                continue
            if status_only:
                done.append(version)
                continue
            with conn.transaction():
                conn.execute("SELECT pg_advisory_xact_lock(%s)", (LOCK_ID,))
                # Re-check under the lock in case another run just applied it.
                if conn.execute(
                    "SELECT 1 FROM feeds.schema_migrations WHERE version = %s", (version,)
                ).fetchone():
                    continue
                conn.execute(path.read_text(encoding="utf-8"))
                conn.execute("INSERT INTO feeds.schema_migrations (version) VALUES (%s)", (version,))
            done.append(version)
    return done


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m jobs.migrate", description=__doc__.splitlines()[0])
    parser.add_argument("--status", action="store_true", help="only list pending migrations")
    args = parser.parse_args(argv)
    dsn = db.dsn_from_env()
    if not dsn:
        print(f"{db.ENV_VAR} is not set", file=sys.stderr)
        return 2
    try:
        versions = apply(dsn, status_only=args.status)
    except db.DatabaseError as exc:
        print(f"migration failed: {exc}", file=sys.stderr)
        return 1
    except psycopg.Error as exc:
        print(f"migration failed ({type(exc).__name__})", file=sys.stderr)
        return 1
    label = "pending" if args.status else "applied"
    print(f"{label}: {', '.join(versions) if versions else 'none'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
