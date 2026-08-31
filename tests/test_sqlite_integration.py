"""Integration tests: the real SQLiteAdapter against a real SQLite file.

These mirror test_mariadb_integration.py's coverage for the same
DatabaseAdapter contract, plus SQLite-specific lock-retry behaviour that has
no MariaDB equivalent. Where MariaDB integration tests prove the MySQL SQL
parses and transacts correctly, these prove the same for SQLite's dialect
(placeholders, BEGIN IMMEDIATE, PRAGMA introspection) — the arithmetic itself
(time-weighted means, counter cascades) is Home Assistant's own and is
already verified against a live recorder in test_statistics_fidelity.py and
test_ha_api_live.py; it does not depend on which database stores it.

They are skipped unless HR_SQLITE_TEST_DB points at a disposable, pre-built
and pre-seeded SQLite file:

    .venv-ha-schema/bin/python dev/build_schema.py --dsn sqlite:///.devdb/sqlite/ha_test.db
    .venv-check/bin/python dev/seed_recorder.py --sqlite-path .devdb/sqlite/ha_test.db
    HR_SQLITE_TEST_DB=.devdb/sqlite/ha_test.db \\
      .venv-check/bin/python -m pytest tests/test_sqlite_integration.py

Never point HR_SQLITE_TEST_DB at a real home-assistant_v2.db. These tests
write to states.
"""

from __future__ import annotations

import contextlib
import os
import shutil
import sqlite3
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from hr_config import SQLiteConfig
from hr_db import ConcurrentModification, LockTimeout, NotFound
from hr_models import Quality, SensorType
from hr_sqlite import SQLiteAdapter

TEMPERATURE = "sensor.living_room_temperature"
ENERGY = "sensor.energy_total"
FLAKY = "sensor.flaky_pressure"
DOOR = "binary_sensor.front_door"

_CANONICAL = os.environ.get("HR_SQLITE_TEST_DB")

pytestmark = pytest.mark.skipif(
    not _CANONICAL, reason="HR_SQLITE_TEST_DB is not set; see this module's docstring"
)

# Basename fragment this suite requires of its target file, so a typo can
# never point it at someone's real recorder database.
_ALLOWED_NAME_FRAGMENT = "test"


def _assert_disposable(path: str) -> None:
    if _ALLOWED_NAME_FRAGMENT not in Path(path).name:
        raise AssertionError(
            f"HR_SQLITE_TEST_DB points at '{path}', whose filename does not "
            f"contain '{_ALLOWED_NAME_FRAGMENT}'. These tests write to states "
            "and would damage a real Home Assistant recorder database. "
            "Refusing to run."
        )


@pytest.fixture(scope="session")
def db_path(tmp_path_factory: pytest.TempPathFactory) -> Iterator[str]:
    """A private copy of the seeded database for this worker.

    Unlike MariaDB, a SQLite recorder database is just a file: cloning it for
    parallel-safety is a plain copy, not a SQL dump-and-restore dance. Each
    xdist worker gets its own copy under pytest's own tmp_path_factory, which
    is already worker-unique, so there is no shared-name race to guard
    against here.
    """
    assert _CANONICAL is not None
    _assert_disposable(_CANONICAL)

    target_dir = tmp_path_factory.mktemp("sqlite_integration")
    target = target_dir / "ha_test.db"
    shutil.copyfile(_CANONICAL, target)
    yield str(target)


@pytest.fixture(scope="session")
def config(db_path: str) -> SQLiteConfig:
    return SQLiteConfig(path=db_path)


@pytest.fixture(scope="session")
def db(config: SQLiteConfig) -> SQLiteAdapter:
    adapter = SQLiteAdapter(config)
    adapter.ensure_audit_table()

    # Start from an empty audit trail. The canonical file this worker's copy
    # came from could carry a correction left over from manual testing
    # against it directly (dev/run_local.sh --sqlite, for instance) — the
    # MariaDB integration suite's own `db` fixture clears it for the same
    # reason.
    conn = sqlite3.connect(config.path)
    try:
        conn.execute("DELETE FROM state_corrections")
        conn.commit()
    finally:
        conn.close()

    return adapter


@pytest.fixture(autouse=True)
def _undo_writes(db: SQLiteAdapter) -> Iterator[None]:
    """Put the database back exactly as the test found it.

    Same undo-log approach as the MariaDB suite: every write goes through a
    correction, and restoring every open correction after each test leaves
    the seeded history untouched for the next one.
    """
    yield

    for correction in db.list_corrections(include_restored=False):
        with contextlib.suppress(ConcurrentModification, NotFound):
            db.restore_correction(correction.id, "test-cleanup")

    conn = sqlite3.connect(db._path)
    try:
        conn.execute("DELETE FROM state_corrections")
        conn.commit()
    finally:
        conn.close()


def _first_state_id(db: SQLiteAdapter, entity_id: str) -> tuple[int, str]:
    entity = db.get_entity(entity_id)
    assert entity.last_updated_ts is not None
    points = db.fetch_states(entity_id, 0, entity.last_updated_ts + 1, limit=5).points
    assert points, f"no seeded states for {entity_id}"
    point = points[0]
    assert point.value is not None
    return point.state_id, point.value


class TestHealth:
    def test_connects_and_reads_a_supported_schema(self, db: SQLiteAdapter) -> None:
        report = db.check_health()
        assert report.connected is True
        assert report.errors == []
        assert report.schema_version is not None
        assert report.schema_version >= 28
        assert report.ok is True

    def test_detects_the_update_privilege(self, db: SQLiteAdapter) -> None:
        assert db.check_health().can_update is True

    def test_reports_the_audit_table_once_created(self, db: SQLiteAdapter) -> None:
        assert db.check_health().audit_table_ready is True

    def test_ensure_audit_table_is_idempotent(self, db: SQLiteAdapter) -> None:
        db.ensure_audit_table()
        db.ensure_audit_table()
        assert db.check_health().audit_table_ready is True

    def test_a_missing_file_fails_cleanly_without_creating_one(self, tmp_path: Path) -> None:
        # sqlite3.connect() would otherwise happily create an empty file at a
        # path that does not exist — there is no server to refuse the
        # connection. Found for real: an add-on pointed at db_path on an
        # install whose recorder actually uses MariaDB left a stray empty
        # database sitting in the user's real Home Assistant config
        # directory, and reported only a confusing "could not read the
        # recorder schema version" with no hint the file never existed.
        path = tmp_path / "does_not_exist.db"
        missing = SQLiteAdapter(SQLiteConfig(path=str(path)))
        report = missing.check_health()
        assert report.connected is False
        assert report.ok is False
        assert report.errors
        assert not path.exists()

    def test_a_file_with_no_recorder_schema_fails_cleanly(self, tmp_path: Path) -> None:
        # Distinct from the missing-file case: the file exists (so a
        # connection can genuinely be made), but it holds no recorder schema
        # at all — an empty file, or some other SQLite database entirely.
        path = tmp_path / "not_a_recorder_db.db"
        sqlite3.connect(str(path)).close()
        wrong_schema = SQLiteAdapter(SQLiteConfig(path=str(path)))
        report = wrong_schema.check_health()
        assert report.connected is True
        assert report.ok is False
        assert report.schema_version is None
        assert report.errors

    def test_a_schema_older_than_supported_is_refused(self, tmp_path: Path) -> None:
        private = tmp_path / "old_schema.db"
        shutil.copyfile(_CANONICAL, private)  # type: ignore[arg-type]
        conn = sqlite3.connect(str(private))
        try:
            conn.execute("UPDATE schema_changes SET schema_version = 20")
            conn.commit()
        finally:
            conn.close()

        report = SQLiteAdapter(SQLiteConfig(path=str(private))).check_health()
        assert report.connected is True
        assert report.ok is False
        assert report.schema_version == 20
        assert any("too old" in e for e in report.errors)

    def test_a_read_only_file_reports_no_update_privilege(self, tmp_path: Path) -> None:
        private = tmp_path / "read_only.db"
        shutil.copyfile(_CANONICAL, private)  # type: ignore[arg-type]
        os.chmod(private, 0o444)
        try:
            report = SQLiteAdapter(SQLiteConfig(path=str(private))).check_health()
            assert report.connected is True
            assert report.can_update is False
            assert report.ok is False
        finally:
            os.chmod(private, 0o644)


