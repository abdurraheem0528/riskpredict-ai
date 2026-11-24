"""Create (or check) the login roles that use the rp_* group roles.

Passwords never live in the repository and are never printed. They come from
an environment variable or stdin, and are sent to Postgres only inside the
CREATE/ALTER ROLE statement (Neon requires plaintext there; it stores a SCRAM
verifier). See docs/database-roles.md.

    # owner connection string (Neon: the neondb_owner role), never a login below
    export DATABASE_URL_MIGRATE='postgresql://neondb_owner:...@.../neondb?sslmode=require'

    # passwords from env: RP_PASSWORD_<NAME IN UPPER CASE>
    RP_PASSWORD_RISKPREDICT_JOBS=... RP_PASSWORD_RISKPREDICT_APP=... \\
      python infra/scripts/create_login_roles.py \\
        --role riskpredict_jobs:rp_feeds --role riskpredict_app:rp_app

    # or one password per line on stdin, in --role order
    pass show neon/jobs neon/app | python infra/scripts/create_login_roles.py --password-stdin \\
        --role riskpredict_jobs:rp_feeds --role riskpredict_app:rp_app

    # only verify existing roles (no changes); exit 1 on any problem
    python infra/scripts/create_login_roles.py --check \\
        --role riskpredict_jobs:rp_feeds --role riskpredict_app:rp_app

Existing roles keep their password unless --rotate is given. Every run checks
that each login role is a plain member of exactly its group: not superuser,
no BYPASSRLS / CREATEROLE / CREATEDB, not in neon_superuser or any other role,
and owning no tables.
"""

from __future__ import annotations

import argparse
import getpass
import os
import re
import sys

import psycopg
from psycopg import sql

GROUPS = ("rp_feeds", "rp_app")
NAME_RE = re.compile(r"^[a-z][a-z0-9_]{2,62}$")
MIN_PASSWORD = 16  # Neon also rejects passwords under ~60 bits of entropy
ATTRS = "LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION NOBYPASSRLS INHERIT"


class RoleError(Exception):
    """Safe to print: never contains a password or a connection string."""


def env_var(name: str) -> str:
    return "RP_PASSWORD_" + name.upper()


def parse_role(spec: str) -> tuple[str, str]:
    name, _, group = spec.partition(":")
    if not NAME_RE.match(name):
        raise RoleError(f"bad role name {name!r}: lower case letters, digits and _ only")
    if name.startswith("rp_"):
        raise RoleError("the rp_ prefix is reserved for the group roles")
    if group not in GROUPS:
        raise RoleError(f"group for {name} must be one of {', '.join(GROUPS)}")
    return name, group


def role_exists(conn: psycopg.Connection, name: str) -> bool:
    return conn.execute("SELECT 1 FROM pg_roles WHERE rolname = %s", (name,)).fetchone() is not None


def ensure_login_role(conn: psycopg.Connection, name: str, group: str, password: str | None, rotate: bool = False) -> str:
    """Create the role, or bring an existing one in line. Returns what was done."""
    if not role_exists(conn, group):
        raise RoleError(f"group role {group} does not exist: apply the migrations first")
    exists = role_exists(conn, name)
    if (not exists or rotate) and (password is None or len(password) < MIN_PASSWORD):
        raise RoleError(f"a password of at least {MIN_PASSWORD} characters is needed for {name}")
    ident = sql.Identifier(name)
    with conn.transaction():
        if not exists:
            conn.execute(
                sql.SQL("CREATE ROLE {} " + ATTRS + " PASSWORD {}").format(ident, sql.Literal(password))
            )
            done = "created"
        else:
            # A non-superuser owner (Neon's neondb_owner) may not even *clear*
            # SUPERUSER/REPLICATION/BYPASSRLS, so only touch them when one is set;
            # then a permission error is the right outcome.
            wide = conn.execute(
                "SELECT rolsuper OR rolcreatedb OR rolcreaterole OR rolreplication OR rolbypassrls "
                "FROM pg_roles WHERE rolname = %s",
                (name,),
            ).fetchone()[0]
            conn.execute(sql.SQL("ALTER ROLE {} " + (ATTRS if wide else "LOGIN INHERIT")).format(ident))
            done = "unchanged"
            if rotate:
                conn.execute(sql.SQL("ALTER ROLE {} PASSWORD {}").format(ident, sql.Literal(password)))
                done = "password rotated"
        if not conn.execute("SELECT pg_has_role(%s, %s, 'MEMBER')", (name, group)).fetchone()[0]:
            conn.execute(sql.SQL("GRANT {} TO {}").format(sql.Identifier(group), ident))
            if done == "unchanged":
                done = "added to group"
    return done


