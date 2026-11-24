"""Measure the real tagging model on the saved real fixture titles.

    python -m jobs.analyse.bench [--out results.json] [--n 100]

Prints only four aggregates: model size on disk, peak RAM, seconds per 100
events and the number of tags per event type. Per-title results and timing
detail go to ``--out`` if given, never to stdout. The fixtures are public responses saved in ``tests/fixtures``; for the
timing run they are cycled up to ``--n`` titles, so timings reflect real title
lengths, not a larger set of distinct articles.
"""

from __future__ import annotations

import argparse
import json
import os
import resource
import sys
import time
from pathlib import Path

FIXTURES = Path(__file__).resolve().parents[2] / "tests" / "fixtures"


def fixture_inputs():
    from jobs.analyse.runner import taggable
    from jobs.connectors import eonet, gdacs, gdelt, ofac

    def j(name):
        return json.loads((FIXTURES / name).read_bytes())

    events = [
        *gdelt.parse_articles(j("gdelt_artlist.json"), "fixture"),
        *gdacs.parse(j("gdacs_events4app_trimmed.json")),
        *eonet.parse(j("eonet_events.json")),
        *ofac.parse((FIXTURES / "ofac_delta_trimmed.xml").read_bytes()),
    ]
    return taggable(events)


def rss_mb() -> float:
    try:
        import psutil

        return psutil.Process().memory_info().rss / 2**20
    except ImportError:
        return float("nan")


def peak_rss_mb() -> float:
    return resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024  # KiB on Linux


def dir_mb(path: str) -> float:
    total = 0
    for root, _, files in os.walk(path):
        for f in files:
            p = os.path.join(root, f)
            if not os.path.islink(p):
                total += os.path.getsize(p)
    return total / 2**20


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog="python -m jobs.analyse.bench")
    parser.add_argument("--out", help="write per-title results (JSON) here")
    parser.add_argument("--n", type=int, default=100, help="events in the timing run")
    args = parser.parse_args(argv)

    from huggingface_hub import constants, snapshot_download

    from jobs.analyse import tagging

    items = fixture_inputs()
    rss0 = rss_mb()
    t0 = time.monotonic()
    model = tagging.Gliner2Model(batch_size=16, threads=os.cpu_count())
    load_s = time.monotonic() - t0
    rss_loaded = rss_mb()
    snapshot = snapshot_download(model.name, revision=model.revision, local_files_only=True)
    blobs = os.path.join(constants.HF_HUB_CACHE, "models--" + model.name.replace("/", "--"))

    raw = model.predict([i.title for i in items])  # warm-up + the results we keep
    tags = [tagging.build_tag(i, r) for i, r in zip(items, raw)]

    cycle = [items[k % len(items)] for k in range(args.n)]
    t1 = time.monotonic()
    model.predict([i.title for i in cycle])
    per_100 = (time.monotonic() - t1) * 100 / args.n

    report = {
        "model_files_mb": round(dir_mb(blobs), 1),
        "peak_rss_mb": round(peak_rss_mb()),
        "s_per_100_events": round(per_100, 2),
        "tags_per_type": {t: sum(1 for x in tags if x.event_type == t) for t in sorted({x.event_type for x in tags})},
    }
    # Detail for --out only (never printed): load time, memory steps, model guesses.
    detail = {
        "model": model.name,
        "revision": model.revision,
        "model_version": tagging.model_version(model.revision),
        "snapshot_files": sorted(os.listdir(snapshot)),
        "threads": os.cpu_count(),
        "load_s": round(load_s, 1),
        "rss_before_mb": round(rss0),
        "rss_after_load_mb": round(rss_loaded),
        "fixture_titles": len(items),
        "timing_titles": args.n,
        "guess_matches_source_type": sum(
            1 for t in tags if t.type_source == "source" and t.entities["type_guess"]["type"] == t.event_type
        ),
        "with_source_type": sum(1 for t in tags if t.type_source == "source"),
        "titles_with_country": sum(1 for t in tags if t.entities["countries"]),
    }
    print(json.dumps(report, indent=2))
    if args.out:
        rows = [
            {"source": i.source, "title": i.title, "event_type": t.event_type, "type_confidence": t.type_confidence,
             "type_source": t.type_source, "entities": t.entities}
            for i, t in zip(items, tags)
        ]
        Path(args.out).write_text(json.dumps({"report": report, "detail": detail, "results": rows}, indent=2, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
