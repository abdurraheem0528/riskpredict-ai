"""Database access for the API and the worker, as an ``rp_app`` login.

Neon free tier (contracts decision 10): the pool starts empty (``min_size=0``)
and closes connections idle for more than 60 s, so the compute can scale to
zero. Nothing holds a connection open between requests or ticks.

Row-level security context (decision 3) is always transaction-local:
``set_config('app.workspace_id', ..., true)`` inside the transaction, never a
session-level ``SET`` (which would leak on a pooled connection).
"""

from __future__ import annotations

import logging
from contextlib import contextmanager
from typing import Iterator
from uuid import UUID

import psycopg
from psycopg.rows import dict_row
from psycopg_pool import ConnectionPool

log = logging.getLogger("riskpredict.db")


class Database:
    def __init__(self, dsn: str, *, max_size: int = 5, max_idle: float = 60.0, application_name: str = "riskpredict-api"):
        if not dsn:
            raise ValueError("DATABASE_URL_APP is not set")
        self._pool = ConnectionPool(
            dsn,
            min_size=0,
            max_size=max_size,
            max_idle=max_idle,
            open=False,
            timeout=20,
            kwargs={
                "autocommit": True,  # each `with conn.transaction()` is a real BEGIN/COMMIT
                "row_factory": dict_row,
                "prepare_threshold": None,  # safe behind PgBouncer (Neon pooled host)
                "application_name": application_name,
                "connect_timeout": 20,
            },
            name=application_name,
        )

    def open(self) -> None:
        self._pool.open(wait=False)

    def close(self) -> None:
        self._pool.close()

    @contextmanager
    def tx(self) -> Iterator[psycopg.Connection]:
        """One transaction with no session context: only `feeds` and the lookups are visible."""
        with self._pool.connection() as conn, conn.transaction():
            yield conn

    @contextmanager
    def user_tx(self, user_id: UUID | str) -> Iterator[psycopg.Connection]:
        with self.tx() as conn:
            set_user(conn, user_id)
            yield conn

    @contextmanager
    def workspace_tx(self, workspace_id: UUID | str, user_id: UUID | str | None = None) -> Iterator[psycopg.Connection]:
        """For the worker and webhooks, which have already resolved the workspace
        through a SECURITY DEFINER lookup. The API's user requests go through
        ``api.context`` instead, which checks the membership first."""
        with self.tx() as conn:
            if user_id is not None:
                set_user(conn, user_id)
            set_workspace(conn, workspace_id)
            yield conn


def set_user(conn: psycopg.Connection, user_id: UUID | str) -> None:
    conn.execute("SELECT set_config('app.user_id', %s, true)", (str(user_id),))


def set_workspace(conn: psycopg.Connection, workspace_id: UUID | str) -> None:
    conn.execute("SELECT set_config('app.workspace_id', %s, true)", (str(workspace_id),))


def schema_version(conn: psycopg.Connection) -> str | None:
    row = conn.execute("SELECT max(version) AS v FROM feeds.schema_migrations").fetchone()
    return row["v"] if row else None


def has_migration(conn: psycopg.Connection, version: str) -> bool:
    return conn.execute("SELECT 1 FROM feeds.schema_migrations WHERE version = %s", (version,)).fetchone() is not None
