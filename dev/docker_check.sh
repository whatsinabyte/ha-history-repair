#!/bin/bash
# ==============================================================================
# Build and run the add-on the way Home Assistant does
# ==============================================================================
#
#   ./dev/docker_check.sh                  build for this machine's architecture,
#                                          MariaDB mode
#   ./dev/docker_check.sh --sqlite         same, but SQLite mode
#   ./dev/docker_check.sh --postgres       same, but PostgreSQL mode
#   ./dev/docker_check.sh --arm            also build the aarch64 image the ODROID
#                                          would run, under emulation
#   ./dev/docker_check.sh --keep           leave containers running for a
#                                          browser click-through
#   ./dev/docker_check.sh --clean          tear everything down and exit
#
# Flags combine, e.g. --sqlite --arm --keep. The three db modes are
# mutually exclusive; the last one given wins.
#
# This is the closest thing to a real installation that does not involve real
# hardware. In MariaDB mode it reproduces the shape of a HAOS install: a
# MariaDB container standing in for the official MariaDB add-on, and the
# add-on image talking to it by container name over a Docker network —
# exactly as it would reach core-mariadb. In SQLite mode it instead bind-mounts
# a directory holding a seeded home-assistant_v2.db at /homeassistant, exactly
# as Supervisor's homeassistant_config:rw map does. Either way the add-on's
# own entry point runs, so run.sh really parses a /data/options.json written
# the way the Supervisor's schema would write it.
#
# Nothing here touches the developer's MariaDB on port 3399, and nothing
# listens on the LAN: every published port is bound to 127.0.0.1.

set -euo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
NETWORK="hc-check-net"
DB_NAME="hc-check-mariadb"
PG_NAME="hc-check-postgres"
APP_NAME="hc-check-app"
IMAGE="history-repair:dev"
DB_PORT=3400
PG_PORT=5443
APP_PORT=8100

cleanup() {
    docker rm -f "${APP_NAME}" "${DB_NAME}" "${PG_NAME}" > /dev/null 2>&1 || true
    docker network rm "${NETWORK}" > /dev/null 2>&1 || true
}

DB_TYPE="mariadb"
BUILD_ARM=0
KEEP=0
for arg in "$@"; do
    case "${arg}" in
        --clean)
            cleanup
            echo "Removed the check containers and network."
            exit 0
            ;;
        --sqlite) DB_TYPE="sqlite" ;;
        --postgres) DB_TYPE="postgres" ;;
        --arm) BUILD_ARM=1 ;;
        --keep) KEEP=1 ;;
        *)
            echo "Unknown argument: ${arg}" >&2
            exit 1
            ;;
    esac
done

# --keep leaves the containers running afterwards so the UI can be clicked
# through in a browser. The trap is therefore only armed when it is absent.
if [ "${KEEP}" != "1" ]; then
    trap cleanup EXIT
fi

echo "==> Tearing down anything left from a previous run"
cleanup

echo "==> Building the add-on image for this machine"
docker build \
    --build-arg BUILD_ARCH=amd64 \
    -t "${IMAGE}" \
    "${REPO}"

if [ "${BUILD_ARM}" = "1" ]; then
    # The ODROID-M1 is aarch64. Building that image under emulation proves the
    # Dockerfile and its dependencies resolve for the architecture the add-on
    # would actually be installed on, without needing the device.
    echo "==> Building the aarch64 image the ODROID would run (emulated, slower)"
    docker build \
        --platform linux/arm64 \
        --build-arg BUILD_ARCH=aarch64 \
        -t "history-repair:dev-arm64" \
        "${REPO}"
fi

# Deliberately inside the repository rather than mktemp -d: colima shares only
# certain host paths into its VM, and macOS mktemp returns /var/folders/...,
# which is not one of them. A bind mount of such a path silently produces an
# empty directory inside the container.
OPTIONS_DIR="${REPO}/.devdb/docker-check/data"
APP_CONFIG_DIR="${REPO}/.devdb/docker-check/app_config"
rm -rf "${REPO}/.devdb/docker-check"
mkdir -p "${OPTIONS_DIR}" "${APP_CONFIG_DIR}"

