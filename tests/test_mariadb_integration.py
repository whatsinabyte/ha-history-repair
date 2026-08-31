"""Integration tests: the real MariaDBAdapter against a real MariaDB server.

Everything else in the suite runs against FakeAdapter, which reimplements the
adapter's rules rather than its SQL. These tests are the only ones that prove
the SQL itself parses, uses the right column names, and transacts correctly.

They are skipped unless HR_TEST_DSN points at a disposable database:

    ./dev/mariadb.sh start
    .venv-ha-schema/bin/python dev/build_schema.py
    .venv-check/bin/python dev/seed_recorder.py
    HR_TEST_DSN=mysql://hatest:hatest@127.0.0.1:3399/ha_test \\
      .venv-check/bin/python -m pytest tests/test_mariadb_integration.py

Never point HR_TEST_DSN at a live recorder database. These tests write to
states.
"""

from __future__ import annotations

import contextlib
import os
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any
from urllib.parse import urlparse

import pymysql
import pytest
from pymysql.cursors import DictCursor

from hr_config import DatabaseConfig
from hr_db import ConcurrentModification, NotFound
from hr_mariadb import MariaDBAdapter
from hr_models import Quality, SensorType

TEMPERATURE = "sensor.living_room_temperature"
ENERGY = "sensor.energy_total"
FLAKY = "sensor.flaky_pressure"
DOOR = "binary_sensor.front_door"

_DSN = os.environ.get("HR_TEST_DSN")

pytestmark = pytest.mark.skipif(
    not _DSN, reason="HR_TEST_DSN is not set; see this module's docstring"
)


def _database_config() -> DatabaseConfig:
    parsed = urlparse(_DSN or "")
    return DatabaseConfig(
        host=parsed.hostname or "127.0.0.1",
        port=parsed.port or 3306,
        name=(parsed.path or "/").lstrip("/"),
        user=parsed.username or "",
        password=parsed.password or "",
    )


def _clone_database(base: DatabaseConfig, target_name: str) -> None:
    """Copy the seeded recorder data into a private database for this worker.

    Structure and contents are copied with plain SQL so this works from the
    project venv, without needing homeassistant installed to regenerate the
    schema.

    state_corrections is deliberately NOT copied. It belongs to the add-on
    rather than to the recorder, and anything left in the base database — a
    correction made while clicking through the UI against the same server —
    would be cloned into every worker and break the tests that count
    corrections. The adapter creates an empty one in each clone instead.
    """
    conn = pymysql.connect(
        host=base.host,
        port=base.port,
        user=base.user,
        password=base.password,
        cursorclass=DictCursor,
        autocommit=True,
    )
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT table_name AS t FROM information_schema.tables "
                "WHERE table_schema = %s AND table_type = 'BASE TABLE' "
                "AND table_name <> 'state_corrections'",
                (base.name,),
            )
            tables = [row["t"] for row in cur.fetchall()]

            cur.execute(f"DROP DATABASE IF EXISTS `{target_name}`")  # nosec B608
            cur.execute(
                f"CREATE DATABASE `{target_name}` "  # nosec B608
                "CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci"
            )
            cur.execute("SET FOREIGN_KEY_CHECKS = 0")
            for table in tables:
                # nosec B608 - table names come from information_schema, not input
                cur.execute(f"CREATE TABLE `{target_name}`.`{table}` LIKE `{base.name}`.`{table}`")
                cur.execute(
                    f"INSERT INTO `{target_name}`.`{table}` SELECT * FROM `{base.name}`.`{table}`"
                )
            cur.execute("SET FOREIGN_KEY_CHECKS = 1")
    finally:
        conn.close()


# Names this suite creates inside the server. Both are made unique per
# process, not merely per worker: two pytest runs started at once would
# otherwise collide on ha_test_gw0 and on the trigger below, which is exactly
# the kind of shared-name race that makes a parallel suite intermittently red.
_RUN_TAG = f"{os.environ.get('PYTEST_XDIST_WORKER', 'solo')}_{os.getpid()}"
_BLOCK_TRIGGER = f"hr_block_states_update_{os.getpid()}"

# Databases this suite is willing to write to. It DELETEs from states and
# state_corrections, so pointing it at a real recorder database would damage
# real history.
_ALLOWED_DB_PREFIX = "ha_test"


def _assert_disposable(config: DatabaseConfig) -> None:
    if not config.name.startswith(_ALLOWED_DB_PREFIX):
        raise AssertionError(
            f"HR_TEST_DSN points at database '{config.name}', which is not a "
            f"disposable test database (expected a name starting with "
            f"'{_ALLOWED_DB_PREFIX}'). These tests write to states and would "
            "damage a real Home Assistant recorder database. Refusing to run."
        )


@pytest.fixture(scope="session")
def config() -> Iterator[DatabaseConfig]:
    """Connection details for this worker's own copy of the seeded database.

    Under pytest-xdist every worker gets its own clone, because the cleanup
    fixture below reverts and clears *all* corrections — six workers sharing
    one database would delete each other's rows mid-test.
    """
    from dataclasses import replace

    base = _database_config()
    _assert_disposable(base)

    if not os.environ.get("PYTEST_XDIST_WORKER"):
        # Running serially (-n0, usually to debug one failure): the base
        # database is used directly and nothing else is competing for it.
        yield base
        return

    target = f"{base.name}_{_RUN_TAG}"
    _clone_database(base, target)
    try:
        yield replace(base, name=target)
    finally:
        conn = pymysql.connect(
            host=base.host,
            port=base.port,
            user=base.user,
            password=base.password,
            autocommit=True,
        )
        try:
            with conn.cursor() as cur:
                cur.execute(f"DROP DATABASE IF EXISTS `{target}`")  # nosec B608
        finally:
            conn.close()


@pytest.fixture(scope="session")
def db(config: DatabaseConfig) -> MariaDBAdapter:
    adapter = MariaDBAdapter(config)
    adapter.ensure_audit_table()

    # Start from an empty audit trail. Clones never carry state_corrections
    # over, but a serial run (-n0) uses the base database directly, where a
    # correction may survive from a UI session against the same server.
    conn = _raw_connection(config)
    try:
        with conn.cursor() as cur:
            cur.execute("DELETE FROM state_corrections")
    finally:
        conn.close()

    return adapter


