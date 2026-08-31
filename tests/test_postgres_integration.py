"""Integration tests: the real PostgresAdapter against a real PostgreSQL server.

Everything else in the suite runs against FakeAdapter, which reimplements the
adapter's rules rather than its SQL. These tests are the only ones that prove
the SQL itself parses, uses the right column names, and transacts correctly —
and for PostgreSQL specifically, that a set of dialect differences from the
MariaDB original were each handled rather than inherited by accident.

They are skipped unless HR_PG_TEST_DSN points at a disposable database:

    ./dev/postgres.sh start
    .venv-ha-schema/bin/python dev/build_schema.py \\
      --dsn postgresql+psycopg://hatest:hatest@127.0.0.1:5442/ha_test
    .venv-check/bin/python dev/seed_recorder.py \\
      --postgres-dsn postgresql://hatest:hatest@127.0.0.1:5442/ha_test
    HR_PG_TEST_DSN=postgresql://hatest:hatest@127.0.0.1:5442/ha_test \\
      .venv-check/bin/python -m pytest tests/test_postgres_integration.py

Never point HR_PG_TEST_DSN at a live recorder database. These tests write to
states.
"""

from __future__ import annotations

import contextlib
import os
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import replace
from typing import Any
from urllib.parse import urlparse

import pytest

from hr_config import DatabaseConfig
from hr_db import ConcurrentModification, InvalidRange, LockTimeout, NotFound
from hr_models import Quality, SensorType

psycopg = pytest.importorskip("psycopg")
from psycopg.rows import dict_row

from hr_postgres import PostgresAdapter

TEMPERATURE = "sensor.living_room_temperature"
ENERGY = "sensor.energy_total"
FLAKY = "sensor.flaky_pressure"
DOOR = "binary_sensor.front_door"

_DSN = os.environ.get("HR_PG_TEST_DSN")

pytestmark = pytest.mark.skipif(
    not _DSN, reason="HR_PG_TEST_DSN is not set; see this module's docstring"
)

# Names this suite creates inside the server, made unique per process rather
# than merely per xdist worker: two pytest runs started at once would
# otherwise collide, which is exactly the kind of shared-name race that makes
# a parallel suite intermittently red.
_RUN_TAG = f"{os.environ.get('PYTEST_XDIST_WORKER', 'solo')}_{os.getpid()}"
_BLOCK_FN = f"hr_block_fn_{os.getpid()}"
_BLOCK_TRIGGER = f"hr_block_states_update_{os.getpid()}"

# Databases this suite is willing to write to. It UPDATEs states and DELETEs
# state_corrections, so pointing it at a real recorder would damage real
# history.
_ALLOWED_DB_PREFIX = "ha_test"


def _database_config() -> DatabaseConfig:
    parsed = urlparse(_DSN or "")
    return DatabaseConfig(
        host=parsed.hostname or "127.0.0.1",
        port=parsed.port or 5432,
        name=(parsed.path or "/").lstrip("/"),
        user=parsed.username or "",
        password=parsed.password or "",
    )


def _assert_disposable(config: DatabaseConfig) -> None:
    if not config.name.startswith(_ALLOWED_DB_PREFIX):
        raise AssertionError(
            f"HR_PG_TEST_DSN points at database '{config.name}', which is not a "
            f"disposable test database (expected a name starting with "
            f"'{_ALLOWED_DB_PREFIX}'). These tests write to states and would "
            "damage a real Home Assistant recorder database. Refusing to run."
        )


def _maintenance_connection(base: DatabaseConfig) -> Any:
    """A connection to the server's own 'postgres' database.

    CREATE DATABASE and DROP DATABASE cannot run inside a transaction block,
    and DROP cannot target the database the connection is using — so both go
    through the maintenance database in autocommit mode.
    """
    return psycopg.connect(
        host=base.host,
        port=base.port,
        user=base.user,
        password=base.password,
        dbname="postgres",
        autocommit=True,
    )


def _clone_database(base: DatabaseConfig, target_name: str) -> None:
    """Copy the seeded recorder data into a private database for this worker.

    CREATE DATABASE ... TEMPLATE is PostgreSQL's own whole-database copy, so
    unlike the MariaDB suite there is no table-by-table loop here. It does
    require that nothing else is connected to the template, hence the
    pg_terminate_backend first — safe because _assert_disposable has already
    established this is a throwaway database.

    state_corrections is dropped afterwards rather than skipped during the
    copy: TEMPLATE is all-or-nothing. It belongs to the add-on rather than to
    the recorder, and anything left in the base database — a correction made
    while clicking through the UI against the same server — would otherwise be
    cloned into every worker and break the tests that count corrections.
    """
    conn = _maintenance_connection(base)
    try:
        conn.execute(
            "SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
            "WHERE datname = %s AND pid <> pg_backend_pid()",
            (base.name,),
        )
        conn.execute(f'DROP DATABASE IF EXISTS "{target_name}"')  # nosec B608
        conn.execute(f'CREATE DATABASE "{target_name}" TEMPLATE "{base.name}"')  # nosec B608
    finally:
        conn.close()

    clone = psycopg.connect(
        host=base.host,
        port=base.port,
        user=base.user,
        password=base.password,
        dbname=target_name,
        autocommit=True,
    )
    try:
        clone.execute("DROP TABLE IF EXISTS state_corrections")
    finally:
        clone.close()


@pytest.fixture(scope="session")
def config() -> Iterator[DatabaseConfig]:
    """Connection details for this worker's own copy of the seeded database.

    Under pytest-xdist every worker gets its own clone, because the cleanup
    fixture below reverts and clears *all* corrections — several workers
    sharing one database would delete each other's rows mid-test.
    """
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
        conn = _maintenance_connection(base)
        try:
            conn.execute(
                "SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
                "WHERE datname = %s AND pid <> pg_backend_pid()",
                (target,),
            )
            conn.execute(f'DROP DATABASE IF EXISTS "{target}"')  # nosec B608
        finally:
            conn.close()


@pytest.fixture(scope="session")
def db(config: DatabaseConfig) -> PostgresAdapter:
    adapter = PostgresAdapter(config)
    adapter.ensure_audit_table()

    # Start from an empty audit trail. Clones drop state_corrections, but a
    # serial run (-n0) uses the base database directly, where a correction may
    # survive from a UI session against the same server.
    conn = _raw_connection(config)
    try:
        conn.execute("DELETE FROM state_corrections")
    finally:
        conn.close()

    return adapter