if [ "${DB_TYPE}" = "sqlite" ]; then
    echo "==> Building a seeded SQLite recorder database"
    # HA_CONFIG_DIR stands in for the Supervisor's homeassistant_config mount,
    # which lands at /homeassistant inside the container — the same path
    # DEFAULT_SQLITE_PATH in hr_config.py assumes.
    HA_CONFIG_DIR="${REPO}/.devdb/docker-check/homeassistant"
    mkdir -p "${HA_CONFIG_DIR}"
    DB_FILE="${HA_CONFIG_DIR}/home-assistant_v2.db"
    "${REPO}/.venv-ha-schema/bin/python" "${REPO}/dev/build_schema.py" \
        --dsn "sqlite:///${DB_FILE}"
    "${REPO}/.venv-check/bin/python" "${REPO}/dev/seed_recorder.py" \
        --sqlite-path "${DB_FILE}" --days 2

    echo "==> Writing the /data/options.json the Supervisor would provide"
    cat > "${OPTIONS_DIR}/options.json" <<JSON
{
  "db_type": "sqlite",
  "db_path": "/homeassistant/home-assistant_v2.db",
  "log_level": "debug"
}
JSON

    echo "==> Starting the add-on container"
    docker run -d --name "${APP_NAME}" \
        -p "127.0.0.1:${APP_PORT}:8099" \
        -v "${OPTIONS_DIR}:/data:ro" \
        -v "${APP_CONFIG_DIR}:/config" \
        -v "${HA_CONFIG_DIR}:/homeassistant" \
        "${IMAGE}" > /dev/null
elif [ "${DB_TYPE}" = "postgres" ]; then
    echo "==> Starting a PostgreSQL container to stand in for an external server"
    docker network create "${NETWORK}" > /dev/null
    docker run -d --name "${PG_NAME}" --network "${NETWORK}" \
        -p "127.0.0.1:${PG_PORT}:5432" \
        -e POSTGRES_USER=hatest \
        -e POSTGRES_PASSWORD=hatest \
        -e POSTGRES_DB=ha_test \
        postgres:17-alpine > /dev/null

    # Checked over TCP from the host, the way the next step actually connects:
    # pg_isready inside the container reports ready while the bootstrap server
    # is still up and not yet accepting real network connections.
    printf "    waiting for PostgreSQL to accept queries over TCP"
    if ! "${REPO}/.venv-check/bin/python" - "${PG_PORT}" <<'PYWAIT'; then
import sys
import time

import psycopg

port = int(sys.argv[1])
deadline = time.time() + 120
while time.time() < deadline:
    try:
        conn = psycopg.connect(
            host="127.0.0.1", port=port, user="hatest",
            password="hatest", dbname="ha_test", connect_timeout=3,
        )
        conn.execute("SELECT 1")
        conn.close()
        sys.exit(0)
    except psycopg.Error:
        sys.stderr.write(".")
        sys.stderr.flush()
        time.sleep(2)
sys.exit(1)
PYWAIT
        echo
        echo "PostgreSQL never became reachable. Container log:" >&2
        docker logs "${PG_NAME}" 2>&1 | tail -20 >&2
        exit 1
    fi
    echo " ready"

    echo "==> Creating the recorder schema from Home Assistant's own models"
    "${REPO}/.venv-ha-schema/bin/python" "${REPO}/dev/build_schema.py" \
        --dsn "postgresql+psycopg://hatest:hatest@127.0.0.1:${PG_PORT}/ha_test"

    echo "==> Seeding sensor history"
    "${REPO}/.venv-check/bin/python" "${REPO}/dev/seed_recorder.py" \
        --postgres-dsn "postgresql://hatest:hatest@127.0.0.1:${PG_PORT}/ha_test" --days 2

    echo "==> Writing the /data/options.json the Supervisor would provide"
    cat > "${OPTIONS_DIR}/options.json" <<JSON
{
  "db_type": "postgres",
  "db_host": "${PG_NAME}",
  "db_port": 0,
  "db_name": "ha_test",
  "db_user": "hatest",
  "db_password": "hatest",
  "log_level": "debug"
}
JSON

    echo "==> Starting the add-on container"
    docker run -d --name "${APP_NAME}" --network "${NETWORK}" \
        -p "127.0.0.1:${APP_PORT}:8099" \
        -v "${OPTIONS_DIR}:/data:ro" \
        -v "${APP_CONFIG_DIR}:/config" \
        "${IMAGE}" > /dev/null
else
    echo "==> Starting a MariaDB container to stand in for the MariaDB add-on"
    docker network create "${NETWORK}" > /dev/null
    docker run -d --name "${DB_NAME}" --network "${NETWORK}" \
        -p "127.0.0.1:${DB_PORT}:3306" \
        -e MARIADB_ROOT_PASSWORD=rootpw \
        -e MARIADB_DATABASE=ha_test \
        -e MARIADB_USER=hatest \
        -e MARIADB_PASSWORD=hatest \
        mariadb:11 > /dev/null

    # Readiness has to be checked the way the next step will actually connect:
    # over TCP from the host. The official image runs a temporary bootstrap
    # server on a socket while initialising, so an in-container ping reports
    # ready long before the real server accepts network connections.
    printf "    waiting for MariaDB to accept queries over TCP"
    if ! "${REPO}/.venv-check/bin/python" - "${DB_PORT}" <<'PYWAIT'; then
