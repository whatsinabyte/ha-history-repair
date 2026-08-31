#!/bin/bash
# ==============================================================================
# Disposable PostgreSQL for development and integration tests
# ==============================================================================
#
# Runs a PostgreSQL server isolated from anything else on the machine: its own
# container, a non-default port, and `reset` removes it and its data outright.
# It exists so the SQL in hr_postgres.py can be tested against a real server
# without going anywhere near a live Home Assistant recorder database.
#
# Docker, not a native binary: like mosquitto and unlike MariaDB, PostgreSQL
# is not otherwise part of this project's toolchain, and the official image
# needs no install step. See dev/mosquitto.sh for the same reasoning.
#
#   ./dev/postgres.sh start     start the server (pulls the image if needed)
#   ./dev/postgres.sh stop      stop and remove the container
#   ./dev/postgres.sh status    is it running?
#   ./dev/postgres.sh shell     open psql against the test database
#   ./dev/postgres.sh reset     stop it and delete its data volume

set -euo pipefail

CONTAINER="hc-dev-postgres"
VOLUME="hc-dev-postgres-data"
IMAGE="postgres:17-alpine"

# Deliberately not 5432: a mistyped host must fail to connect rather than
# quietly reach some other PostgreSQL on this machine.
PORT="${HR_DEV_PG_PORT:-5442}"
DBNAME="${HR_DEV_PG_NAME:-ha_test}"
DBUSER="${HR_DEV_PG_USER:-hatest}"
DBPASS="${HR_DEV_PG_PASSWORD:-hatest}"

is_running() {
    docker ps --format '{{.Names}}' | grep -qx "${CONTAINER}"
}

cmd_start() {
    if is_running; then
        echo "Already running on port ${PORT}."
        return 0
    fi
    docker rm -f "${CONTAINER}" > /dev/null 2>&1 || true

    echo "Starting PostgreSQL on port ${PORT}…"
    docker run -d --name "${CONTAINER}" \
        -e POSTGRES_USER="${DBUSER}" \
        -e POSTGRES_PASSWORD="${DBPASS}" \
        -e POSTGRES_DB="${DBNAME}" \
        -p "127.0.0.1:${PORT}:5432" \
        -v "${VOLUME}:/var/lib/postgresql/data" \
        "${IMAGE}" > /dev/null

    for _ in $(seq 1 60); do
        if docker exec "${CONTAINER}" pg_isready -U "${DBUSER}" -d "${DBNAME}" \
            > /dev/null 2>&1; then
            break
        fi
        sleep 0.5
    done

    if ! docker exec "${CONTAINER}" pg_isready -U "${DBUSER}" -d "${DBNAME}" > /dev/null 2>&1; then
        echo "PostgreSQL failed to start. Container logs:" >&2
        docker logs "${CONTAINER}" >&2
        exit 1
    fi

    # The integration suite clones this database once per pytest-xdist worker
    # (ha_test_gw0_1234, …) so parallel runs cannot trample each other, the
    # same way the MariaDB suite does. CREATEDB is what lets it.
    docker exec "${CONTAINER}" psql -U "${DBUSER}" -d "${DBNAME}" \
        -c "ALTER USER ${DBUSER} CREATEDB;" > /dev/null

    echo "Ready: postgresql://${DBUSER}:${DBPASS}@127.0.0.1:${PORT}/${DBNAME}"
}

cmd_stop() {
    if ! is_running; then
        echo "Not running."
        return 0
    fi
    echo "Stopping PostgreSQL…"
    docker rm -f "${CONTAINER}" > /dev/null
    echo "Stopped."
}

cmd_status() {
    if is_running; then
        echo "Running on port ${PORT}."
    else
        echo "Not running."
    fi
}

cmd_shell() {
    docker exec -it "${CONTAINER}" psql -U "${DBUSER}" -d "${DBNAME}"
}

cmd_reset() {
    cmd_stop
    echo "Deleting volume ${VOLUME}"
    docker volume rm "${VOLUME}" > /dev/null 2>&1 || true
    echo "Gone. Run 'start' for a clean one."
}

case "${1:-}" in
    start)  cmd_start ;;
    stop)   cmd_stop ;;
    status) cmd_status ;;
    shell)  cmd_shell ;;
    reset)  cmd_reset ;;
    *)
        echo "usage: $0 {start|stop|status|shell|reset}" >&2
        exit 64
        ;;
esac