class TestEntityBrowser:
    def test_lists_the_seeded_entities(self, db: SQLiteAdapter) -> None:
        ids = {e.entity_id for e in db.list_entities(limit=100)}
        assert {TEMPERATURE, ENERGY, FLAKY, DOOR} <= ids

    def test_classifies_sensor_types_from_statistics_meta(self, db: SQLiteAdapter) -> None:
        assert db.get_entity(TEMPERATURE).sensor_type is SensorType.MEASUREMENT
        assert db.get_entity(ENERGY).sensor_type is SensorType.COUNTER
        assert db.get_entity(DOOR).sensor_type is SensorType.UNKNOWN

    def test_extracts_the_friendly_name_from_the_attributes_json(self, db: SQLiteAdapter) -> None:
        assert db.get_entity(TEMPERATURE).friendly_name == "Living Room Temperature"

    def test_reads_the_unit_from_statistics_meta(self, db: SQLiteAdapter) -> None:
        assert db.get_entity(TEMPERATURE).unit == "°C"

    def test_reports_the_latest_value(self, db: SQLiteAdapter) -> None:
        entity = db.get_entity(TEMPERATURE)
        assert entity.last_value is not None
        assert entity.last_updated_ts is not None

    def test_search_narrows_the_list(self, db: SQLiteAdapter) -> None:
        found = db.list_entities(search="energy")
        assert [e.entity_id for e in found] == [ENERGY]
        assert db.count_entities(search="energy") == 1

    def test_search_wildcards_are_escaped(self, db: SQLiteAdapter) -> None:
        assert db.count_entities(search="%") == 0

    def test_paging_walks_the_whole_list_without_repeats(self, db: SQLiteAdapter) -> None:
        total = db.count_entities()
        seen: list[str] = []
        for offset in range(0, total, 2):
            seen.extend(e.entity_id for e in db.list_entities(limit=2, offset=offset))
        assert len(seen) == total
        assert len(set(seen)) == total

    def test_unknown_entity_raises_not_found(self, db: SQLiteAdapter) -> None:
        with pytest.raises(NotFound):
            db.get_entity("sensor.does_not_exist")

    def test_filters_by_sensor_type_through_real_sql(self, db: SQLiteAdapter) -> None:
        measurement_ids = {
            e.entity_id for e in db.list_entities(sensor_type=SensorType.MEASUREMENT)
        }
        assert TEMPERATURE in measurement_ids
        assert ENERGY not in measurement_ids

        counter_ids = {e.entity_id for e in db.list_entities(sensor_type=SensorType.COUNTER)}
        assert ENERGY in counter_ids
        assert TEMPERATURE not in counter_ids

        unknown_ids = {e.entity_id for e in db.list_entities(sensor_type=SensorType.UNKNOWN)}
        assert DOOR in unknown_ids
        assert TEMPERATURE not in unknown_ids

    def test_sorts_by_last_updated_through_real_sql(self, db: SQLiteAdapter) -> None:
        # NULLs (an entity with no states at all) always sort first
        # regardless of direction, so ascending is not a plain reversal of
        # descending — only the non-null tail's order actually flips.
        descending = db.list_entities(sort="last_updated", sort_dir="desc", limit=100)
        with_ts = [e for e in descending if e.last_updated_ts is not None]
        timestamps = [e.last_updated_ts for e in with_ts]
        assert timestamps == sorted(timestamps, reverse=True)

        ascending = db.list_entities(sort="last_updated", sort_dir="asc", limit=100)
        ascending_with_ts = [e.last_updated_ts for e in ascending if e.last_updated_ts is not None]
        assert ascending_with_ts == sorted(ascending_with_ts)

    def test_sorts_by_correction_count_through_real_sql(self, db: SQLiteAdapter) -> None:
        state_id, original = _first_state_id(db, TEMPERATURE)
        db.apply_correction(
            entity_id=TEMPERATURE,
            state_id=state_id,
            expected_original=original,
            new_value="21.5",
            quality=Quality.UNCERTAIN,
            note=None,
            created_by="pytest",
        )
        ranked = db.list_entities(sort="corrections", sort_dir="desc")
        assert ranked[0].entity_id == TEMPERATURE

    def test_a_corrupted_attributes_blob_does_not_break_the_whole_query(
        self, db: SQLiteAdapter
    ) -> None:
        # shared_attrs is TEXT, not enforced JSON — a row that fails to
        # parse must not take the friendly-name lookup for every other
        # entity down with it.
        conn = sqlite3.connect(db._path)
        try:
            cur = conn.cursor()
            cur.execute(
                "SELECT sa.attributes_id FROM states s "
                "JOIN states_meta sm ON s.metadata_id = sm.metadata_id "
                "JOIN state_attributes sa ON s.attributes_id = sa.attributes_id "
                "WHERE sm.entity_id = ? LIMIT 1",
                (TEMPERATURE,),
            )
            row = cur.fetchone()
            assert row is not None
            cur.execute(
                "UPDATE state_attributes SET shared_attrs = ? WHERE attributes_id = ?",
                ("{not valid json", row[0]),
            )
            conn.commit()
        finally:
            conn.close()

        try:
            entity = db.get_entity(TEMPERATURE)
            assert entity.friendly_name is None
        finally:
            conn = sqlite3.connect(db._path)
            try:
                conn.execute(
                    "UPDATE state_attributes SET shared_attrs = "
                    '\'{"friendly_name": "Living Room Temperature"}\' '
                    "WHERE attributes_id = ?",
                    (row[0],),
                )
                conn.commit()
            finally:
                conn.close()


class TestCorrectionLookup:
    def test_an_unknown_correction_id_is_not_found(self, db: SQLiteAdapter) -> None:
        with pytest.raises(NotFound):
            db.get_correction(999_999_999)


class TestClose:
    def test_close_is_safe_to_call(self, tmp_path: Path) -> None:
        assert _CANONICAL is not None
        private = tmp_path / "close_check.db"
        shutil.copyfile(_CANONICAL, private)
        adapter = SQLiteAdapter(SQLiteConfig(path=str(private)))
        adapter.check_health()  # opens the held-open connection
        adapter.close()
        # A second close on an already-closed adapter must not raise either.
        adapter.close()


