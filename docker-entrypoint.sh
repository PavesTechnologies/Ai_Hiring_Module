#!/usr/bin/env bash
# ==============================================================================
# AIRS container entrypoint.
#
# One image runs all three AIRS processes; the first argument picks which.
# They are separate containers on purpose - the API, the Celery worker and
# Celery beat have different scaling rules, and beat in particular MUST be a
# single replica (two beats duplicate every one of the 10 scheduled jobs).
#
#   api         uvicorn (app.main:app)
#   worker      celery worker
#   beat        celery beat            <- exactly one replica, ever
#   migrate     alembic upgrade head, then exit
#   healthcheck used by HEALTHCHECK; dispatches on the role
#   <anything>  executed verbatim (e.g. `bash`, `python -m ...`)
# ==============================================================================
set -euo pipefail

CELERY_APP="app.core.celery_app"
ROLE="${1:-api}"

log() { printf '[entrypoint] %s\n' "$*" >&2; }

# ------------------------------------------------------------------------------
# Record the role so `healthcheck` (invoked later, as its own process, with no
# arguments of its own) knows what kind of container it is running inside.
# ------------------------------------------------------------------------------
ROLE_FILE="/tmp/airs-role"

# ------------------------------------------------------------------------------
# Config sanity: settings.Settings has no defaults for these, so a missing one
# fails deep inside pydantic at import time with a stack trace that does not
# obviously say "you forgot an env var". Fail fast and name the variable.
# ------------------------------------------------------------------------------
require_env() {
    local missing=()
    local var
    for var in DB_USER DB_PASSWORD DB_HOST DB_NAME \
               SUPABASE_URL SUPABASE_PUBLISHABLE_KEY SUPABASE_SECRET_KEY SUPABASE_JWKS_URL \
               UMS_URL CORS_ORIGINS; do
        if [ -z "${!var:-}" ]; then
            missing+=("${var}")
        fi
    done
    if [ ${#missing[@]} -gt 0 ]; then
        log "FATAL: required environment variables are unset: ${missing[*]}"
        log "Pass them with --env-file .env, or individually with -e."
        exit 78  # EX_CONFIG
    fi

    # DEBUG=True makes app/db/database.py build the engine with echo=True,
    # which logs every single SQL statement. Harmless locally, a genuine
    # problem in production (log volume, and query params land in the logs).
    case "${DEBUG:-}" in
        [Tt]rue|1|dev|development|local)
            log "WARNING: DEBUG=${DEBUG} enables SQLAlchemy echo - every SQL statement will be logged."
            log "WARNING: set DEBUG=production for a deployed environment."
            ;;
    esac
}

case "${ROLE}" in

    api)
        require_env
        echo "api" > "${ROLE_FILE}"
        # Deliberately single-worker by default. settings.db_pool_size=1 with
        # max_overflow=2 is per-process, so every extra uvicorn worker adds 3
        # more PostgreSQL connections; scale with more containers (each with
        # its own known connection cost) rather than with workers inside one.
        # Both FastAPI startup hooks also re-run per worker, and one of them
        # writes (recover_stalled_resume_uploads).
        log "starting uvicorn on ${HOST}:${PORT} (workers=${UVICORN_WORKERS})"
        exec uvicorn app.main:app \
            --host "${HOST}" \
            --port "${PORT}" \
            --workers "${UVICORN_WORKERS}" \
            --proxy-headers \
            --forwarded-allow-ips '*' \
            --timeout-keep-alive 65 \
            --log-level "${UVICORN_LOG_LEVEL:-info}"
        ;;

    worker)
        require_env
        echo "worker" > "${ROLE_FILE}"
        # No --pool flag: celery_app.py already selects the pool by platform
        # (solo on Windows, prefork elsewhere), and this image is Linux, so it
        # correctly gets prefork. Concurrency is capped low because each
        # prefork child holds its own engine - PostgreSQL connections are
        # roughly CELERY_CONCURRENCY x 3.
        log "starting celery worker (concurrency=${CELERY_CONCURRENCY})"
        exec celery -A "${CELERY_APP}" worker \
            --concurrency "${CELERY_CONCURRENCY}" \
            --loglevel "${CELERY_LOGLEVEL}" \
            --without-gossip \
            --without-mingle
        ;;

    beat)
        require_env
        echo "beat" > "${ROLE_FILE}"
        # RUN EXACTLY ONE OF THESE. beat is a scheduler, not a worker: a second
        # replica double-fires all 10 entries in celery_app.conf.beat_schedule.
        # --schedule points at a writable path outside /app (the shipped code
        # is owned by root and the process runs as `airs`).
        log "starting celery beat (schedule=${CELERYBEAT_SCHEDULE})"
        exec celery -A "${CELERY_APP}" beat \
            --schedule "${CELERYBEAT_SCHEDULE}" \
            --loglevel "${CELERY_LOGLEVEL}"
        ;;

    migrate)
        require_env
        # Run as a one-shot job BEFORE rolling the api/worker/beat containers,
        # never as an init container on every replica - concurrent alembic runs
        # race on the alembic_version row.
        log "running alembic upgrade head"
        exec alembic upgrade head
        ;;

    healthcheck)
        case "$(cat "${ROLE_FILE}" 2>/dev/null || echo unknown)" in
            api)
                exec python -c "\
import os, sys, urllib.request; \
url = 'http://127.0.0.1:' + os.environ.get('PORT', '8002') + '/health'; \
sys.exit(0 if urllib.request.urlopen(url, timeout=8).status == 200 else 1)"
                ;;
            worker)
                # Pings this worker by name only; a broker-wide ping would go
                # green on any healthy worker in the cluster, including when
                # this particular container is the wedged one.
                exec celery -A "${CELERY_APP}" inspect ping \
                    --destination "celery@$(hostname)" --timeout 8 >/dev/null
                ;;
            beat)
                # beat exposes nothing to probe - no HTTP port, and it is not
                # a worker so it never answers an inspect ping. Docker already
                # restarts the container if the process dies, which is the only
                # failure mode worth catching here.
                exit 0
                ;;
            *)
                # Reached before the role file is written, or in a `bash` shell
                # container. Not a failure.
                exit 0
                ;;
        esac
        ;;

    *)
        # Escape hatch: `docker run airs:latest bash`, one-off seed scripts
        # (python -m app.seeds.seed_platform_config), celery inspect, etc.
        log "executing: $*"
        exec "$@"
        ;;
esac
