"""Fetch public feeds and store them.

    python -m jobs.run --sources all
    python -m jobs.run --sources usgs,gdacs --dry-run
    python -m jobs.run --sources all --force      # ignore minimum intervals
    python -m jobs.run --analyse                  # tag events + detect spikes in stored data
    python -m jobs.run --analyse --dry-run        # fetch live, analyse in memory, store nothing

``--dry-run`` fetches and prints counts only; no database is needed. Output is
limited to per-source status, counts and timings — never record contents —
because job logs are public. ``--analyse`` does not fetch (except with
``--dry-run``, which has no stored data to analyse); see :mod:`jobs.analyse`.

Exit codes: 0 done (individual sources may still have failed: see the table
and ``feeds.source_health``), 1 database failure, 2 bad usage or missing
configuration. With ``--strict``, any source error also exits 1.
"""

from __future__ import annotations

import argparse
import sys
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from typing import TYPE_CHECKING

import psycopg

from jobs import db
from jobs.connectors import CONNECTORS
from jobs.connectors.base import RunContext
from jobs.http import make_client
from jobs.models import FetchResult, Status
from jobs.watch_defaults import DEFAULT_TERMS

# jobs.analyse needs numpy/scikit-learn (requirements-analyse.txt), which the
# fetch-only install (requirements.txt) does not have, so it is imported only
# on the --analyse path.
if TYPE_CHECKING:
    from jobs.analyse.runner import AnalysisResult

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
        elif r.sanctions:
            note = "; ".join(filter(None, [sanctions_note(r, written is not None), note]))
        print(
            f"{r.source:<12} {r.status.value:<10} {len(r.events):>7} {len(r.series):>7} "
            f"{changed:>8} {r.duration_ms:>7}  {note}",
            file=out,
        )


def sanctions_note(r: FetchResult, stored: bool) -> str:
    """Sanctions lists go to ``feeds.sanctions_entries``, not events or series, so
    the table would otherwise show 0 / 0 for a run that read every entry."""
    parts = [f"entries={len(r.sanctions)}"]
    parts += [f"{d}={r.notes[f'{d}_entries']}" for d in ("sdn", "consolidated") if f"{d}_entries" in r.notes]
    if stored and "sanctions_upserted" in r.notes:
        parts += [f"upserted={r.notes['sanctions_upserted']}", f"delisted={r.notes.get('sanctions_delisted', 0)}"]
    return " ".join(parts)


def parse_steps(value: str) -> list[str]:
    from jobs.analyse import runner as analyse

    if value.strip().lower() == "all":
        return list(analyse.STEPS)
    steps = [n.strip().lower() for n in value.split(",") if n.strip()]
    if not steps or any(n not in analyse.STEPS for n in steps):
        raise argparse.ArgumentTypeError(f"choose from: all, {', '.join(analyse.STEPS)}")
    return list(dict.fromkeys(steps))


def print_analysis(results: list[AnalysisResult], out=None) -> None:
    """Status and counts per step; notes hold only counts keyed by rule/type names."""
    out = out or sys.stdout
    header = f"{'analysis':<10} {'status':<21} {'inputs':>7} {'outputs':>8} {'ms':>8}  note"
    print(header, file=out)
    print("-" * len(header), file=out)
    for r in results:
        print(
            f"{r.name:<10} {r.status:<21} {r.inputs:>7} {r.outputs:>8} {r.duration_ms:>8}  {r.error or ''}",
            file=out,
        )
    for r in results:
        if r.notes:
            print(f"  {r.name}: " + ", ".join(f"{k}={v}" for k, v in sorted(r.notes.items())), file=out)


def load_model():
    """Load the tagging model. Replaced in tests with a fake model."""
    from jobs.analyse import runner as analyse

    return analyse.default_loader()


