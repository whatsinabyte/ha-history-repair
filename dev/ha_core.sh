#!/bin/bash
# ==============================================================================
# A real Home Assistant, recording into a disposable MariaDB
# ==============================================================================
#
#   ./dev/ha_core.sh start    bring up MariaDB + Home Assistant, onboard it,
#                             and print a long-lived access token
#   ./dev/ha_core.sh stop     stop both containers, keep the data
#   ./dev/ha_core.sh clean    remove containers, network, and config
#   ./dev/ha_core.sh logs     follow the Home Assistant log
#   ./dev/ha_core.sh token    print the saved access token
#
# Why this exists
# ---------------
# The statistics work needs things only a running recorder can provide:
#
#   * Statistics rows computed by Home Assistant's own arithmetic, rather than
#     by this project's approximation of it. Correcting a bucket means
#     reproducing that arithmetic exactly, and validating against our own
#     guess would prove nothing.
#   * The recorder/import_statistics WebSocket command, which is not a
#     database operation at all and cannot be exercised against a bare schema.
#   * Answers about columns the design document predates — schema 53's
#     mean_weight on statistics, and unit_class on statistics_meta.
#
# Everything is disposable and bound to 127.0.0.1. It touches nothing on the
# ODROID and shares nothing with the development database on port 3399.

set -euo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
STATE="${REPO}/.devdb/ha-core"
CONFIG_DIR="${STATE}/config"
TOKEN_FILE="${STATE}/token.txt"

NETWORK="hc-ha-net"
DB_CONTAINER="hc-ha-mariadb"
HA_CONTAINER="hc-ha-core"
DB_PORT=3402
HA_PORT=8123

DB_NAME="ha_test_core"
DB_USER="hatest"
DB_PASS="hatest"

# Onboarding credentials for the throwaway instance. Local only, never exposed
# beyond 127.0.0.1, and destroyed by `clean`.
HA_USER="dev"
HA_PASS="devpassword123"

cmd_clean() {
    docker rm -f "${HA_CONTAINER}" "${DB_CONTAINER}" > /dev/null 2>&1 || true
    docker network rm "${NETWORK}" > /dev/null 2>&1 || true
    rm -rf "${STATE}"
    echo "Removed containers, network, and ${STATE}."
}

cmd_stop() {
    docker stop "${HA_CONTAINER}" "${DB_CONTAINER}" > /dev/null 2>&1 || true
    echo "Stopped. Data kept — 'start' brings it back."
}

wait_for_db() {
    printf "    waiting for MariaDB over TCP"
    "${REPO}/.venv-check/bin/python" - "${DB_PORT}" "${DB_NAME}" <<'PYWAIT'
import sys
import time

import pymysql

port, database = int(sys.argv[1]), sys.argv[2]
deadline = time.time() + 180
while time.time() < deadline:
    try:
        conn = pymysql.connect(
            host="127.0.0.1", port=port, user="hatest", password="hatest",
            database=database, connect_timeout=3,
        )
        conn.close()
        sys.exit(0)
    except pymysql.Error:
        sys.stderr.write(".")
        sys.stderr.flush()
        time.sleep(2)
sys.exit(1)
PYWAIT
    echo " ready"
}