@pytest.fixture(autouse=True)
def _undo_writes(db: PostgresAdapter, config: DatabaseConfig) -> Iterator[None]:
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

    # Corrections also rewrite the statistics tables, so undoing them by hand
    # is not enough — restore_correction is the only thing that knows how to
    # put both back. Anything it refuses (a test that changed the row behind
    # the add-on's back on purpose) falls through to the manual revert below.
    for correction in db.list_corrections(include_restored=False):
        with contextlib.suppress(ConcurrentModification, NotFound):
            db.restore_correction(correction.id, "test-cleanup")

    conn = _raw_connection(config, autocommit=False)
    try:
        cur = conn.cursor()
        cur.execute(
            "SELECT state_id, orig_state_value FROM state_corrections WHERE state_id IS NOT NULL"
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
    return psycopg.connect(
        host=config.host,
        port=config.port,
        user=config.user,
        password=config.password,
        dbname=config.name,
        row_factory=dict_row,
        autocommit=autocommit,
    )


@contextmanager
def _states_updates_blocked(config: DatabaseConfig) -> Iterator[None]:
    """Make every UPDATE on states fail, for the body of the with-block.

    PostgreSQL has no MySQL-style inline SIGNAL, so the trigger calls a
    plpgsql function that raises instead.
    """
    conn = _raw_connection(config)
    try:
        conn.execute(
            f"CREATE OR REPLACE FUNCTION {_BLOCK_FN}() RETURNS trigger AS $$ "  # nosec B608
            "BEGIN RAISE EXCEPTION 'blocked by integration test'; END; "
            "$$ LANGUAGE plpgsql"
        )
        conn.execute(f"DROP TRIGGER IF EXISTS {_BLOCK_TRIGGER} ON states")  # nosec B608
        conn.execute(
            f"CREATE TRIGGER {_BLOCK_TRIGGER} BEFORE UPDATE ON states "  # nosec B608
            f"FOR EACH ROW EXECUTE FUNCTION {_BLOCK_FN}()"
        )
        yield
    finally:
        conn.execute(f"DROP TRIGGER IF EXISTS {_BLOCK_TRIGGER} ON states")  # nosec B608
        conn.execute(f"DROP FUNCTION IF EXISTS {_BLOCK_FN}()")  # nosec B608
        conn.close()


@contextmanager
def _row_locked(config: DatabaseConfig, state_id: int) -> Iterator[None]:
    """Hold a write lock on one states row, as the recorder would."""
    conn = _raw_connection(config, autocommit=False)
    try:
        conn.execute("SELECT state_id FROM states WHERE state_id = %s FOR UPDATE", (state_id,))
        yield
    finally:
        conn.rollback()
        conn.close()


def _first_state_id(db: PostgresAdapter, entity_id: str) -> tuple[int, str]:
    """A real state row from the seeded history, with its current value."""
    entity = db.get_entity(entity_id)
    assert entity.last_updated_ts is not None
    points = db.fetch_states(entity_id, 0, entity.last_updated_ts + 1, limit=5).points
    assert points, f"no seeded states for {entity_id}"
    point = points[0]
    assert point.value is not None
    return point.state_id, point.value


class TestHealth:
    def test_connects_and_reads_a_supported_schema(self, db: PostgresAdapter) -> None:
        report = db.check_health()
        assert report.connected
        assert report.schema_version is not None
        assert report.schema_version >= 28
        assert report.server_version is not None
        assert report.server_version.startswith("PostgreSQL ")
        assert not report.errors

    def test_detects_the_update_privilege(self, db: PostgresAdapter) -> None:
        # has_table_privilege answers this directly, where the MariaDB adapter
        # has to parse a SHOW GRANTS listing.
        assert db.check_health().can_update

    def test_reports_the_audit_table_once_created(self, db: PostgresAdapter) -> None:
        assert db.check_health().audit_table_ready

    def test_ensure_audit_table_is_idempotent(self, db: PostgresAdapter) -> None:
        db.ensure_audit_table()
        db.ensure_audit_table()
        assert db.check_health().audit_table_ready

    def test_a_wrong_password_fails_cleanly(self, config: DatabaseConfig) -> None:
        adapter = PostgresAdapter(replace(config, password="definitely-not-it"))
        report = adapter.check_health()
        assert not report.connected
        assert report.errors

    def test_a_schema_older_than_supported_is_refused(self, config: DatabaseConfig) -> None:
        conn = _raw_connection(config)
        try:
            with conn.cursor() as cur:
                cur.execute("SELECT MAX(schema_version) AS v FROM schema_changes")
                real_version = cur.fetchone()["v"]
                cur.execute("UPDATE schema_changes SET schema_version = 20")
        finally:
            conn.close()

        try:
            report = PostgresAdapter(config).check_health()
            assert report.connected
            assert not report.ok
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
        self, config: DatabaseConfig
    ) -> None:
        admin = _raw_connection(config)
        role = "hr_readonly_check"
        try:
            with admin.cursor() as cur:
                cur.execute(f"DROP ROLE IF EXISTS {role}")  # nosec B608
                cur.execute(
                    f"CREATE ROLE {role} LOGIN PASSWORD 'readonly-pw'"  # nosec B608
                )
                cur.execute(f"GRANT CONNECT ON DATABASE {config.name} TO {role}")  # nosec B608
                cur.execute(f"GRANT USAGE ON SCHEMA public TO {role}")  # nosec B608
                cur.execute(f"GRANT SELECT ON states TO {role}")  # nosec B608

            readonly_config = replace(config, user=role, password="readonly-pw")
            report = PostgresAdapter(readonly_config).check_health()
            assert report.connected
            assert report.can_update is False
            assert not report.ok
        finally:
            with admin.cursor() as cur:
                cur.execute(f"DROP OWNED BY {role}")  # nosec B608
                cur.execute(f"DROP ROLE IF EXISTS {role}")  # nosec B608
            admin.close()