@pytest.fixture(autouse=True)
def _undo_writes(db: MariaDBAdapter, config: DatabaseConfig) -> Iterator[None]:
    """Put the database back exactly as the test found it.

    Every write this suite makes goes through a correction, and every
    correction records the value it replaced — so the audit table itself is
    the undo log. Reverting from it and then clearing it leaves the seeded
    history untouched for the next test, whatever order they run in.

    The unused `db` parameter is load-bearing: it is what guarantees
    ensure_audit_table has run on this worker's clone. Without it, a test that
    requests only `config` (the wrong-password health check) can be scheduled
    first on a worker, and this teardown then queries a state_corrections
    table that does not exist yet — an ordering-dependent failure that appears
    only in some parallel runs.
    """
    yield

    # Corrections now also rewrite the statistics tables, so undoing them by
    # hand is no longer enough — restore_correction is the only thing that
    # knows how to put both back. Anything it refuses (a test that changed the
    # row behind the add-on's back on purpose) falls through to the manual
    # revert below.
    for correction in db.list_corrections(include_restored=False):
        with contextlib.suppress(ConcurrentModification, NotFound):
            db.restore_correction(correction.id, "test-cleanup")

    conn = pymysql.connect(
        host=config.host,
        port=config.port,
        user=config.user,
        password=config.password,
        database=config.name,
        cursorclass=DictCursor,
        autocommit=False,
    )
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT state_id, orig_state_value FROM state_corrections "
                "WHERE state_id IS NOT NULL"
            )
            for row in cur.fetchall():
                cur.execute(
                    "UPDATE states SET state = %s WHERE state_id = %s",
                    (row["orig_state_value"], row["state_id"]),
                )
            cur.execute("DELETE FROM state_corrections")
        conn.commit()
    finally:
        conn.close()


def _raw_connection(config: DatabaseConfig, autocommit: bool = True) -> Any:
    return pymysql.connect(
        host=config.host,
        port=config.port,
        user=config.user,
        password=config.password,
        database=config.name,
        cursorclass=DictCursor,
        autocommit=autocommit,
    )


@contextmanager
def _states_updates_blocked(config: DatabaseConfig) -> Iterator[None]:
    """Make every UPDATE on states fail, for the body of the with-block."""
    conn = _raw_connection(config)
    try:
        with conn.cursor() as cur:
            cur.execute(f"DROP TRIGGER IF EXISTS {_BLOCK_TRIGGER}")  # nosec B608
            cur.execute(
                f"CREATE TRIGGER {_BLOCK_TRIGGER} BEFORE UPDATE ON states "  # nosec B608
                "FOR EACH ROW SIGNAL SQLSTATE '45000' "
                "SET MESSAGE_TEXT = 'blocked by integration test'"
            )
        yield
    finally:
        with conn.cursor() as cur:
            cur.execute(f"DROP TRIGGER IF EXISTS {_BLOCK_TRIGGER}")  # nosec B608
        conn.close()


@contextmanager
def _row_locked(config: DatabaseConfig, state_id: int) -> Iterator[None]:
    """Hold a write lock on one states row, as the recorder would."""
    conn = _raw_connection(config, autocommit=False)
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT state_id FROM states WHERE state_id = %s FOR UPDATE", (state_id,))
            yield
    finally:
        conn.rollback()
        conn.close()


def _first_state_id(db: MariaDBAdapter, entity_id: str) -> tuple[int, str]:
    """A real state row from the seeded history, with its current value."""
    entity = db.get_entity(entity_id)
    assert entity.last_updated_ts is not None
    points = db.fetch_states(entity_id, 0, entity.last_updated_ts + 1, limit=5).points
    assert points, f"no seeded states for {entity_id}"
    point = points[0]
    assert point.value is not None
    return point.state_id, point.value


class TestHealth:
    def test_connects_and_reads_a_supported_schema(self, db: MariaDBAdapter) -> None:
        report = db.check_health()
        assert report.connected is True
        assert report.errors == []
        # Generated by Home Assistant's own SQLAlchemy models, so this is
        # whatever version the installed HA declares — not a hand-written guess.
        assert report.schema_version is not None
        assert report.schema_version >= 28
        assert report.ok is True

    def test_detects_the_update_privilege(self, db: MariaDBAdapter) -> None:
        assert db.check_health().can_update is True

    def test_reports_the_audit_table_once_created(self, db: MariaDBAdapter) -> None:
        assert db.check_health().audit_table_ready is True

    def test_ensure_audit_table_is_idempotent(self, db: MariaDBAdapter) -> None:
        db.ensure_audit_table()
        db.ensure_audit_table()
        assert db.check_health().audit_table_ready is True

    def test_a_wrong_password_fails_cleanly(self, config: DatabaseConfig) -> None:
        from dataclasses import replace

        report = MariaDBAdapter(replace(config, password="definitely-wrong")).check_health()
        assert report.connected is False
        assert report.ok is False
        assert report.errors

    def test_a_schema_older_than_supported_is_refused(
        self, db: MariaDBAdapter, config: DatabaseConfig
    ) -> None:
        conn = _raw_connection(config)
        try:
            with conn.cursor() as cur:
                cur.execute("SELECT MAX(schema_version) AS v FROM schema_changes")
                real_version = cur.fetchone()["v"]
                cur.execute("UPDATE schema_changes SET schema_version = 20")
        finally:
            conn.close()

        try:
            report = MariaDBAdapter(config).check_health()
            assert report.connected is True
            assert report.ok is False
            assert report.schema_version == 20
            assert any("too old" in e for e in report.errors)
        finally:
            conn = _raw_connection(config)
            try:
                with conn.cursor() as cur:
                    cur.execute("UPDATE schema_changes SET schema_version = %s", (real_version,))
            finally:
                conn.close()

    def test_a_user_without_update_privilege_reports_no_update(
        self, config: DatabaseConfig, monkeypatch: Any
    ) -> None:
        # The dev database user (hatest) has ALL PRIVILEGES and no
        # CREATE USER grant of its own, so a genuinely unprivileged MariaDB
        # account cannot be provisioned inside this test. What is real here
        # is check_health()'s handling of _has_update_privilege's result —
        # only that one static classification (SHOW GRANTS parsing) is
        # substituted for a lesser-privileged account's real answer.
        monkeypatch.setattr(
            MariaDBAdapter, "_has_update_privilege", staticmethod(lambda cur: False)
        )
        report = MariaDBAdapter(config).check_health()
        assert report.connected is True
        assert report.can_update is False
        assert report.ok is False
        assert any("UPDATE privilege" in e for e in report.errors)


