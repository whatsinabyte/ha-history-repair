"""Tests for hr_mariadb's dialect-independent logic.

The SQL itself needs a real server and is covered by
test_mariadb_integration.py. What is tested here is everything around it:
value parsing, LIKE escaping, sensor-type classification, privilege parsing,
and the schema introspection that keeps the queries working as Home Assistant
renames statistics_meta's columns.
"""

from __future__ import annotations

from typing import Any

import pytest
from hypothesis import assume, example, given
from hypothesis import strategies as st

from hr_config import DatabaseConfig
from hr_mariadb import AUDIT_TABLE_DDL, MariaDBAdapter, escape_like, to_float
from hr_models import SensorType


@pytest.fixture
def adapter() -> MariaDBAdapter:
    """An adapter that never connects — every test here works offline."""
    return MariaDBAdapter(
        DatabaseConfig(host="localhost", port=3306, name="homeassistant", user="ha", password="")
    )


class _FakeCursor:
    """Just enough cursor to drive the row-parsing helpers."""

    def __init__(self, rows: list[dict[str, Any]] | Exception) -> None:
        self._rows = rows
        self.executed: list[str] = []

    def execute(self, sql: str, params: Any = None) -> None:
        self.executed.append(sql)
        if isinstance(self._rows, Exception):
            raise self._rows

    def fetchall(self) -> list[dict[str, Any]]:
        assert not isinstance(self._rows, Exception)
        return self._rows

    def fetchone(self) -> dict[str, Any] | None:
        assert not isinstance(self._rows, Exception)
        return self._rows[0] if self._rows else None


class TestToFloat:
    @given(st.floats(allow_nan=False, allow_infinity=False, width=32))
    def test_parses_every_finite_number_back(self, value: float) -> None:
        assert to_float(repr(value)) == value

    @pytest.mark.parametrize("value", ["unknown", "unavailable", "", "on", None, "20,5"])
    def test_non_numeric_states_become_none(self, value: str | None) -> None:
        # The recorder stores these routinely; they must not raise, because a
        # single one would otherwise break a whole graph load.
        assert to_float(value) is None


class TestEscapeLike:
    @given(st.text())
    @example("100%")
    @example("sensor_temp")
    @example("back\\slash")
    def test_wildcards_never_survive_unescaped(self, term: str) -> None:
        escaped = escape_like(term)
        # Every wildcard in the output is preceded by a backslash, so a user
        # searching for "100%" cannot turn their filter into a match-all.
        for index, char in enumerate(escaped):
            if char in "%_":
                assert index > 0 and escaped[index - 1] == "\\"

    def test_plain_terms_are_unchanged(self) -> None:
        assert escape_like("living room") == "living room"

    def test_every_special_character_escapes_to_exactly_the_right_sequence(self) -> None:
        # test_wildcards_never_survive_unescaped only checks that a wildcard
        # is *preceded* by a backslash — found by mutation testing not to
        # catch a corrupted replacement that pads the escape sequence with
        # extra characters (e.g. "%" -> "XX\%XX" instead of "\%"): every "%"
        # in that corrupted output is still preceded by a backslash, so the
        # weaker property-based test passed regardless. An exact-output
        # comparison, covering all three special characters at once, pins
        # down the real contract: nothing beyond the correct backslash may
        # appear, or a search for a literal "%" or "_" would generate a LIKE
        # pattern that can never match real data.
        assert escape_like("a\\b%c_d") == "a\\\\b\\%c\\_d"


class TestSensorTypeClassification:
    def test_a_sum_makes_it_a_counter(self) -> None:
        assert MariaDBAdapter.sensor_type_from_flags(1, 0) is SensorType.COUNTER

    def test_a_mean_makes_it_a_measurement(self) -> None:
        assert MariaDBAdapter.sensor_type_from_flags(0, 1) is SensorType.MEASUREMENT

    def test_a_sum_wins_when_both_flags_are_set(self) -> None:
        # An energy meter can carry both; its cumulative total is what makes
        # the cascade necessary, so it must not be treated as a measurement.
        assert MariaDBAdapter.sensor_type_from_flags(1, 1) is SensorType.COUNTER

    def test_no_statistics_row_is_unknown(self) -> None:
        assert MariaDBAdapter.sensor_type_from_flags(None, None) is SensorType.UNKNOWN