class TestEntityBrowser:
    def test_lists_the_seeded_entities(self, db: PostgresAdapter) -> None:
        ids = {e.entity_id for e in db.list_entities(limit=100)}
        assert {TEMPERATURE, ENERGY, FLAKY, DOOR} <= ids

    def test_classifies_sensor_types_from_statistics_meta(self, db: PostgresAdapter) -> None:
        # The real point of this test on PostgreSQL: has_mean and has_sum are
        # genuine BOOLEAN columns here, so the sensor-type SQL cannot use the
        # MariaDB adapter's `= 1` integer comparisons.
        assert db.get_entity(TEMPERATURE).sensor_type is SensorType.MEASUREMENT
        assert db.get_entity(ENERGY).sensor_type is SensorType.COUNTER
        assert db.get_entity(DOOR).sensor_type is SensorType.UNKNOWN

    def test_extracts_the_friendly_name_from_the_attributes_json(self, db: PostgresAdapter) -> None:
        # shared_attrs is TEXT, so this is parsed in Python rather than with a
        # SQL-side jsonb cast that would raise on a malformed row.
        assert db.get_entity(TEMPERATURE).friendly_name == "Living Room Temperature"

    def test_a_malformed_attributes_blob_does_not_break_the_listing(
        self, db: PostgresAdapter, config: DatabaseConfig
    ) -> None:
        # The reason friendly_name is not extracted in SQL: `::jsonb` on this
        # row would abort the whole query rather than yield one nameless entity.
        #
        # The original blob is put back explicitly: _undo_writes reverts
        # `states` from the audit trail, which is not a record of anything
        # done to state_attributes, so a corrupted row here would otherwise
        # leak into every test that reads a friendly name afterwards.
        conn = _raw_connection(config)
        try:
            row = conn.execute(
                "SELECT attributes_id, shared_attrs FROM state_attributes "
                "ORDER BY attributes_id LIMIT 1"
            ).fetchone()
            assert row is not None
            try:
                conn.execute(
                    "UPDATE state_attributes SET shared_attrs = 'not json at all' "
                    "WHERE attributes_id = %s",
                    (row["attributes_id"],),
                )
                entities = db.list_entities(limit=100)
                assert len(entities) >= 4
                corrupted = [e for e in entities if e.friendly_name is None]
                assert corrupted, "expected the corrupted row to yield no friendly name"
            finally:
                conn.execute(
                    "UPDATE state_attributes SET shared_attrs = %s WHERE attributes_id = %s",
                    (row["shared_attrs"], row["attributes_id"]),
                )
        finally:
            conn.close()

    def test_reads_the_unit_from_statistics_meta(self, db: PostgresAdapter) -> None:
        assert db.get_entity(TEMPERATURE).unit == "°C"

    def test_reports_the_latest_value(self, db: PostgresAdapter) -> None:
        entity = db.get_entity(TEMPERATURE)
        assert entity.last_value is not None
        assert entity.last_updated_ts is not None

    def test_search_narrows_the_list(self, db: PostgresAdapter) -> None:
        found = db.list_entities(search="energy")
        assert [e.entity_id for e in found] == [ENERGY]

    def test_search_is_case_insensitive(self, db: PostgresAdapter) -> None:
        # PostgreSQL's LIKE is case-sensitive where MySQL's default collation
        # is not, so the adapter uses ILIKE. Without that this returns nothing
        # and the search box appears broken for anything typed in caps.
        assert [e.entity_id for e in db.list_entities(search="ENERGY")] == [ENERGY]

    def test_search_wildcards_are_escaped(self, db: PostgresAdapter) -> None:
        # '%' must match a literal per cent sign, not act as a wildcard.
        assert db.list_entities(search="%") == []

    def test_paging_walks_the_whole_list_without_repeats(self, db: PostgresAdapter) -> None:
        total = db.count_entities()
        seen: list[str] = []
        for offset in range(0, total, 2):
            seen.extend(e.entity_id for e in db.list_entities(limit=2, offset=offset))
        assert len(seen) == total
        assert len(set(seen)) == total

    def test_unknown_entity_raises_not_found(self, db: PostgresAdapter) -> None:
        with pytest.raises(NotFound):
            db.get_entity("sensor.does_not_exist")


class TestEntitySortingAndFiltering:
    def test_filters_by_sensor_type(self, db: PostgresAdapter) -> None:
        measurement = {e.entity_id for e in db.list_entities(sensor_type=SensorType.MEASUREMENT)}
        counter = {e.entity_id for e in db.list_entities(sensor_type=SensorType.COUNTER)}
        unknown = {e.entity_id for e in db.list_entities(sensor_type=SensorType.UNKNOWN)}

        assert TEMPERATURE in measurement
        assert ENERGY in counter
        assert DOOR in unknown
        # The three are disjoint: a counter is never also a measurement.
        assert not (measurement & counter)
        assert not (measurement & unknown)

    def test_the_type_filter_counts_agree_with_the_listing(self, db: PostgresAdapter) -> None:
        # count_entities and list_entities build their WHERE separately, so a
        # boolean-expression mistake in one and not the other would show here.
        for sensor_type in SensorType:
            listed = len(db.list_entities(sensor_type=sensor_type, limit=100))
            assert db.count_entities(sensor_type=sensor_type) == listed

    def test_type_filter_and_search_combine(self, db: PostgresAdapter) -> None:
        found = db.list_entities(search="sensor.", sensor_type=SensorType.COUNTER)
        assert [e.entity_id for e in found] == [ENERGY]

    def test_sort_by_entity_id_direction(self, db: PostgresAdapter) -> None:
        ascending = [e.entity_id for e in db.list_entities(sort="entity_id", sort_dir="asc")]
        descending = [e.entity_id for e in db.list_entities(sort="entity_id", sort_dir="desc")]
        assert ascending == sorted(ascending)
        assert descending == list(reversed(ascending))

    def test_sort_by_last_updated_puts_entities_without_states_last(
        self, db: PostgresAdapter, config: DatabaseConfig
    ) -> None:
        # NULLS LAST, in both directions: an entity with no states at all
        # should never lead a "most recently updated" sort.
        conn = _raw_connection(config)
        try:
            conn.execute("INSERT INTO states_meta (entity_id) VALUES ('sensor.no_states_at_all')")
            for direction in ("asc", "desc"):
                ids = [
                    e.entity_id
                    for e in db.list_entities(sort="last_updated", sort_dir=direction, limit=100)
                ]
                assert ids[-1] == "sensor.no_states_at_all", direction
        finally:
            conn.execute("DELETE FROM states_meta WHERE entity_id = 'sensor.no_states_at_all'")
            conn.close()

    def test_sort_by_corrections(self, db: PostgresAdapter) -> None:
        state_id, value = _first_state_id(db, TEMPERATURE)
        db.apply_correction(
            entity_id=TEMPERATURE,
            state_id=state_id,
            expected_original=value,
            new_value="1.0",
            quality=Quality.SPIKE,
            note=None,
            created_by="pytest",
        )
        ordered = db.list_entities(sort="corrections", sort_dir="desc", limit=100)
        assert ordered[0].entity_id == TEMPERATURE
        assert ordered[0].correction_count == 1


class TestFetchStates:
    def test_returns_rows_in_time_order(self, db: PostgresAdapter) -> None:
        points = db.fetch_states(TEMPERATURE, 0, 2_000_000_000, limit=500).points
        assert points
        assert [p.ts for p in points] == sorted(p.ts for p in points)

    def test_the_seeded_outlier_is_present(self, db: PostgresAdapter) -> None:
        points = db.fetch_states(TEMPERATURE, 0, 2_000_000_000, limit=5000).points
        assert any(p.numeric_value == -2000.0 for p in points)

    def test_non_numeric_states_survive_as_none(self, db: PostgresAdapter) -> None:
        points = db.fetch_states(FLAKY, 0, 2_000_000_000, limit=5000).points
        sentinels = [p for p in points if p.value in ("unknown", "unavailable")]
        assert sentinels
        assert all(p.numeric_value is None for p in sentinels)

    def test_the_range_is_half_open(self, db: PostgresAdapter) -> None:
        points = db.fetch_states(TEMPERATURE, 0, 2_000_000_000, limit=5000).points
        first, second = points[0], points[1]
        window = db.fetch_states(TEMPERATURE, first.ts, second.ts).points
        assert [p.state_id for p in window] == [first.state_id]

    def test_limit_is_honoured_and_truncation_reported(self, db: PostgresAdapter) -> None:
        series = db.fetch_states(TEMPERATURE, 0, 2_000_000_000, limit=10)
        assert len(series.points) == 10
        assert series.truncated