class TestEntityBrowser:
    def test_lists_the_seeded_entities(self, db: MariaDBAdapter) -> None:
        ids = {e.entity_id for e in db.list_entities(limit=100)}
        assert {TEMPERATURE, ENERGY, FLAKY, DOOR} <= ids

    def test_classifies_sensor_types_from_statistics_meta(self, db: MariaDBAdapter) -> None:
        assert db.get_entity(TEMPERATURE).sensor_type is SensorType.MEASUREMENT
        assert db.get_entity(ENERGY).sensor_type is SensorType.COUNTER
        # No statistics_meta row at all, so the type cannot be confirmed.
        assert db.get_entity(DOOR).sensor_type is SensorType.UNKNOWN

    def test_extracts_the_friendly_name_from_the_attributes_json(self, db: MariaDBAdapter) -> None:
        assert db.get_entity(TEMPERATURE).friendly_name == "Living Room Temperature"

    def test_reads_the_unit_from_statistics_meta(self, db: MariaDBAdapter) -> None:
        assert db.get_entity(TEMPERATURE).unit == "°C"

    def test_reports_the_latest_value(self, db: MariaDBAdapter) -> None:
        entity = db.get_entity(TEMPERATURE)
        assert entity.last_value is not None
        assert entity.last_updated_ts is not None

    def test_search_narrows_the_list(self, db: MariaDBAdapter) -> None:
        found = db.list_entities(search="energy")
        assert [e.entity_id for e in found] == [ENERGY]
        assert db.count_entities(search="energy") == 1

    def test_search_wildcards_are_escaped(self, db: MariaDBAdapter) -> None:
        # A literal % must not turn the filter into a match-all.
        assert db.count_entities(search="%") == 0

    def test_paging_walks_the_whole_list_without_repeats(self, db: MariaDBAdapter) -> None:
        total = db.count_entities()
        seen: list[str] = []
        for offset in range(0, total, 2):
            seen.extend(e.entity_id for e in db.list_entities(limit=2, offset=offset))
        assert len(seen) == total
        assert len(set(seen)) == total

    def test_unknown_entity_raises_not_found(self, db: MariaDBAdapter) -> None:
        with pytest.raises(NotFound):
            db.get_entity("sensor.does_not_exist")


class TestEntitySortingAndFiltering:
    """Both happen inside list_entities's pagination subquery, before the
    page is selected — see the comment there for why that matters at scale.
    """

    def test_filters_by_sensor_type(self, db: MariaDBAdapter) -> None:
        assert [e.entity_id for e in db.list_entities(sensor_type=SensorType.COUNTER)] == [ENERGY]
        assert db.count_entities(sensor_type=SensorType.COUNTER) == 1

        measurements = {e.entity_id for e in db.list_entities(sensor_type=SensorType.MEASUREMENT)}
        assert {TEMPERATURE, FLAKY} <= measurements
        assert ENERGY not in measurements

        unknowns = {e.entity_id for e in db.list_entities(sensor_type=SensorType.UNKNOWN)}
        assert DOOR in unknowns
        assert ENERGY not in unknowns

    def test_type_filter_and_search_combine(self, db: MariaDBAdapter) -> None:
        assert db.count_entities(search="energy", sensor_type=SensorType.MEASUREMENT) == 0
        assert db.count_entities(search="energy", sensor_type=SensorType.COUNTER) == 1

    def test_sort_by_entity_id_direction(self, db: MariaDBAdapter) -> None:
        ascending = [e.entity_id for e in db.list_entities(sort="entity_id", sort_dir="asc")]
        descending = [e.entity_id for e in db.list_entities(sort="entity_id", sort_dir="desc")]
        assert ascending == sorted(ascending)
        assert descending == list(reversed(ascending))

    def test_sort_by_last_updated(self, db: MariaDBAdapter, config: DatabaseConfig) -> None:
        # Isolated with a dedicated search term rather than relying on the
        # seeded fixture's own relative timestamps: every seeded entity is
        # generated from the same timestamp sequence, so their last_updated_ts
        # values tie, which cannot support a deterministic order assertion.
        conn = _raw_connection(config)
        older, newer = "sort_test.older", "sort_test.newer"
        try:
            with conn.cursor() as cur:
                for entity_id, ts in ((older, 1_600_000_000.0), (newer, 1_700_000_000.0)):
                    cur.execute("INSERT INTO states_meta (entity_id) VALUES (%s)", (entity_id,))
                    metadata_id = cur.lastrowid
                    cur.execute(
                        "INSERT INTO states (metadata_id, state, last_updated_ts) "
                        "VALUES (%s, %s, %s)",
                        (metadata_id, "1.0", ts),
                    )

            found = [
                e.entity_id
                for e in db.list_entities(search="sort_test", sort="last_updated", sort_dir="desc")
            ]
            assert found == [newer, older]
            found_asc = [
                e.entity_id
                for e in db.list_entities(search="sort_test", sort="last_updated", sort_dir="asc")
            ]
            assert found_asc == [older, newer]
        finally:
            with conn.cursor() as cur:
                for entity_id in (older, newer):
                    cur.execute(
                        "DELETE s FROM states s JOIN states_meta sm "
                        "ON sm.metadata_id = s.metadata_id WHERE sm.entity_id = %s",
                        (entity_id,),
                    )
                    cur.execute("DELETE FROM states_meta WHERE entity_id = %s", (entity_id,))
            conn.close()

    def test_sort_by_corrections(self, db: MariaDBAdapter) -> None:
        state_id, original = _first_state_id(db, TEMPERATURE)
        db.apply_correction(
            entity_id=TEMPERATURE,
            state_id=state_id,
            expected_original=original,
            new_value="21.5",
            quality=Quality.SPIKE,
            note=None,
            created_by="pytest",
        )
        top = db.list_entities(sort="corrections", sort_dir="desc", limit=1)
        assert top[0].entity_id == TEMPERATURE
        assert top[0].correction_count == 1


class TestFetchStates:
    def test_returns_rows_in_time_order(self, db: MariaDBAdapter) -> None:
        entity = db.get_entity(TEMPERATURE)
        assert entity.last_updated_ts is not None
        points = db.fetch_states(TEMPERATURE, 0, entity.last_updated_ts + 1).points
        assert len(points) > 100
        assert [p.ts for p in points] == sorted(p.ts for p in points)

    def test_the_seeded_outlier_is_present(self, db: MariaDBAdapter) -> None:
        entity = db.get_entity(TEMPERATURE)
        assert entity.last_updated_ts is not None
        points = db.fetch_states(TEMPERATURE, 0, entity.last_updated_ts + 1).points
        values = [p.numeric_value for p in points if p.numeric_value is not None]
        assert min(values) == pytest.approx(-2000.0)

    def test_non_numeric_states_survive_as_none(self, db: MariaDBAdapter) -> None:
        entity = db.get_entity(FLAKY)
        assert entity.last_updated_ts is not None
        points = db.fetch_states(FLAKY, 0, entity.last_updated_ts + 1).points
        unparsed = [p for p in points if p.numeric_value is None]
        # The row is still returned — a user may well want to correct it.
        assert unparsed
        assert {p.value for p in unparsed} <= {"unknown", "unavailable"}

    def test_the_range_is_half_open(self, db: MariaDBAdapter) -> None:
        entity = db.get_entity(TEMPERATURE)
        assert entity.last_updated_ts is not None
        all_points = db.fetch_states(TEMPERATURE, 0, entity.last_updated_ts + 1).points
        first, second = all_points[0], all_points[1]
        window = db.fetch_states(TEMPERATURE, first.ts, second.ts).points
        assert [p.state_id for p in window] == [first.state_id]

    def test_limit_is_honoured(self, db: MariaDBAdapter) -> None:
        entity = db.get_entity(TEMPERATURE)
        assert entity.last_updated_ts is not None
        assert len(db.fetch_states(TEMPERATURE, 0, entity.last_updated_ts + 1, limit=7).points) == 7