class TestFetchStates:
    def test_returns_rows_in_time_order(self, db: SQLiteAdapter) -> None:
        entity = db.get_entity(TEMPERATURE)
        assert entity.last_updated_ts is not None
        points = db.fetch_states(TEMPERATURE, 0, entity.last_updated_ts + 1).points
        assert len(points) > 100
        assert [p.ts for p in points] == sorted(p.ts for p in points)

    def test_the_seeded_outlier_is_present(self, db: SQLiteAdapter) -> None:
        entity = db.get_entity(TEMPERATURE)
        assert entity.last_updated_ts is not None
        points = db.fetch_states(TEMPERATURE, 0, entity.last_updated_ts + 1).points
        values = [p.numeric_value for p in points if p.numeric_value is not None]
        assert min(values) == pytest.approx(-2000.0)

    def test_non_numeric_states_survive_as_none(self, db: SQLiteAdapter) -> None:
        entity = db.get_entity(FLAKY)
        assert entity.last_updated_ts is not None
        points = db.fetch_states(FLAKY, 0, entity.last_updated_ts + 1).points
        unparsed = [p for p in points if p.numeric_value is None]
        assert unparsed
        assert {p.value for p in unparsed} <= {"unknown", "unavailable"}

    def test_the_range_is_half_open(self, db: SQLiteAdapter) -> None:
        entity = db.get_entity(TEMPERATURE)
        assert entity.last_updated_ts is not None
        all_points = db.fetch_states(TEMPERATURE, 0, entity.last_updated_ts + 1).points
        first, second = all_points[0], all_points[1]
        window = db.fetch_states(TEMPERATURE, first.ts, second.ts).points
        assert [p.state_id for p in window] == [first.state_id]

    def test_limit_is_honoured(self, db: SQLiteAdapter) -> None:
        entity = db.get_entity(TEMPERATURE)
        assert entity.last_updated_ts is not None
        assert len(db.fetch_states(TEMPERATURE, 0, entity.last_updated_ts + 1, limit=7).points) == 7

    def test_reports_truncation_when_the_window_holds_more(self, db: SQLiteAdapter) -> None:
        entity = db.get_entity(TEMPERATURE)
        assert entity.last_updated_ts is not None
        series = db.fetch_states(TEMPERATURE, 0, entity.last_updated_ts + 1, limit=10)
        assert series.truncated is True
        assert len(series.points) == 10

    def test_reports_no_truncation_when_everything_fits(self, db: SQLiteAdapter) -> None:
        entity = db.get_entity(TEMPERATURE)
        assert entity.last_updated_ts is not None
        series = db.fetch_states(TEMPERATURE, 0, entity.last_updated_ts + 1, limit=100_000)
        assert series.truncated is False
        assert len(series.points) > 100


class TestApplyCorrection:
    def test_writes_the_audit_row_and_updates_the_state(self, db: SQLiteAdapter) -> None:
        state_id, original = _first_state_id(db, TEMPERATURE)

        correction = db.apply_correction(
            entity_id=TEMPERATURE,
            state_id=state_id,
            expected_original=original,
            new_value="21.5",
            quality=Quality.SPIKE,
            note="integration test",
            created_by="pytest",
        )

        assert correction.original_value == original
        assert correction.corrected_value == "21.5"
        assert correction.quality is Quality.SPIKE
        assert correction.sensor_type is SensorType.MEASUREMENT
        assert correction.created_by == "pytest"
        assert correction.stats_corrected is False

        points = db.fetch_states(TEMPERATURE, correction.state_ts, correction.state_ts + 1).points
        assert points[0].value == "21.5"
        assert points[0].correction_id == correction.id
        assert points[0].original_value == original

    def test_the_entity_correction_count_reflects_it(self, db: SQLiteAdapter) -> None:
        assert db.get_entity(TEMPERATURE).correction_count == 0
        state_id, original = _first_state_id(db, TEMPERATURE)
        db.apply_correction(
            entity_id=TEMPERATURE,
            state_id=state_id,
            expected_original=original,
            new_value="21.5",
            quality=Quality.UNCERTAIN,
            note=None,
            created_by="pytest",
        )
        assert db.get_entity(TEMPERATURE).correction_count == 1

    def test_a_stale_expected_value_is_refused(self, db: SQLiteAdapter) -> None:
        state_id, original = _first_state_id(db, TEMPERATURE)
        with pytest.raises(ConcurrentModification):
            db.apply_correction(
                entity_id=TEMPERATURE,
                state_id=state_id,
                expected_original="this is not what is stored",
                new_value="21.5",
                quality=Quality.UNCERTAIN,
                note=None,
                created_by="pytest",
            )
        points = db.fetch_states(TEMPERATURE, 0, 1e12, limit=1).points
        assert points[0].value == original
        assert db.list_corrections() == []

    def test_a_missing_state_row_is_not_found(self, db: SQLiteAdapter) -> None:
        with pytest.raises(NotFound):
            db.apply_correction(
                entity_id=TEMPERATURE,
                state_id=999_999_999,
                expected_original=None,
                new_value="21.5",
                quality=Quality.UNCERTAIN,
                note=None,
                created_by="pytest",
            )

    def test_a_state_row_belonging_to_another_entity_is_refused(self, db: SQLiteAdapter) -> None:
        state_id, _ = _first_state_id(db, ENERGY)
        with pytest.raises(NotFound, match="belongs to"):
            db.apply_correction(
                entity_id=TEMPERATURE,
                state_id=state_id,
                expected_original=None,
                new_value="21.5",
                quality=Quality.UNCERTAIN,
                note=None,
                created_by="pytest",
            )

    def test_counter_sensors_are_not_blocked_at_the_adapter_layer(self, db: SQLiteAdapter) -> None:
        state_id, original = _first_state_id(db, ENERGY)
        correction = db.apply_correction(
            entity_id=ENERGY,
            state_id=state_id,
            expected_original=original,
            new_value="1001.0",
            quality=Quality.SPIKE,
            note=None,
            created_by="pytest",
        )
        assert correction.sensor_type is SensorType.COUNTER


class TestRestore:
    def _correct(self, db: SQLiteAdapter) -> tuple[int, str]:
        state_id, original = _first_state_id(db, TEMPERATURE)
        correction = db.apply_correction(
            entity_id=TEMPERATURE,
            state_id=state_id,
            expected_original=original,
            new_value="21.5",
            quality=Quality.SPIKE,
            note=None,
            created_by="pytest",
        )
        return correction.id, original

    def test_puts_the_original_value_back(self, db: SQLiteAdapter) -> None:
        correction_id, original = self._correct(db)
        restored = db.restore_correction(correction_id, "pytest")

        assert restored.restored_at is not None
        assert restored.restored_by == "pytest"
        points = db.fetch_states(TEMPERATURE, 0, 1e12, limit=1).points
        assert points[0].value == original
        assert points[0].correction_id is None

    def test_restoring_twice_is_refused(self, db: SQLiteAdapter) -> None:
        correction_id, _ = self._correct(db)
        db.restore_correction(correction_id, "pytest")
        with pytest.raises(ConcurrentModification, match="already been restored"):
            db.restore_correction(correction_id, "pytest")

    def test_refuses_when_the_row_changed_outside_the_addon(self, db: SQLiteAdapter) -> None:
        correction_id, _ = self._correct(db)
        correction = db.get_correction(correction_id)

        conn = sqlite3.connect(db._path)
        try:
            conn.execute(
                "UPDATE states SET state = '17.7' WHERE state_id = ?", (correction.state_id,)
            )
            conn.commit()
        finally:
            conn.close()

        with pytest.raises(ConcurrentModification, match="changed outside"):
            db.restore_correction(correction_id, "pytest")

        points = db.fetch_states(TEMPERATURE, 0, 1e12, limit=1).points
        assert points[0].value == "17.7"

    def test_an_unknown_correction_is_not_found(self, db: SQLiteAdapter) -> None:
        with pytest.raises(NotFound):
            db.restore_correction(999_999_999, "pytest")

    def test_a_correction_with_no_recorded_state_id_cannot_be_restored(
        self, db: SQLiteAdapter
    ) -> None:
        # Not reachable through the adapter's own writes today, but the
        # column is nullable and restore_correction must still refuse
        # cleanly rather than crash on a None where it expects an id.
        correction_id, _ = self._correct(db)
        conn = sqlite3.connect(db._path)
        try:
            conn.execute(
                "UPDATE state_corrections SET state_id = NULL WHERE id = ?", (correction_id,)
            )
            conn.commit()
        finally:
            conn.close()

        with pytest.raises(NotFound, match="no states row recorded"):
            db.restore_correction(correction_id, "pytest")

    def test_a_purged_states_row_cannot_be_restored(self, db: SQLiteAdapter) -> None:
        correction_id, _ = self._correct(db)
        correction = db.get_correction(correction_id)

        conn = sqlite3.connect(db._path)
        try:
            conn.execute("DELETE FROM states WHERE state_id = ?", (correction.state_id,))
            conn.commit()
        finally:
            conn.close()

        with pytest.raises(NotFound, match="purged by the recorder"):
            db.restore_correction(correction_id, "pytest")