class TestApplyCorrection:
    def test_writes_the_audit_row_and_updates_the_state(self, db: PostgresAdapter) -> None:
        state_id, original = _first_state_id(db, TEMPERATURE)
        correction = db.apply_correction(
            entity_id=TEMPERATURE,
            state_id=state_id,
            expected_original=original,
            new_value="42.0",
            quality=Quality.SPIKE,
            note="integration",
            created_by="pytest",
        )
        assert correction.original_value == original
        assert correction.corrected_value == "42.0"
        assert correction.quality is Quality.SPIKE
        assert correction.created_by == "pytest"
        # RETURNING, not lastrowid: psycopg has no equivalent.
        assert correction.id > 0

        points = db.fetch_states(TEMPERATURE, correction.state_ts, correction.state_ts + 1).points
        assert points[0].value == "42.0"

    def test_the_corrected_point_is_marked_in_the_graph_data(self, db: PostgresAdapter) -> None:
        state_id, original = _first_state_id(db, TEMPERATURE)
        correction = db.apply_correction(
            entity_id=TEMPERATURE,
            state_id=state_id,
            expected_original=original,
            new_value="43.0",
            quality=Quality.SPIKE,
            note=None,
            created_by="pytest",
        )
        points = db.fetch_states(TEMPERATURE, correction.state_ts, correction.state_ts + 1).points
        assert points[0].correction_id == correction.id
        assert points[0].original_value == original

    def test_the_entity_correction_count_reflects_it(self, db: PostgresAdapter) -> None:
        before = db.get_entity(TEMPERATURE).correction_count
        state_id, original = _first_state_id(db, TEMPERATURE)
        db.apply_correction(
            entity_id=TEMPERATURE,
            state_id=state_id,
            expected_original=original,
            new_value="44.0",
            quality=Quality.SPIKE,
            note=None,
            created_by="pytest",
        )
        assert db.get_entity(TEMPERATURE).correction_count == before + 1

    def test_a_stale_expected_value_is_refused(self, db: PostgresAdapter) -> None:
        state_id, _ = _first_state_id(db, TEMPERATURE)
        with pytest.raises(ConcurrentModification):
            db.apply_correction(
                entity_id=TEMPERATURE,
                state_id=state_id,
                expected_original="not-what-is-stored",
                new_value="45.0",
                quality=Quality.SPIKE,
                note=None,
                created_by="pytest",
            )

    def test_a_missing_state_row_is_not_found(self, db: PostgresAdapter) -> None:
        with pytest.raises(NotFound):
            db.apply_correction(
                entity_id=TEMPERATURE,
                state_id=2_000_000_000,
                expected_original=None,
                new_value="46.0",
                quality=Quality.SPIKE,
                note=None,
                created_by="pytest",
            )

    def test_a_state_row_belonging_to_another_entity_is_refused(self, db: PostgresAdapter) -> None:
        state_id, _ = _first_state_id(db, ENERGY)
        with pytest.raises(NotFound):
            db.apply_correction(
                entity_id=TEMPERATURE,
                state_id=state_id,
                expected_original=None,
                new_value="47.0",
                quality=Quality.SPIKE,
                note=None,
                created_by="pytest",
            )

    def test_a_failing_update_rolls_the_audit_row_back(
        self, db: PostgresAdapter, config: DatabaseConfig
    ) -> None:
        """The guarantee the whole tool rests on (design document 9.9).

        The audit INSERT happens before the states UPDATE; if the UPDATE
        fails, both must disappear together.
        """
        before = len(db.list_corrections())
        state_id, original = _first_state_id(db, TEMPERATURE)

        with _states_updates_blocked(config), pytest.raises(Exception):  # noqa: B017
            db.apply_correction(
                entity_id=TEMPERATURE,
                state_id=state_id,
                expected_original=original,
                new_value="48.0",
                quality=Quality.SPIKE,
                note=None,
                created_by="pytest",
            )

        assert len(db.list_corrections()) == before
        points = db.fetch_states(TEMPERATURE, 0, 2_000_000_000, limit=5).points
        assert points[0].value == original

    def test_a_row_held_by_another_writer_times_out_cleanly(
        self, db: PostgresAdapter, config: DatabaseConfig
    ) -> None:
        # PostgreSQL surfaces an exceeded lock_timeout as LockNotAvailable
        # (55P03), not as a MySQL error code — the adapter translates it into
        # the same domain error either way.
        state_id, original = _first_state_id(db, TEMPERATURE)
        with _row_locked(config, state_id), pytest.raises(LockTimeout):
            db.apply_correction(
                entity_id=TEMPERATURE,
                state_id=state_id,
                expected_original=original,
                new_value="49.0",
                quality=Quality.SPIKE,
                note=None,
                created_by="pytest",
            )

    def test_counter_sensors_are_not_blocked_at_the_adapter_layer(
        self, db: PostgresAdapter
    ) -> None:
        state_id, original = _first_state_id(db, ENERGY)
        correction = db.apply_correction(
            entity_id=ENERGY,
            state_id=state_id,
            expected_original=original,
            new_value="50.0",
            quality=Quality.SPIKE,
            note=None,
            created_by="pytest",
        )
        assert correction.sensor_type is SensorType.COUNTER


