"""Tests for hr_discovery — MariaDB and MQTT service discovery through Supervisor.

The transport is faked throughout: there is no real Supervisor to talk to in
this suite, and the whole point of this module is to behave identically
whether or not one is present.
"""

from __future__ import annotations

import json
from typing import Any, Self

from hr_config import AppConfig, DatabaseConfig, SQLiteConfig
from hr_discovery import (
    _REQUEST_TIMEOUT,
    DiscoveredMariaDB,
    DiscoveredMqtt,
    apply_discovered_mariadb,
    discover_mariadb,
    discover_mqtt,
)


class _FakeResponse:
    def __init__(self, body: dict[str, Any]) -> None:
        self._body = json.dumps(body).encode()

    def read(self) -> bytes:
        return self._body

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *args: object) -> None:
        return None


class _CapturingTransport:
    """Records the exact request/timeout urlopen was called with.

    Plain lambdas elsewhere in this file only check the *response* side —
    they cannot tell a genuine Request (right URL, right header) apart from
    one that was silently replaced by None or built from the wrong service
    name, since the fake response is handed back regardless either way.
    """

    def __init__(self, body: dict[str, Any]) -> None:
        self._body = body
        self.request: Any = None
        self.timeout: float | None = None

    def __call__(self, request: Any, timeout: float) -> _FakeResponse:
        self.request = request
        self.timeout = timeout
        return _FakeResponse(self._body)


def _mariadb_config(**overrides: Any) -> AppConfig:
    defaults: dict[str, Any] = {
        "db_type": "mariadb",
        "database": DatabaseConfig(
            host="core-mariadb", port=3306, name="homeassistant", user="ha", password="secret"
        ),
        "sqlite": None,
        "log_level": "INFO",
        "state_dir": "/config",
        "port": 8099,
        "time_format": "24",
    }
    defaults.update(overrides)
    return AppConfig(**defaults)


