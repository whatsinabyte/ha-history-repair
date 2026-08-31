"""Publishing an "orphaned corrections" entity to Home Assistant via MQTT.

find_orphaned_corrections() (design document 9.11) already detects when a
Home Assistant backup restore has silently invalidated an active correction —
until now, only visible by opening the add-on's own Corrections page. When an
MQTT broker is discovered (hr_discovery.discover_mqtt), this publishes that
same fact as a normal `binary_sensor` entity, using Home Assistant's MQTT
discovery protocol, so it can sit on a dashboard or trigger a notification
automation without opening this add-on's UI at all.

Best-effort throughout: MQTT is advisory here, not part of the correction
transaction or its audit trail. Every failure mode (no broker discovered, the
broker unreachable, publishing failing) degrades to "no MQTT entity exists",
never to blocking startup or a correction.
"""

from __future__ import annotations

import json
import logging
import threading
from collections.abc import Callable

import paho.mqtt.client as mqtt

from hr_discovery import DiscoveredMqtt

_LOGGER = logging.getLogger(__name__)

# Home Assistant's own default; matches what a user's MQTT integration
# expects without further configuration on their part.
_DISCOVERY_PREFIX = "homeassistant"
DEFAULT_NODE_ID = "history_repair"
_OBJECT_ID = "needs_review"

# How often the state is republished. Orphaned-correction status only
# actually changes after a backup restore, a correction, or a dismissal —
# none of which are frequent — so this is about keeping the entity fresh
# and available, not reacting quickly to a change.
DEFAULT_POLL_INTERVAL = 300.0

_CONNECT_TIMEOUT = 10


# Every topic and identifier is derived from a node id rather than a module
# constant, so two publishers can coexist on one broker without overwriting
# each other's retained messages. In production that is one Home Assistant
# install per broker and the default is always used; it matters for two real
# cases — two Home Assistant installs sharing a broker, which would otherwise
# collide on identical retained topics, and the integration suite, whose
# parallel workers share one broker and would flake on each other's
# availability messages without it.
def _discovery_topic(node_id: str = DEFAULT_NODE_ID) -> str:
    return f"{_DISCOVERY_PREFIX}/binary_sensor/{node_id}/{_OBJECT_ID}/config"


def _state_topic(node_id: str = DEFAULT_NODE_ID) -> str:
    return f"{node_id}/{_OBJECT_ID}/state"


def _availability_topic(node_id: str = DEFAULT_NODE_ID) -> str:
    return f"{node_id}/availability"


def _discovery_payload(sw_version: str, node_id: str = DEFAULT_NODE_ID) -> dict[str, object]:
    return {
        "name": "Needs review",
        "unique_id": f"{node_id}_{_OBJECT_ID}",
        "state_topic": _state_topic(node_id),
        "payload_on": "ON",
        "payload_off": "OFF",
        "device_class": "problem",
        "availability_topic": _availability_topic(node_id),
        "payload_available": "online",
        "payload_not_available": "offline",
        "device": {
            "identifiers": [node_id],
            "name": "History Repair",
            "manufacturer": "whatsinabyte",
            "model": "History Repair",
            "sw_version": sw_version,
        },
    }


class MqttPublisher:
    """A connected MQTT client, publishing this add-on's one entity.

    Takes an already-constructed `mqtt.Client` rather than building one
    itself, so a test can substitute a fake transport without patching
    paho's internals — the same shape as `HomeAssistantClient` in
    hr_ha_api.py taking a WebSocket connection function.
    """

    def __init__(
        self,
        client: mqtt.Client,
        discovered: DiscoveredMqtt,
        sw_version: str,
        keepalive: int = 60,
        node_id: str = DEFAULT_NODE_ID,
    ) -> None:
        self._client = client
        self._discovered = discovered
        self._sw_version = sw_version
        self._keepalive = keepalive
        self._node_id = node_id

    def connect(self) -> None:
        self._client.will_set(_availability_topic(self._node_id), "offline", retain=True)
        if self._discovered.user:
            self._client.username_pw_set(self._discovered.user, self._discovered.password)
        if self._discovered.ssl:
            self._client.tls_set()
        self._client.connect(self._discovered.host, self._discovered.port, self._keepalive)
        self._client.loop_start()
        self._client.publish(
            _discovery_topic(self._node_id),
            json.dumps(_discovery_payload(self._sw_version, self._node_id)),
            retain=True,
        )
        self._client.publish(_availability_topic(self._node_id), "online", retain=True)

    def publish_needs_review(self, needs_review: bool) -> None:
        self._client.publish(
            _state_topic(self._node_id), "ON" if needs_review else "OFF", retain=True
        )

    def disconnect(self) -> None:
        self._client.publish(_availability_topic(self._node_id), "offline", retain=True)
        self._client.loop_stop()
        self._client.disconnect()


def run_publisher_loop(
    discovered: DiscoveredMqtt,
    sw_version: str,
    get_needs_review: Callable[[], bool],
    stop_event: threading.Event,
    poll_interval: float = DEFAULT_POLL_INTERVAL,
    node_id: str = DEFAULT_NODE_ID,
) -> None:
    """Connect, publish, and keep republishing until `stop_event` is set.

    Intended as the target of a daemon thread, started once at add-on
    startup when a broker was discovered. Any failure to connect at all
    (wrong credentials, broker unreachable) is logged and this simply
    returns — the caller does not need its own try/except, and the rest of
    the add-on is unaffected either way.
    """
    client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2)
    try:
        client.connect_timeout = _CONNECT_TIMEOUT
        publisher = MqttPublisher(client, discovered, sw_version, node_id=node_id)
        publisher.connect()
    except (OSError, ValueError) as err:
        _LOGGER.info("Could not connect to the discovered MQTT broker: %s", err)
        return

    _LOGGER.info(
        "Publishing the 'needs review' entity to MQTT at %s:%d",
        discovered.host,
        discovered.port,
    )
    try:
        while True:
            try:
                publisher.publish_needs_review(get_needs_review())
            except Exception:
                _LOGGER.exception("Could not publish to MQTT")
            if stop_event.wait(poll_interval):
                break
    finally:
        publisher.disconnect()