class TestOrphanedCorrections:
    """Backup consistency (design document, 9.11), against real SQL.

    A restore reverts states without touching state_corrections, so
    find_orphaned_corrections has to notice a mismatch via a real JOIN, not
    just the in-memory comparison the web-layer FakeAdapter tests use.
    """

    def _correct(self, db: SQLiteAdapter) -> tuple[int, int, str]:
        state_id, original = _first_state_id(db, TEMPERATURE)
        correction = db.apply_correction(
            entity_id=TEMPERATURE,
            state_id=state_id,
            expected_original=original,
            new_value="21.5",
            quality=Quality.SPIKE,
            note=None,
            created_by="pytest",
        )
        return correction.id, state_id, original

    def test_a_correction_still_matching_the_database_is_not_orphaned(
        self, db: SQLiteAdapter
    ) -> None:
        self._correct(db)
        assert db.find_orphaned_corrections() == []

    def test_a_backup_restore_is_detected_as_orphaned(self, db: SQLiteAdapter) -> None:
        correction_id, state_id, original = self._correct(db)
        conn = sqlite3.connect(db._path)
        try:
            conn.execute("UPDATE states SET state = ? WHERE state_id = ?", (original, state_id))
            conn.commit()
        finally:
            conn.close()

        orphaned = db.find_orphaned_corrections()
        assert [c.id for c in orphaned] == [correction_id]

    def test_a_restored_correction_is_never_orphaned(self, db: SQLiteAdapter) -> None:
        correction_id, _, _ = self._correct(db)
        db.restore_correction(correction_id, "pytest")
        assert db.find_orphaned_corrections() == []

    def test_dismissing_stops_it_reappearing(self, db: SQLiteAdapter) -> None:
        correction_id, state_id, original = self._correct(db)
        conn = sqlite3.connect(db._path)
        try:
            conn.execute("UPDATE states SET state = ? WHERE state_id = ?", (original, state_id))
            conn.commit()
        finally:
            conn.close()

        dismissed = db.dismiss_correction(correction_id, "pytest")
        assert dismissed.dismissed_at is not None
        assert dismissed.dismissed_by == "pytest"
        assert db.find_orphaned_corrections() == []

    def test_dismissing_twice_is_refused(self, db: SQLiteAdapter) -> None:
        correction_id, state_id, original = self._correct(db)
        conn = sqlite3.connect(db._path)
        try:
            conn.execute("UPDATE states SET state = ? WHERE state_id = ?", (original, state_id))
            conn.commit()
        finally:
            conn.close()
        db.dismiss_correction(correction_id, "pytest")
        with pytest.raises(ConcurrentModification):
            db.dismiss_correction(correction_id, "pytest")

    def test_dismissing_a_restored_correction_is_refused(self, db: SQLiteAdapter) -> None:
        correction_id, _, _ = self._correct(db)
        db.restore_correction(correction_id, "pytest")
        with pytest.raises(ConcurrentModification):
            db.dismiss_correction(correction_id, "pytest")

    def test_dismissing_an_unknown_correction_is_not_found(self, db: SQLiteAdapter) -> None:
        with pytest.raises(NotFound):
            db.dismiss_correction(999_999_999, "pytest")


class TestAuditTrail:
    def test_lists_newest_first(self, db: SQLiteAdapter) -> None:
        entity = db.get_entity(TEMPERATURE)
        assert entity.last_updated_ts is not None
        points = db.fetch_states(TEMPERATURE, 0, entity.last_updated_ts + 1, limit=3).points
        for index, point in enumerate(points):
            assert point.value is not None
            db.apply_correction(
                entity_id=TEMPERATURE,
                state_id=point.state_id,
                expected_original=point.value,
                new_value=f"2{index}.0",
                quality=Quality.UNCERTAIN,
                note=None,
                created_by="pytest",
            )
        corrections = db.list_corrections()
        assert len(corrections) == 3
        assert [c.id for c in corrections] == sorted((c.id for c in corrections), reverse=True)

    def test_filters_by_entity(self, db: SQLiteAdapter) -> None:
        state_id, original = _first_state_id(db, TEMPERATURE)
        db.apply_correction(
            entity_id=TEMPERATURE,
            state_id=state_id,
            expected_original=original,
            new_value="21.5",
            quality=Quality.UNCERTAIN,
            note=None,
            created_by="pytest",
        )
        assert len(db.list_corrections(entity_id=TEMPERATURE)) == 1
        assert db.list_corrections(entity_id=ENERGY) == []

    def test_can_hide_restored_corrections(self, db: SQLiteAdapter) -> None:
        state_id, original = _first_state_id(db, TEMPERATURE)
        correction = db.apply_correction(
            entity_id=TEMPERATURE,
            state_id=state_id,
            expected_original=original,
            new_value="21.5",
            quality=Quality.UNCERTAIN,
            note=None,
            created_by="pytest",
        )
        db.restore_correction(correction.id, "pytest")
        assert len(db.list_corrections(include_restored=True)) == 1
        assert db.list_corrections(include_restored=False) == []

    def test_a_note_round_trips_through_the_database(self, db: SQLiteAdapter) -> None:
        state_id, original = _first_state_id(db, TEMPERATURE)
        note = "modem reboot — 3 sensors affected"
        correction = db.apply_correction(
            entity_id=TEMPERATURE,
            state_id=state_id,
            expected_original=original,
            new_value="21.5",
            quality=Quality.BAD_COMM,
            note=note,
            created_by="Marcel Breij",
        )
        stored = db.get_correction(correction.id)
        assert stored.note == note
        assert stored.created_by == "Marcel Breij"
        assert stored.quality is Quality.BAD_COMM