class TestApplyCorrection:
    def test_correcting_a_value_to_itself_still_succeeds(self, db: MariaDBAdapter) -> None:
        # MySQL's UPDATE reports rows *changed*, not rows *matched*, unless
        # the connection sets CLIENT.FOUND_ROWS — without it, "corrected" to
        # the exact value already stored reports 0 affected rows, which
        # apply_correction's cur.rowcount != 1 check would misread as the
        # row having disappeared mid-transaction.
        state_id, original = _first_state_id(db, TEMPERATURE)
        correction = db.apply_correction(
            entity_id=TEMPERATURE,
            state_id=state_id,
            expected_original=original,
            new_value=original,
            quality=Quality.UNCERTAIN,
            note=None,
            created_by="pytest",
        )
        assert correction.corrected_value == original

    def test_writes_the_audit_row_and_updates_the_state(self, db: MariaDBAdapter) -> None:
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

    def test_the_corrected_point_is_marked_in_the_graph_data(self, db: MariaDBAdapter) -> None:
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
        entity = db.get_entity(TEMPERATURE)
        assert entity.last_updated_ts is not None
        marked = [
            p
            for p in db.fetch_states(TEMPERATURE, 0, entity.last_updated_ts + 1).points
            if p.correction_id is not None
        ]
        assert len(marked) == 1

    def test_the_entity_correction_count_reflects_it(self, db: MariaDBAdapter) -> None:
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

    def test_a_stale_expected_value_is_refused(self, db: MariaDBAdapter) -> None:
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

    def test_a_missing_state_row_is_not_found(self, db: MariaDBAdapter) -> None:
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

    def test_a_state_row_belonging_to_another_entity_is_refused(self, db: MariaDBAdapter) -> None:
        # Guards against a crafted request correcting one sensor's history
        # while claiming to be editing another's.
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

    def test_a_failing_update_rolls_the_audit_row_back(
        self, db: MariaDBAdapter, config: DatabaseConfig
    ) -> None:
        # The whole design rests on the audit row and the states UPDATE being
        # one transaction, audit first. To prove that, the UPDATE has to fail
        # *after* the INSERT has already succeeded — so a trigger rejects any
        # write to states for the duration of this test. An oversized value
        # would not do: corrected_value is VARCHAR(255) too, so it would fail
        # at the INSERT and prove nothing about the ordering.
        state_id, original = _first_state_id(db, TEMPERATURE)

        with _states_updates_blocked(config), pytest.raises(pymysql.Error):
            db.apply_correction(
                entity_id=TEMPERATURE,
                state_id=state_id,
                expected_original=original,
                new_value="21.5",
                quality=Quality.UNCERTAIN,
                note=None,
                created_by="pytest",
            )

        # No orphaned audit record, and the history is untouched.
        assert db.list_corrections() == []
        points = db.fetch_states(TEMPERATURE, 0, 1e12, limit=1).points
        assert points[0].value == original

    def test_a_row_held_by_another_writer_times_out_cleanly(
        self, db: MariaDBAdapter, config: DatabaseConfig
    ) -> None:
        # Standing in for Home Assistant's recorder holding the row. The
        # correction must fail as a LockTimeout the UI can explain, rather
        # than hanging the request (design document, 9.9).
        from hr_db import LockTimeout

        state_id, original = _first_state_id(db, TEMPERATURE)

        with _row_locked(config, state_id), pytest.raises(LockTimeout):
            db.apply_correction(
                entity_id=TEMPERATURE,
                state_id=state_id,
                expected_original=original,
                new_value="21.5",
                quality=Quality.UNCERTAIN,
                note=None,
                created_by="pytest",
            )

        assert db.list_corrections() == []

    def test_counter_sensors_are_not_blocked_at_the_adapter_layer(self, db: MariaDBAdapter) -> None:
        # The refusal lives in hr_corrections, deliberately: the adapter is a
        # mechanism, and the phase policy sits above it. This pins that split
        # so the Phase 2 cascade does not have to unpick an adapter-level ban.
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
    def _correct(self, db: MariaDBAdapter) -> tuple[int, str]:
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

    def test_puts_the_original_value_back(self, db: MariaDBAdapter) -> None:
        correction_id, original = self._correct(db)
        restored = db.restore_correction(correction_id, "pytest")

        assert restored.restored_at is not None
        assert restored.restored_by == "pytest"
        points = db.fetch_states(TEMPERATURE, 0, 1e12, limit=1).points
        assert points[0].value == original
        # The point is no longer marked, because the correction is inactive.
        assert points[0].correction_id is None

    def test_restoring_twice_is_refused(self, db: MariaDBAdapter) -> None:
        correction_id, _ = self._correct(db)
        db.restore_correction(correction_id, "pytest")
        with pytest.raises(ConcurrentModification, match="already been restored"):
            db.restore_correction(correction_id, "pytest")

    def test_refuses_when_the_row_changed_outside_the_addon(
        self, db: MariaDBAdapter, config: DatabaseConfig
    ) -> None:
        # The backup-restore case from the design document, 9.11: the states
        # row no longer holds what this add-on wrote, so putting the original
        # back would destroy whatever is there now.
        correction_id, _ = self._correct(db)
        correction = db.get_correction(correction_id)

        conn = pymysql.connect(
            host=config.host,
            port=config.port,
            user=config.user,
            password=config.password,
            database=config.name,
            autocommit=True,
        )
        try:
            with conn.cursor() as cur:
                cur.execute(
                    "UPDATE states SET state = '17.7' WHERE state_id = %s",
                    (correction.state_id,),
                )
        finally:
            conn.close()

        with pytest.raises(ConcurrentModification, match="changed outside"):
            db.restore_correction(correction_id, "pytest")

        points = db.fetch_states(TEMPERATURE, 0, 1e12, limit=1).points
        assert points[0].value == "17.7"

    def test_an_unknown_correction_is_not_found(self, db: MariaDBAdapter) -> None:
        with pytest.raises(NotFound):
            db.restore_correction(999_999_999, "pytest")

    def test_a_correction_with_no_recorded_state_id_cannot_be_restored(
        self, db: MariaDBAdapter, config: DatabaseConfig
    ) -> None:
        # Not reachable through the adapter's own writes today, but the
        # column is nullable and restore_correction must still refuse
        # cleanly rather than crash on a None where it expects an id.
        correction_id, _ = self._correct(db)
        state_id = db.get_correction(correction_id).state_id
        conn = _raw_connection(config)
        try:
            with conn.cursor() as cur:
                cur.execute(
                    "UPDATE state_corrections SET state_id = NULL WHERE id = %s",
                    (correction_id,),
                )
        finally:
            conn.close()

        try:
            with pytest.raises(NotFound, match="no states row recorded"):
                db.restore_correction(correction_id, "pytest")
        finally:
            # Put state_id back so the normal restore-based undo log (the
            # _undo_writes fixture) can find and revert this correction like
            # any other, rather than this test having to also restore the
            # states row's value by hand.
            conn = _raw_connection(config)
            try:
                with conn.cursor() as cur:
                    cur.execute(
                        "UPDATE state_corrections SET state_id = %s WHERE id = %s",
                        (state_id, correction_id),
                    )
            finally:
                conn.close()

    def test_a_purged_states_row_cannot_be_restored(
        self, db: MariaDBAdapter, config: DatabaseConfig
    ) -> None:
        correction_id, _ = self._correct(db)
        correction = db.get_correction(correction_id)

        conn = _raw_connection(config, autocommit=False)
        try:
            with conn.cursor() as cur:
                cur.execute("SELECT * FROM states WHERE state_id = %s", (correction.state_id,))
                original_row = cur.fetchone()
                assert original_row is not None
                # _correct() already applied its own correction before this
                # row was captured — restore the pre-correction value, not
                # the corrected one, or "undoing the purge" would leave the
                # row corrected forever.
                original_row["state"] = correction.original_value
                cur.execute("DELETE FROM states WHERE state_id = %s", (correction.state_id,))
            conn.commit()
        finally:
            conn.close()

        try:
            with pytest.raises(NotFound, match="purged by the recorder"):
                db.restore_correction(correction_id, "pytest")
        finally:
            # Re-insert the purged row exactly as it was, and drop the
            # correction directly — restore_correction can never succeed for
            # it now (it refers to a state_id AUTO_INCREMENT will not reuse),
            # so the normal undo log cannot clean this one up either.
            conn = _raw_connection(config, autocommit=False)
            try:
                with conn.cursor() as cur:
                    columns = ", ".join(original_row.keys())
                    placeholders = ", ".join(["%s"] * len(original_row))
                    cur.execute(
                        f"INSERT INTO states ({columns}) VALUES ({placeholders})",  # nosec B608
                        list(original_row.values()),
                    )
                    cur.execute("DELETE FROM state_corrections WHERE id = %s", (correction_id,))
                conn.commit()
            finally:
                conn.close()


