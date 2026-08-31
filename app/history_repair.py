"""Entry point for the History Repair add-on.

run.sh execs this module directly. It reads the configuration prepared there
from the environment, builds the Flask application, and serves it with
gunicorn in-process so the Python process stays PID 1 and receives signals.
"""

from __future__ import annotations

import datetime
import logging
import sys
import threading
from typing import Any

from hr_config import AppConfig
from hr_discovery import apply_discovered_mariadb, discover_mariadb, discover_mqtt
from hr_mqtt import run_publisher_loop
from hr_web import create_app

# Matches the version: field in config.yaml. TestVersionParity asserts the two
# cannot drift apart.
ADDON_VERSION = "0.1.0"

_LOGGER = logging.getLogger(__name__)


class _UnifiedFormatter(logging.Formatter):
    """One line shape for every source in this add-on's log output.

    Without this, the same log stream mixed four unrelated formats: this
    module's own `%(asctime)s LEVEL name: msg`, gunicorn's bracketed
    `[timestamp] [pid] [LEVEL] msg`, gunicorn's Apache-style access log
    (`ip - - [date] "request" status size "referrer" "user-agent"`), and a
    plain, timestamp-less `echo` from run.sh. Applied to gunicorn's error and
    access loggers too (see `_GunicornLogger` below), so every line — this
    add-on's own messages, gunicorn's own startup/shutdown messages, and each
    HTTP request — reads the same way. Matches the convention already used in
    the sibling nibe-smo-mqtt-bridge-local project.
    """

    def format(self, record: logging.LogRecord) -> str:
        ct = datetime.datetime.fromtimestamp(record.created).astimezone()
        ts = ct.strftime("%H:%M:%S") + f".{ct.microsecond // 1000:03d}"
        return f"{ts} [{record.levelname:<8}] {record.name}: {record.getMessage()}"


def configure_logging(level: str) -> None:
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(_UnifiedFormatter())
    logging.basicConfig(
        level=getattr(logging, level.upper(), logging.INFO),
        handlers=[handler],
    )


def report_startup_health(app: Any) -> None:
    """Log the outcome of the database gates without refusing to start.

    A failing gate must not take the web server down: the onboarding screen is
    where the user reads what went wrong and fixes it, and that screen is only
    reachable if the server is running.
    """
    adapter = app.extensions["hr_db"]
    try:
        report = adapter.check_health()
    except Exception:
        _LOGGER.exception("Database health check raised during startup")
        return

    if report.ok:
        _LOGGER.info(
            "Connected to %s (recorder schema %s)",
            report.server_version,
            report.schema_version,
        )
    else:
        for message in report.errors:
            _LOGGER.error("Startup check failed: %s", message)
    for message in report.warnings:
        _LOGGER.warning("Startup check warning: %s", message)

    # Backup consistency check (design document, 9.11): a Home Assistant
    # backup restored while the add-on was stopped reverts states without
    # touching this add-on's own audit table, so a correction can claim to be
    # active while no longer describing what is actually stored. Only
    # meaningful once the audit table exists — before onboarding there is
    # nothing in it to be orphaned.
    if report.audit_table_ready:
        try:
            orphaned = adapter.find_orphaned_corrections()
        except Exception:
            _LOGGER.exception("Backup consistency check raised during startup")
        else:
            if orphaned:
                _LOGGER.warning(
                    "%d correction(s) no longer match the recorder database — "
                    "likely a backup was restored. Review them on the Corrections "
                    "page.",
                    len(orphaned),
                )