class TestDiscoverMariaDB:
    def test_no_token_means_no_discovery(self, monkeypatch: Any) -> None:
        monkeypatch.delenv("SUPERVISOR_TOKEN", raising=False)
        assert discover_mariadb() is None

    def test_a_provided_service_is_returned(self, monkeypatch: Any) -> None:
        monkeypatch.setenv("SUPERVISOR_TOKEN", "test-token")
        monkeypatch.setattr(
            "hr_discovery.urllib.request.urlopen",
            lambda request, timeout: _FakeResponse(
                {
                    "result": "ok",
                    "data": {
                        "host": "core-mariadb",
                        "port": 3306,
                        "username": "hass",
                        "password": "discovered-pw",
                    },
                }
            ),
        )
        discovered = discover_mariadb()
        assert discovered == DiscoveredMariaDB(
            host="core-mariadb", port=3306, user="hass", password="discovered-pw"
        )

    def test_optional_credentials_are_absent_not_empty(self, monkeypatch: Any) -> None:
        # SCHEMA_SERVICE_MYSQL marks username/password vol.Optional — a
        # provider need not supply them.
        monkeypatch.setenv("SUPERVISOR_TOKEN", "test-token")
        monkeypatch.setattr(
            "hr_discovery.urllib.request.urlopen",
            lambda request, timeout: _FakeResponse(
                {"result": "ok", "data": {"host": "core-mariadb", "port": 3306}}
            ),
        )
        discovered = discover_mariadb()
        assert discovered == DiscoveredMariaDB(
            host="core-mariadb", port=3306, user=None, password=None
        )

    def test_no_provider_is_not_an_error(self, monkeypatch: Any) -> None:
        # Supervisor answers "Service not enabled" with an HTTP error status
        # for the ordinary, common case of nobody providing mysql.
        import urllib.error

        monkeypatch.setenv("SUPERVISOR_TOKEN", "test-token")

        def _raise(request: Any, timeout: float) -> Any:
            raise urllib.error.HTTPError(
                "http://supervisor/services/mysql", 400, "Service not enabled", {}, None
            )

        monkeypatch.setattr("hr_discovery.urllib.request.urlopen", _raise)
        assert discover_mariadb() is None

    def test_malformed_response_is_not_an_error(self, monkeypatch: Any) -> None:
        monkeypatch.setenv("SUPERVISOR_TOKEN", "test-token")
        monkeypatch.setattr(
            "hr_discovery.urllib.request.urlopen",
            lambda request, timeout: _FakeResponse({"result": "ok", "data": {}}),
        )
        assert discover_mariadb() is None

    def test_a_non_numeric_port_is_not_an_error(self, monkeypatch: Any) -> None:
        # int(data["port"]) used to run outside _query_service's own
        # try/except, so a malformed port raised uncaught — and since
        # discover_mariadb() is called unconditionally at startup before
        # db_type is even checked, that would have crashed every install,
        # not just MariaDB ones.
        monkeypatch.setenv("SUPERVISOR_TOKEN", "test-token")
        monkeypatch.setattr(
            "hr_discovery.urllib.request.urlopen",
            lambda request, timeout: _FakeResponse(
                {"result": "ok", "data": {"host": "core-mariadb", "port": "not-a-port"}}
            ),
        )
        assert discover_mariadb() is None

    def test_a_response_missing_only_the_port_is_not_an_error(self, monkeypatch: Any) -> None:
        # Both "host" and "port" must be checked independently — a response
        # with one but not the other must still fall back cleanly rather
        # than crash trying to read the missing key.
        monkeypatch.setenv("SUPERVISOR_TOKEN", "test-token")
        monkeypatch.setattr(
            "hr_discovery.urllib.request.urlopen",
            lambda request, timeout: _FakeResponse(
                {"result": "ok", "data": {"host": "core-mariadb"}}
            ),
        )
        assert discover_mariadb() is None

    def test_a_response_missing_only_the_host_is_not_an_error(self, monkeypatch: Any) -> None:
        monkeypatch.setenv("SUPERVISOR_TOKEN", "test-token")
        monkeypatch.setattr(
            "hr_discovery.urllib.request.urlopen",
            lambda request, timeout: _FakeResponse({"result": "ok", "data": {"port": 3306}}),
        )
        assert discover_mariadb() is None

    def test_the_request_targets_the_mysql_service_specifically(self, monkeypatch: Any) -> None:
        transport = _CapturingTransport(
            {"result": "ok", "data": {"host": "core-mariadb", "port": 3306}}
        )
        monkeypatch.setenv("SUPERVISOR_TOKEN", "test-token")
        monkeypatch.setattr("hr_discovery.urllib.request.urlopen", transport)
        discover_mariadb()
        assert transport.request is not None
        assert transport.request.full_url == "http://supervisor/services/mysql"


