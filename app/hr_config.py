"""Add-on configuration, read from the environment prepared by run.sh.

Pure module: no I/O beyond reading os.environ, no imports from other hr_*
modules. Everything else in the add-on receives an AppConfig rather than
reaching for the environment itself.
"""

from __future__ import annotations

import os
from dataclasses import dataclass

# Fail a correction quickly rather than hang if the recorder is holding the
# target row. See the design document, section 9.9.
DEFAULT_LOCK_WAIT_TIMEOUT = 5

# Home Assistant's own default location for a SQLite recorder database,
# inside the configuration directory every installation already has.
DEFAULT_SQLITE_PATH = "/homeassistant/home-assistant_v2.db"

# Each server backend's own conventional port, used when db_port is left at
# the schema default. Without this a PostgreSQL user would have to know to
# change a field whose default silently names MySQL's port.
_DEFAULT_PORTS = {"postgres": 5432, "mariadb": 3306}


@dataclass(frozen=True)
class DatabaseConfig:
    """Connection details for a MariaDB/MySQL recorder database."""

    host: str
    port: int
    name: str
    user: str
    password: str
    lock_wait_timeout: int = DEFAULT_LOCK_WAIT_TIMEOUT


@dataclass(frozen=True)
class SQLiteConfig:
    """Connection details for a SQLite recorder database file.

    A separate type from DatabaseConfig rather than overloading its `name`
    field as a file path: a SQLite connection has no host, port, user, or
    password, and leaving those fields present but meaningless invites a
    caller to read them by mistake.
    """

    path: str
    lock_wait_timeout: int = DEFAULT_LOCK_WAIT_TIMEOUT


@dataclass(frozen=True)
class AppConfig:
    db_type: str
    database: DatabaseConfig | None
    sqlite: SQLiteConfig | None
    log_level: str
    state_dir: str
    port: int
    # "12" or "24". Not read from Home Assistant: there is no API a backend
    # add-on can use to read a specific browsing user's personal date/time
    # display preference — that setting lives entirely in the frontend's own
    # per-user storage, and Ingress does not hand the add-on that user's own
    # access token to query it with. A real, deterministic day-month-year
    # format was tried first with the browser's ambient locale driving 12 vs
    # 24-hour, and that rendered US-style 12-hour time inside Home Assistant's
    # own Ingress panel for a user whose system was set to 24-hour — so this
    # is instead an explicit choice, set once, rather than detected.
    time_format: str

    @classmethod
    def from_env(cls) -> AppConfig:
        db_type = os.environ.get("HR_DB_TYPE", "mariadb").strip().lower()

        database: DatabaseConfig | None = None
        sqlite: SQLiteConfig | None = None
        if db_type == "sqlite":
            sqlite = SQLiteConfig(path=os.environ.get("HR_DB_PATH", DEFAULT_SQLITE_PATH))
        else:
            # An empty db_port means "use whatever is standard for this
            # backend" — 3306 for MariaDB, 5432 for PostgreSQL — so a
            # PostgreSQL user is not silently pointed at MySQL's port by a
            # default that predates PostgreSQL support.
            raw_port = os.environ.get("HR_DB_PORT", "").strip()
            port = int(raw_port) if raw_port else _DEFAULT_PORTS.get(db_type, 3306)
            database = DatabaseConfig(
                host=os.environ.get("HR_DB_HOST", "core-mariadb"),
                port=port,
                name=os.environ.get("HR_DB_NAME", "homeassistant"),
                user=os.environ.get("HR_DB_USER", "homeassistant"),
                password=os.environ.get("HR_DB_PASSWORD", ""),
            )

        time_format = os.environ.get("HR_TIME_FORMAT", "24").strip()
        if time_format not in ("12", "24"):
            time_format = "24"

        return cls(
            db_type=db_type,
            database=database,
            sqlite=sqlite,
            log_level=os.environ.get("HR_LOG_LEVEL", "info").upper(),
            state_dir=os.environ.get("HR_STATE_DIR", "/config"),
            port=int(os.environ.get("HR_PORT", "8099")),
            time_format=time_format,
        )
