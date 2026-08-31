#!/bin/bash
# ==============================================================================
# Disposable MQTT broker for development and integration tests
# ==============================================================================
#
# Runs a plain eclipse-mosquitto container, isolated from anything else on the
# machine: a non-default port, no persistence, and `reset` removes it outright.
# It exists so that hr_mqtt.py's actual wire behaviour (discovery payload,
# retained messages, the will) can be verified against a real broker rather
# than only against the fake client in tests/test_mqtt.py.
#
# Docker/colima, not a native binary: unlike dev/mariadb.sh's Homebrew
# mariadbd, mosquitto is not already part of this project's toolchain, and
# the official image starts in under a second with no install step.
#
#   ./dev/mosquitto.sh start     start the broker (pulls the image if needed)
#   ./dev/mosquitto.sh stop      stop and remove the container
#   ./dev/mosquitto.sh status    is it running?
#   ./dev/mosquitto.sh reset     alias for stop; there is no persisted state

set -euo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
CONTAINER="hc-dev-mosquitto"
IMAGE="eclipse-mosquitto:2"

# Deliberately not 1883: a mistyped host must fail to connect rather than
# quietly reach some other broker on this machine.
PORT="${HR_DEV_MQTT_PORT:-1893}"

is_running() {
    docker ps --format '{{.Names}}' | grep -qx "${CONTAINER}"
}

cmd_start() {
    if is_running; then
        echo "Already running on port ${PORT}."
        return 0
    fi
    docker rm -f "${CONTAINER}" > /dev/null 2>&1 || true

    echo "Starting mosquitto on port ${PORT}…"
    docker run -d --name "${CONTAINER}" \
        -p "127.0.0.1:${PORT}:1893" \
        -v "${REPO}/dev/mosquitto.conf:/mosquitto/config/mosquitto.conf:ro" \
        "${IMAGE}" > /dev/null

    for _ in $(seq 1 40); do
        if docker exec "${CONTAINER}" sh -c "mosquitto_pub -p 1893 -t healthcheck -m ok" \
            > /dev/null 2>&1; then
            break
        fi
        sleep 0.25
    done

    if ! docker exec "${CONTAINER}" sh -c "mosquitto_pub -p 1893 -t healthcheck -m ok" \
        > /dev/null 2>&1; then
        echo "mosquitto failed to start. Container logs:" >&2
        docker logs "${CONTAINER}" >&2
        exit 1
    fi

    echo "Ready: 127.0.0.1:${PORT}"
}

cmd_stop() {
    if ! is_running; then
        echo "Not running."
        return 0
    fi
    echo "Stopping mosquitto…"
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

case "${1:-}" in
    start)  cmd_start ;;
    stop)   cmd_stop ;;
    status) cmd_status ;;
    reset)  cmd_stop ;;
    *)
        echo "usage: $0 {start|stop|status|reset}" >&2
        exit 64
        ;;
esac
