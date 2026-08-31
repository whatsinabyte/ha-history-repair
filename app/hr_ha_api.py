"""Talk to Home Assistant's WebSocket API.

Correcting the statistics tables with SQL works, but it writes behind Home
Assistant's back: it keeps statistics in memory, so a correction is not
reflected on a statistics card or the energy dashboard until it is restarted.
The `recorder/import_statistics` command asks Home Assistant to write the
values itself, which keeps its cache honest and needs no restart. The design
document calls this the primary path, with SQL as the fallback.

The command's schema is versioned, and the deprecation is not cosmetic:

    metadata.mean_type   optional today, REQUIRED from HA Core 2026.11
    metadata.unit_class  optional today, REQUIRED from HA Core 2026.11
    metadata.has_mean    the field they replace

Both new fields are sent whenever they are known, which satisfies current and
future versions at once, and `has_mean` is sent alongside for versions that
predate `mean_type`. This is the versioned handling the design document asks
for in 9.16.

Everything here fails soft. A statistics correction that cannot reach Home
Assistant is not an error: the caller falls back to SQL and tells the user a
restart may be needed.
"""

from __future__ import annotations

import json
import logging
import os
import ssl
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

_LOGGER = logging.getLogger(__name__)

# Inside an add-on, Supervisor proxies the Core API and the token comes from
# the environment. `homeassistant_api: true` in config.yaml is what grants it.
SUPERVISOR_WS_URL = "ws://supervisor/core/websocket"

CONNECT_TIMEOUT = 15
COMMAND_TIMEOUT = 30

# mean_type values, mirroring homeassistant.components.recorder.models.
MEAN_TYPE_NONE = 0
MEAN_TYPE_ARITHMETIC = 1
MEAN_TYPE_CIRCULAR = 2


class HomeAssistantApiError(Exception):
    """The WebSocket path did not work; the caller should fall back to SQL."""


@dataclass(frozen=True)
class ApiConfig:
    url: str
    token: str

    @classmethod
    def from_env(cls) -> ApiConfig | None:
        """Supervisor's token and URL, or an explicit override for development.

        Returns None when neither is available, which simply means the
        WebSocket path is unavailable and SQL will be used.
        """
        override_url = os.environ.get("HR_HA_URL")
        override_token = os.environ.get("HR_HA_TOKEN")
        if override_url and override_token:
            return cls(url=override_url, token=override_token)

        supervisor_token = os.environ.get("SUPERVISOR_TOKEN")
        if supervisor_token:
            return cls(url=SUPERVISOR_WS_URL, token=supervisor_token)
        return None


def _to_iso(ts: float) -> str:
    """A bucket start as the ISO timestamp the API expects.

    Home Assistant parses this with cv.datetime and requires the start to be on
    a bucket boundary, so the value handed in must already be aligned.
    """
    return datetime.fromtimestamp(ts, tz=timezone.utc).isoformat()