class TestBeforeOnboarding:
    """Every read path has to work before state_corrections exists.

    The audit table is created during onboarding, so any of these queries can
    run against a database that has never seen it — a fresh install being
    browsed for the first time, or this add-on pointed at a database purely
    to look. Each query catches the missing-table OperationalError and
    behaves as if nothing has ever been corrected.
    """

    @pytest.fixture
    def without_audit_table(self, config: SQLiteConfig) -> Iterator[SQLiteAdapter]:
        conn = sqlite3.connect(config.path)
        try:
            conn.execute("DROP TABLE IF EXISTS state_corrections")
            conn.commit()
        finally:
            conn.close()
        adapter = SQLiteAdapter(config)
        try:
            yield adapter
        finally:
            adapter.ensure_audit_table()

    def test_list_entities_works_and_reports_no_corrections(
        self, without_audit_table: SQLiteAdapter
    ) -> None:
        entities = without_audit_table.list_entities(limit=100)
        assert entities
        assert all(e.correction_count == 0 for e in entities)

    def test_get_entity_works(self, without_audit_table: SQLiteAdapter) -> None:
        assert without_audit_table.get_entity(TEMPERATURE).correction_count == 0

    def test_fetch_states_works_with_nothing_marked(
        self, without_audit_table: SQLiteAdapter
    ) -> None:
        points = without_audit_table.fetch_states(TEMPERATURE, 0, 2_000_000_000, limit=50).points
        assert points
        assert all(p.correction_id is None for p in points)

    def test_bulk_preview_works(self, without_audit_table: SQLiteAdapter) -> None:
        preview = without_audit_table.bulk_correction_preview(TEMPERATURE, 0, 2_000_000_000)
        assert preview.total > 0
        assert preview.already_corrected == 0

    def test_health_reports_the_table_as_missing(self, without_audit_table: SQLiteAdapter) -> None:
        report = without_audit_table.check_health()
        assert report.connected
        assert not report.audit_table_ready
        assert report.schema_version is not None


class TestStatisticsCorrection:
    """Correcting a state must leave the statistics tables consistent with it."""

    def _outlier(self, db: SQLiteAdapter) -> tuple[int, str, float]:
        entity = db.get_entity(TEMPERATURE)
        assert entity.last_updated_ts is not None
        points = db.fetch_states(TEMPERATURE, 0, entity.last_updated_ts + 1).points
        worst = min(
            (p for p in points if p.numeric_value is not None),
            key=lambda p: p.numeric_value,  # type: ignore[arg-type,return-value]
        )
        assert worst.value is not None
        return worst.state_id, worst.value, worst.ts

    def test_the_short_term_bucket_follows_the_correction(self, db: SQLiteAdapter) -> None:
        from hr_statistics import SHORT_TERM_SECONDS, bucket_start

        state_id, original, ts = self._outlier(db)
        meta = db.get_statistics_metadata(TEMPERATURE)
        assert meta is not None
        start = bucket_start(ts, SHORT_TERM_SECONDS)

        before = db.fetch_statistics(meta.id, start, start + SHORT_TERM_SECONDS, short_term=True)
        assert before and before[0].min is not None
        assert float(before[0].min) == pytest.approx(-2000.0)

        db.apply_correction(
            entity_id=TEMPERATURE,
            state_id=state_id,
            expected_original=original,
            new_value="20.4",
            quality=Quality.SPIKE,
            note=None,
            created_by="pytest",
            recompute_statistics=True,
        )

        after = db.fetch_statistics(meta.id, start, start + SHORT_TERM_SECONDS, short_term=True)
        assert after and after[0].min is not None
        assert float(after[0].min) > -100.0
        assert after[0].mean is not None and float(after[0].mean) > -100.0

    def test_the_hourly_bucket_follows_too(self, db: SQLiteAdapter) -> None:
        from hr_statistics import HOURLY_SECONDS, bucket_start

        state_id, original, ts = self._outlier(db)
        meta = db.get_statistics_metadata(TEMPERATURE)
        assert meta is not None
        hour = bucket_start(ts, HOURLY_SECONDS)

        before = db.fetch_statistics(meta.id, hour, hour + HOURLY_SECONDS)
        assert before and before[0].min is not None
        assert float(before[0].min) == pytest.approx(-2000.0)

        db.apply_correction(
            entity_id=TEMPERATURE,
            state_id=state_id,
            expected_original=original,
            new_value="20.4",
            quality=Quality.SPIKE,
            note=None,
            created_by="pytest",
            recompute_statistics=True,
        )

        after = db.fetch_statistics(meta.id, hour, hour + HOURLY_SECONDS)
        assert after and after[0].min is not None
        assert float(after[0].min) > -100.0

    def test_the_audit_row_records_the_previous_statistics(self, db: SQLiteAdapter) -> None:
        state_id, original, _ = self._outlier(db)
        correction = db.apply_correction(
            entity_id=TEMPERATURE,
            state_id=state_id,
            expected_original=original,
            new_value="20.4",
            quality=Quality.SPIKE,
            note=None,
            created_by="pytest",
            recompute_statistics=True,
        )
        assert correction.stats_corrected is True

    def test_restore_puts_the_statistics_back_as_they_were(self, db: SQLiteAdapter) -> None:
        from hr_statistics import SHORT_TERM_SECONDS, bucket_start

        state_id, original, ts = self._outlier(db)
        meta = db.get_statistics_metadata(TEMPERATURE)
        assert meta is not None
        start = bucket_start(ts, SHORT_TERM_SECONDS)

        before = db.fetch_statistics(meta.id, start, start + SHORT_TERM_SECONDS, short_term=True)[0]

        correction = db.apply_correction(
            entity_id=TEMPERATURE,
            state_id=state_id,
            expected_original=original,
            new_value="20.4",
            quality=Quality.SPIKE,
            note=None,
            created_by="pytest",
            recompute_statistics=True,
        )
        db.restore_correction(correction.id, "pytest")

        after = db.fetch_statistics(meta.id, start, start + SHORT_TERM_SECONDS, short_term=True)[0]
        assert float(after.mean) == pytest.approx(float(before.mean))  # type: ignore[arg-type]
        assert float(after.min) == pytest.approx(float(before.min))  # type: ignore[arg-type]
        assert float(after.max) == pytest.approx(float(before.max))  # type: ignore[arg-type]


