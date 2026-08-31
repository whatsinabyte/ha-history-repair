#!/bin/bash
# ==============================================================================
# Disposable MariaDB for development and integration tests
# ==============================================================================
#
# Runs a MariaDB server that is completely isolated from anything else on the
# machine: its own data directory inside this repository, its own socket, and a
# non-default port. It shares nothing with a Homebrew service instance, and
# `reset` deletes it outright.
#
# It exists so that the SQL in hr_mariadb.py can be tested against a real
# server without going anywhere near a live Home Assistant recorder database.
#
#   ./dev/mariadb.sh start     start the server (creates the data dir if needed)
#   ./dev/mariadb.sh stop      stop it
#   ./dev/mariadb.sh status    is it running?
#   ./dev/mariadb.sh shell     open a client against the test database
#   ./dev/mariadb.sh reset     stop it and delete every trace

set -euo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
DEVDB="${REPO}/.devdb"
DATADIR="${DEVDB}/data"
SOCKET="${DEVDB}/mysql.sock"
PIDFILE="${DEVDB}/mysqld.pid"
LOGFILE="${DEVDB}/mysqld.log"

# Deliberately not 3306: a mistyped host must fail to connect rather than
# quietly reach some other database on this machine.
PORT="${HR_DEV_DB_PORT:-3399}"
DBNAME="${HR_DEV_DB_NAME:-ha_test}"
DBUSER="${HR_DEV_DB_USER:-hatest}"

for candidate in /usr/local/opt/mariadb/bin /opt/homebrew/opt/mariadb/bin; do
    [ -d "${candidate}" ] && export PATH="${candidate}:${PATH}"
done

is_running() {
    [ -f "${PIDFILE}" ] && kill -0 "$(cat "${PIDFILE}")" 2>/dev/null
}

cmd_start() {
    if is_running; then
        echo "Already running on port ${PORT} (pid $(cat "${PIDFILE}"))."
        return 0
    fi

    if [ ! -d "${DATADIR}" ]; then
        echo "Initialising a fresh data directory at ${DATADIR}"
        mkdir -p "${DATADIR}"
        mariadb-install-db --datadir="${DATADIR}" --auth-root-authentication-method=normal \
            > "${DEVDB}/install.log" 2>&1
    fi

    echo "Starting MariaDB on port ${PORT}…"
    mariadbd \
        --datadir="${DATADIR}" \
        --socket="${SOCKET}" \
        --port="${PORT}" \
        --pid-file="${PIDFILE}" \
        --bind-address=127.0.0.1 \
        --skip-name-resolve \
        > "${LOGFILE}" 2>&1 &

    for _ in $(seq 1 60); do
        if mariadb-admin --socket="${SOCKET}" --user=root ping > /dev/null 2>&1; then
            break
        fi
        sleep 0.5
    done

    if ! mariadb-admin --socket="${SOCKET}" --user=root ping > /dev/null 2>&1; then
        echo "MariaDB failed to start. Last lines of ${LOGFILE}:" >&2
        tail -20 "${LOGFILE}" >&2
        exit 1
    fi

    # The test user mirrors what the official MariaDB add-on grants the
    # recorder user, so the add-on's privilege check exercises a realistic
    # grant rather than root's implicit everything.
    mariadb --socket="${SOCKET}" --user=root <<SQL
CREATE DATABASE IF NOT EXISTS \`${DBNAME}\`
  CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci;
CREATE USER IF NOT EXISTS '${DBUSER}'@'%' IDENTIFIED BY '${DBUSER}';
GRANT ALL PRIVILEGES ON \`${DBNAME}\`.* TO '${DBUSER}'@'%';
-- The integration suite clones this database once per pytest-xdist worker
-- (ha_test_gw0, ha_test_gw1, …) so parallel runs cannot trample each other.
-- The underscore is escaped because it is a LIKE wildcard in GRANT; the
-- trailing % is left as a wildcard on purpose.
GRANT ALL PRIVILEGES ON \`${DBNAME}\\_%\`.* TO '${DBUSER}'@'%';
FLUSH PRIVILEGES;
SQL

    echo "Ready: mysql://${DBUSER}:${DBUSER}@127.0.0.1:${PORT}/${DBNAME}"
}

cmd_stop() {
    if ! is_running; then
        echo "Not running."
        return 0
    fi
    echo "Stopping MariaDB (pid $(cat "${PIDFILE}"))…"
    mariadb-admin --socket="${SOCKET}" --user=root shutdown 2>/dev/null || kill "$(cat "${PIDFILE}")"
    for _ in $(seq 1 40); do
        is_running || break
        sleep 0.5
    done
    echo "Stopped."
}

cmd_status() {
    if is_running; then
        echo "Running on port ${PORT} (pid $(cat "${PIDFILE}"))."
    else
        echo "Not running."
    fi
}

cmd_shell() {
    mariadb --socket="${SOCKET}" --user=root "${DBNAME}"
}

cmd_reset() {
    cmd_stop
    echo "Deleting ${DEVDB}"
    rm -rf "${DEVDB}"
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