class TestOrphanedCorrections:
    """Backup consistency (design document, 9.11), against real SQL.

    A restore reverts states without touching state_corrections, so
    find_orphaned_corrections has to notice a mismatch via a real JOIN, not
    just the in-memory comparison FakeAdapter uses for the web-layer tests.
    """

    def _correct(self, db: MariaDBAdapter) -> tuple[int, int, str]:
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
        self, db: MariaDBAdapter
    ) -> None:
        self._correct(db)
        assert db.find_orphaned_corrections() == []

    def test_a_backup_restore_is_detected_as_orphaned(
        self, db: MariaDBAdapter, config: DatabaseConfig
    ) -> None:
        correction_id, state_id, original = self._correct(db)
        conn = _raw_connection(config)
        try:
            with conn.cursor() as cur:
                cur.execute(
                    "UPDATE states SET state = %s WHERE state_id = %s", (original, state_id)
                )
        finally:
            conn.close()

        orphaned = db.find_orphaned_corrections()
        assert [c.id for c in orphaned] == [correction_id]

    def test_a_restored_correction_is_never_orphaned(self, db: MariaDBAdapter) -> None:
        correction_id, _, _ = self._correct(db)
        db.restore_correction(correction_id, "pytest")
        assert db.find_orphaned_corrections() == []

    def test_dismissing_stops_it_reappearing(
        self, db: MariaDBAdapter, config: DatabaseConfig
    ) -> None:
        correction_id, state_id, original = self._correct(db)
        conn = _raw_connection(config)
        try:
            with conn.cursor() as cur:
                cur.execute(
                    "UPDATE states SET state = %s WHERE state_id = %s", (original, state_id)
                )
        finally:
            conn.close()

        dismissed = db.dismiss_correction(correction_id, "pytest")
        assert dismissed.dismissed_at is not None
        assert dismissed.dismissed_by == "pytest"
        assert db.find_orphaned_corrections() == []

    def test_dismissing_twice_is_refused(self, db: MariaDBAdapter, config: DatabaseConfig) -> None:
        correction_id, state_id, original = self._correct(db)
        conn = _raw_connection(config)
        try:
            with conn.cursor() as cur:
                cur.execute(
                    "UPDATE states SET state = %s WHERE state_id = %s", (original, state_id)
                )
        finally:
            conn.close()
        db.dismiss_correction(correction_id, "pytest")
        with pytest.raises(ConcurrentModification):
            db.dismiss_correction(correction_id, "pytest")

    def test_dismissing_a_restored_correction_is_refused(self, db: MariaDBAdapter) -> None:
        correction_id, _, _ = self._correct(db)
        db.restore_correction(correction_id, "pytest")
        with pytest.raises(ConcurrentModification):
            db.dismiss_correction(correction_id, "pytest")

    def test_dismissing_an_unknown_correction_is_not_found(self, db: MariaDBAdapter) -> None:
        with pytest.raises(NotFound):
            db.dismiss_correction(999_999_999, "pytest")


class TestAuditTrail:
    def test_lists_newest_first(self, db: MariaDBAdapter) -> None:
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

    def test_filters_by_entity(self, db: MariaDBAdapter) -> None:
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

    def test_can_hide_restored_corrections(self, db: MariaDBAdapter) -> None:
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

    def test_a_note_round_trips_through_the_database(self, db: MariaDBAdapter) -> None:
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


class TestAuditTableShape:
    def test_reserves_the_statistics_columns_for_the_next_phase(
        self, db: MariaDBAdapter, config: DatabaseConfig
    ) -> None:
        conn = pymysql.connect(
            host=config.host,
            port=config.port,
            user=config.user,
            password=config.password,
            database=config.name,
            cursorclass=DictCursor,
        )
        try:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT column_name AS c FROM information_schema.columns "
                    "WHERE table_schema = %s AND table_name = 'state_corrections'",
                    (config.name,),
                )
                columns = {row["c"].lower() for row in cur.fetchall()}
        finally:
            conn.close()

        assert {"orig_sst_mean", "orig_stat_sum", "sum_delta_applied"} <= columns
        assert "stats_corrected" in columns