class TestDiscoverMqtt:
    def test_no_token_means_no_discovery(self, monkeypatch: Any) -> None:
        monkeypatch.delenv("SUPERVISOR_TOKEN", raising=False)
        assert discover_mqtt() is None

    def test_a_provided_broker_is_returned(self, monkeypatch: Any) -> None:
        monkeypatch.setenv("SUPERVISOR_TOKEN", "test-token")
        monkeypatch.setattr(
            "hr_discovery.urllib.request.urlopen",
            lambda request, timeout: _FakeResponse(
                {
                    "result": "ok",
                    "data": {
                        "host": "core-mosquitto",
                        "port": 1883,
                        "username": "addon",
                        "password": "broker-pw",
                        "ssl": False,
                        "protocol": "3.1.1",
                    },
                }
            ),
        )
        assert discover_mqtt() == DiscoveredMqtt(
            host="core-mosquitto", port=1883, user="addon", password="broker-pw", ssl=False
        )

    def test_ssl_flag_is_read_not_assumed(self, monkeypatch: Any) -> None:
        # A provider can register a TLS-only listener here — connecting in
        # plain text to it, or vice versa, is a handshake failure either
        # way, so this must reflect what Supervisor actually reports.
        monkeypatch.setenv("SUPERVISOR_TOKEN", "test-token")
        monkeypatch.setattr(
            "hr_discovery.urllib.request.urlopen",
            lambda request, timeout: _FakeResponse(
                {"result": "ok", "data": {"host": "core-mosquitto", "port": 8883, "ssl": True}}
            ),
        )
        discovered = discover_mqtt()
        assert discovered is not None
        assert discovered.ssl is True

    def test_missing_ssl_flag_defaults_to_false(self, monkeypatch: Any) -> None:
        monkeypatch.setenv("SUPERVISOR_TOKEN", "test-token")
        monkeypatch.setattr(
            "hr_discovery.urllib.request.urlopen",
            lambda request, timeout: _FakeResponse(
                {"result": "ok", "data": {"host": "core-mosquitto", "port": 1883}}
            ),
        )
        discovered = discover_mqtt()
        assert discovered is not None
        assert discovered.ssl is False

    def test_no_provider_is_not_an_error(self, monkeypatch: Any) -> None:
        import urllib.error

        monkeypatch.setenv("SUPERVISOR_TOKEN", "test-token")

        def _raise(request: Any, timeout: float) -> Any:
            raise urllib.error.HTTPError(
                "http://supervisor/services/mqtt", 400, "Service not enabled", {}, None
            )

        monkeypatch.setattr("hr_discovery.urllib.request.urlopen", _raise)
        assert discover_mqtt() is None

    def test_a_non_numeric_port_is_not_an_error(self, monkeypatch: Any) -> None:
        monkeypatch.setenv("SUPERVISOR_TOKEN", "test-token")
        monkeypatch.setattr(
            "hr_discovery.urllib.request.urlopen",
            lambda request, timeout: _FakeResponse(
                {"result": "ok", "data": {"host": "core-mosquitto", "port": "not-a-port"}}
            ),
        )
        assert discover_mqtt() is None

    def test_a_response_missing_only_the_port_is_not_an_error(self, monkeypatch: Any) -> None:
        monkeypatch.setenv("SUPERVISOR_TOKEN", "test-token")
        monkeypatch.setattr(
            "hr_discovery.urllib.request.urlopen",
            lambda request, timeout: _FakeResponse(
                {"result": "ok", "data": {"host": "core-mosquitto"}}
            ),
        )
        assert discover_mqtt() is None

    def test_a_response_missing_only_the_host_is_not_an_error(self, monkeypatch: Any) -> None:
        monkeypatch.setenv("SUPERVISOR_TOKEN", "test-token")
        monkeypatch.setattr(
            "hr_discovery.urllib.request.urlopen",
            lambda request, timeout: _FakeResponse({"result": "ok", "data": {"port": 1883}}),
        )
        assert discover_mqtt() is None

    def test_the_request_targets_the_mqtt_service_specifically(self, monkeypatch: Any) -> None:
        transport = _CapturingTransport(
            {"result": "ok", "data": {"host": "core-mosquitto", "port": 1883}}
        )
        monkeypatch.setenv("SUPERVISOR_TOKEN", "test-token")
        monkeypatch.setattr("hr_discovery.urllib.request.urlopen", transport)
        discover_mqtt()
        assert transport.request is not None
        assert transport.request.full_url == "http://supervisor/services/mqtt"