class TestCounterCascade:
    """Correcting an energy meter, and the running totals that follow it."""

    def _spike(self, db: SQLiteAdapter) -> tuple[int, str, float]:
        entity = db.get_entity(ENERGY)
        assert entity.last_updated_ts is not None
        points = db.fetch_states(ENERGY, 0, entity.last_updated_ts + 1).points
        worst = max(
            (p for p in points if p.numeric_value is not None),
            key=lambda p: p.numeric_value,  # type: ignore[arg-type,return-value]
        )
        assert worst.value is not None
        return worst.state_id, worst.value, worst.ts

    def test_the_seeded_counter_has_an_inflated_total(self, db: SQLiteAdapter) -> None:
        _, value, _ = self._spike(db)
        assert float(value) == pytest.approx(99999.0)

    def test_correcting_the_spike_rewrites_every_later_total(self, db: SQLiteAdapter) -> None:
        from hr_statistics import HOURLY_SECONDS, bucket_start

        state_id, original, ts = self._spike(db)
        meta = db.get_statistics_metadata(ENERGY)
        assert meta is not None
        hour = bucket_start(ts, HOURLY_SECONDS)

        before = db.fetch_statistics(meta.id, hour, 2_000_000_000)
        assert len(before) > 1, "need later buckets for a cascade to be visible"
        final_before = float(before[-1].sum)  # type: ignore[arg-type]

        db.apply_correction(
            entity_id=ENERGY,
            state_id=state_id,
            expected_original=original,
            new_value="1100.0",
            quality=Quality.SPIKE,
            note=None,
            created_by="pytest",
            recompute_statistics=True,
        )

        after = db.fetch_statistics(meta.id, hour, 2_000_000_000)
        final_after = float(after[-1].sum)  # type: ignore[arg-type]
        assert final_after < final_before

    def test_the_audit_row_records_the_cascade_scope(self, db: SQLiteAdapter) -> None:
        state_id, original, _ = self._spike(db)
        correction = db.apply_correction(
            entity_id=ENERGY,
            state_id=state_id,
            expected_original=original,
            new_value="1100.0",
            quality=Quality.SPIKE,
            note=None,
            created_by="pytest",
            recompute_statistics=True,
        )
        assert correction.stats_corrected is True

    def test_a_hour_with_no_surviving_short_term_rows_falls_back_to_its_own_chain(
        self, db: SQLiteAdapter, config: SQLiteConfig
    ) -> None:
        """Home Assistant purges statistics_short_term after ~10 days but
        keeps the hourly `statistics` table forever, so an old correction can
        land in an hour with no surviving 5-minute rows at all. Only ever
        exercised at the unit level (FakeAdapter never modelled the purge);
        this is what proves the real fallback SQL in
        _recompute_counter_statistics — rebuilding the hourly chain from
        itself rather than from statistics_short_term — actually works."""
        from hr_statistics import HOURLY_SECONDS

        entity_id = "sensor.bulk_it_purged_hour_fallback"
        hour1 = 1_780_002_000.0
        readings = [
            (hour1, "100.0"),
            (hour1 + 300, "110.0"),
            (hour1 + 600, "120.0"),
            (hour1 + HOURLY_SECONDS, "200.0"),
            (hour1 + HOURLY_SECONDS + 300, "210.0"),
        ]
        _create_bulk_test_entity(config, entity_id, readings, has_sum=True)
        try:
            conn = sqlite3.connect(config.path)
            try:
                cur = conn.cursor()
                cur.execute("SELECT id FROM statistics_meta WHERE statistic_id = ?", (entity_id,))
                stats_metadata_id = cur.fetchone()[0]
                # The purge: every 5-minute row for the first hour is gone,
                # but its hourly row (and the second hour's short_term rows)
                # remain, exactly as Home Assistant's own retention leaves it.
                cur.execute(
                    "DELETE FROM statistics_short_term WHERE metadata_id = ? "
                    "AND start_ts >= ? AND start_ts < ?",
                    (stats_metadata_id, hour1, hour1 + HOURLY_SECONDS),
                )
                conn.commit()
            finally:
                conn.close()

            entity = db.get_entity(entity_id)
            assert entity.last_updated_ts is not None
            points = db.fetch_states(entity_id, 0, entity.last_updated_ts + 1).points
            target = next(p for p in points if p.ts == hour1)
            assert target.state_id is not None

            db.apply_correction(
                entity_id=entity_id,
                state_id=target.state_id,
                expected_original="100.0",
                new_value="150.0",
                quality=Quality.SPIKE,
                note=None,
                created_by="pytest",
                recompute_statistics=True,
            )

            hourly = db.fetch_statistics(stats_metadata_id, hour1, hour1 + HOURLY_SECONDS)
            assert len(hourly) == 1
            # With no 5-minute rows left to rebuild from, and nothing before
            # this hour either, the fallback treats it as the zero point:
            # the state is whatever the hourly row already held (there is no
            # surviving raw bucket to recompute it from), but the sum must
            # still come out as a real, non-crashing cascade result.
            assert hourly[0].state is not None
            assert float(hourly[0].sum) == pytest.approx(0.0)
        finally:
            _delete_bulk_test_entity(config, entity_id)


class TestLockRetry:
    """SQLite has no row-level locking: a writer blocks the whole file.

    This is the one behaviour with no MariaDB equivalent worth testing here —
    the retry-with-backoff logic in hr_sqlite.SQLiteAdapter._transaction
    exists purely to make that single-writer constraint tolerable to a UI
    caller, rather than surfacing as a hung request or a raw sqlite3 error.
    """

    def test_a_correction_succeeds_after_a_brief_lock_is_released(
        self, db: SQLiteAdapter, config: SQLiteConfig
    ) -> None:
        import threading

        state_id, original = _first_state_id(db, TEMPERATURE)

        holder = sqlite3.connect(config.path, timeout=0.1, check_same_thread=False)
        holder.execute("BEGIN IMMEDIATE")
        holder.execute("SELECT 1")

        def release_after_delay() -> None:
            time.sleep(0.3)
            holder.rollback()
            holder.close()

        releaser = threading.Thread(target=release_after_delay)
        releaser.start()
        try:
            correction = db.apply_correction(
                entity_id=TEMPERATURE,
                state_id=state_id,
                expected_original=original,
                new_value="21.5",
                quality=Quality.UNCERTAIN,
                note=None,
                created_by="pytest",
            )
            assert correction.corrected_value == "21.5"
        finally:
            releaser.join()

    def test_a_lock_held_past_the_budget_times_out_cleanly(self, tmp_path: Path) -> None:
        # A short timeout of its own, so this test does not have to wait out
        # the production default to prove the failure path works.
        assert _CANONICAL is not None
        private = tmp_path / "lock_timeout.db"
        shutil.copyfile(_CANONICAL, private)

        short_config = SQLiteConfig(path=str(private), lock_wait_timeout=1)
        adapter = SQLiteAdapter(short_config)
        adapter.ensure_audit_table()
        # This copy bypasses the `db` fixture's own cleanup, so it needs its
        # own: the canonical file can carry a correction left over from manual
        # testing directly against it.
        cleanup_conn = sqlite3.connect(str(private))
        try:
            cleanup_conn.execute("DELETE FROM state_corrections")
            cleanup_conn.commit()
        finally:
            cleanup_conn.close()
        state_id, original = _first_state_id(adapter, TEMPERATURE)

        holder = sqlite3.connect(str(private), timeout=0.1)
        holder.execute("BEGIN IMMEDIATE")
        holder.execute("SELECT 1")
        try:
            with pytest.raises(LockTimeout):
                adapter.apply_correction(
                    entity_id=TEMPERATURE,
                    state_id=state_id,
                    expected_original=original,
                    new_value="21.5",
                    quality=Quality.UNCERTAIN,
                    note=None,
                    created_by="pytest",
                )
            assert adapter.list_corrections() == []
        finally:
            holder.rollback()
            holder.close()

    def test_a_genuine_sql_error_is_never_reported_as_a_lock_timeout(
        self, db: SQLiteAdapter
    ) -> None:
        with pytest.raises(sqlite3.OperationalError), db._transaction() as cur:
            cur.execute("SELECT * FROM this_table_does_not_exist")

    def test_a_lock_discovered_only_at_commit_time_is_a_clean_timeout(self, tmp_path: Path) -> None:
        """BEGIN IMMEDIATE claims the write lock up front, so it can succeed
        even while another connection holds a plain SHARED (read) lock — but
        COMMIT still has to upgrade past that SHARED lock, and fails if the
        reader has not let go yet by then.

        This is the one lock failure _transaction cannot retry by looping
        back to a second `yield`: doing so previously crashed with
        `RuntimeError: generator didn't stop after throw()` — a
        @contextmanager generator that catches an exception thrown into it
        and yields again instead of stopping is a protocol violation
        contextlib itself rejects, confirmed directly and unconditionally,
        independent of whether the retried attempt would have gone on to
        succeed. The fix reports LockTimeout immediately instead.
        """
        import threading

        assert _CANONICAL is not None
        private = tmp_path / "commit_time_lock.db"
        shutil.copyfile(_CANONICAL, private)

        adapter = SQLiteAdapter(SQLiteConfig(path=str(private)))
        adapter.ensure_audit_table()
        cleanup_conn = sqlite3.connect(str(private))
        try:
            cleanup_conn.execute("DELETE FROM state_corrections")
            cleanup_conn.commit()
        finally:
            cleanup_conn.close()
        state_id, original = _first_state_id(adapter, TEMPERATURE)

        # A plain reader: a deferred (not IMMEDIATE) transaction holding only
        # a SHARED lock, which BEGIN IMMEDIATE tolerates but COMMIT does not.
        reader = sqlite3.connect(
            str(private), timeout=0.1, isolation_level=None, check_same_thread=False
        )
        reader.execute("BEGIN")
        reader.execute("SELECT 1 FROM states LIMIT 1")

        def release_after_delay() -> None:
            time.sleep(0.5)
            reader.execute("COMMIT")
            reader.close()

        releaser = threading.Thread(target=release_after_delay)
        releaser.start()
        try:
            with pytest.raises(LockTimeout):
                adapter.apply_correction(
                    entity_id=TEMPERATURE,
                    state_id=state_id,
                    expected_original=original,
                    new_value="21.5",
                    quality=Quality.UNCERTAIN,
                    note=None,
                    created_by="pytest",
                )
        finally:
            releaser.join()

        # Rolled back cleanly, not left half-applied.
        assert adapter.list_corrections() == []
        points = adapter.fetch_states(TEMPERATURE, 0, 1e12, limit=1).points
        assert points[0].value == original