class TestRestore:
    def _correct(self, db: PostgresAdapter) -> tuple[int, str]:
        state_id, original = _first_state_id(db, TEMPERATURE)
        correction = db.apply_correction(
            entity_id=TEMPERATURE,
            state_id=state_id,
            expected_original=original,
            new_value="60.0",
            quality=Quality.SPIKE,
            note=None,
            created_by="pytest",
        )
        return correction.id, original

    def test_puts_the_original_value_back(self, db: PostgresAdapter) -> None:
        correction_id, original = self._correct(db)
        restored = db.restore_correction(correction_id, "pytest")
        assert restored.restored_at is not None
        assert restored.restored_by == "pytest"
        points = db.fetch_states(TEMPERATURE, 0, 2_000_000_000, limit=5).points
        assert points[0].value == original

    def test_restoring_twice_is_refused(self, db: PostgresAdapter) -> None:
        correction_id, _ = self._correct(db)
        db.restore_correction(correction_id, "pytest")
        with pytest.raises(ConcurrentModification):
            db.restore_correction(correction_id, "pytest")

    def test_refuses_when_the_row_changed_outside_the_addon(
        self, db: PostgresAdapter, config: DatabaseConfig
    ) -> None:
        correction_id, _ = self._correct(db)
        correction = db.get_correction(correction_id)

        conn = _raw_connection(config)
        try:
            conn.execute(
                "UPDATE states SET state = %s WHERE state_id = %s",
                ("changed-by-someone-else", correction.state_id),
            )
        finally:
            conn.close()

        with pytest.raises(ConcurrentModification):
            db.restore_correction(correction_id, "pytest")

    def test_an_unknown_correction_is_not_found(self, db: PostgresAdapter) -> None:
        with pytest.raises(NotFound):
            db.restore_correction(2_000_000_000, "pytest")

    def test_a_correction_with_no_recorded_state_id_cannot_be_restored(
        self, db: PostgresAdapter, config: DatabaseConfig
    ) -> None:
        # Not reachable through the adapter's own writes today, but the
        # column is nullable and restore_correction must still refuse
        # cleanly rather than crash on a None where it expects an id.
        correction_id, _ = self._correct(db)
        state_id = db.get_correction(correction_id).state_id
        conn = _raw_connection(config)
        try:
            conn.execute(
                "UPDATE state_corrections SET state_id = NULL WHERE id = %s", (correction_id,)
            )
        finally:
            conn.close()

        try:
            with pytest.raises(NotFound, match="no states row recorded"):
                db.restore_correction(correction_id, "pytest")
        finally:
            # Put state_id back so the normal restore-based undo log (the
            # _undo_writes fixture) can find and revert this correction like
            # any other.
            conn = _raw_connection(config)
            try:
                conn.execute(
                    "UPDATE state_corrections SET state_id = %s WHERE id = %s",
                    (state_id, correction_id),
                )
            finally:
                conn.close()

    def test_a_purged_states_row_cannot_be_restored(
        self, db: PostgresAdapter, config: DatabaseConfig
    ) -> None:
        correction_id, _ = self._correct(db)
        correction = db.get_correction(correction_id)

        conn = _raw_connection(config, autocommit=False)
        try:
            row = conn.execute(
                "SELECT * FROM states WHERE state_id = %s", (correction.state_id,)
            ).fetchone()
            assert row is not None
            # _correct() already applied its own correction before this row
            # was captured — restore the pre-correction value, not the
            # corrected one.
            row["state"] = correction.original_value
            conn.execute("DELETE FROM states WHERE state_id = %s", (correction.state_id,))
            conn.commit()
        finally:
            conn.close()

        try:
            with pytest.raises(NotFound, match="purged by the recorder"):
                db.restore_correction(correction_id, "pytest")
        finally:
            conn = _raw_connection(config, autocommit=False)
            try:
                columns = ", ".join(row.keys())
                placeholders = ", ".join(["%s"] * len(row))
                conn.execute(
                    f"INSERT INTO states ({columns}) VALUES ({placeholders})",  # nosec B608
                    list(row.values()),
                )
                conn.execute("DELETE FROM state_corrections WHERE id = %s", (correction_id,))
                conn.commit()
            finally:
                conn.close()


class TestOrphanedCorrections:
    def _correct(self, db: PostgresAdapter) -> tuple[int, int, str]:
        state_id, original = _first_state_id(db, TEMPERATURE)
        correction = db.apply_correction(
            entity_id=TEMPERATURE,
            state_id=state_id,
            expected_original=original,
            new_value="70.0",
            quality=Quality.SPIKE,
            note=None,
            created_by="pytest",
        )
        return correction.id, state_id, original

    def test_a_correction_still_matching_the_database_is_not_orphaned(
        self, db: PostgresAdapter
    ) -> None:
        self._correct(db)
        assert db.find_orphaned_corrections() == []

    def test_a_backup_restore_is_detected_as_orphaned(
        self, db: PostgresAdapter, config: DatabaseConfig
    ) -> None:
        correction_id, state_id, original = self._correct(db)
        conn = _raw_connection(config)
        try:
            # What restoring a pre-correction backup looks like from here.
            conn.execute("UPDATE states SET state = %s WHERE state_id = %s", (original, state_id))
        finally:
            conn.close()
        assert [c.id for c in db.find_orphaned_corrections()] == [correction_id]

    def test_a_purged_states_row_also_counts_as_orphaned(
        self, db: PostgresAdapter, config: DatabaseConfig
    ) -> None:
        # The LEFT JOIN's purpose: a row the recorder deleted outright must
        # not silently vanish from the check.
        correction_id, state_id, _ = self._correct(db)
        conn = _raw_connection(config)
        try:
            conn.execute("DELETE FROM states WHERE state_id = %s", (state_id,))
        finally:
            conn.close()
        assert [c.id for c in db.find_orphaned_corrections()] == [correction_id]

    def test_a_restored_correction_is_never_orphaned(self, db: PostgresAdapter) -> None:
        correction_id, _, _ = self._correct(db)
        db.restore_correction(correction_id, "pytest")
        assert db.find_orphaned_corrections() == []

    def test_dismissing_stops_it_reappearing(
        self, db: PostgresAdapter, config: DatabaseConfig
    ) -> None:
        correction_id, state_id, original = self._correct(db)
        conn = _raw_connection(config)
        try:
            conn.execute("UPDATE states SET state = %s WHERE state_id = %s", (original, state_id))
        finally:
            conn.close()
        assert db.find_orphaned_corrections()

        dismissed = db.dismiss_correction(correction_id, "pytest")
        assert dismissed.dismissed_at is not None
        assert dismissed.dismissed_by == "pytest"
        assert db.find_orphaned_corrections() == []

    def test_dismissing_twice_is_refused(self, db: PostgresAdapter, config: DatabaseConfig) -> None:
        correction_id, state_id, original = self._correct(db)
        conn = _raw_connection(config)
        try:
            conn.execute("UPDATE states SET state = %s WHERE state_id = %s", (original, state_id))
        finally:
            conn.close()
        db.dismiss_correction(correction_id, "pytest")
        with pytest.raises(ConcurrentModification):
            db.dismiss_correction(correction_id, "pytest")

    def test_dismissing_a_restored_correction_is_refused(self, db: PostgresAdapter) -> None:
        correction_id, _, _ = self._correct(db)
        db.restore_correction(correction_id, "pytest")
        with pytest.raises(ConcurrentModification):
            db.dismiss_correction(correction_id, "pytest")

    def test_dismissing_an_unknown_correction_is_not_found(self, db: PostgresAdapter) -> None:
        with pytest.raises(NotFound):
            db.dismiss_correction(2_000_000_000, "pytest")


class TestAuditTrail:
    def _correct(self, db: PostgresAdapter, new_value: str) -> int:
        state_id, original = _first_state_id(db, TEMPERATURE)
        return db.apply_correction(
            entity_id=TEMPERATURE,
            state_id=state_id,
            expected_original=original,
            new_value=new_value,
            quality=Quality.SPIKE,
            note=None,
            created_by="pytest",
        ).id

    def test_lists_newest_first(self, db: PostgresAdapter) -> None:
        first = self._correct(db, "80.0")
        db.restore_correction(first, "pytest")
        second = self._correct(db, "81.0")
        assert [c.id for c in db.list_corrections()][:2] == [second, first]

    def test_filters_by_entity(self, db: PostgresAdapter) -> None:
        self._correct(db, "82.0")
        assert db.list_corrections(entity_id="sensor.nothing_here") == []
        assert db.list_corrections(entity_id=TEMPERATURE)

    def test_can_exclude_restored(self, db: PostgresAdapter) -> None:
        correction_id = self._correct(db, "83.0")
        db.restore_correction(correction_id, "pytest")
        assert correction_id not in [c.id for c in db.list_corrections(include_restored=False)]
        assert correction_id in [c.id for c in db.list_corrections(include_restored=True)]

    def test_an_unknown_correction_is_not_found(self, db: PostgresAdapter) -> None:
        with pytest.raises(NotFound):
            db.get_correction(2_000_000_000)