def run_analysis(args, names: list[str], now: datetime) -> int:
    from jobs.analyse import runner as analyse
    from jobs.analyse import store as analyse_store
    from jobs.analyse.tagging import TAG_SOURCES

    steps: list[str] = args.analysis or parse_steps("all")
    results: list[AnalysisResult] = []

    if args.dry_run:
        fetched = fetch_all(names, RunContext(now=now, watch_terms=list(DEFAULT_TERMS)))
        print_table(fetched, None)
        print()
        events = [e for r in fetched for e in r.events]
        if "tagging" in steps:
            items = analyse.taggable(events)
            res, _, _ = analyse.tag(lambda: load_model(), lambda m, v: items)
            results.append(res)
        if "anomalies" in steps:
            terms = [t.term for t in DEFAULT_TERMS if t.kind == "gdelt_query"]
            res, _ = analyse.detect_all(
                analyse.series_from_results(fetched), analyse.news_rows_from_events(events, now), terms
            )
            results.append(res)
        print_analysis(results)
        print("\ndry run, nothing written")
        return 1 if args.strict and any(r.status == "error" for r in results) else 0

    dsn = db.dsn_from_env()
    if not dsn:
        print(f"{db.ENV_VAR} is not set (use --dry-run to analyse freshly fetched data)", file=sys.stderr)
        return 2
    try:
        last = analyse_store.last_success(dsn)
        if "tagging" in steps:
            res, tags, ident = analyse.tag(
                lambda: load_model(),
                lambda m, v: analyse_store.pending_tag_inputs(dsn, m, v, TAG_SOURCES, analyse.TAG_MAX_EVENTS),
            )
            if ident and tags:
                res.outputs = analyse_store.write_tags(dsn, tags, *ident, now)
            notes = {**res.notes, "inputs": res.inputs, "written": res.outputs, "duration_ms": res.duration_ms}
            if ident:
                notes.update(model=ident[0], model_version=ident[1])
            analyse_store.write_status(
                dsn, [analyse_store.status_row("tagging", "*", res.status, now, error=res.error, notes=notes)]
            )
            results.append(res)
        if "anomalies" in steps:
            if not (args.force or analyse.is_due(last.get("anomalies"), now, analyse.ANOMALY_MIN_INTERVAL)):
                results.append(analyse.AnalysisResult("anomalies", "skipped", error="not due yet"))
            else:
                series = analyse_store.load_series(dsn, now)
                news_rows, terms = analyse_store.load_news_counts(dsn, now)
                res, detections = analyse.detect_all(series, news_rows, terms)
                job = analyse_store.status_row(
                    "anomalies", "*", res.status, now,
                    notes={**res.notes, "keys": res.inputs, "flags": res.outputs, "duration_ms": res.duration_ms},
                )
                analyse_store.write_detections(dsn, detections, job, now)
                results.append(res)
    except db.DatabaseError as exc:
        print_analysis(results)
        print(f"database: {exc}", file=sys.stderr)
        return 1
    except psycopg.Error as exc:  # e.g. migration 003 not applied
        print_analysis(results)
        print(f"database: analysis failed ({type(exc).__name__})", file=sys.stderr)
        return 1

    print_analysis(results)
    print("\nwritten to feeds schema")
    return 1 if args.strict and any(r.status == "error" for r in results) else 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m jobs.run", description="Fetch public risk feeds.")
    parser.add_argument("--sources", type=parse_sources, default="all", help="'all' or comma-separated names")
    parser.add_argument("--dry-run", action="store_true", help="fetch and print counts; no database")
    parser.add_argument("--force", action="store_true", help="run sources even if not due yet")
    parser.add_argument("--strict", action="store_true", help="exit 1 if any source errors")
    parser.add_argument("--analyse", action="store_true", help="tag events and detect spikes (no fetching)")
    parser.add_argument(
        "--analysis", type=parse_steps, default=None, help="with --analyse: 'all', 'tagging' or 'anomalies'"
    )
    args = parser.parse_args(argv)
    names: list[str] = args.sources  # argparse also runs `type` on the string default
    now = datetime.now(timezone.utc)
    if args.analyse:
        return run_analysis(args, names, now)

    dsn = None
    if args.dry_run:
        terms = DEFAULT_TERMS
        last_success: dict[str, datetime | None] = {}
        state: dict[str, dict] = {}
    else:
        dsn = db.dsn_from_env()
        if not dsn:
            print(f"{db.ENV_VAR} is not set (use --dry-run to fetch without a database)", file=sys.stderr)
            return 2
        try:
            terms, last_success, state = db.load_state(dsn)
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

    ctx = RunContext(now=now, watch_terms=list(terms), state=state)
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