def _create_bulk_test_entity(
    config: SQLiteConfig,
    entity_id: str,
    readings: list[tuple[float, str]],
    *,
    mean_type: int = 1,
    has_sum: bool = False,
) -> None:
    """Insert a dedicated entity with exact readings, for a bulk-correction test.

    Mirrors test_mariadb_integration.py's helper of the same name — see its
    docstring for why exact, chosen readings are needed instead of the seeded
    7-day random walk, and why statistics are built from hr_statistics rather
    than a fresh approximation. Only the dialect differs here: `?`
    placeholders instead of `%s`, and sqlite3's own `lastrowid`.
    """
    from hr_statistics import (
        HOURLY_SECONDS,
        SHORT_TERM_SECONDS,
        CounterBucket,
        Reading,
        bucket_start,
        cascade_sums,
        recompute_short_term,
        summarise_hourly,
    )

    numeric = [(ts, float(value)) for ts, value in readings]
    starts = sorted({bucket_start(ts, SHORT_TERM_SECONDS) for ts, _ in numeric})

    conn = sqlite3.connect(config.path)
    try:
        cur = conn.cursor()
        cur.execute("INSERT INTO states_meta (entity_id) VALUES (?)", (entity_id,))
        metadata_id = cur.lastrowid
        cur.execute(
            "INSERT INTO statistics_meta "
            "(statistic_id, source, has_sum, mean_type, unit_of_measurement) "
            "VALUES (?, 'recorder', ?, ?, ?)",
            (entity_id, int(has_sum), mean_type, "°C"),
        )
        stats_metadata_id = cur.lastrowid
        cur.executemany(
            "INSERT INTO states (metadata_id, state, last_updated_ts, last_reported_ts) "
            "VALUES (?, ?, ?, ?)",
            [(metadata_id, value, ts, ts) for ts, value in readings],
        )

        if has_sum:
            buckets = [
                CounterBucket(
                    start_ts=start, state=next(v for t, v in numeric if t == start), sum=None
                )
                for start in starts
            ]
            chained = [CounterBucket(buckets[0].start_ts, buckets[0].state, 0.0)]
            chained.extend(cascade_sums(buckets[1:], buckets[0].state, 0.0))
            cur.executemany(
                "INSERT INTO statistics_short_term "
                "(created_ts, metadata_id, start_ts, state, sum) "
                "VALUES (?, ?, ?, ?, ?)",
                [(b.start_ts, stats_metadata_id, b.start_ts, b.state, b.sum) for b in chained],
            )
            hourly: dict[float, CounterBucket] = {}
            for bucket in chained:
                hourly[bucket_start(bucket.start_ts, HOURLY_SECONDS)] = bucket
            cur.executemany(
                "INSERT INTO statistics (created_ts, metadata_id, start_ts, state, sum) "
                "VALUES (?, ?, ?, ?, ?)",
                [(h, stats_metadata_id, h, b.state, b.sum) for h, b in sorted(hourly.items())],
            )
        else:
            short_term_buckets = []
            for start in starts:
                inside = [
                    Reading(value=v, ts=t)
                    for t, v in numeric
                    if start <= t < start + SHORT_TERM_SECONDS
                ]
                carried = [Reading(value=v, ts=t) for t, v in numeric if t < start]
                selected = (carried[-1:] if carried else []) + inside
                short_term_buckets.append(recompute_short_term(selected, start))
            cur.executemany(
                "INSERT INTO statistics_short_term "
                "(created_ts, metadata_id, start_ts, mean, min, max) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                [
                    (b.start_ts, stats_metadata_id, b.start_ts, b.mean, b.min, b.max)
                    for b in short_term_buckets
                ],
            )
            by_hour: dict[float, list[Any]] = {}
            for bucket in short_term_buckets:
                by_hour.setdefault(bucket_start(bucket.start_ts, HOURLY_SECONDS), []).append(bucket)
            hourly_summaries = [
                summarise_hourly(buckets_in_hour, hour_start)
                for hour_start, buckets_in_hour in sorted(by_hour.items())
            ]
            cur.executemany(
                "INSERT INTO statistics (created_ts, metadata_id, start_ts, mean, min, max) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                [
                    (b.start_ts, stats_metadata_id, b.start_ts, b.mean, b.min, b.max)
                    for b in hourly_summaries
                ],
            )
        conn.commit()
    finally:
        conn.close()


def _delete_bulk_test_entity(config: SQLiteConfig, entity_id: str) -> None:
    conn = sqlite3.connect(config.path)
    try:
        cur = conn.cursor()
        cur.execute("SELECT metadata_id FROM states_meta WHERE entity_id = ?", (entity_id,))
        row = cur.fetchone()
        if row:
            metadata_id = row[0]
            cur.execute("DELETE FROM states WHERE metadata_id = ?", (metadata_id,))
            cur.execute("DELETE FROM states_meta WHERE metadata_id = ?", (metadata_id,))
        cur.execute("SELECT id FROM statistics_meta WHERE statistic_id = ?", (entity_id,))
        row = cur.fetchone()
        if row:
            stats_id = row[0]
            cur.execute("DELETE FROM statistics WHERE metadata_id = ?", (stats_id,))
            cur.execute("DELETE FROM statistics_short_term WHERE metadata_id = ?", (stats_id,))
            cur.execute("DELETE FROM statistics_meta WHERE id = ?", (stats_id,))
        cur.execute("DELETE FROM state_corrections WHERE entity_id = ?", (entity_id,))
        conn.commit()
    finally:
        conn.close()