class TestBeforeOnboarding:
    """Every read path has to work before state_corrections exists.

    This is where PostgreSQL differs most sharply from the other two backends:
    a failed statement poisons the entire transaction, so the try/except the
    other adapters use around an optional audit-table query would break
    everything after it. The adapter wraps those in SAVEPOINTs; these tests
    are what prove it.
    """

    @pytest.fixture
    def without_audit_table(self, config: DatabaseConfig) -> Iterator[PostgresAdapter]:
        conn = _raw_connection(config)
        try:
            conn.execute("DROP TABLE IF EXISTS state_corrections")
        finally:
            conn.close()
        adapter = PostgresAdapter(config)
        try:
            yield adapter
        finally:
            adapter.ensure_audit_table()

    def test_list_entities_works_and_reports_no_corrections(
        self, without_audit_table: PostgresAdapter
    ) -> None:
        entities = without_audit_table.list_entities(limit=100)
        assert entities
        assert all(e.correction_count == 0 for e in entities)

    def test_get_entity_works(self, without_audit_table: PostgresAdapter) -> None:
        assert without_audit_table.get_entity(TEMPERATURE).correction_count == 0

    def test_fetch_states_works_with_nothing_marked(
        self, without_audit_table: PostgresAdapter
    ) -> None:
        points = without_audit_table.fetch_states(TEMPERATURE, 0, 2_000_000_000, limit=50).points
        assert points
        assert all(p.correction_id is None for p in points)

    def test_bulk_preview_works(self, without_audit_table: PostgresAdapter) -> None:
        preview = without_audit_table.bulk_correction_preview(TEMPERATURE, 0, 2_000_000_000)
        assert preview.total > 0
        assert preview.already_corrected == 0

    def test_health_reports_the_table_as_missing(
        self, without_audit_table: PostgresAdapter
    ) -> None:
        report = without_audit_table.check_health()
        assert report.connected
        assert not report.audit_table_ready
        # The savepoint around the schema-version read is what keeps the rest
        # of the health check working rather than aborting with it.
        assert report.schema_version is not None


class TestStatisticsCorrection:
    def _outlier(self, db: PostgresAdapter) -> tuple[int, str, float]:
        points = db.fetch_states(TEMPERATURE, 0, 2_000_000_000, limit=5000).points
        worst = min(points, key=lambda p: p.numeric_value if p.numeric_value is not None else 1e9)
        assert worst.value is not None
        return worst.state_id, worst.value, worst.ts

    def test_the_short_term_bucket_follows_the_correction(self, db: PostgresAdapter) -> None:
        from hr_statistics import SHORT_TERM_SECONDS, bucket_start

        metadata = db.get_statistics_metadata(TEMPERATURE)
        assert metadata is not None
        state_id, original, ts = self._outlier(db)
        start = bucket_start(ts, SHORT_TERM_SECONDS)

        before = db.fetch_statistics(metadata.id, start, start + SHORT_TERM_SECONDS, True)
        assert before and before[0].min is not None
        assert float(before[0].min) < -100

        db.apply_correction(
            entity_id=TEMPERATURE,
            state_id=state_id,
            expected_original=original,
            new_value="20.5",
            quality=Quality.SPIKE,
            note=None,
            created_by="pytest",
            recompute_statistics=True,
        )

        after = db.fetch_statistics(metadata.id, start, start + SHORT_TERM_SECONDS, True)
        assert after and after[0].min is not None
        assert float(after[0].min) > -100

    def test_the_hourly_bucket_follows_too(self, db: PostgresAdapter) -> None:
        from hr_statistics import HOURLY_SECONDS, bucket_start

        metadata = db.get_statistics_metadata(TEMPERATURE)
        assert metadata is not None
        state_id, original, ts = self._outlier(db)
        hour = bucket_start(ts, HOURLY_SECONDS)

        db.apply_correction(
            entity_id=TEMPERATURE,
            state_id=state_id,
            expected_original=original,
            new_value="20.5",
            quality=Quality.SPIKE,
            note=None,
            created_by="pytest",
            recompute_statistics=True,
        )

        after = db.fetch_statistics(metadata.id, hour, hour + HOURLY_SECONDS)
        assert after and after[0].min is not None
        assert float(after[0].min) > -100

    def test_the_audit_row_records_the_previous_statistics(self, db: PostgresAdapter) -> None:
        state_id, original, _ = self._outlier(db)
        correction = db.apply_correction(
            entity_id=TEMPERATURE,
            state_id=state_id,
            expected_original=original,
            new_value="20.5",
            quality=Quality.SPIKE,
            note=None,
            created_by="pytest",
            recompute_statistics=True,
        )
        # stats_corrected is a real BOOLEAN column here rather than a
        # TINYINT(1), so this also pins that it round-trips as one.
        assert correction.stats_corrected is True

    def test_restore_puts_the_statistics_back_as_they_were(self, db: PostgresAdapter) -> None:
        from hr_statistics import SHORT_TERM_SECONDS, bucket_start

        metadata = db.get_statistics_metadata(TEMPERATURE)
        assert metadata is not None
        state_id, original, ts = self._outlier(db)
        start = bucket_start(ts, SHORT_TERM_SECONDS)
        before = db.fetch_statistics(metadata.id, start, start + SHORT_TERM_SECONDS, True)[0]

        correction = db.apply_correction(
            entity_id=TEMPERATURE,
            state_id=state_id,
            expected_original=original,
            new_value="20.5",
            quality=Quality.SPIKE,
            note=None,
            created_by="pytest",
            recompute_statistics=True,
        )
        db.restore_correction(correction.id, "pytest")

        after = db.fetch_statistics(metadata.id, start, start + SHORT_TERM_SECONDS, True)[0]
        assert after.min == before.min
        assert after.max == before.max
        assert after.mean == before.mean

    def test_a_correction_without_recomputation_leaves_statistics_alone(
        self, db: PostgresAdapter
    ) -> None:
        from hr_statistics import SHORT_TERM_SECONDS, bucket_start

        metadata = db.get_statistics_metadata(TEMPERATURE)
        assert metadata is not None
        state_id, original, ts = self._outlier(db)
        start = bucket_start(ts, SHORT_TERM_SECONDS)
        before = db.fetch_statistics(metadata.id, start, start + SHORT_TERM_SECONDS, True)[0]

        correction = db.apply_correction(
            entity_id=TEMPERATURE,
            state_id=state_id,
            expected_original=original,
            new_value="20.5",
            quality=Quality.SPIKE,
            note=None,
            created_by="pytest",
        )
        assert correction.stats_corrected is False

        after = db.fetch_statistics(metadata.id, start, start + SHORT_TERM_SECONDS, True)[0]
        assert after.min == before.min


