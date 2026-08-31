"""Service discovery through Supervisor: MariaDB and MQTT.

When another add-on already provides a service this add-on can use — the
official MariaDB add-on's "mysql" service, or any MQTT broker add-on's "mqtt"
service — Supervisor can hand over its connection details automatically, so a
user does not need to copy host/port/credentials into this add-on's own
configuration by hand. Manual configuration remains what is used for a
service Supervisor does not already know about (an external MariaDB, or no
MQTT broker at all).

This requires `services: - mysql:want` / `- mqtt:want` in config.yaml, and
nothing else: Supervisor's own API deliberately exempts every `/services/*`
path from the broader `hassio_api` permission it requires for almost
everything else (see `supervisor/api/middleware/security.py`'s `api_bypass`
pattern), so the `SUPERVISOR_TOKEN` every add-on already receives is
sufficient on its own.

Isolated in its own module, not folded into `hr_config.py`, because
`hr_config.py` is a pure module — no I/O beyond reading `os.environ` — and
this necessarily makes a network call.
"""

from __future__ import annotations

import json
import logging
import os
import urllib.error
import urllib.request
from dataclasses import dataclass, replace
from typing import Any

from hr_config import AppConfig

_LOGGER = logging.getLogger(__name__)

_SUPERVISOR_SERVICES_URL = "http://supervisor/services/{service}"
_REQUEST_TIMEOUT = 5


def _query_service(service: str) -> dict[str, Any] | None:
    """The raw `data` object Supervisor holds for a service, or None.

    None covers every reason this can fail to produce data: no
    SUPERVISOR_TOKEN (running outside an add-on, e.g. local development), no
    add-on currently providing the service, or any network or protocol error
    talking to Supervisor. All of these are routine — most installations have
    no such service at all — so none of them are raised as an error; callers
    fall back to the user's own configuration.
    """
    token = os.environ.get("SUPERVISOR_TOKEN")
    if not token:
        return None

    request = urllib.request.Request(
        _SUPERVISOR_SERVICES_URL.format(service=service),
        headers={"Authorization": f"Bearer {token}"},
    )
    try:
        # The URL above is built only from a module constant and this
        # function's own `service` argument, which every caller in this file
        # passes as a literal — never a user- or environment-controlled
        # value — so the arbitrary-scheme risk B310 warns about does not
        # apply here. Same reasoning as hr_mariadb.py's own B608 annotations.
        with urllib.request.urlopen(request, timeout=_REQUEST_TIMEOUT) as response:  # nosec B310
            body = json.loads(response.read())
    except (urllib.error.URLError, TimeoutError, ValueError) as err:
        # Includes HTTPError (urllib.error.URLError's subclass) for the
        # ordinary "nothing currently provides this service" case, which
        # Supervisor reports as a 400 with {"result": "error", ...}.
        _LOGGER.debug("No '%s' service discovered: %s", service, err)
        return None

    data = body.get("data")
    return data if isinstance(data, dict) else None


@dataclass(frozen=True)
class DiscoveredMariaDB:
    """A MariaDB connection Supervisor already knows about."""

    host: str
    port: int
    user: str | None
    password: str | None


def discover_mariadb() -> DiscoveredMariaDB | None:
    """The MariaDB service Supervisor has on offer, or None — see `_query_service`."""
    data = _query_service("mysql")
    if data is None or "host" not in data or "port" not in data:
        return None

    try:
        port = int(data["port"])
    except (TypeError, ValueError):
        # A malformed port is exactly as routine as a missing one — this
        # entire module's promise is that discovery never raises, only ever
        # falls back to the user's own configuration.
        _LOGGER.debug("Discovered mysql service has a non-numeric port: %r", data.get("port"))
        return None

    return DiscoveredMariaDB(
        host=data["host"],
        port=port,
        user=data.get("username") or None,
        password=data.get("password") or None,
    )


@dataclass(frozen=True)
class DiscoveredMqtt:
    """An MQTT broker Supervisor already knows about."""

    host: str
    port: int
    user: str | None
    password: str | None
    ssl: bool


def discover_mqtt() -> DiscoveredMqtt | None:
    """The MQTT broker Supervisor has on offer, or None — see `_query_service`.

    `ssl` is Supervisor's own flag for whether *this* host/port needs TLS —
    not a fixed assumption, since a provider can register a TLS-only
    listener here. Getting this wrong means hr_mqtt.py either connects in
    plain text to a TLS listener (handshake failure) or negotiates TLS
    against a plain listener (also a handshake failure), so it is read and
    passed through rather than ignored.
    """
    data = _query_service("mqtt")
    if data is None or "host" not in data or "port" not in data:
        return None

    try:
        port = int(data["port"])
    except (TypeError, ValueError):
        _LOGGER.debug("Discovered mqtt service has a non-numeric port: %r", data.get("port"))
        return None

    return DiscoveredMqtt(
        host=data["host"],
        port=port,
        user=data.get("username") or None,
        password=data.get("password") or None,
        ssl=bool(data.get("ssl", False)),
    )


def apply_discovered_mariadb(config: AppConfig, discovered: DiscoveredMariaDB | None) -> AppConfig:
    """Prefer a discovered MariaDB service over the user's own configuration.

    Discovery wins when both are present: a user running the common HAOS +
    official MariaDB add-on setup should not need to keep host/port/user/
    password in sync by hand when Supervisor already knows them. The
    database *name* is left untouched either way — the mysql service does
    not carry one, and which database within the server to use is this
    add-on's own concern, not something to discover.

    A no-op when `discovered` is None (nothing currently provides the
    service) or `config.db_type` is not "mariadb" (an explicit choice of
    SQLite is not overridden just because some other add-on happens to offer
    MariaDB — db_type is what the user chose during onboarding, not
    something discovery should second-guess).
    """
    if discovered is None or config.db_type != "mariadb" or config.database is None:
        return config

    database = replace(
        config.database,
        host=discovered.host,
        port=discovered.port,
        user=discovered.user or config.database.user,
        password=discovered.password or config.database.password,
    )
    return replace(config, database=database)