def check_login_role(conn: psycopg.Connection, name: str, group: str) -> list[str]:
    """Problems with an existing login role; empty when it is safe to use."""
    row = conn.execute(
        "SELECT rolcanlogin, rolsuper, rolbypassrls, rolcreaterole, rolcreatedb, rolreplication "
        "FROM pg_roles WHERE rolname = %s",
        (name,),
    ).fetchone()
    if row is None:
        return [f"{name}: does not exist"]
    problems = []
    for flag, label in zip(row, ("LOGIN", "SUPERUSER", "BYPASSRLS", "CREATEROLE", "CREATEDB", "REPLICATION")):
        if label == "LOGIN":
            if not flag:
                problems.append(f"{name}: cannot log in")
        elif flag:
            problems.append(f"{name}: has {label}")
    member_of = [
        r[0]
        for r in conn.execute(
            "SELECT g.rolname FROM pg_auth_members m JOIN pg_roles g ON g.oid = m.roleid "
            "JOIN pg_roles u ON u.oid = m.member WHERE u.rolname = %s ORDER BY 1",
            (name,),
        ).fetchall()
    ]
    if group not in member_of:
        problems.append(f"{name}: not a member of {group}")
    for other in member_of:
        if other != group:
            problems.append(f"{name}: also a member of {other}")
    owned = conn.execute(
        "SELECT count(*) FROM pg_class c JOIN pg_roles r ON r.oid = c.relowner WHERE r.rolname = %s",
        (name,),
    ).fetchone()[0]
    if owned:
        problems.append(f"{name}: owns {owned} relation(s)")
    return problems


def read_passwords(names: list[str], from_stdin: bool) -> dict[str, str | None]:
    out: dict[str, str | None] = {}
    lines = sys.stdin.read().splitlines() if from_stdin else []
    for i, name in enumerate(names):
        if from_stdin:
            out[name] = lines[i] if i < len(lines) and lines[i] else None
        elif os.environ.get(env_var(name)):
            out[name] = os.environ[env_var(name)]
        elif sys.stdin.isatty():
            out[name] = getpass.getpass(f"password for {name} (leave empty to keep an existing role's): ") or None
        else:
            out[name] = None
    return out


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--role", action="append", required=True, metavar="NAME:GROUP",
                        help="login role and its group (rp_feeds or rp_app); repeatable")
    parser.add_argument("--dsn-env", default="DATABASE_URL_MIGRATE",
                        help="env var holding the owner connection string (default: %(default)s)")
    parser.add_argument("--password-stdin", action="store_true", help="read one password per line, in --role order")
    parser.add_argument("--rotate", action="store_true", help="set a new password on roles that already exist")
    parser.add_argument("--check", action="store_true", help="only verify; change nothing")
    args = parser.parse_args(argv)

    dsn = os.environ.get(args.dsn_env)
    if not dsn:
        print(f"{args.dsn_env} is not set", file=sys.stderr)
        return 2
    try:
        roles = [parse_role(r) for r in args.role]
        passwords = {} if args.check else read_passwords([n for n, _ in roles], args.password_stdin)
        with psycopg.connect(dsn, autocommit=True, connect_timeout=20, application_name="riskpredict-roles") as conn:
            problems: list[str] = []
            for name, group in roles:
                if not args.check:
                    print(f"{name} ({group}): {ensure_login_role(conn, name, group, passwords[name], args.rotate)}")
                problems += check_login_role(conn, name, group)
    except RoleError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    except psycopg.Error as exc:
        # The message of a failed CREATE ROLE can echo the statement; print only the class.
        print(f"database error ({type(exc).__name__})", file=sys.stderr)
        return 1
    for p in problems:
        print(f"problem: {p}", file=sys.stderr)
    if not problems:
        print("ok: every role is a plain member of its group")
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main())
