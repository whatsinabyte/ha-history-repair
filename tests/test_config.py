"""Tests for hr_config — turning run.sh's environment into an AppConfig.

A pure module with no I/O beyond os.environ, so these are plain unit tests.
They exist mostly for the port defaulting, which is the one place a mistake
would silently point a user's add-on at the wrong server rather than fail
loudly.
"""

from __future__ import annotations

from typing import Any

import pytest

from hr_config import DEFAULT_SQLITE_PATH, AppConfig

_DB_ENV = (
    "HR_DB_TYPE",
    "HR_DB_PATH",
    "HR_DB_HOST",
    "HR_DB_PORT",
    "HR_DB_NAME",
    "HR_DB_USER",
    "HR_DB_PASSWORD",
    "HR_STATE_DIR",
    "HR_PORT",
    "HR_TIME_FORMAT",
    "HR_LOG_LEVEL",
)


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch: Any) -> None:
    """Start from an environment with none of these set.

    Without this a developer's own exported HR_* variable — the ones
    dev/run_local.sh sets, for instance — would decide what these tests see.
    """
    for name in _DB_ENV:
        monkeypatch.delenv(name, raising=False)


class TestDatabaseSelection:
    def test_sqlite_builds_a_sqlite_config_only(self, monkeypatch: Any) -> None:
        monkeypatch.setenv("HR_DB_TYPE", "sqlite")
        config = AppConfig.from_env()
        assert config.sqlite is not None
        assert config.sqlite.path == DEFAULT_SQLITE_PATH
        assert config.database is None

    @pytest.mark.parametrize("db_type", ["mariadb", "postgres"])
    def test_server_backends_build_a_database_config_only(
        self, monkeypatch: Any, db_type: str
    ) -> None:
        monkeypatch.setenv("HR_DB_TYPE", db_type)
        config = AppConfig.from_env()
        assert config.database is not None
        assert config.sqlite is None

    def test_the_db_type_is_normalised(self, monkeypatch: Any) -> None:
        monkeypatch.setenv("HR_DB_TYPE", "  PostgreS  ")
        assert AppConfig.from_env().db_type == "postgres"


class TestPortDefaulting:
    """An unset port means "whatever is standard for this backend".

    run.sh turns the schema's `db_port: 0` sentinel into an empty string, so
    a PostgreSQL user who never touches the field is not silently pointed at
    MySQL's 3306 by a default that predates PostgreSQL support.
    """

    @pytest.mark.parametrize(
        ("db_type", "expected"),
        [("mariadb", 3306), ("postgres", 5432)],
    )
    def test_an_unset_port_uses_the_backends_own_default(
        self, monkeypatch: Any, db_type: str, expected: int
    ) -> None:
        monkeypatch.setenv("HR_DB_TYPE", db_type)
        config = AppConfig.from_env()
        assert config.database is not None
        assert config.database.port == expected

    @pytest.mark.parametrize(
        ("db_type", "expected"),
        [("mariadb", 3306), ("postgres", 5432)],
    )
    def test_an_empty_port_uses_the_backends_own_default(
        self, monkeypatch: Any, db_type: str, expected: int
    ) -> None:
        # What run.sh actually exports for the `0` sentinel.
        monkeypatch.setenv("HR_DB_TYPE", db_type)
        monkeypatch.setenv("HR_DB_PORT", "")
        config = AppConfig.from_env()
        assert config.database is not None
        assert config.database.port == expected

    @pytest.mark.parametrize("db_type", ["mariadb", "postgres"])
    def test_an_explicit_port_always_wins(self, monkeypatch: Any, db_type: str) -> None:
        monkeypatch.setenv("HR_DB_TYPE", db_type)
        monkeypatch.setenv("HR_DB_PORT", "15432")
        config = AppConfig.from_env()
        assert config.database is not None
        assert config.database.port == 15432

    def test_an_unrecognised_backend_falls_back_to_the_mysql_port(self, monkeypatch: Any) -> None:
        # Not reachable through config.yaml's own enum, but from_env reads the
        # environment rather than the schema and should not raise on it.
        monkeypatch.setenv("HR_DB_TYPE", "something-else")
        config = AppConfig.from_env()
        assert config.database is not None
        assert config.database.port == 3306


class TestTimeFormat:
    @pytest.mark.parametrize("value", ["12", "24"])
    def test_valid_values_are_kept(self, monkeypatch: Any, value: str) -> None:
        monkeypatch.setenv("HR_TIME_FORMAT", value)
        assert AppConfig.from_env().time_format == value

    @pytest.mark.parametrize("value", ["", "13", "nonsense"])
    def test_anything_else_falls_back_to_24_hour(self, monkeypatch: Any, value: str) -> None:
        monkeypatch.setenv("HR_TIME_FORMAT", value)
        assert AppConfig.from_env().time_format == "24"
