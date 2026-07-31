#!/bin/sh
# Bring the schema up to date, then hand off to the container command.
#
# The app never creates tables, so something has to run migrations before the
# first request. Doing it here means `docker compose up` on a clean volume
# works with no manual step.
set -e

# Set RUN_MIGRATIONS=false on any second container (a worker, a one-off shell)
# so two processes don't race each other through alembic on startup.
if [ "${RUN_MIGRATIONS:-true}" = "true" ]; then
    # Compose gates us on the postgres healthcheck, but a remote database
    # (Neon free tier) can still be waking up, so retry rather than crash-loop.
    attempt=1
    until alembic upgrade head; do
        if [ "$attempt" -ge 10 ]; then
            echo "entrypoint: alembic upgrade head failed after $attempt attempts" >&2
            exit 1
        fi
        echo "entrypoint: database not ready, retrying ($attempt/10)..." >&2
        attempt=$((attempt + 1))
        sleep 2
    done
    echo "entrypoint: schema is at head"
fi

exec "$@"
