"""Fetch public feeds and store them.

    python -m jobs.run --sources all
    python -m jobs.run --sources usgs,gdacs --dry-run
    python -m jobs.run --sources all --force      # ignore minimum intervals

``--dry-run`` fetches and prints counts only; no database is needed. Output is
limited to per-source status, counts and timings — never record contents —
because job logs are public.

Exit codes: 0 done (individual sources may still have failed: see the table
and ``feeds.source_health``), 1 database failure, 2 bad usage or missing
configuration. With ``--strict``, any source error also exits 1.
"""

from __future__ import annotations

import argparse
import sys
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone

from jobs import db
from jobs.connectors import CONNECTORS
from jobs.connectors.base import RunContext
from jobs.http import make_client
from jobs.models import FetchResult, Status
from jobs.watch_defaults import DEFAULT_TERMS

# GitHub's scheduler often starts runs late; don't skip a source for that.
SCHEDULE_SLACK = timedelta(minutes=3)
MAX_WORKERS = 4


def parse_sources(value: str) -> list[str]:
    if value.strip().lower() == "all":
        return list(CONNECTORS)
    names = [n.strip().lower() for n in value.split(",") if n.strip()]
    unknown = [n for n in names if n not in CONNECTORS]
    if unknown or not names:
        raise argparse.ArgumentTypeError(
            f"unknown source(s): {', '.join(unknown) or '(none)'}; choose from: all, {', '.join(CONNECTORS)}"
        )
    return list(dict.fromkeys(names))


def is_due(name: str, last_success: datetime | None, now: datetime) -> bool:
    if last_success is None:
        return True
    return now - last_success >= CONNECTORS[name].min_interval - SCHEDULE_SLACK


def fetch_all(names: list[str], ctx: RunContext, client=None) -> list[FetchResult]:
    own_client = client is None
    client = client or make_client()
    try:
        with ThreadPoolExecutor(max_workers=MAX_WORKERS) as pool:
            futures = [pool.submit(CONNECTORS[n]().run, client, ctx) for n in names]
            return [f.result() for f in futures]
    finally:
        if own_client:
            client.close()


def print_table(results: list[FetchResult], written: dict[str, int | str] | None, out=None) -> None:
    out = out or sys.stdout
    header = f"{'source':<12} {'status':<10} {'events':>7} {'series':>7} {'changed':>8} {'ms':>7}  note"
    print(header, file=out)
    print("-" * len(header), file=out)
    for r in results:
        changed = "-" if written is None else str(written.get(r.source, "-"))
        note = r.error or ""
        if changed.startswith("db error"):
            note, changed = changed, "-"
        print(
            f"{r.source:<12} {r.status.value:<10} {len(r.events):>7} {len(r.series):>7} "
            f"{changed:>8} {r.duration_ms:>7}  {note}",
            file=out,
        )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m jobs.run", description="Fetch public risk feeds.")
    parser.add_argument("--sources", type=parse_sources, default="all", help="'all' or comma-separated names")
    parser.add_argument("--dry-run", action="store_true", help="fetch and print counts; no database")
    parser.add_argument("--force", action="store_true", help="run sources even if not due yet")
    parser.add_argument("--strict", action="store_true", help="exit 1 if any source errors")
    args = parser.parse_args(argv)
    names: list[str] = args.sources  # argparse also runs `type` on the string default
    now = datetime.now(timezone.utc)

    dsn = None
    if args.dry_run:
        terms = DEFAULT_TERMS
        last_success: dict[str, datetime | None] = {}
    else:
        dsn = db.dsn_from_env()
        if not dsn:
            print(f"{db.ENV_VAR} is not set (use --dry-run to fetch without a database)", file=sys.stderr)
            return 2
        try:
            terms, last_success = db.load_state(dsn)
        except db.DatabaseError as exc:
            print(f"database: {exc}", file=sys.stderr)
            return 1
        except Exception as exc:  # e.g. migrations not applied
            print(f"database: could not read state ({type(exc).__name__})", file=sys.stderr)
            return 1

    due = [n for n in names if args.force or args.dry_run or is_due(n, last_success.get(n), now)]
    not_due = [n for n in names if n not in due]
    if not_due:
        print(f"not due yet: {', '.join(not_due)}")
    if not due:
        print("nothing to do")
        return 0

    ctx = RunContext(now=now, watch_terms=list(terms))
    results = fetch_all(due, ctx)

    written = None
    if not args.dry_run:
        try:
            written = db.write_results(dsn, results, now)
        except db.DatabaseError as exc:
            print_table(results, None)
            print(f"database: {exc}", file=sys.stderr)
            return 1

    print_table(results, written)
    total = sum(r.record_count for r in results)
    mode = "dry run, nothing written" if args.dry_run else "written to feeds schema"
    print(f"\n{len(results)} source(s), {total} record(s) — {mode}")
    db_failed = written is not None and any(isinstance(v, str) for v in written.values())
    if db_failed:
        return 1
    if args.strict and any(r.status in (Status.ERROR, Status.THROTTLED) for r in results):
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
