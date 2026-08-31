"""Integration tests: hr_mqtt against a real MQTT broker.

tests/test_mqtt.py proves the publisher calls the right client methods with
the right arguments; this proves those calls actually produce the messages
Home Assistant's MQTT integration expects on the wire — retained messages
really are retained, the discovery payload really is valid JSON a subscriber
can parse, and the will really does fire on an unclean disconnect.

Skipped unless HR_MQTT_TEST_HOST points at a disposable broker:

    ./dev/mosquitto.sh start
    HR_MQTT_TEST_HOST=127.0.0.1 HR_MQTT_TEST_PORT=1893 \\
      .venv-check/bin/python -m pytest tests/test_mqtt_integration.py

Never point these at a real broker serving a real Home Assistant instance:
this add-on's own discovery topic is published (and left retained) on it.
"""

from __future__ import annotations

import json
import os
import threading
import time
from typing import Any

import paho.mqtt.client as mqtt
import pytest

from hr_discovery import DiscoveredMqtt
from hr_mqtt import (
    MqttPublisher,
    _availability_topic,
    _discovery_topic,
    _state_topic,
    run_publisher_loop,
)

_HOST = os.environ.get("HR_MQTT_TEST_HOST")
_PORT = int(os.environ.get("HR_MQTT_TEST_PORT", "1893"))

# Every topic this suite touches is namespaced to this process. The broker is
# shared — by xdist workers within one run, and by two runs started at once —
# and every message here is *retained*, so without a unique namespace one
# test's "online" overwrites another's "offline" on the same topic and the
# will test fails intermittently. Found by running the suite repeatedly rather
# than by reasoning about it: two failures in eight runs.
_NODE_ID = f"hr_test_{os.environ.get('PYTEST_XDIST_WORKER', 'solo')}_{os.getpid()}"


pytestmark = pytest.mark.skipif(
    not _HOST, reason="HR_MQTT_TEST_HOST is not set; see this module's docstring"
)


def _discovered() -> DiscoveredMqtt:
    return DiscoveredMqtt(host=_HOST or "", port=_PORT, user=None, password=None, ssl=False)


class _Subscriber:
    """A second, independent client — collects whatever the publisher sends,
    the way Home Assistant's own MQTT integration would."""

    def __init__(self, topics: list[str]) -> None:
        self.messages: dict[str, str] = {}
        self._client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2)
        self._client.on_message = self._on_message
        self._client.connect(_HOST or "", _PORT, keepalive=10)
        for topic in topics:
            self._client.subscribe(topic)
        self._client.loop_start()

    def _on_message(self, client: Any, userdata: Any, message: Any) -> None:
        self.messages[message.topic] = message.payload.decode()

    def wait_for(self, topic: str, timeout: float = 5.0) -> str | None:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if topic in self.messages:
                return self.messages[topic]
            time.sleep(0.05)
        return None

    def wait_for_value(self, topic: str, expected: str, timeout: float = 5.0) -> str | None:
        """Like wait_for, but for a specific value — for a topic that may
        already hold a previous message, waiting for *any* value would
        return stale data instead of actually waiting for the update."""
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if self.messages.get(topic) == expected:
                return expected
            time.sleep(0.05)
        return self.messages.get(topic)

    def close(self) -> None:
        self._client.loop_stop()
        self._client.disconnect()


class TestMqttPublisherAgainstARealBroker:
    def test_connect_publishes_a_parseable_discovery_config(self) -> None:
        subscriber = _Subscriber([_discovery_topic(_NODE_ID)])
        try:
            client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2)
            publisher = MqttPublisher(client, _discovered(), "0.1.0", node_id=_NODE_ID)
            publisher.connect()
            try:
                raw = subscriber.wait_for(_discovery_topic(_NODE_ID))
                assert raw is not None
                payload = json.loads(raw)
                assert payload["state_topic"] == _state_topic(_NODE_ID)
            finally:
                publisher.disconnect()
        finally:
            subscriber.close()

    def test_state_is_retained_for_a_late_subscriber(self) -> None:
        # Publish first, subscribe after — a retained message must still
        # arrive, which is exactly what lets Home Assistant show a value
        # immediately on restart without waiting for this add-on's next poll.
        client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2)
        publisher = MqttPublisher(client, _discovered(), "0.1.0", node_id=_NODE_ID)
        publisher.connect()
        try:
            publisher.publish_needs_review(True)
            time.sleep(0.2)  # let the broker apply the retained message

            late_subscriber = _Subscriber([_state_topic(_NODE_ID)])
            try:
                assert late_subscriber.wait_for(_state_topic(_NODE_ID)) == "ON"
            finally:
                late_subscriber.close()
        finally:
            publisher.disconnect()

    def test_unclean_disconnect_triggers_the_will(self) -> None:
        # A short keepalive here only, so the broker notices the severed
        # connection within this test's own timeout — production code uses
        # the class default (60s); this is about proving the will mechanism
        # works on a real broker at all, not the specific delay before it.
        subscriber = _Subscriber([_availability_topic(_NODE_ID)])
        try:
            client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2)
            publisher = MqttPublisher(client, _discovered(), "0.1.0", keepalive=2, node_id=_NODE_ID)
            publisher.connect()
            assert subscriber.wait_for_value(_availability_topic(_NODE_ID), "online") == "online"

            # Simulating a crash: no publisher.disconnect(), just severing
            # the socket, so the broker is the one detecting the loss and
            # firing the will — not this process asking nicely.
            client.loop_stop()
            sock = client.socket()
            assert sock is not None
            sock.close()

            # 1.5x keepalive is the MQTT spec's own grace period before a
            # broker considers a silent peer gone.
            assert (
                subscriber.wait_for_value(_availability_topic(_NODE_ID), "offline", timeout=6.0)
                == "offline"
            )
        finally:
            subscriber.close()


class TestRunPublisherLoopAgainstARealBroker:
    def test_end_to_end_through_the_real_entry_point(self) -> None:
        subscriber = _Subscriber([_state_topic(_NODE_ID), _availability_topic(_NODE_ID)])
        stop_event = threading.Event()
        thread = threading.Thread(
            target=run_publisher_loop,
            args=(_discovered(), "0.1.0", lambda: True, stop_event),
            kwargs={"poll_interval": 1000.0, "node_id": _NODE_ID},
        )
        thread.start()
        try:
            assert subscriber.wait_for(_availability_topic(_NODE_ID)) == "online"
            assert subscriber.wait_for(_state_topic(_NODE_ID)) == "ON"
        finally:
            stop_event.set()
            thread.join(timeout=5)
            subscriber.close()
        assert not thread.is_alive()