import sys
import time

import pymysql

port = int(sys.argv[1])
deadline = time.time() + 120
while time.time() < deadline:
    try:
        conn = pymysql.connect(
            host="127.0.0.1", port=port, user="hatest",
            password="hatest", database="ha_test", connect_timeout=3,
        )
        with conn.cursor() as cur:
            cur.execute("SELECT 1")
            cur.fetchone()
        conn.close()
        sys.exit(0)
    except pymysql.Error:
        sys.stderr.write(".")
        sys.stderr.flush()
        time.sleep(2)
sys.exit(1)
PYWAIT
        echo
        echo "MariaDB never became reachable. Container log:" >&2
        docker logs "${DB_NAME}" 2>&1 | tail -20 >&2
        exit 1
    fi
    echo " ready"

    echo "==> Creating the recorder schema from Home Assistant's own models"
    "${REPO}/.venv-ha-schema/bin/python" "${REPO}/dev/build_schema.py" \
        --dsn "mysql+pymysql://hatest:hatest@127.0.0.1:${DB_PORT}/ha_test"

    echo "==> Seeding sensor history"
    "${REPO}/.venv-check/bin/python" "${REPO}/dev/seed_recorder.py" \
        --port "${DB_PORT}" --days 2

    echo "==> Writing the /data/options.json the Supervisor would provide"
    cat > "${OPTIONS_DIR}/options.json" <<JSON
{
  "db_type": "mariadb",
  "db_host": "${DB_NAME}",
  "db_port": 3306,
  "db_name": "ha_test",
  "db_user": "hatest",
  "db_password": "hatest",
  "log_level": "debug"
}
JSON

    echo "==> Starting the add-on container"
    docker run -d --name "${APP_NAME}" --network "${NETWORK}" \
        -p "127.0.0.1:${APP_PORT}:8099" \
        -v "${OPTIONS_DIR}:/data:ro" \
        -v "${APP_CONFIG_DIR}:/config" \
        "${IMAGE}" > /dev/null
fi

printf "    waiting for the add-on"
for _ in $(seq 1 60); do
    if curl -sf "http://127.0.0.1:${APP_PORT}/onboarding" > /dev/null 2>&1; then
        break
    fi
    printf "."
    sleep 1
done
echo

echo
echo "==> Container log"
docker logs "${APP_NAME}" 2>&1 | head -20

echo
echo "==> Checks"
status=$(curl -s -o /dev/null -w '%{http_code}' "http://127.0.0.1:${APP_PORT}/onboarding")
echo "    onboarding page          HTTP ${status}"

health=$(curl -s "http://127.0.0.1:${APP_PORT}/api/health")
echo "    health                   ${health}"

# Ingress serves the add-on under a token prefix; the Supervisor signals it
# with this header. Generated URLs must carry the prefix or every asset 404s.
ingress=$(curl -s -H 'X-Ingress-Path: /api/hassio_ingress/tok' \
    "http://127.0.0.1:${APP_PORT}/onboarding" | grep -c '/api/hassio_ingress/tok/static' || true)
echo "    ingress-prefixed assets  ${ingress} references"

if [ "${DB_TYPE}" = "sqlite" ]; then
    echo
    echo "==> Verifying the add-on actually wrote through the homeassistant_config mount"
    # The audit table lives inside home-assistant_v2.db itself in SQLite mode,
    # so a write the add-on makes must be visible from the host side of the
    # bind mount — proving the container really reached the mounted file, not
    # some path inside its own filesystem that happens to also be writable.
    onboard_status=$(curl -s -o /dev/null -w '%{http_code}' -X POST \
        -H "Content-Type: application/json" -d '{"backup_acknowledged": true}' \
        "http://127.0.0.1:${APP_PORT}/api/onboarding")
    echo "    onboarding POST           HTTP ${onboard_status}"
    audit_ready=$("${REPO}/.venv-check/bin/python" - "${HA_CONFIG_DIR}/home-assistant_v2.db" <<'PYCHECK'
import sqlite3
import sys

conn = sqlite3.connect(sys.argv[1])
row = conn.execute(
    "SELECT name FROM sqlite_master WHERE type='table' AND name='state_corrections'"
).fetchone()
print("yes" if row else "no")
PYCHECK
)
    echo "    audit table visible on host mount  ${audit_ready}"
fi

echo
if [ "${KEEP}" = "1" ]; then
    echo "Add-on left running at http://127.0.0.1:${APP_PORT}/"
    echo "Tear it down with: ./dev/docker_check.sh --clean"
elif [ -t 0 ]; then
    echo "Add-on is running at http://127.0.0.1:${APP_PORT}/ — press Enter to tear down."
    read -r _
else
    echo "Ran unattended; tearing down. Use --keep to leave the containers up."
fi