class TestBeforeOnboarding:
    """Every read path has to work before state_corrections exists.

    The audit table is created during onboarding, so any of these queries can
    run against a database that has never seen it — a fresh install being
    browsed for the first time, or this add-on pointed at a database purely
    to look. Each query catches the missing-table pymysql.Error and behaves
    as if nothing has ever been corrected.
    """

    @pytest.fixture
    def without_audit_table(self, config: DatabaseConfig) -> Iterator[MariaDBAdapter]:
        conn = _raw_connection(config)
        try:
            with conn.cursor() as cur:
                cur.execute("DROP TABLE IF EXISTS state_corrections")
        finally:
            conn.close()
        adapter = MariaDBAdapter(config)
        try:
            yield adapter
        finally:
            adapter.ensure_audit_table()

    def test_list_entities_works_and_reports_no_corrections(
        self, without_audit_table: MariaDBAdapter
    ) -> None:
        entities = without_audit_table.list_entities(limit=100)
        assert entities
        assert all(e.correction_count == 0 for e in entities)

    def test_get_entity_works(self, without_audit_table: MariaDBAdapter) -> None:
        assert without_audit_table.get_entity(TEMPERATURE).correction_count == 0

    def test_fetch_states_works_with_nothing_marked(
        self, without_audit_table: MariaDBAdapter
    ) -> None:
        points = without_audit_table.fetch_states(TEMPERATURE, 0, 2_000_000_000, limit=50).points
        assert points
        assert all(p.correction_id is None for p in points)

    def test_bulk_preview_works(self, without_audit_table: MariaDBAdapter) -> None:
        preview = without_audit_table.bulk_correction_preview(TEMPERATURE, 0, 2_000_000_000)
        assert preview.total > 0
        assert preview.already_corrected == 0

    def test_health_reports_the_table_as_missing(self, without_audit_table: MariaDBAdapter) -> None:
        report = without_audit_table.check_health()
        assert report.connected
        assert not report.audit_table_ready
        assert report.schema_version is not None


class TestUnknownSensorTypesAreReadable:
    def test_an_entity_without_statistics_still_lists_and_graphs(self, db: MariaDBAdapter) -> None:
        # binary_sensor.front_door has no statistics_meta row. It must not
        # break the browser or the graph — its non-numeric value is what
        # makes it not correctable, not its UNKNOWN sensor type, which is
        # states-only correctable like any other entity.
        entity = db.get_entity(DOOR)
        assert entity.sensor_type is SensorType.UNKNOWN
        assert entity.last_updated_ts is not None
        points = db.fetch_states(DOOR, 0, entity.last_updated_ts + 1, limit=10).points
        assert points
        assert all(p.numeric_value is None for p in points)


class TestSchemaIntrospection:
    def test_detects_the_statistics_meta_columns_this_schema_has(self, db: MariaDBAdapter) -> None:
        columns = db._statistics_meta_columns()
        assert "has_sum" in columns
        # Schema 48+ has both; has_mean is deprecated from HA 2026.11, and the
        # adapter falls back to mean_type when it disappears.
        assert "has_mean" in columns or "mean_type" in columns

    def test_the_generated_fragment_is_valid_sql(self, db: MariaDBAdapter) -> None:
        # It reaches the database inside list_entities, so a syntax error here
        # would surface as a query failure rather than a silent wrong answer.
        assert db.list_entities(limit=1)


class TestTruncation:
    """A window holding more rows than the limit must say so.

    Found by benchmarking against five million rows: a 30-day range on a
    sensor reporting every 30 seconds returned exactly the limit, silently
    dropping the newest data. A user hunting an outlier would have seen a
    clean graph and concluded the sensor was fine.
    """

    def test_reports_truncation_when_the_window_holds_more(self, db: MariaDBAdapter) -> None:
        entity = db.get_entity(TEMPERATURE)
        assert entity.last_updated_ts is not None
        series = db.fetch_states(TEMPERATURE, 0, entity.last_updated_ts + 1, limit=10)

        assert series.truncated is True
        assert len(series.points) == 10
        assert series.limit == 10

    def test_reports_no_truncation_when_everything_fits(self, db: MariaDBAdapter) -> None:
        entity = db.get_entity(TEMPERATURE)
        assert entity.last_updated_ts is not None
        series = db.fetch_states(TEMPERATURE, 0, entity.last_updated_ts + 1, limit=100_000)

        assert series.truncated is False
        assert len(series.points) > 100

    def test_the_extra_probe_row_is_never_returned(self, db: MariaDBAdapter) -> None:
        # The adapter asks for limit + 1 rows to detect truncation. That extra
        # row must be discarded, or every truncated graph would show one point
        # beyond what it claims.
        entity = db.get_entity(TEMPERATURE)
        assert entity.last_updated_ts is not None
        for limit in (1, 2, 25):
            series = db.fetch_states(TEMPERATURE, 0, entity.last_updated_ts + 1, limit=limit)
            assert len(series.points) == limit


class TestSensorTypeOnModernSchemas:
    """Regression: has_mean is NULL on Home Assistant 2025.11 and later.

    The column still exists in the schema, but HA stopped writing it once
    mean_type took over. Preferring has_mean therefore classified every
    measurement sensor as UNKNOWN — and UNKNOWN sensors are refused
    correction, so the add-on would have listed a user's whole system and
    declined to fix any of it. Hand-seeded fixtures hid this by setting
    has_mean themselves; only a real recorder exposed it.
    """

    def test_the_fixture_reproduces_what_home_assistant_actually_writes(
        self, db: MariaDBAdapter, config: DatabaseConfig
    ) -> None:
        conn = _raw_connection(config)
        try:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT statistic_id, has_mean, mean_type FROM statistics_meta "
                    "WHERE statistic_id = %s",
                    (TEMPERATURE,),
                )
                row = cur.fetchone()
        finally:
            conn.close()

        assert row is not None
        assert row["has_mean"] is None, (
            "the seed must leave has_mean NULL as a current Home Assistant does, "
            "or this regression cannot be caught"
        )
        assert row["mean_type"] == 1

    def test_a_measurement_sensor_is_still_classified_correctly(self, db: MariaDBAdapter) -> None:
        assert db.get_entity(TEMPERATURE).sensor_type is SensorType.MEASUREMENT

    def test_a_counter_is_still_classified_correctly(self, db: MariaDBAdapter) -> None:
        assert db.get_entity(ENERGY).sensor_type is SensorType.COUNTER

    def test_measurement_sensors_remain_correctable(self, db: MariaDBAdapter) -> None:
        # The user-visible consequence of the bug: the correction is refused
        # at the service layer because the sensor type cannot be confirmed.
        import hr_corrections

        hr_corrections.validate_sensor_type(db.get_entity(TEMPERATURE).sensor_type)


