"""Tests for hr_ha_api — the WebSocket path to Home Assistant.

The wire format matters more than it looks. Two metadata fields are optional
today and required from HA Core 2026.11, so a message that works now can stop
working on an update; these tests pin that both are always sent.

The transport itself is faked. Whether a real Home Assistant accepts these
messages is settled by test_ha_api_live.py, which talks to a running one.
"""

from __future__ import annotations

import json
import ssl
from typing import Any

import pytest
import websocket as _websocket_module

from hr_ha_api import (
    MEAN_TYPE_ARITHMETIC,
    MEAN_TYPE_NONE,
    ApiConfig,
    HomeAssistantApiError,
    HomeAssistantClient,
    client_from_env,
)


class FakeSocket:
    """A scripted WebSocket, recording what the client sent."""

    def __init__(self, replies: list[dict[str, Any]] | None = None) -> None:
        self.sent: list[dict[str, Any]] = []
        self.closed = False
        self._queue: list[dict[str, Any]] = [{"type": "auth_required"}]
        self._queue.extend(replies or [{"type": "auth_ok"}])

    def recv(self) -> str:
        return json.dumps(self._queue.pop(0))

    def send(self, raw: str) -> None:
        self.sent.append(json.loads(raw))

    def close(self) -> None:
        self.closed = True


def _client(monkeypatch: Any, socket: FakeSocket) -> HomeAssistantClient:
    client = HomeAssistantClient(ApiConfig(url="ws://test/api/websocket", token="t"))
    monkeypatch.setattr(client, "_connect", lambda: (client._authenticate(socket), socket)[1])
    return client


class TestApiConfig:
    def test_an_explicit_override_wins(self, monkeypatch: Any) -> None:
        monkeypatch.setenv("HR_HA_URL", "ws://dev:8123/api/websocket")
        monkeypatch.setenv("HR_HA_TOKEN", "dev-token")
        monkeypatch.setenv("SUPERVISOR_TOKEN", "supervisor-token")
        config = ApiConfig.from_env()
        assert config is not None
        assert config.token == "dev-token"

    def test_supervisor_credentials_are_used_inside_an_addon(self, monkeypatch: Any) -> None:
        monkeypatch.delenv("HR_HA_URL", raising=False)
        monkeypatch.delenv("HR_HA_TOKEN", raising=False)
        monkeypatch.setenv("SUPERVISOR_TOKEN", "supervisor-token")
        config = ApiConfig.from_env()
        assert config is not None
        assert config.url.startswith("ws://supervisor/")

    def test_no_credentials_means_no_client(self, monkeypatch: Any) -> None:
        # Running outside an add-on. The SQL path still works, so this is a
        # normal condition rather than an error.
        for name in ("HR_HA_URL", "HR_HA_TOKEN", "SUPERVISOR_TOKEN"):
            monkeypatch.delenv(name, raising=False)
        assert ApiConfig.from_env() is None
        assert client_from_env() is None


class TestImportStatisticsMessage:
    def _send_one(self, monkeypatch: Any, **overrides: Any) -> dict[str, Any]:
        socket = FakeSocket([{"type": "auth_ok"}, {"id": 1, "success": True}])
        client = _client(monkeypatch, socket)
        payload: dict[str, Any] = {
            "statistic_id": "sensor.living_room_temperature",
            "unit_of_measurement": "°C",
            "mean_type": MEAN_TYPE_ARITHMETIC,
            "has_sum": False,
            "unit_class": "temperature",
            "stats": [{"start_ts": 1_700_000_000.0, "mean": 20.5, "min": 19.0, "max": 22.0}],
        }
        payload.update(overrides)
        client.import_statistics(**payload)
        return next(m for m in socket.sent if m.get("type") == "recorder/import_statistics")

    def test_sends_the_fields_required_from_ha_2026_11(self, monkeypatch: Any) -> None:
        # mean_type and unit_class are optional today and required later.
        # Omitting them would work until an update silently broke it.
        message = self._send_one(monkeypatch)
        assert message["metadata"]["mean_type"] == MEAN_TYPE_ARITHMETIC
        assert message["metadata"]["unit_class"] == "temperature"

    def test_still_sends_has_mean_for_older_versions(self, monkeypatch: Any) -> None:
        # Versions predating mean_type understand only this field.
        message = self._send_one(monkeypatch)
        assert message["metadata"]["has_mean"] is True

    def test_has_mean_is_false_for_a_counter(self, monkeypatch: Any) -> None:
        message = self._send_one(monkeypatch, mean_type=MEAN_TYPE_NONE, has_sum=True)
        assert message["metadata"]["has_mean"] is False
        assert message["metadata"]["has_sum"] is True

    def test_the_source_must_be_the_recorder_domain(self, monkeypatch: Any) -> None:
        # Home Assistant rejects an entity-id statistic whose source is
        # anything else, treating it as an external statistic.
        assert self._send_one(monkeypatch)["metadata"]["source"] == "recorder"

    def test_timestamps_are_sent_as_iso_strings(self, monkeypatch: Any) -> None:
        message = self._send_one(monkeypatch)
        start = message["stats"][0]["start"]
        assert isinstance(start, str)
        assert start.startswith("2023-11-14T")

    def test_absent_values_are_omitted_rather_than_sent_as_null(self, monkeypatch: Any) -> None:
        message = self._send_one(
            monkeypatch,
            stats=[{"start_ts": 1_700_000_000.0, "mean": 20.5, "min": None, "max": None}],
        )
        entry = message["stats"][0]
        assert entry["mean"] == 20.5
        assert "min" not in entry and "max" not in entry

    def test_an_empty_batch_sends_nothing(self, monkeypatch: Any) -> None:
        socket = FakeSocket([{"type": "auth_ok"}])
        client = _client(monkeypatch, socket)
        client.import_statistics(
            statistic_id="sensor.x",
            unit_of_measurement=None,
            mean_type=MEAN_TYPE_ARITHMETIC,
            has_sum=False,
            unit_class=None,
            stats=[],
        )
        assert socket.sent == []