class TestCounterCascade:
    def _spike(self, db: PostgresAdapter) -> tuple[int, str, float, str]:
        points = db.fetch_states(ENERGY, 0, 2_000_000_000, limit=5000).points
        spike = max(points, key=lambda p: p.numeric_value if p.numeric_value is not None else -1e9)
        previous = points[points.index(spike) - 1]
        assert spike.value is not None
        assert previous.value is not None
        return spike.state_id, spike.value, spike.ts, previous.value

    def test_the_seeded_counter_has_an_inflated_total(self, db: PostgresAdapter) -> None:
        metadata = db.get_statistics_metadata(ENERGY)
        assert metadata is not None
        hourly = db.fetch_statistics(metadata.id, 0, 2_000_000_000)
        assert hourly[-1].sum is not None
        assert float(hourly[-1].sum) > 50_000

    def test_correcting_the_spike_rewrites_every_later_total(self, db: PostgresAdapter) -> None:
        metadata = db.get_statistics_metadata(ENERGY)
        assert metadata is not None
        state_id, original, _, previous_value = self._spike(db)

        db.apply_correction(
            entity_id=ENERGY,
            state_id=state_id,
            expected_original=original,
            new_value=previous_value,
            quality=Quality.SPIKE,
            note=None,
            created_by="pytest",
            recompute_statistics=True,
        )

        hourly = db.fetch_statistics(metadata.id, 0, 2_000_000_000)
        assert hourly[-1].sum is not None
        assert float(hourly[-1].sum) < 1_000

    def test_the_rebuilt_chain_never_decreases(self, db: PostgresAdapter) -> None:
        metadata = db.get_statistics_metadata(ENERGY)
        assert metadata is not None
        state_id, original, _, previous_value = self._spike(db)
        db.apply_correction(
            entity_id=ENERGY,
            state_id=state_id,
            expected_original=original,
            new_value=previous_value,
            quality=Quality.SPIKE,
            note=None,
            created_by="pytest",
            recompute_statistics=True,
        )
        sums = [
            float(r.sum)
            for r in db.fetch_statistics(metadata.id, 0, 2_000_000_000)
            if r.sum is not None
        ]
        assert sums == sorted(sums)

    def test_the_scope_is_knowable_before_committing(self, db: PostgresAdapter) -> None:
        _, _, ts, _ = self._spike(db)
        scope = db.counter_cascade_scope(ENERGY, ts)
        assert scope["hourly"] > 0
        assert scope["short_term"] > 0

    def test_restore_rebuilds_the_original_chain(self, db: PostgresAdapter) -> None:
        metadata = db.get_statistics_metadata(ENERGY)
        assert metadata is not None
        before = db.fetch_statistics(metadata.id, 0, 2_000_000_000)[-1].sum
        assert before is not None

        state_id, original, _, previous_value = self._spike(db)
        correction = db.apply_correction(
            entity_id=ENERGY,
            state_id=state_id,
            expected_original=original,
            new_value=previous_value,
            quality=Quality.SPIKE,
            note=None,
            created_by="pytest",
            recompute_statistics=True,
        )
        db.restore_correction(correction.id, "pytest")

        after = db.fetch_statistics(metadata.id, 0, 2_000_000_000)[-1].sum
        assert after is not None
        assert abs(float(after) - float(before)) < 0.01

    def test_a_hour_with_no_surviving_short_term_rows_falls_back_to_its_own_chain(
        self, db: PostgresAdapter, config: DatabaseConfig
    ) -> None:
        """Home Assistant purges statistics_short_term after ~10 days but
        keeps the hourly `statistics` table forever, so an old correction can
        land in an hour with no surviving 5-minute rows at all. This is what
        proves the fallback SQL in _recompute_counter_statistics — rebuilding
        the hourly chain from itself rather than from statistics_short_term —
        actually works against real PostgreSQL."""
        from hr_statistics import (
            HOURLY_SECONDS,
            SHORT_TERM_SECONDS,
            CounterBucket,
            bucket_start,
            cascade_sums,
        )

        entity_id = "sensor.bulk_it_purged_hour_fallback_postgres"
        hour1 = 1_780_002_000.0  # aligned to both a 5-minute and an hour boundary
        readings = [
            (hour1, 100.0),
            (hour1 + 300, 110.0),
            (hour1 + 600, 120.0),
            (hour1 + HOURLY_SECONDS, 200.0),
            (hour1 + HOURLY_SECONDS + 300, 210.0),
        ]

        conn = _raw_connection(config)
        try:
            metadata_id = conn.execute(
                "INSERT INTO states_meta (entity_id) VALUES (%s) RETURNING metadata_id",
                (entity_id,),
            ).fetchone()["metadata_id"]
            stats_metadata_id = conn.execute(
                "INSERT INTO statistics_meta "
                "(statistic_id, source, has_sum, mean_type, unit_of_measurement) "
                "VALUES (%s, 'recorder', TRUE, 1, '°C') RETURNING id",
                (entity_id,),
            ).fetchone()["id"]
            state_ids = {}
            for ts, value in readings:
                state_ids[ts] = conn.execute(
                    "INSERT INTO states (metadata_id, state, last_updated_ts, "
                    "last_reported_ts) VALUES (%s, %s, %s, %s) RETURNING state_id",
                    (metadata_id, repr(value), ts, ts),
                ).fetchone()["state_id"]

            starts = sorted({bucket_start(ts, SHORT_TERM_SECONDS) for ts, _ in readings})
            buckets = [
                CounterBucket(
                    start_ts=start, state=next(v for t, v in readings if t == start), sum=None
                )
                for start in starts
            ]
            chained = [CounterBucket(buckets[0].start_ts, buckets[0].state, 0.0)]
            chained.extend(cascade_sums(buckets[1:], buckets[0].state, 0.0))
            for bucket in chained:
                conn.execute(
                    "INSERT INTO statistics_short_term "
                    "(created_ts, metadata_id, start_ts, state, sum) "
                    "VALUES (%s, %s, %s, %s, %s)",
                    (bucket.start_ts, stats_metadata_id, bucket.start_ts, bucket.state, bucket.sum),
                )
            hourly: dict[float, CounterBucket] = {}
            for bucket in chained:
                hourly[bucket_start(bucket.start_ts, HOURLY_SECONDS)] = bucket
            for h, b in sorted(hourly.items()):
                conn.execute(
                    "INSERT INTO statistics (created_ts, metadata_id, start_ts, state, sum) "
                    "VALUES (%s, %s, %s, %s, %s)",
                    (h, stats_metadata_id, h, b.state, b.sum),
                )

            # The purge: every 5-minute row for the first hour is gone, but
            # its hourly row (and the second hour's short_term rows) remain.
            conn.execute(
                "DELETE FROM statistics_short_term WHERE metadata_id = %s "
                "AND start_ts >= %s AND start_ts < %s",
                (stats_metadata_id, hour1, hour1 + HOURLY_SECONDS),
            )

            db.apply_correction(
                entity_id=entity_id,
                state_id=state_ids[hour1],
                expected_original="100.0",
                new_value="150.0",
                quality=Quality.SPIKE,
                note=None,
                created_by="pytest",
                recompute_statistics=True,
            )

            hourly_rows = db.fetch_statistics(stats_metadata_id, hour1, hour1 + HOURLY_SECONDS)
            assert len(hourly_rows) == 1
            assert hourly_rows[0].state is not None
            assert float(hourly_rows[0].sum) == pytest.approx(0.0)
        finally:
            conn.execute(
                "DELETE FROM statistics WHERE metadata_id IN "
                "(SELECT id FROM statistics_meta WHERE statistic_id = %s)",
                (entity_id,),
            )
            conn.execute(
                "DELETE FROM statistics_short_term WHERE metadata_id IN "
                "(SELECT id FROM statistics_meta WHERE statistic_id = %s)",
                (entity_id,),
            )
            conn.execute("DELETE FROM statistics_meta WHERE statistic_id = %s", (entity_id,))
            conn.execute(
                "DELETE FROM states WHERE metadata_id IN "
                "(SELECT metadata_id FROM states_meta WHERE entity_id = %s)",
                (entity_id,),
            )
            conn.execute("DELETE FROM states_meta WHERE entity_id = %s", (entity_id,))
            conn.execute("DELETE FROM state_corrections WHERE entity_id = %s", (entity_id,))
            conn.close()


