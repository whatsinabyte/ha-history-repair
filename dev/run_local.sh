#!/bin/bash
# Run the add-on against a disposable development database.
#
#   ./dev/run_local.sh            # MariaDB (default), assumes ./dev/mariadb.sh start
#   ./dev/run_local.sh --sqlite   # SQLite, assumes dev/build_schema.py + seed_recorder.py
#                                  # have already built .devdb/sqlite/ha_test.db
#
# Then open http://127.0.0.1:8099/. Outside Supervisor Ingress there are no
# X-Remote-User-* headers, so corrections are attributed to "unknown".
#
# Onboarding state is kept under .devdb/ so it survives restarts and can be
# wiped with --fresh (combine as `--sqlite --fresh`, in either order).

set -euo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
STATE_DIR="${REPO}/.devdb/state"
mkdir -p "${STATE_DIR}"

DB_TYPE="mariadb"
FRESH=false
for arg in "$@"; do
    case "${arg}" in
        --sqlite) DB_TYPE="sqlite" ;;
        --fresh) FRESH=true ;;
        *)
            echo "Unknown argument: ${arg}" >&2
            exit 1
            ;;
    esac
done

if [ "${FRESH}" = true ]; then
    echo "Clearing onboarding state so the wizard runs again."
    rm -f "${STATE_DIR}/history_repair_state.json"
fi

export HR_DB_TYPE="${DB_TYPE}"
export HR_LOG_LEVEL="${HR_LOG_LEVEL:-debug}"
export HR_PORT="${HR_PORT:-8099}"
export HR_STATE_DIR="${STATE_DIR}"

if [ "${DB_TYPE}" = "sqlite" ]; then
    export HR_DB_PATH="${HR_DEV_DB_PATH:-${REPO}/.devdb/sqlite/ha_test.db}"
    echo "History Repair → http://127.0.0.1:${HR_PORT}/"
    echo "Database: sqlite:${HR_DB_PATH}"
else
    export HR_DB_HOST="${HR_DEV_DB_HOST:-127.0.0.1}"
    export HR_DB_PORT="${HR_DEV_DB_PORT:-3399}"
    export HR_DB_NAME="${HR_DEV_DB_NAME:-ha_test}"
    export HR_DB_USER="${HR_DEV_DB_USER:-hatest}"
    export HR_DB_PASSWORD="${HR_DEV_DB_PASSWORD:-hatest}"
    echo "History Repair → http://127.0.0.1:${HR_PORT}/"
    echo "Database: ${HR_DB_USER}@${HR_DB_HOST}:${HR_DB_PORT}/${HR_DB_NAME}"
fi
echo

cd "${REPO}"
exec .venv-check/bin/python app/history_repair.py