class TestStatisticsCorrection:
    """Correcting a state must leave the statistics tables consistent with it.

    This is the whole point of the statistics phase. Before it, a correction
    fixed the history graph and left the statistics graph and the energy
    dashboard showing the outlier — the exact failure mode the design document
    criticises phpMyAdmin for.
    """

    def _outlier(self, db: MariaDBAdapter) -> tuple[int, str, float]:
        """The seeded -2000 spike: its row id, value, and timestamp."""
        entity = db.get_entity(TEMPERATURE)
        assert entity.last_updated_ts is not None
        points = db.fetch_states(TEMPERATURE, 0, entity.last_updated_ts + 1).points
        worst = min(
            (p for p in points if p.numeric_value is not None),
            key=lambda p: p.numeric_value,  # type: ignore[arg-type,return-value]
        )
        assert worst.value is not None
        return worst.state_id, worst.value, worst.ts

    def test_the_short_term_bucket_follows_the_correction(self, db: MariaDBAdapter) -> None:
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
        # The spike is gone from the aggregate, not merely from the raw row.
        assert float(after[0].min) > -100.0
        assert after[0].mean is not None and float(after[0].mean) > -100.0

    def test_the_hourly_bucket_follows_too(self, db: MariaDBAdapter) -> None:
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

    def test_the_audit_row_records_the_previous_statistics(self, db: MariaDBAdapter) -> None:
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

    def test_restore_puts_the_statistics_back_as_they_were(self, db: MariaDBAdapter) -> None:
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
        # Byte-for-byte what was there, because restore writes the recorded
        # originals back rather than recomputing them.
        assert float(after.mean) == pytest.approx(float(before.mean))  # type: ignore[arg-type]
        assert float(after.min) == pytest.approx(float(before.min))  # type: ignore[arg-type]
        assert float(after.max) == pytest.approx(float(before.max))  # type: ignore[arg-type]

    def test_a_correction_without_recomputation_leaves_statistics_alone(
        self, db: MariaDBAdapter
    ) -> None:
        # The Phase 1 behaviour is still reachable, and still flagged, so a
        # later backfill can find those corrections.
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
            recompute_statistics=False,
        )

        after = db.fetch_statistics(meta.id, start, start + SHORT_TERM_SECONDS, short_term=True)[0]
        assert correction.stats_corrected is False
        assert float(after.min) == pytest.approx(float(before.min))  # type: ignore[arg-type]


class TestCounterCascade:
    """Correcting an energy meter, and the running totals that follow it.

    The seeded counter carries a spike partway through its history. Every later
    running total is inflated by it, which is precisely the damage the design
    document calls the most severe case for the energy dashboard.
    """

    def _spike(self, db: MariaDBAdapter) -> tuple[int, str, float]:
        entity = db.get_entity(ENERGY)
        assert entity.last_updated_ts is not None
        points = db.fetch_states(ENERGY, 0, entity.last_updated_ts + 1).points
        worst = max(
            (p for p in points if p.numeric_value is not None),
            key=lambda p: p.numeric_value,  # type: ignore[arg-type,return-value]
        )
        assert worst.value is not None
        return worst.state_id, worst.value, worst.ts

    def test_the_seeded_counter_has_an_inflated_total(self, db: MariaDBAdapter) -> None:
        _, value, _ = self._spike(db)
        assert float(value) == pytest.approx(99999.0)

    def test_correcting_the_spike_rewrites_every_later_total(self, db: MariaDBAdapter) -> None:
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
        # The inflation is gone from the final total, not just from the bucket
        # the spike was in.
        assert final_after < final_before

    def test_the_audit_row_records_the_cascade_scope(self, db: MariaDBAdapter) -> None:
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
        assert correction.sensor_type is SensorType.COUNTER

    def test_the_scope_is_knowable_before_committing(self, db: MariaDBAdapter) -> None:
        # The user has to be told how far a correction reaches before making
        # it, because an old energy reading can carry through years of totals.
        _, _, ts = self._spike(db)
        scope = db.counter_cascade_scope(ENERGY, ts)
        assert scope["hourly"] > 0
        assert scope["short_term"] > 0

    def test_restore_rebuilds_the_original_chain(self, db: MariaDBAdapter) -> None:
        from hr_statistics import HOURLY_SECONDS, bucket_start

        state_id, original, ts = self._spike(db)
        meta = db.get_statistics_metadata(ENERGY)
        assert meta is not None
        hour = bucket_start(ts, HOURLY_SECONDS)

        before = [
            (r.start_ts, None if r.sum is None else float(r.sum))
            for r in db.fetch_statistics(meta.id, hour, 2_000_000_000)
        ]

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
        db.restore_correction(correction.id, "pytest")

        after = [
            (r.start_ts, None if r.sum is None else float(r.sum))
            for r in db.fetch_statistics(meta.id, hour, 2_000_000_000)
        ]
        # Restoring a counter recomputes rather than replaying stored values,
        # so this asserts the recomputation is faithful over the whole chain.
        assert len(after) == len(before)
        for (_, was), (_, now) in zip(before, after, strict=True):
            if was is None or now is None:
                assert was == now
            else:
                assert now == pytest.approx(was, abs=1e-6)

    def test_the_service_layer_now_allows_counters(self, db: MariaDBAdapter) -> None:
        import hr_corrections

        state_id, original, _ = self._spike(db)
        correction = hr_corrections.apply(
            db,
            entity_id=ENERGY,
            state_id=state_id,
            expected_original=original,
            new_value="1100.0",
            quality="spike",
            note="meter glitch",
            created_by="pytest",
        )
        assert correction.stats_corrected is True

    def test_a_hour_with_no_surviving_short_term_rows_falls_back_to_its_own_chain(
        self, db: MariaDBAdapter, config: DatabaseConfig
    ) -> None:
        """Home Assistant purges statistics_short_term after ~10 days but
        keeps the hourly `statistics` table forever, so an old correction can
        land in an hour with no surviving 5-minute rows at all. This is what
        proves the fallback SQL in _recompute_counter_statistics — rebuilding
        the hourly chain from itself rather than from statistics_short_term —
        actually works against real MariaDB."""
        from hr_statistics import HOURLY_SECONDS

        entity_id = "sensor.bulk_it_purged_hour_fallback_mariadb"
        hour1 = 1_780_002_000.0  # aligned to both a 5-minute and an hour boundary
        readings = [
            (hour1, "100.0"),
            (hour1 + 300, "110.0"),
            (hour1 + 600, "120.0"),
            (hour1 + HOURLY_SECONDS, "200.0"),
            (hour1 + HOURLY_SECONDS + 300, "210.0"),
        ]
        _create_bulk_test_entity(config, entity_id, readings, has_sum=True)
        try:
            conn = _raw_connection(config)
            try:
                with conn.cursor() as cur:
                    cur.execute(
                        "SELECT id FROM statistics_meta WHERE statistic_id = %s", (entity_id,)
                    )
                    stats_metadata_id = cur.fetchone()["id"]
                    cur.execute(
                        "DELETE FROM statistics_short_term WHERE metadata_id = %s "
                        "AND start_ts >= %s AND start_ts < %s",
                        (stats_metadata_id, hour1, hour1 + HOURLY_SECONDS),
                    )
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
            assert hourly[0].state is not None
            assert float(hourly[0].sum) == pytest.approx(0.0)
        finally:
            _delete_bulk_test_entity(config, entity_id)