class TestStatisticsMetaIntrospection:
    """statistics_meta gained mean_type in schema 48; has_mean is deprecated
    from Home Assistant 2026.11. The SELECT fragment is built from whichever
    columns the database actually has, so neither era needs a code change."""

    def test_uses_has_mean_when_present(self, adapter: MariaDBAdapter) -> None:
        adapter._stats_meta_columns = {"has_sum", "has_mean", "statistic_id"}
        fragment = adapter._sensor_type_columns()
        assert "stm.has_mean AS has_mean" in fragment
        assert "stm.has_sum AS has_sum" in fragment

    def test_falls_back_to_mean_type_when_has_mean_is_gone(self, adapter: MariaDBAdapter) -> None:
        adapter._stats_meta_columns = {"has_sum", "mean_type", "statistic_id"}
        fragment = adapter._sensor_type_columns()
        assert "mean_type > 0" in fragment
        assert "has_mean" not in fragment.replace("AS has_mean", "")

    def test_degrades_to_unknown_when_neither_column_exists(self, adapter: MariaDBAdapter) -> None:
        adapter._stats_meta_columns = {"statistic_id"}
        fragment = adapter._sensor_type_columns()
        assert fragment == "0 AS has_sum, 0 AS has_mean"


class TestSchemaVersion:
    def test_reads_the_highest_recorded_migration(self) -> None:
        cursor = _FakeCursor([{"v": 48}])
        assert MariaDBAdapter._read_schema_version(cursor) == 48

    def test_missing_table_reads_as_none(self) -> None:
        import pymysql

        cursor = _FakeCursor(pymysql.Error("no such table"))
        assert MariaDBAdapter._read_schema_version(cursor) is None

    def test_an_empty_table_reads_as_none(self) -> None:
        assert MariaDBAdapter._read_schema_version(_FakeCursor([{"v": None}])) is None


class TestUpdatePrivilege:
    @pytest.mark.parametrize(
        "grant",
        [
            "GRANT ALL PRIVILEGES ON *.* TO `ha`@`%`",
            "GRANT SELECT, INSERT, UPDATE, DELETE ON `homeassistant`.* TO `ha`@`%`",
            "GRANT UPDATE ON `homeassistant`.* TO `ha`@`%`",
        ],
    )
    def test_detects_the_privilege(self, grant: str) -> None:
        cursor = _FakeCursor([{"Grants": grant}])
        assert MariaDBAdapter._has_update_privilege(cursor) is True

    @pytest.mark.parametrize(
        "grant",
        [
            "GRANT SELECT ON `homeassistant`.* TO `ha`@`%`",
            "GRANT USAGE ON *.* TO `ha`@`%`",
        ],
    )
    def test_detects_its_absence(self, grant: str) -> None:
        cursor = _FakeCursor([{"Grants": grant}])
        assert MariaDBAdapter._has_update_privilege(cursor) is False

    def test_a_database_named_update_does_not_count_as_the_privilege(self) -> None:
        # Only the privilege list before " ON " is inspected, so a database or
        # user whose name contains "update" cannot fake the grant.
        cursor = _FakeCursor([{"Grants": "GRANT SELECT ON `update_db`.* TO `ha`@`%`"}])
        assert MariaDBAdapter._has_update_privilege(cursor) is False

    def test_assumes_yes_when_grants_cannot_be_read(self) -> None:
        import pymysql

        # Better to let a real correction fail loudly than to block the user
        # on a check their MariaDB refuses to answer.
        cursor = _FakeCursor(pymysql.Error("access denied"))
        assert MariaDBAdapter._has_update_privilege(cursor) is True


class TestAuditTableDdl:
    def test_is_idempotent(self) -> None:
        assert "CREATE TABLE IF NOT EXISTS state_corrections" in AUDIT_TABLE_DDL

    @pytest.mark.parametrize(
        "column",
        [
            "state_id",
            "state_ts",
            "orig_state_value",
            "corrected_value",
            "stats_corrected",
            "restored_at",
            "created_by",
        ],
    )
    def test_carries_the_columns_the_adapter_writes(self, column: str) -> None:
        assert column in AUDIT_TABLE_DDL

    @pytest.mark.parametrize(
        "column",
        ["orig_sst_mean", "orig_stat_sum", "sum_delta_applied", "cascade_rows_updated"],
    )
    def test_reserves_the_columns_the_statistics_phase_will_need(self, column: str) -> None:
        # Created now so that adding statistics correction later is a code
        # change, not a migration on a table users already depend on.
        assert column in AUDIT_TABLE_DDL

    def test_uses_double_not_float_for_stored_values(self) -> None:
        # The design document's draft schema says FLOAT; the recorder stores
        # doubles, and narrowing them would silently lose precision on the
        # very values being preserved for restore.
        assert "FLOAT" not in AUDIT_TABLE_DDL
        assert "DOUBLE" in AUDIT_TABLE_DDL