class HomeAssistantClient:
    """A short-lived WebSocket connection for one batch of statistics.

    Deliberately not held open. A correction is a rare, user-initiated event,
    and a long-lived socket would need reconnection handling for no benefit.
    """

    def __init__(self, config: ApiConfig) -> None:
        self._config = config
        self._message_id = 0

    def _connect(self) -> Any:
        try:
            import websocket
        except ImportError as err:  # pragma: no cover - dependency is declared
            raise HomeAssistantApiError(
                "websocket-client is not installed; using the SQL path"
            ) from err

        try:
            options: dict[str, Any] = {"timeout": CONNECT_TIMEOUT}
            if self._config.url.startswith("wss://"):
                options["sslopt"] = {"cert_reqs": ssl.CERT_REQUIRED}
            connection = websocket.create_connection(self._config.url, **options)
        except Exception as err:  # any failure here means: fall back to SQL
            raise HomeAssistantApiError(
                f"Could not reach Home Assistant at {self._config.url}: {err}"
            ) from err

        try:
            self._authenticate(connection)
        except Exception:
            connection.close()
            raise
        return connection

    def _authenticate(self, connection: Any) -> None:
        greeting = json.loads(connection.recv())
        if greeting.get("type") != "auth_required":
            raise HomeAssistantApiError(
                f"Unexpected greeting from Home Assistant: {greeting.get('type')!r}"
            )
        connection.send(json.dumps({"type": "auth", "access_token": self._config.token}))
        result = json.loads(connection.recv())
        if result.get("type") != "auth_ok":
            raise HomeAssistantApiError(
                "Home Assistant rejected the access token. In an add-on this "
                "means homeassistant_api is not granted."
            )

    def _send(self, connection: Any, payload: dict[str, Any]) -> dict[str, Any]:
        self._message_id += 1
        payload["id"] = self._message_id
        try:
            connection.send(json.dumps(payload))

            # Home Assistant may interleave events; read until the reply to
            # this id.
            deadline = time.time() + COMMAND_TIMEOUT
            while time.time() < deadline:
                message: dict[str, Any] = json.loads(connection.recv())
                if message.get("id") == payload["id"]:
                    return message
        except HomeAssistantApiError:
            raise
        except Exception as err:
            # The socket's own timeout (set once, for the connection's whole
            # lifetime, by _connect's create_connection call) applies to
            # every recv() here too — not just the initial handshake — so a
            # single slow reply raises the websocket library's own
            # WebSocketTimeoutException here well before the deadline above
            # is reached. That, and any other transport-level failure mid
            # command, must become a HomeAssistantApiError like every other
            # failure mode in this class, or it escapes the "fails soft"
            # contract this whole module promises — surfacing as an
            # unhandled error for a correction that, by this point, the
            # database transaction already committed successfully.
            raise HomeAssistantApiError(f"Lost the connection to Home Assistant: {err}") from err
        raise HomeAssistantApiError("Home Assistant did not answer in time")

    def get_version(self) -> str | None:
        """The running Home Assistant version, or None if it cannot be read."""
        try:
            connection = self._connect()
        except HomeAssistantApiError:
            return None
        try:
            result = self._send(connection, {"type": "get_config"})
            if result.get("success"):
                version: str | None = result.get("result", {}).get("version")
                return version
        except HomeAssistantApiError:
            return None
        finally:
            connection.close()
        return None

    def import_statistics(
        self,
        *,
        statistic_id: str,
        unit_of_measurement: str | None,
        mean_type: int,
        has_sum: bool,
        unit_class: str | None,
        stats: list[dict[str, Any]],
    ) -> None:
        """Ask Home Assistant to write these statistics rows itself.

        `stats` entries use bucket start timestamps as epoch floats under the
        key "start_ts"; they are converted to the ISO form the API wants here,
        so callers never have to think about the wire format.

        Raises HomeAssistantApiError on any failure, which the caller treats as
        "use the SQL path instead" rather than as a lost correction.
        """
        if not stats:
            return

        metadata: dict[str, Any] = {
            "has_sum": has_sum,
            "name": None,
            # Must equal the recorder domain for a statistic_id that is an
            # entity id; anything else is rejected as an external statistic.
            "source": "recorder",
            "statistic_id": statistic_id,
            "unit_of_measurement": unit_of_measurement,
            # Deprecated but still accepted, and the only field understood by
            # versions older than mean_type.
            "has_mean": mean_type == MEAN_TYPE_ARITHMETIC,
            # Required from HA Core 2026.11. Sending both satisfies every
            # version in between.
            "mean_type": mean_type,
            "unit_class": unit_class,
        }

        payload_stats: list[dict[str, Any]] = []
        for row in stats:
            entry: dict[str, Any] = {"start": _to_iso(float(row["start_ts"]))}
            for key in ("mean", "min", "max", "state", "sum"):
                if row.get(key) is not None:
                    entry[key] = float(row[key])
            payload_stats.append(entry)

        connection = self._connect()
        try:
            result = self._send(
                connection,
                {
                    "type": "recorder/import_statistics",
                    "metadata": metadata,
                    "stats": payload_stats,
                },
            )
        finally:
            connection.close()

        if not result.get("success"):
            error = result.get("error", {})
            raise HomeAssistantApiError(
                f"Home Assistant refused the statistics import: "
                f"{error.get('code')} {error.get('message')}"
            )


def client_from_env() -> HomeAssistantClient | None:
    """A client if Home Assistant is reachable, otherwise None."""
    config = ApiConfig.from_env()
    if config is None:
        # Not a debug-only detail: every correction's statistics cache
        # refresh depends on this, and its absence means every one of them
        # silently falls back to "restart Home Assistant to see the change"
        # — worth seeing at the add-on's default log level, not only once
        # a user has already turned on debug logging to investigate why.
        _LOGGER.info("No Home Assistant API credentials; statistics will be written with SQL")
        return None
    return HomeAssistantClient(config)