def _create_bulk_test_entity(
    config: DatabaseConfig,
    entity_id: str,
    readings: list[tuple[float, str]],
    *,
    mean_type: int = 1,
    has_sum: bool = False,
) -> None:
    """Insert a dedicated entity with exact readings, for a bulk-correction test.

    The seeded 7-day fixture is a random walk, which is wrong for asserting a
    precise interpolated value: this needs anchors and gaps whose numbers are
    chosen, not sampled. `entity_id` should be unique per test — tests in this
    class run against the same real database, and this leaves rows behind for
    _delete_bulk_test_entity to clean up rather than trying to reuse them.

    Statistics rows are built from hr_statistics — the same functions the
    application uses, verified against a live recorder — rather than a fresh
    approximation. A fixture that computes statistics its own way has already
    hidden three real bugs in this project; see ARCHITECTURE.md.
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

    conn = _raw_connection(config)
    try:
        with conn.cursor() as cur:
            cur.execute("INSERT INTO states_meta (entity_id) VALUES (%s)", (entity_id,))
            metadata_id = cur.lastrowid
            cur.execute(
                "INSERT INTO statistics_meta "
                "(statistic_id, source, has_sum, mean_type, unit_of_measurement) "
                "VALUES (%s, 'recorder', %s, %s, %s)",
                (entity_id, int(has_sum), mean_type, "°C"),
            )
            stats_metadata_id = cur.lastrowid
            cur.executemany(
                "INSERT INTO states (metadata_id, state, last_updated_ts, last_reported_ts) "
                "VALUES (%s, %s, %s, %s)",
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
                    "VALUES (%s, %s, %s, %s, %s)",
                    [(b.start_ts, stats_metadata_id, b.start_ts, b.state, b.sum) for b in chained],
                )
                hourly: dict[float, CounterBucket] = {}
                for bucket in chained:
                    hourly[bucket_start(bucket.start_ts, HOURLY_SECONDS)] = bucket
                cur.executemany(
                    "INSERT INTO statistics (created_ts, metadata_id, start_ts, state, sum) "
                    "VALUES (%s, %s, %s, %s, %s)",
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
                    "VALUES (%s, %s, %s, %s, %s, %s)",
                    [
                        (b.start_ts, stats_metadata_id, b.start_ts, b.mean, b.min, b.max)
                        for b in short_term_buckets
                    ],
                )
                by_hour: dict[float, list[Any]] = {}
                for bucket in short_term_buckets:
                    by_hour.setdefault(bucket_start(bucket.start_ts, HOURLY_SECONDS), []).append(
                        bucket
                    )
                hourly_summaries = [
                    summarise_hourly(buckets_in_hour, hour_start)
                    for hour_start, buckets_in_hour in sorted(by_hour.items())
                ]
                cur.executemany(
                    "INSERT INTO statistics (created_ts, metadata_id, start_ts, mean, min, max) "
                    "VALUES (%s, %s, %s, %s, %s, %s)",
                    [
                        (b.start_ts, stats_metadata_id, b.start_ts, b.mean, b.min, b.max)
                        for b in hourly_summaries
                    ],
                )
        conn.commit()
    finally:
        conn.close()


def _delete_bulk_test_entity(config: DatabaseConfig, entity_id: str) -> None:
    conn = _raw_connection(config)
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT metadata_id FROM states_meta WHERE entity_id = %s", (entity_id,))
            row = cur.fetchone()
            if row:
                metadata_id = row["metadata_id"]
                cur.execute("DELETE FROM states WHERE metadata_id = %s", (metadata_id,))
                cur.execute("DELETE FROM states_meta WHERE metadata_id = %s", (metadata_id,))
            cur.execute("SELECT id FROM statistics_meta WHERE statistic_id = %s", (entity_id,))
            row = cur.fetchone()
            if row:
                stats_id = row["id"]
                cur.execute("DELETE FROM statistics WHERE metadata_id = %s", (stats_id,))
                cur.execute("DELETE FROM statistics_short_term WHERE metadata_id = %s", (stats_id,))
                cur.execute("DELETE FROM statistics_meta WHERE id = %s", (stats_id,))
            cur.execute("DELETE FROM state_corrections WHERE entity_id = %s", (entity_id,))
        conn.commit()
    finally:
        conn.close()


class TestBulkCorrectionIntegration:
    """The SQL behind bulk correction, against a real MariaDB.

    Each test gets its own entity with exact, chosen readings — the seeded
    7-day fixture is a random walk, wrong for asserting a precise interpolated
    number.
    """

    # 1_780_002_000.0 is exactly on an hour boundary; +600 keeps every reading
    # in this fixture (spanning up to 1200s further) inside that same hour,
    # so the hourly-bucket assertions don't have to account for a split.
    BASE = 1_780_002_600.0

    @pytest.fixture
    def bulk_entity(self, config: DatabaseConfig, request: Any) -> Iterator[str]:
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
        self, db: MariaDBAdapter, bulk_entity: str
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

    def test_interpolate_strategy_ramps_linearly(
        self, db: MariaDBAdapter, bulk_entity: str
    ) -> None:
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

    def test_every_row_gets_its_own_audit_record(
        self, db: MariaDBAdapter, bulk_entity: str
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
        corrections = db.list_corrections(entity_id=bulk_entity)
        assert len(corrections) == 3
        assert {c.id for c in corrections} == set(result.correction_ids)
        assert all(c.original_value == "-999.0" for c in corrections)
        assert all(c.note == "wifi outage" for c in corrections)

    def test_statistics_are_corrected_for_every_row(
        self, db: MariaDBAdapter, bulk_entity: str
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

        # Every bucket the bad readings touched now reflects the correction —
        # the whole point of the exercise.
        hourly = db.fetch_statistics(meta.id, 0, 2_000_000_000)
        assert hourly
        assert all(r.min is None or float(r.min) > -100.0 for r in hourly)

    def test_a_row_cap_violation_leaves_the_database_untouched(
        self, db: MariaDBAdapter, bulk_entity: str, monkeypatch: Any
    ) -> None:
        import hr_db

        monkeypatch.setattr(hr_db, "MAX_BULK_ROWS", 2)
        import hr_mariadb

        monkeypatch.setattr(hr_mariadb, "MAX_BULK_ROWS", 2)

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
        self, db: MariaDBAdapter, bulk_entity: str
    ) -> None:
        from hr_db import InvalidRange

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
        self, db: MariaDBAdapter, bulk_entity: str
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
        self, db: MariaDBAdapter, config: DatabaseConfig
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
            # The spike's inflated total is gone from the final running sum.
            assert hourly[-1].sum is not None
            assert float(hourly[-1].sum) < 1000.0
        finally:
            _delete_bulk_test_entity(config, entity_id)