class TestFailureHandling:
    def test_a_transport_failure_mid_command_is_reported_not_raised_raw(
        self, monkeypatch: Any
    ) -> None:
        # The underlying socket's own timeout applies to every recv(), not
        # just the initial connection — so a slow or dropped reply mid
        # command raises the websocket library's own exception type here,
        # not HomeAssistantApiError, unless _send translates it.
        class _DroppedSocket:
            def send(self, raw: str) -> None:
                raise ConnectionResetError("connection reset by peer")

        client = HomeAssistantClient(ApiConfig(url="ws://test", token="t"))
        with pytest.raises(HomeAssistantApiError, match="Lost the connection"):
            client._send(_DroppedSocket(), {"type": "get_config"})

    def test_a_rejected_token_is_reported(self, monkeypatch: Any) -> None:
        socket = FakeSocket([{"type": "auth_invalid"}])
        client = HomeAssistantClient(ApiConfig(url="ws://test", token="bad"))
        with pytest.raises(HomeAssistantApiError, match="rejected the access token"):
            client._authenticate(socket)

    def test_an_unexpected_greeting_is_reported(self, monkeypatch: Any) -> None:
        # A real Home Assistant always opens with "auth_required"; anything
        # else means this is not talking to what it thinks it is.
        socket = FakeSocket.__new__(FakeSocket)
        socket.sent = []
        socket.closed = False
        socket._queue = [{"type": "not_a_real_greeting"}]
        client = HomeAssistantClient(ApiConfig(url="ws://test", token="t"))
        with pytest.raises(HomeAssistantApiError, match="Unexpected greeting"):
            client._authenticate(socket)

    def test_a_reply_that_never_arrives_times_out(self, monkeypatch: Any) -> None:
        # _send loops reading messages until one matches the request id, or
        # the deadline passes — simulated here by a socket that only ever
        # answers with the wrong id, and a fake clock that jumps straight
        # past the timeout so the test does not actually wait 30 seconds.
        class _NeverMatchingSocket(FakeSocket):
            def recv(self) -> str:
                return json.dumps({"id": 999, "success": True})

        socket = _NeverMatchingSocket([])
        client = HomeAssistantClient(ApiConfig(url="ws://test", token="t"))

        times = iter([0.0, 1000.0])
        monkeypatch.setattr("hr_ha_api.time.time", lambda: next(times))
        with pytest.raises(HomeAssistantApiError, match="did not answer in time"):
            client._send(socket, {"type": "get_config"})

    def test_an_error_result_is_raised_for_the_caller_to_fall_back(self, monkeypatch: Any) -> None:
        socket = FakeSocket(
            [
                {"type": "auth_ok"},
                {
                    "id": 1,
                    "success": False,
                    "error": {"code": "home_assistant_error", "message": "nope"},
                },
            ]
        )
        client = _client(monkeypatch, socket)
        with pytest.raises(HomeAssistantApiError, match="refused the statistics import"):
            client.import_statistics(
                statistic_id="sensor.x",
                unit_of_measurement="°C",
                mean_type=MEAN_TYPE_ARITHMETIC,
                has_sum=False,
                unit_class=None,
                stats=[{"start_ts": 1_700_000_000.0, "mean": 1.0}],
            )

    def test_the_connection_is_closed_even_when_the_command_fails(self, monkeypatch: Any) -> None:
        socket = FakeSocket([{"type": "auth_ok"}, {"id": 1, "success": False, "error": {}}])
        client = _client(monkeypatch, socket)
        with pytest.raises(HomeAssistantApiError):
            client.import_statistics(
                statistic_id="sensor.x",
                unit_of_measurement=None,
                mean_type=MEAN_TYPE_ARITHMETIC,
                has_sum=False,
                unit_class=None,
                stats=[{"start_ts": 1_700_000_000.0, "mean": 1.0}],
            )
        assert socket.closed is True


