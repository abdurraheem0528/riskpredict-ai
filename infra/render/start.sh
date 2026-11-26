#!/bin/sh
# One container, two processes (Render free web service):
#   * the worker (python -m worker) in the background, under a watchdog;
#   * uvicorn, exec'd so it is PID 1 and receives Render's SIGTERM directly.
# If the worker stops, the watchdog sends SIGTERM to uvicorn; when uvicorn stops,
# the container exits. Either way Render restarts the service.
# Nothing here prints the environment.
set -eu

PORT="${PORT:-10000}"
# The worker reaches the API inside this container, never over the internet.
API_INTERNAL_URL="http://127.0.0.1:${PORT}"
export PORT API_INTERNAL_URL

main_pid=$$
(
  # Give uvicorn a moment to bind; the worker retries its long poll anyway.
  sleep 2
  status=0
  python -m worker || status=$?
  echo "start.sh: worker exited (status ${status}); stopping the API so the container restarts" >&2
  kill -TERM "${main_pid}" 2>/dev/null || true
) &

# No access log: URLs can carry one-time secrets; api/main.py logs route templates only.
exec uvicorn api.main:app --host 0.0.0.0 --port "${PORT}" --no-access-log --proxy-headers \
  --timeout-graceful-shutdown 5
