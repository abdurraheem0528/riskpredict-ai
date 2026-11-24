from __future__ import annotations

import json
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import httpx

FIXTURES = Path(__file__).parent / "fixtures"


def fixture_bytes(name: str) -> bytes:
    return (FIXTURES / name).read_bytes()


def fixture_json(name: str):
    return json.loads(fixture_bytes(name))


def query_of(request: httpx.Request) -> dict[str, str]:
    return {k: v[0] for k, v in parse_qs(urlsplit(str(request.url)).query).items()}


def reset_database(dsn: str) -> None:
    """Drop everything the migrations create in a throwaway test database.

    Roles are cluster-wide and survive; migration 005 is written to cope.
    """
    import psycopg

    with psycopg.connect(dsn, autocommit=True) as conn:
        conn.execute("DROP SCHEMA IF EXISTS biz CASCADE")
        conn.execute("DROP SCHEMA IF EXISTS feeds CASCADE")