class TestBulkCorrectionIntegration:
    def _range(self, db: PostgresAdapter, entity_id: str) -> tuple[float, float]:
        points = db.fetch_states(entity_id, 0, 2_000_000_000, limit=5000).points
        return points[100].ts, points[110].ts

    def test_constant_strategy_writes_every_row_through_real_sql(self, db: PostgresAdapter) -> None:
        start, end = self._range(db, TEMPERATURE)
        result = db.apply_bulk_correction(
            entity_id=TEMPERATURE,
            start_ts=start,
            end_ts=end,
            strategy="constant",
            value="20.0",
            quality=Quality.FROZEN,
            note=None,
            created_by="pytest",
        )
        assert result.applied == 10
        assert result.skipped == 0
        values = {p.value for p in db.fetch_states(TEMPERATURE, start, end).points}
        assert values == {"20.0"}

    def test_interpolate_strategy_ramps_linearly(self, db: PostgresAdapter) -> None:
        start, end = self._range(db, TEMPERATURE)
        db.apply_bulk_correction(
            entity_id=TEMPERATURE,
            start_ts=start,
            end_ts=end,
            strategy="interpolate",
            value=None,
            quality=Quality.BAD_COMM,
            note=None,
            created_by="pytest",
        )
        values = [p.numeric_value for p in db.fetch_states(TEMPERATURE, start, end).points]
        assert all(v is not None for v in values)
        assert values == sorted(values) or values == sorted(values, reverse=True)

    def test_every_row_gets_its_own_audit_record(self, db: PostgresAdapter) -> None:
        start, end = self._range(db, TEMPERATURE)
        result = db.apply_bulk_correction(
            entity_id=TEMPERATURE,
            start_ts=start,
            end_ts=end,
            strategy="constant",
            value="20.0",
            quality=Quality.FROZEN,
            note=None,
            created_by="pytest",
        )
        assert len(set(result.correction_ids)) == result.applied
        for correction_id in result.correction_ids:
            assert db.get_correction(correction_id).corrected_value == "20.0"

    def test_already_corrected_rows_are_skipped_not_failed(self, db: PostgresAdapter) -> None:
        start, end = self._range(db, TEMPERATURE)
        first = db.apply_bulk_correction(
            entity_id=TEMPERATURE,
            start_ts=start,
            end_ts=end,
            strategy="constant",
            value="20.0",
            quality=Quality.FROZEN,
            note=None,
            created_by="pytest",
        )
        second = db.apply_bulk_correction(
            entity_id=TEMPERATURE,
            start_ts=start,
            end_ts=end,
            strategy="constant",
            value="21.0",
            quality=Quality.FROZEN,
            note=None,
            created_by="pytest",
        )
        assert second.applied == 0
        assert second.skipped == first.applied

    def test_a_row_cap_violation_leaves_the_database_untouched(self, db: PostgresAdapter) -> None:
        before = len(db.list_corrections())
        with pytest.raises(InvalidRange):
            db.apply_bulk_correction(
                entity_id=TEMPERATURE,
                start_ts=0,
                end_ts=2_000_000_000,
                strategy="constant",
                value="20.0",
                quality=Quality.FROZEN,
                note=None,
                created_by="pytest",
            )
        assert len(db.list_corrections()) == before

    def test_interpolating_without_both_anchors_is_an_invalid_range(
        self, db: PostgresAdapter
    ) -> None:
        # A range starting at (or before) the very first reading has no
        # "before" anchor for the real SQL to find.
        points = db.fetch_states(TEMPERATURE, 0, 2_000_000_000, limit=5000).points
        start, end = points[0].ts, points[10].ts
        with pytest.raises(InvalidRange, match="numeric reading on both"):
            db.apply_bulk_correction(
                entity_id=TEMPERATURE,
                start_ts=start,
                end_ts=end,
                strategy="interpolate",
                value=None,
                quality=Quality.BAD_COMM,
                note=None,
                created_by="pytest",
            )
        assert db.list_corrections(entity_id=TEMPERATURE) == []

    def test_a_missing_constant_value_is_an_invalid_range(self, db: PostgresAdapter) -> None:
        start, end = self._range(db, TEMPERATURE)
        with pytest.raises(InvalidRange, match="value is required"):
            db.apply_bulk_correction(
                entity_id=TEMPERATURE,
                start_ts=start,
                end_ts=end,
                strategy="constant",
                value=None,
                quality=Quality.BAD_COMM,
                note=None,
                created_by="pytest",
            )

    def test_a_counter_entity_bulk_corrects_and_cascades(self, db: PostgresAdapter) -> None:
        metadata = db.get_statistics_metadata(ENERGY)
        assert metadata is not None
        start, end = self._range(db, ENERGY)
        result = db.apply_bulk_correction(
            entity_id=ENERGY,
            start_ts=start,
            end_ts=end,
            strategy="interpolate",
            value=None,
            quality=Quality.BAD_COMM,
            note=None,
            created_by="pytest",
        )
        assert result.applied > 0
        # Applied chronologically inside one transaction, so the chain each
        # row's cascade inherits is the one the previous row already fixed.
        sums = [
            float(r.sum)
            for r in db.fetch_statistics(metadata.id, 0, 2_000_000_000)
            if r.sum is not None
        ]
        assert sums == sorted(sums)