class TestBulkCorrectionIntegration:
    """The SQL behind bulk correction, against a real SQLite file.

    Mirrors test_mariadb_integration.py's TestBulkCorrectionIntegration —
    same scenarios, same assertions — so the bulk-correction code path in
    hr_sqlite.py (previously exercised only by unit tests against FakeAdapter)
    gets the same real-SQL coverage the other two backends already have.
    Each test gets its own entity with exact, chosen readings — the seeded
    7-day fixture is a random walk, wrong for asserting a precise
    interpolated number.
    """

    BASE = 1_780_002_600.0

    @pytest.fixture
    def bulk_entity(self, config: SQLiteConfig, request: Any) -> Iterator[str]:
        entity_id = f"sensor.bulk_it_{request.node.name.lower()[:40]}"
        readings = [
            (self.BASE, "10.0"),
            (self.BASE + 300, "-999.0"),
            (self.BASE + 600, "-999.0"),
            (self.BASE + 900, "-999.0"),
            (self.BASE + 1200, "30.0"),
        ]
        _create_bulk_test_entity(config, entity_id, readings)
        try:
            yield entity_id
        finally:
            _delete_bulk_test_entity(config, entity_id)

    def test_constant_strategy_writes_every_row_through_real_sql(
        self, db: SQLiteAdapter, bulk_entity: str
    ) -> None:
        result = db.apply_bulk_correction(
            entity_id=bulk_entity,
            start_ts=self.BASE + 150,
            end_ts=self.BASE + 1050,
            strategy="constant",
            value="20.0",
            quality=Quality.BAD_COMM,
            note="wifi outage",
            created_by="pytest",
        )
        assert result.applied == 3
        assert result.skipped == 0

        points = db.fetch_states(bulk_entity, 0, 2_000_000_000).points
        by_ts = {p.ts: p.value for p in points}
        assert by_ts[self.BASE + 300] == "20.0"
        assert by_ts[self.BASE + 600] == "20.0"
        assert by_ts[self.BASE + 900] == "20.0"
        assert by_ts[self.BASE] == "10.0"
        assert by_ts[self.BASE + 1200] == "30.0"

    def test_interpolate_strategy_ramps_linearly(self, db: SQLiteAdapter, bulk_entity: str) -> None:
        db.apply_bulk_correction(
            entity_id=bulk_entity,
            start_ts=self.BASE + 150,
            end_ts=self.BASE + 1050,
            strategy="interpolate",
            value=None,
            quality=Quality.FROZEN,
            note=None,
            created_by="pytest",
        )
        points = db.fetch_states(bulk_entity, 0, 2_000_000_000).points
        by_ts = {p.ts: float(p.value) for p in points if p.value is not None}
        assert by_ts[self.BASE + 300] == pytest.approx(15.0)
        assert by_ts[self.BASE + 600] == pytest.approx(20.0)
        assert by_ts[self.BASE + 900] == pytest.approx(25.0)

    def test_every_row_gets_its_own_audit_record(self, db: SQLiteAdapter, bulk_entity: str) -> None:
        result = db.apply_bulk_correction(
            entity_id=bulk_entity,
            start_ts=self.BASE + 150,
            end_ts=self.BASE + 1050,
            strategy="constant",
            value="20.0",
            quality=Quality.BAD_COMM,
            note="wifi outage",
            created_by="pytest",
        )
        corrections = db.list_corrections(entity_id=bulk_entity)
        assert len(corrections) == 3
        assert {c.id for c in corrections} == set(result.correction_ids)
        assert all(c.original_value == "-999.0" for c in corrections)
        assert all(c.note == "wifi outage" for c in corrections)

    def test_statistics_are_corrected_for_every_row(
        self, db: SQLiteAdapter, bulk_entity: str
    ) -> None:
        meta = db.get_statistics_metadata(bulk_entity)
        assert meta is not None
        result = db.apply_bulk_correction(
            entity_id=bulk_entity,
            start_ts=self.BASE + 150,
            end_ts=self.BASE + 1050,
            strategy="constant",
            value="20.0",
            quality=Quality.BAD_COMM,
            note=None,
            created_by="pytest",
        )
        corrections = [db.get_correction(cid) for cid in result.correction_ids]
        assert all(c.stats_corrected for c in corrections)

        hourly = db.fetch_statistics(meta.id, 0, 2_000_000_000)
        assert hourly
        assert all(r.min is None or float(r.min) > -100.0 for r in hourly)

    def test_a_row_cap_violation_leaves_the_database_untouched(
        self, db: SQLiteAdapter, bulk_entity: str, monkeypatch: Any
    ) -> None:
        import hr_db

        monkeypatch.setattr(hr_db, "MAX_BULK_ROWS", 2)
        import hr_sqlite

        monkeypatch.setattr(hr_sqlite, "MAX_BULK_ROWS", 2)

        from hr_db import InvalidRange

        with pytest.raises(InvalidRange):
            db.apply_bulk_correction(
                entity_id=bulk_entity,
                start_ts=self.BASE + 150,
                end_ts=self.BASE + 1050,
                strategy="constant",
                value="20.0",
                quality=Quality.BAD_COMM,
                note=None,
                created_by="pytest",
            )
        assert db.list_corrections(entity_id=bulk_entity) == []
        points = db.fetch_states(bulk_entity, 0, 2_000_000_000).points
        assert {p.value for p in points if p.ts not in (self.BASE, self.BASE + 1200)} == {"-999.0"}

    def test_interpolating_without_both_anchors_is_an_invalid_range(
        self, db: SQLiteAdapter, bulk_entity: str
    ) -> None:
        from hr_db import InvalidRange

        # A range reaching to the very first reading has no "before" anchor
        # for the real SQL to find — only ever exercised before through the
        # web layer's FakeAdapter, never against real SQL.
        with pytest.raises(InvalidRange, match="numeric reading on both"):
            db.apply_bulk_correction(
                entity_id=bulk_entity,
                start_ts=self.BASE - 1,
                end_ts=self.BASE + 1050,
                strategy="interpolate",
                value=None,
                quality=Quality.BAD_COMM,
                note=None,
                created_by="pytest",
            )
        assert db.list_corrections(entity_id=bulk_entity) == []

    def test_a_missing_constant_value_is_an_invalid_range(
        self, db: SQLiteAdapter, bulk_entity: str
    ) -> None:
        from hr_db import InvalidRange

        with pytest.raises(InvalidRange, match="value is required"):
            db.apply_bulk_correction(
                entity_id=bulk_entity,
                start_ts=self.BASE + 150,
                end_ts=self.BASE + 1050,
                strategy="constant",
                value=None,
                quality=Quality.BAD_COMM,
                note=None,
                created_by="pytest",
            )
        assert db.list_corrections(entity_id=bulk_entity) == []

    def test_a_counter_entity_bulk_corrects_and_cascades(
        self, db: SQLiteAdapter, config: SQLiteConfig
    ) -> None:
        entity_id = "sensor.bulk_counter_cascade_test"
        readings = [
            (self.BASE, "100.0"),
            (self.BASE + 300, "99999.0"),
            (self.BASE + 600, "99999.0"),
            (self.BASE + 900, "115.0"),
        ]
        _create_bulk_test_entity(config, entity_id, readings, mean_type=0, has_sum=True)
        try:
            meta = db.get_statistics_metadata(entity_id)
            assert meta is not None

            result = db.apply_bulk_correction(
                entity_id=entity_id,
                start_ts=self.BASE + 150,
                end_ts=self.BASE + 750,
                strategy="interpolate",
                value=None,
                quality=Quality.SPIKE,
                note=None,
                created_by="pytest",
            )
            assert result.applied == 2

            hourly = db.fetch_statistics(meta.id, 0, 2_000_000_000)
            assert hourly
            assert float(hourly[-1].sum) < 1000.0
        finally:
            _delete_bulk_test_entity(config, entity_id)
