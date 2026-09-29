#!/bin/bash
# Run the API and the indexing worker in one container.
#
# For single-instance hosting (Render free tier), where a separate background
# worker is a paid service. Locally, docker compose still runs them apart.
# If either process dies, the other is stopped and the container exits so the
# platform restarts both.

# bash is PID 1 here and does not pass SIGTERM on by itself; without this trap
# every redeploy waits out the grace period and SIGKILLs both processes.
trap 'kill -TERM "$worker" "$api" 2>/dev/null' TERM INT

arq app.worker.WorkerSettings &
worker=$!
uvicorn app.main:app --host 0.0.0.0 --port "${PORT:-8000}" &
api=$!

wait -n
status=$?
kill -TERM "$worker" "$api" 2>/dev/null
wait
exit "$status"
