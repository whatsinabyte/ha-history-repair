#!/bin/bash
# ==============================================================================
# Home Assistant Add-on: History Repair
# ==============================================================================
#
# Reads the recorder database connection and log level from options.json and
# exports them for the Python web application. The app binds the Ingress port
# only — nothing is published on the LAN, because anything that can reach it
# can rewrite the recorder database.

set -euo pipefail

DB_TYPE=$(jq -r '.db_type // "sqlite"' /data/options.json)
DB_PATH=$(jq -r '.db_path // "/homeassistant/home-assistant_v2.db"' /data/options.json)
DB_HOST=$(jq -r '.db_host // "core-mariadb"' /data/options.json)
# 0 (the schema's default) means "whichever port is standard for db_type".
# Exported as an empty string so hr_config.py picks 3306 or 5432 for itself
# rather than this script having to know the mapping too.
DB_PORT=$(jq -r '.db_port // 0' /data/options.json)
if [ "${DB_PORT}" = "0" ]; then
  DB_PORT=""
fi
DB_NAME=$(jq -r '.db_name // "homeassistant"' /data/options.json)
DB_USER=$(jq -r '.db_user // "homeassistant"' /data/options.json)
DB_PASSWORD=$(jq -r '.db_password // ""' /data/options.json)
LOG_LEVEL=$(jq -r '.log_level // "info"' /data/options.json)
TIME_FORMAT=$(jq -r '.time_format // "24"' /data/options.json)

export HR_DB_TYPE="${DB_TYPE}"
export HR_DB_PATH="${DB_PATH}"
export HR_DB_HOST="${DB_HOST}"
export HR_DB_PORT="${DB_PORT}"
export HR_DB_NAME="${DB_NAME}"
export HR_DB_USER="${DB_USER}"
export HR_DB_PASSWORD="${DB_PASSWORD}"
export HR_LOG_LEVEL="${LOG_LEVEL}"
export HR_TIME_FORMAT="${TIME_FORMAT}"

# Ingress always proxies to this port; it is not reachable from the LAN.
export HR_PORT=8099

# Onboarding state lives in this add-on's own config directory, which the
# Supervisor maps for us at /config — the app_config map type's default
# container path since Supervisor freed it up for this purpose (previously
# /addon_config under the now-deprecated addon_config map type).
export HR_STATE_DIR=/config

# history_repair.py logs an equivalent startup line itself, in the same
# format as everything else it and gunicorn log — a second, differently
# formatted line here would be redundant and the first inconsistency in the
# log stream.
cd /app
exec python3 history_repair.py