cmd_start() {
    mkdir -p "${CONFIG_DIR}"

    docker network create "${NETWORK}" > /dev/null 2>&1 || true

    if ! docker ps -a --format '{{.Names}}' | grep -qx "${DB_CONTAINER}"; then
        echo "==> Starting MariaDB"
        docker run -d --name "${DB_CONTAINER}" --network "${NETWORK}" \
            -p "127.0.0.1:${DB_PORT}:3306" \
            -e MARIADB_ROOT_PASSWORD=rootpw \
            -e "MARIADB_DATABASE=${DB_NAME}" \
            -e "MARIADB_USER=${DB_USER}" \
            -e "MARIADB_PASSWORD=${DB_PASS}" \
            mariadb:11 > /dev/null
    else
        docker start "${DB_CONTAINER}" > /dev/null
    fi
    wait_for_db

    # A deliberately small configuration: the recorder pointed at MariaDB, and
    # a handful of template sensors that change every few seconds so the
    # recorder has something to aggregate into statistics quickly.
    if [ ! -f "${CONFIG_DIR}/configuration.yaml" ]; then
        echo "==> Writing configuration.yaml"
        cat > "${CONFIG_DIR}/configuration.yaml" <<YAML
default_config:

recorder:
  db_url: mysql://${DB_USER}:${DB_PASS}@${DB_CONTAINER}/${DB_NAME}?charset=utf8mb4
  commit_interval: 1
  purge_keep_days: 30

# Sensors that move on their own, so the recorder has real values to
# aggregate. state_class is what makes the recorder generate statistics:
# measurement produces mean/min/max, total_increasing produces state/sum —
# the two cases the correction engine has to treat differently.
template:
  - trigger:
      - platform: time_pattern
        seconds: "/5"
    sensor:
      - name: Dev Temperature
        unique_id: dev_temperature
        unit_of_measurement: "°C"
        device_class: temperature
        state_class: measurement
        state: "{{ (20 + range(-30, 30) | random / 10.0) | round(2) }}"

      - name: Dev Humidity
        unique_id: dev_humidity
        unit_of_measurement: "%"
        device_class: humidity
        state_class: measurement
        state: "{{ (55 + range(-80, 80) | random / 10.0) | round(2) }}"

      - name: Dev Energy
        unique_id: dev_energy
        unit_of_measurement: "kWh"
        device_class: energy
        state_class: total_increasing
        state: >-
          {{ (states('sensor.dev_energy') | float(1000) + range(1, 20) | random / 100.0) | round(4) }}

logger:
  default: warning
  logs:
    homeassistant.components.recorder: info
YAML
    fi

    if ! docker ps -a --format '{{.Names}}' | grep -qx "${HA_CONTAINER}"; then
        echo "==> Starting Home Assistant"
        docker run -d --name "${HA_CONTAINER}" --network "${NETWORK}" \
            -p "127.0.0.1:${HA_PORT}:8123" \
            -v "${CONFIG_DIR}:/config" \
            -e TZ=Europe/Amsterdam \
            ghcr.io/home-assistant/home-assistant:stable > /dev/null
    else
        docker start "${HA_CONTAINER}" > /dev/null
    fi

    printf "    waiting for Home Assistant to answer"
    for _ in $(seq 1 180); do
        if curl -sf "http://127.0.0.1:${HA_PORT}/manifest.json" > /dev/null 2>&1; then
            break
        fi
        printf "."
        sleep 2
    done
    echo " up"

    if [ ! -f "${TOKEN_FILE}" ]; then
        echo "==> Onboarding and creating an access token"
        "${REPO}/.venv-check/bin/python" "${REPO}/dev/ha_onboard.py" \
            --url "http://127.0.0.1:${HA_PORT}" \
            --username "${HA_USER}" \
            --password "${HA_PASS}" \
            --out "${TOKEN_FILE}"
    fi

    echo
    echo "Home Assistant  http://127.0.0.1:${HA_PORT}/   (${HA_USER} / ${HA_PASS})"
    echo "MariaDB         127.0.0.1:${DB_PORT}  ${DB_NAME}  (${DB_USER}/${DB_PASS})"
    echo "Access token    ${TOKEN_FILE}"
    echo
    echo "Statistics appear within ~5 minutes (short term) and on the hour."
}

case "${1:-}" in
    start) cmd_start ;;
    stop)  cmd_stop ;;
    clean) cmd_clean ;;
    logs)  docker logs -f "${HA_CONTAINER}" ;;
    token) cat "${TOKEN_FILE}" ;;
    *)
        echo "usage: $0 {start|stop|clean|logs|token}" >&2
        exit 64
        ;;
esac