class TestQueryServiceTransport:
    """The request/timeout _query_service actually hands to urlopen.

    A capturing transport, unlike the plain lambdas above, can tell a
    correctly-built Request (right URL, a real bearer-token header) apart
    from a broken one that would still receive the same faked response.
    """

    def test_the_authorization_header_carries_the_bearer_token(self, monkeypatch: Any) -> None:
        transport = _CapturingTransport(
            {"result": "ok", "data": {"host": "core-mariadb", "port": 3306}}
        )
        monkeypatch.setenv("SUPERVISOR_TOKEN", "test-token")
        monkeypatch.setattr("hr_discovery.urllib.request.urlopen", transport)
        discover_mariadb()
        assert transport.request is not None
        assert transport.request.get_header("Authorization") == "Bearer test-token"

    def test_the_timeout_matches_the_module_constant(self, monkeypatch: Any) -> None:
        transport = _CapturingTransport(
            {"result": "ok", "data": {"host": "core-mariadb", "port": 3306}}
        )
        monkeypatch.setenv("SUPERVISOR_TOKEN", "test-token")
        monkeypatch.setattr("hr_discovery.urllib.request.urlopen", transport)
        discover_mariadb()
        assert transport.timeout == _REQUEST_TIMEOUT

    def test_a_network_error_is_logged_with_the_service_and_the_real_error(
        self, monkeypatch: Any, caplog: Any
    ) -> None:
        import urllib.error

        monkeypatch.setenv("SUPERVISOR_TOKEN", "test-token")

        def _raise(request: Any, timeout: float) -> Any:
            raise urllib.error.HTTPError(
                "http://supervisor/services/mysql", 400, "Service not enabled", {}, None
            )

        monkeypatch.setattr("hr_discovery.urllib.request.urlopen", _raise)
        with caplog.at_level("DEBUG"):
            assert discover_mariadb() is None
        message = caplog.records[-1].getMessage()
        assert message.startswith("No 'mysql' service discovered: ")
        assert "%s" not in message
        assert not message.endswith(": None")
        assert not message.startswith("No 'None'")


class TestApplyDiscoveredMariaDB:
    def test_discovery_overrides_host_and_port(self) -> None:
        config = _mariadb_config()
        discovered = DiscoveredMariaDB(host="discovered-host", port=1234, user=None, password=None)
        result = apply_discovered_mariadb(config, discovered)
        assert result.database is not None
        assert result.database.host == "discovered-host"
        assert result.database.port == 1234

    def test_discovered_credentials_override_configured_ones(self) -> None:
        config = _mariadb_config()
        discovered = DiscoveredMariaDB(
            host="discovered-host", port=1234, user="discovered-user", password="discovered-pw"
        )
        result = apply_discovered_mariadb(config, discovered)
        assert result.database is not None
        assert result.database.user == "discovered-user"
        assert result.database.password == "discovered-pw"

    def test_missing_discovered_credentials_keep_the_configured_ones(self) -> None:
        # username/password are optional in the service schema — a provider
        # exposing only host/port should not blank out what the user typed.
        config = _mariadb_config()
        discovered = DiscoveredMariaDB(host="discovered-host", port=1234, user=None, password=None)
        result = apply_discovered_mariadb(config, discovered)
        assert result.database is not None
        assert result.database.user == "ha"
        assert result.database.password == "secret"

    def test_database_name_is_never_overridden(self) -> None:
        # The mysql service carries no database name — which database within
        # the server to use stays this add-on's own configured concern.
        config = _mariadb_config()
        discovered = DiscoveredMariaDB(host="discovered-host", port=1234, user=None, password=None)
        result = apply_discovered_mariadb(config, discovered)
        assert result.database is not None
        assert result.database.name == "homeassistant"

    def test_no_discovery_is_a_no_op(self) -> None:
        config = _mariadb_config()
        assert apply_discovered_mariadb(config, None) == config

    def test_sqlite_installs_are_never_overridden(self) -> None:
        # db_type is an explicit onboarding choice — a discoverable MariaDB
        # service must not silently switch a SQLite install over to it.
        config = AppConfig(
            db_type="sqlite",
            database=None,
            sqlite=SQLiteConfig(path="/homeassistant/home-assistant_v2.db"),
            log_level="INFO",
            state_dir="/config",
            port=8099,
            time_format="24",
        )
        discovered = DiscoveredMariaDB(host="discovered-host", port=1234, user=None, password=None)
        assert apply_discovered_mariadb(config, discovered) == config

    def test_a_non_mariadb_db_type_is_never_overridden_even_with_a_stale_database_field(
        self,
    ) -> None:
        # The three no-op conditions (discovered is None / db_type is not
        # "mariadb" / config.database is None) must each refuse independently
        # — db_type is a onboarding choice that "database happens to still be
        # set" must never override.
        config = _mariadb_config(db_type="sqlite")
        discovered = DiscoveredMariaDB(host="discovered-host", port=1234, user=None, password=None)
        assert apply_discovered_mariadb(config, discovered) == config