class TestConnect:
    """_connect() itself — every other test monkeypatches it away entirely,
    so the real websocket-building logic (URL scheme handling, wrapping a
    transport failure, closing on an auth failure) had no coverage at all."""

    def test_a_transport_failure_falls_back_to_sql(self, monkeypatch: Any) -> None:
        def _raise(url: str, **kwargs: Any) -> Any:
            raise OSError("connection refused")

        monkeypatch.setattr(_websocket_module, "create_connection", _raise)
        client = HomeAssistantClient(ApiConfig(url="ws://unreachable", token="t"))
        with pytest.raises(HomeAssistantApiError, match="Could not reach Home Assistant"):
            client._connect()

    def test_a_successful_connection_is_authenticated_and_returned(self, monkeypatch: Any) -> None:
        socket = FakeSocket()
        monkeypatch.setattr(_websocket_module, "create_connection", lambda url, **kw: socket)
        client = HomeAssistantClient(ApiConfig(url="ws://test", token="t"))
        assert client._connect() is socket
        assert {"type": "auth", "access_token": "t"} in socket.sent

    def test_a_wss_url_requires_a_valid_certificate(self, monkeypatch: Any) -> None:
        captured: dict[str, Any] = {}

        def _capture(url: str, **kwargs: Any) -> Any:
            captured.update(kwargs)
            return FakeSocket()

        monkeypatch.setattr(_websocket_module, "create_connection", _capture)
        client = HomeAssistantClient(ApiConfig(url="wss://secure", token="t"))
        client._connect()
        assert captured["sslopt"] == {"cert_reqs": ssl.CERT_REQUIRED}

    def test_a_plain_ws_url_sets_no_tls_options(self, monkeypatch: Any) -> None:
        captured: dict[str, Any] = {}

        def _capture(url: str, **kwargs: Any) -> Any:
            captured.update(kwargs)
            return FakeSocket()

        monkeypatch.setattr(_websocket_module, "create_connection", _capture)
        client = HomeAssistantClient(ApiConfig(url="ws://plain", token="t"))
        client._connect()
        assert "sslopt" not in captured

    def test_a_failed_authentication_closes_the_connection(self, monkeypatch: Any) -> None:
        socket = FakeSocket([{"type": "auth_invalid"}])
        monkeypatch.setattr(_websocket_module, "create_connection", lambda url, **kw: socket)
        client = HomeAssistantClient(ApiConfig(url="ws://test", token="bad"))
        with pytest.raises(HomeAssistantApiError, match="rejected the access token"):
            client._connect()
        assert socket.closed is True


class TestGetVersion:
    def test_returns_the_reported_version(self, monkeypatch: Any) -> None:
        socket = FakeSocket(
            [{"type": "auth_ok"}, {"id": 1, "success": True, "result": {"version": "2026.8.0"}}]
        )
        client = _client(monkeypatch, socket)
        assert client.get_version() == "2026.8.0"
        assert socket.closed is True

    def test_an_unsuccessful_result_returns_none_without_raising(self, monkeypatch: Any) -> None:
        socket = FakeSocket([{"type": "auth_ok"}, {"id": 1, "success": False}])
        client = _client(monkeypatch, socket)
        assert client.get_version() is None
        assert socket.closed is True

    def test_a_send_failure_after_connecting_returns_none(self, monkeypatch: Any) -> None:
        # Distinct from a connect() failure: the connection succeeds, but
        # the command itself fails or times out (_send raising) — the
        # connection must still be closed in that case, not leaked.
        socket = FakeSocket([{"type": "auth_ok"}])
        client = _client(monkeypatch, socket)
        monkeypatch.setattr(
            client,
            "_send",
            lambda *a, **kw: (_ for _ in ()).throw(HomeAssistantApiError("timed out")),
        )
        assert client.get_version() is None
        assert socket.closed is True

    def test_a_connection_failure_returns_none(self, monkeypatch: Any) -> None:
        client = HomeAssistantClient(ApiConfig(url="ws://unreachable", token="t"))
        monkeypatch.setattr(
            client,
            "_connect",
            lambda: (_ for _ in ()).throw(HomeAssistantApiError("unreachable")),
        )
        assert client.get_version() is None


class TestClientFromEnv:
    def test_valid_credentials_return_a_real_client(self, monkeypatch: Any) -> None:
        monkeypatch.delenv("HR_HA_URL", raising=False)
        monkeypatch.delenv("HR_HA_TOKEN", raising=False)
        monkeypatch.setenv("SUPERVISOR_TOKEN", "supervisor-token")
        client = client_from_env()
        assert isinstance(client, HomeAssistantClient)
