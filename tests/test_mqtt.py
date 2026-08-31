"""Tests for hr_mqtt — publishing the "needs review" entity over MQTT.

The broker is faked throughout: whether a real MQTT broker accepts these
messages the way Home Assistant expects is settled by
test_mqtt_integration.py, which talks to a real one.
"""

from __future__ import annotations

import json
import threading
import time
from typing import Any

import pytest

from hr_discovery import DiscoveredMqtt
from hr_mqtt import (
    _CONNECT_TIMEOUT,
    DEFAULT_NODE_ID,
    MqttPublisher,
    _availability_topic,
    _discovery_payload,
    _discovery_topic,
    _state_topic,
    run_publisher_loop,
)


class FakeMqttClient:
    """A scripted stand-in for paho.mqtt.client.Client, recording calls."""

    def __init__(self) -> None:
        self.will: tuple[str, str, bool] | None = None
        self.credentials: tuple[str, str | None] | None = None
        self.tls_enabled = False
        self.connected_to: tuple[str, int, int] | None = None
        self.loop_started = False
        self.loop_stopped = False
        self.disconnected = False
        self.published: list[tuple[str, str, bool]] = []

    def will_set(self, topic: str, payload: str, retain: bool = False) -> None:
        self.will = (topic, payload, retain)

    def username_pw_set(self, username: str, password: str | None = None) -> None:
        self.credentials = (username, password)

    def tls_set(self) -> None:
        self.tls_enabled = True

    def connect(self, host: str, port: int, keepalive: int = 60) -> None:
        self.connected_to = (host, port, keepalive)

    def loop_start(self) -> None:
        self.loop_started = True

    def loop_stop(self) -> None:
        self.loop_stopped = True

    def disconnect(self) -> None:
        self.disconnected = True

    def publish(self, topic: str, payload: str, retain: bool = False) -> None:
        self.published.append((topic, payload, retain))


_DISCOVERED = DiscoveredMqtt(host="core-mosquitto", port=1883, user=None, password=None, ssl=False)


class TestDiscoveryPayload:
    def test_matches_the_state_and_availability_topics_it_declares(self) -> None:
        payload = _discovery_payload("0.1.0")
        assert payload["state_topic"] == _state_topic()
        assert payload["availability_topic"] == _availability_topic()

    def test_is_grouped_under_one_device(self) -> None:
        payload = _discovery_payload("0.1.0")
        device = payload["device"]
        assert isinstance(device, dict)
        assert device["sw_version"] == "0.1.0"
        assert device["identifiers"] == ["history_repair"]

    def test_is_json_serialisable(self) -> None:
        # It gets published as a JSON string body; anything not
        # JSON-serialisable here would fail silently deep inside publish().
        json.dumps(_discovery_payload("0.1.0"))

    def test_matches_the_full_expected_payload_exactly(self) -> None:
        # The individual field-level tests above only check a handful of keys
        # — a corrupted key name or value elsewhere in this dict (a stray
        # case change, a duplicated/garbled string) would still satisfy them.
        # Home Assistant's MQTT discovery schema is exact-match on these key
        # names, so nothing here is free to drift.
        assert _discovery_payload("0.1.0", "history_repair") == {
            "name": "Needs review",
            "unique_id": "history_repair_needs_review",
            "state_topic": "history_repair/needs_review/state",
            "payload_on": "ON",
            "payload_off": "OFF",
            "device_class": "problem",
            "availability_topic": "history_repair/availability",
            "payload_available": "online",
            "payload_not_available": "offline",
            "device": {
                "identifiers": ["history_repair"],
                "name": "History Repair",
                "manufacturer": "whatsinabyte",
                "model": "History Repair",
                "sw_version": "0.1.0",
            },
        }

    def test_a_custom_node_id_threads_through_every_topic_and_identifier(self) -> None:
        payload = _discovery_payload("2.0.0", "hr_test_worker3")
        assert payload["unique_id"] == "hr_test_worker3_needs_review"
        assert payload["state_topic"] == "hr_test_worker3/needs_review/state"
        assert payload["availability_topic"] == "hr_test_worker3/availability"
        device = payload["device"]
        assert isinstance(device, dict)
        assert device["identifiers"] == ["hr_test_worker3"]