def serve(app: Any, port: int) -> None:
    """Run the app under gunicorn without spawning a separate process."""
    from gunicorn.app.base import BaseApplication
    from gunicorn.glogging import Logger as GunicornLogger

    class _GunicornLogger(GunicornLogger):
        """Gunicorn's own startup/shutdown messages and access log, in the
        same line shape as everything else — see `_UnifiedFormatter`."""

        def now(self) -> str:
            # The base class hardcodes Apache Common Log Format here
            # regardless of any Formatter passed in, so the access log's
            # embedded %(t)s atom (set to nothing below) would otherwise
            # stay in that format even after everything else changed.
            return ""

        def setup(self, cfg: Any) -> None:
            super().setup(cfg)
            formatter = _UnifiedFormatter()
            for logger in (self.error_log, self.access_log):
                for handler in logger.handlers:
                    handler.setFormatter(formatter)

    class _Server(BaseApplication):
        def load_config(self) -> None:
            self.cfg.set("bind", f"0.0.0.0:{port}")
            # One worker keeps the add-on's memory footprint small on the
            # low-powered hardware Home Assistant commonly runs on; threads
            # cover the handful of concurrent requests a single user makes.
            self.cfg.set("workers", 1)
            self.cfg.set("threads", 4)
            self.cfg.set("accesslog", "-")
            self.cfg.set("errorlog", "-")
            self.cfg.set("logger_class", _GunicornLogger)
            # The default embeds its own timestamp, remote user, referrer,
            # and full user-agent string — every request from the same
            # browser then repeats the same long user-agent verbatim, which
            # is what made the access log's lines look so different from
            # everything else in length and shape. The timestamp and level
            # already come from _UnifiedFormatter; the rest here is what is
            # actually useful for a single-user local add-on.
            self.cfg.set("access_log_format", '%(h)s "%(r)s" %(s)s %(b)s')

        def load(self) -> Any:
            return app

    _Server().run()


def _describe_db_target(config: AppConfig) -> str:
    if config.db_type == "sqlite":
        if config.sqlite is None:
            return "sqlite:(unconfigured)"
        return f"sqlite:{config.sqlite.path}"
    if config.database is None:
        return f"{config.db_type}:(unconfigured)"
    return f"{config.database.host}:{config.database.port}/{config.database.name}"


def main() -> None:
    config = AppConfig.from_env()
    configure_logging(config.log_level)

    # Prefer a MariaDB service Supervisor already knows about (typically the
    # official MariaDB add-on) over the user's own db_host/db_port/etc. — see
    # hr_discovery.py. A no-op for SQLite installs, or when nothing currently
    # provides the service.
    discovered = discover_mariadb()
    if discovered is not None and config.db_type == "mariadb":
        _LOGGER.info(
            "Using a MariaDB service discovered via Supervisor at %s:%d, "
            "overriding the configured db_host/db_port",
            discovered.host,
            discovered.port,
        )
    config = apply_discovered_mariadb(config, discovered)

    # Carries what run.sh used to report in its own pre-Python echo (the
    # database target and log level) — folded in here instead of left as a
    # separate, differently formatted line before this add-on's own logging
    # is even configured.
    _LOGGER.info(
        "History Repair %s starting (db=%s, log=%s)",
        ADDON_VERSION,
        _describe_db_target(config),
        config.log_level,
    )

    app = create_app(config)
    report_startup_health(app)
    start_mqtt_publisher(app)
    serve(app, config.port)


def start_mqtt_publisher(app: Any) -> None:
    """A daemon thread publishing the "needs review" entity, if a broker is
    discovered — see hr_mqtt.py. A no-op otherwise; MQTT is advisory and this
    add-on works identically without it.

    Daemon so it never blocks a clean shutdown: gunicorn's own stop timeout
    governs how long the process has to exit, and there is nothing here that
    needs an orderly finish more than that allows.
    """
    discovered = discover_mqtt()
    if discovered is None:
        return

    db = app.extensions["hr_db"]
    stop_event = threading.Event()
    thread = threading.Thread(
        target=run_publisher_loop,
        args=(discovered, ADDON_VERSION, lambda: bool(db.find_orphaned_corrections()), stop_event),
        daemon=True,
        name="mqtt-publisher",
    )
    thread.start()


if __name__ == "__main__":
    main()