class TestRowToCorrection:
    def test_maps_a_row_onto_the_dataclass(self) -> None:
        from datetime import datetime, timezone

        correction = MariaDBAdapter._row_to_correction(
            {
                "id": 7,
                "entity_id": "sensor.t",
                "sensor_type": "measurement",
                "state_id": 42,
                "state_ts": 1_700_000_000.0,
                "orig_state_value": "-2000.0",
                "corrected_value": "20.2",
                "quality": "spike",
                "note": None,
                "created_at": datetime(2026, 1, 1, 12, 0, tzinfo=timezone.utc),
                "created_by": "marcel",
                "restored_at": None,
                "restored_by": None,
                "stats_corrected": 0,
                "dismissed_at": None,
                "dismissed_by": None,
            }
        )
        assert correction.sensor_type is SensorType.MEASUREMENT
        assert correction.created_at.startswith("2026-01-01T12:00:00")
        assert correction.restored_at is None
        assert correction.stats_corrected is False

    @given(st.integers(min_value=0, max_value=1))
    def test_stats_corrected_is_always_a_bool(self, raw: int) -> None:
        # MySQL returns TINYINT(1) as an int; the API contract is a JSON bool.
        from datetime import datetime, timezone

        correction = MariaDBAdapter._row_to_correction(
            {
                "id": 1,
                "entity_id": "sensor.t",
                "sensor_type": "measurement",
                "state_id": 1,
                "state_ts": 0.0,
                "orig_state_value": "1",
                "corrected_value": "2",
                "quality": "uncertain",
                "note": None,
                "created_at": datetime(2026, 1, 1, tzinfo=timezone.utc),
                "created_by": "u",
                "restored_at": None,
                "restored_by": None,
                "stats_corrected": raw,
                "dismissed_at": None,
                "dismissed_by": None,
            }
        )
        assert isinstance(correction.stats_corrected, bool)


class TestCorrectionCounts:
    def test_an_empty_entity_list_never_reaches_the_database(self, adapter: MariaDBAdapter) -> None:
        # Guards against building "IN ()", which is a syntax error, on a page
        # of results that happens to be empty.
        assert adapter._correction_counts([]) == {}


class TestQueryShape:
    """The design document's draft SQL names a table that does not exist and
    keys corrections on a float timestamp. These assertions pin the fixes."""

    def test_queries_use_states_meta_not_state_metadata(self) -> None:
        import inspect

        import hr_mariadb

        source = inspect.getsource(hr_mariadb)
        assert "states_meta" in source
        assert "state_metadata" not in source

    def test_corrections_are_keyed_on_the_primary_key(self) -> None:
        import inspect

        import hr_mariadb

        # apply_correction (single row) and apply_bulk_correction (many rows,
        # one transaction) both delegate the actual write to this method.
        source = inspect.getsource(hr_mariadb.MariaDBAdapter._apply_one_correction)
        assert "WHERE state_id = %s" in source
        assert "FROM_UNIXTIME" not in source

    def test_the_locked_row_is_selected_for_update(self) -> None:
        import inspect

        import hr_mariadb

        source = inspect.getsource(hr_mariadb.MariaDBAdapter._apply_one_correction)
        assert "FOR UPDATE" in source
        # Audit first, then the states UPDATE, so a failure rolls both back.
        assert source.index("INSERT INTO state_corrections") < source.index(
            "UPDATE states SET state"
        )


class TestNoInterpolationOfUserInput:
    @given(st.text(min_size=1))
    def test_search_terms_never_reach_the_sql_string(self, term: str) -> None:
        # escape_like only neutralises LIKE wildcards; the term itself is
        # always passed as a bound parameter. This asserts the escaping never
        # introduces a quote that would matter if that ever changed.
        assume("'" not in term)
        assert "'" not in escape_like(term)


class TestMeanTypeTakesPrecedence:
    """has_mean is NULL on current Home Assistant; mean_type carries the meaning."""

    def test_mean_type_is_preferred_when_both_columns_exist(self, adapter: MariaDBAdapter) -> None:
        adapter._stats_meta_columns = {"has_sum", "has_mean", "mean_type", "statistic_id"}
        fragment = adapter._sensor_type_columns()
        # has_mean survives only as the fallback for rows written before the
        # migration, where mean_type is still NULL.
        assert "stm.mean_type IS NOT NULL" in fragment
        assert "ELSE stm.has_mean END" in fragment

    def test_pre_migration_rows_still_fall_back_to_has_mean(self, adapter: MariaDBAdapter) -> None:
        adapter._stats_meta_columns = {"has_sum", "has_mean", "mean_type", "statistic_id"}
        assert "stm.has_mean" in adapter._sensor_type_columns()