class TestMqttPublisher:
    def test_connect_sets_the_will_before_connecting(self) -> None:
        client = FakeMqttClient()
        MqttPublisher(client, _DISCOVERED, "0.1.0").connect()
        assert client.will == (_availability_topic(), "offline", True)

    def test_connect_uses_the_discovered_host_and_port(self) -> None:
        client = FakeMqttClient()
        MqttPublisher(client, _DISCOVERED, "0.1.0").connect()
        assert client.connected_to is not None
        assert client.connected_to[0] == "core-mosquitto"
        assert client.connected_to[1] == 1883

    def test_credentials_are_set_when_present(self) -> None:
        client = FakeMqttClient()
        discovered = DiscoveredMqtt(
            host="core-mosquitto", port=1883, user="addon", password="pw", ssl=False
        )
        MqttPublisher(client, discovered, "0.1.0").connect()
        assert client.credentials == ("addon", "pw")

    def test_no_credentials_set_when_broker_has_none(self) -> None:
        client = FakeMqttClient()
        MqttPublisher(client, _DISCOVERED, "0.1.0").connect()
        assert client.credentials is None

    def test_tls_is_enabled_when_the_broker_requires_it(self) -> None:
        client = FakeMqttClient()
        discovered = DiscoveredMqtt(
            host="core-mosquitto", port=8883, user=None, password=None, ssl=True
        )
        MqttPublisher(client, discovered, "0.1.0").connect()
        assert client.tls_enabled is True

    def test_connect_publishes_discovery_config_and_online_availability(self) -> None:
        client = FakeMqttClient()
        MqttPublisher(client, _DISCOVERED, "0.1.0").connect()
        topics = {topic for topic, _, _ in client.published}
        assert _discovery_topic() in topics
        assert (_availability_topic(), "online", True) in client.published

    def test_the_published_discovery_config_matches_the_real_sw_version_and_node_id(
        self,
    ) -> None:
        client = FakeMqttClient()
        MqttPublisher(client, _DISCOVERED, "7.3.1", node_id="hr_test_x").connect()
        published_by_topic = {
            topic: (payload, retain) for topic, payload, retain in client.published
        }
        expected_payload = json.dumps(_discovery_payload("7.3.1", "hr_test_x"))
        assert published_by_topic[_discovery_topic("hr_test_x")] == (expected_payload, True)

    def test_the_default_keepalive_is_used_when_not_overridden(self) -> None:
        client = FakeMqttClient()
        MqttPublisher(client, _DISCOVERED, "0.1.0").connect()
        assert client.connected_to == (_DISCOVERED.host, _DISCOVERED.port, 60)

    def test_a_custom_keepalive_reaches_the_real_connect_call(self) -> None:
        client = FakeMqttClient()
        MqttPublisher(client, _DISCOVERED, "0.1.0", keepalive=15).connect()
        assert client.connected_to == (_DISCOVERED.host, _DISCOVERED.port, 15)

    def test_published_state_reflects_needs_review(self) -> None:
        client = FakeMqttClient()
        publisher = MqttPublisher(client, _DISCOVERED, "0.1.0")
        publisher.connect()
        publisher.publish_needs_review(True)
        assert (_state_topic(), "ON", True) in client.published
        publisher.publish_needs_review(False)
        assert (_state_topic(), "OFF", True) in client.published

    def test_disconnect_publishes_offline_and_stops_the_loop(self) -> None:
        client = FakeMqttClient()
        publisher = MqttPublisher(client, _DISCOVERED, "0.1.0")
        publisher.connect()
        publisher.disconnect()
        assert (_availability_topic(), "offline", True) in client.published
        assert client.loop_stopped is True
        assert client.disconnected is True


class _RecordingEvent(threading.Event):
    """A stop_event that remembers what timeout run_publisher_loop passed to
    wait() — the only way to tell the real poll_interval apart from a
    corrupted call that waits forever instead, since both still return once
    the event is externally set."""

    def __init__(self) -> None:
        super().__init__()
        self.wait_calls: list[float | None] = []

    def wait(self, timeout: float | None = None) -> bool:
        self.wait_calls.append(timeout)
        return super().wait(timeout)


