#!/bin/sh
# Container entrypoint: migrate, then fetch once — or forever, every
# RUN_EVERY_SECONDS, which is how docker-compose self-hosts the 15-min schedule.
set -eu

python -m jobs.migrate

if [ -n "${RUN_EVERY_SECONDS:-}" ]; then
  while true; do
    # A failed run is reported in feeds.source_health; keep the loop alive.
    python -m jobs.run --sources "${SOURCES:-all}" "$@" || echo "run failed (exit $?)"
    sleep "$RUN_EVERY_SECONDS"
  done
fi

exec python -m jobs.run --sources "${SOURCES:-all}" "$@"