class TestRunPublisherLoop:
    def test_publishes_immediately_then_stops_cleanly(self, monkeypatch: Any) -> None:
        client = FakeMqttClient()
        monkeypatch.setattr("hr_mqtt.mqtt.Client", lambda callback_api_version: client)
        stop_event = threading.Event()

        thread = threading.Thread(
            target=run_publisher_loop,
            args=(_DISCOVERED, "0.1.0", lambda: True, stop_event),
            kwargs={"poll_interval": 1000.0},
        )
        thread.start()
        # The first publish happens before the loop's first wait, so a short
        # real wait here is enough without depending on poll_interval at all.
        for _ in range(50):
            if any(topic == _state_topic() for topic, _, _ in client.published):
                break
            time.sleep(0.02)
        stop_event.set()
        thread.join(timeout=5)

        assert not thread.is_alive()
        assert (_state_topic(), "ON", True) in client.published
        assert client.disconnected is True

    def test_a_connection_failure_returns_without_raising(self, monkeypatch: Any) -> None:
        class _RefusingClient(FakeMqttClient):
            def connect(self, host: str, port: int, keepalive: int = 60) -> None:
                raise OSError("connection refused")

        monkeypatch.setattr("hr_mqtt.mqtt.Client", lambda callback_api_version: _RefusingClient())
        stop_event = threading.Event()
        stop_event.set()  # loop would exit immediately anyway; connect fails first
        run_publisher_loop(_DISCOVERED, "0.1.0", lambda: True, stop_event)

    def test_a_connection_failure_is_logged_with_the_real_error(
        self, monkeypatch: Any, caplog: pytest.LogCaptureFixture
    ) -> None:
        class _RefusingClient(FakeMqttClient):
            def connect(self, host: str, port: int, keepalive: int = 60) -> None:
                raise OSError("connection refused")

        monkeypatch.setattr("hr_mqtt.mqtt.Client", lambda callback_api_version: _RefusingClient())
        stop_event = threading.Event()
        stop_event.set()
        with caplog.at_level("INFO"):
            run_publisher_loop(_DISCOVERED, "0.1.0", lambda: True, stop_event)
        message = caplog.records[-1].getMessage()
        assert message.startswith("Could not connect to the discovered MQTT broker: ")
        assert "%s" not in message
        assert not message.endswith(": None")

    def test_connect_timeout_is_set_on_the_real_client(self, monkeypatch: Any) -> None:
        client = FakeMqttClient()
        monkeypatch.setattr("hr_mqtt.mqtt.Client", lambda callback_api_version: client)
        stop_event = threading.Event()
        stop_event.set()
        run_publisher_loop(_DISCOVERED, "0.1.0", lambda: True, stop_event)
        assert client.connect_timeout == _CONNECT_TIMEOUT

    def test_the_client_is_built_with_the_v2_callback_api(self, monkeypatch: Any) -> None:
        import paho.mqtt.client as mqtt

        captured: dict[str, Any] = {}

        def _fake_client(callback_api_version: Any) -> FakeMqttClient:
            captured["callback_api_version"] = callback_api_version
            return FakeMqttClient()

        monkeypatch.setattr("hr_mqtt.mqtt.Client", _fake_client)
        stop_event = threading.Event()
        stop_event.set()
        run_publisher_loop(_DISCOVERED, "0.1.0", lambda: True, stop_event)
        assert captured["callback_api_version"] == mqtt.CallbackAPIVersion.VERSION2

    def test_a_custom_node_id_reaches_the_publisher(self, monkeypatch: Any) -> None:
        client = FakeMqttClient()
        monkeypatch.setattr("hr_mqtt.mqtt.Client", lambda callback_api_version: client)
        stop_event = threading.Event()
        stop_event.set()
        run_publisher_loop(_DISCOVERED, "0.1.0", lambda: True, stop_event, node_id="hr_test_y")
        topics = {topic for topic, _, _ in client.published}
        assert _discovery_topic("hr_test_y") in topics
        assert _discovery_topic(DEFAULT_NODE_ID) not in topics

    def test_the_sw_version_reaches_the_published_discovery_config(self, monkeypatch: Any) -> None:
        client = FakeMqttClient()
        monkeypatch.setattr("hr_mqtt.mqtt.Client", lambda callback_api_version: client)
        stop_event = threading.Event()
        stop_event.set()
        run_publisher_loop(_DISCOVERED, "9.9.9", lambda: True, stop_event)
        published_by_topic = {topic: payload for topic, payload, _retain in client.published}
        assert json.loads(published_by_topic[_discovery_topic()])["device"]["sw_version"] == (
            "9.9.9"
        )

    def test_a_successful_connection_logs_the_broker_address(
        self, monkeypatch: Any, caplog: pytest.LogCaptureFixture
    ) -> None:
        client = FakeMqttClient()
        monkeypatch.setattr("hr_mqtt.mqtt.Client", lambda callback_api_version: client)
        stop_event = threading.Event()
        stop_event.set()
        with caplog.at_level("INFO"):
            run_publisher_loop(_DISCOVERED, "0.1.0", lambda: True, stop_event)
        message = caplog.records[-1].getMessage()
        assert message == (
            f"Publishing the 'needs review' entity to MQTT at {_DISCOVERED.host}:{_DISCOVERED.port}"
        )

    def test_a_publish_failure_is_logged_with_the_real_exception(
        self, monkeypatch: Any, caplog: pytest.LogCaptureFixture
    ) -> None:
        client = FakeMqttClient()
        monkeypatch.setattr("hr_mqtt.mqtt.Client", lambda callback_api_version: client)
        stop_event = threading.Event()
        stop_event.set()

        def _raise() -> bool:
            raise RuntimeError("boom")

        with caplog.at_level("ERROR"):
            run_publisher_loop(_DISCOVERED, "0.1.0", _raise, stop_event)
        messages = [r.getMessage() for r in caplog.records]
        assert "Could not publish to MQTT" in messages

    def test_the_configured_poll_interval_is_passed_to_the_wait_call(
        self, monkeypatch: Any
    ) -> None:
        client = FakeMqttClient()
        monkeypatch.setattr("hr_mqtt.mqtt.Client", lambda callback_api_version: client)
        stop_event = _RecordingEvent()

        thread = threading.Thread(
            target=run_publisher_loop,
            args=(_DISCOVERED, "0.1.0", lambda: True, stop_event),
            kwargs={"poll_interval": 0.05},
        )
        thread.start()
        for _ in range(50):
            if stop_event.wait_calls:
                break
            time.sleep(0.02)
        stop_event.set()
        thread.join(timeout=5)

        assert not thread.is_alive()
        assert 0.05 in stop_event.wait_calls
