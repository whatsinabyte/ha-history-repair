"""SQLite implementation of DatabaseAdapter.

SQLite is Home Assistant's default recorder database — the majority of
installations never switch to MariaDB — so this is not a secondary option;
see the design document, 9.7.

Every SQL string for this dialect lives in this module, mirroring
hr_mariadb.py's structure and business logic exactly. Scanning the MariaDB
adapter's actual queries (not the design document's original draft SQL) shows
every UPDATE already targets a single table by primary key — there is no
`UPDATE ... JOIN` anywhere in the implemented code, only in the superseded
draft. That removes the single biggest feared translation problem. What
remains, confirmed against a real SQLite database before writing a line of
adapter code:

* Placeholders are `?`, not `%s`.
* `FOR UPDATE` does not exist. SQLite has no row-level locking; a write
  transaction locks the whole database. The equivalent is starting the
  transaction with `BEGIN IMMEDIATE`, which acquires that lock up front
  instead of on the first write, avoiding the "database is locked" a deferred
  transaction can hit when it tries to upgrade a read lock to a write lock
  after another writer has started.
* `information_schema` does not exist; introspection uses `PRAGMA table_info`
  and `sqlite_master`.
* There is no `SHOW GRANTS`; instead, a real write is attempted and its
  success is the privilege check — the same "let it fail loudly" fallback
  the MariaDB adapter already uses when it cannot introspect.
* Home Assistant's recorder writes to this same file continuously, and
  SQLite's single-writer model means a `database is locked` error is the
  normal cost of doing business here, not a rare edge case. Every write is
  wrapped in a short retry loop with exponential backoff, per the design
  document's explicit ask in 9.7.
* Python's sqlite3 module deprecated its automatic datetime adapters in 3.12,
  so timestamps here are handled explicitly as ISO 8601 text rather than
  relying on driver magic that is being removed.
* `shared_attrs` (the JSON blob holding friendly_name) is parsed in Python
  rather than with SQLite's JSON1 functions — not every SQLite build has JSON1
  compiled in, and this avoids depending on it at all.
"""

from __future__ import annotations

import json
import logging
import os
import sqlite3
import time
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import datetime, timezone
from typing import Any

from hr_config import SQLiteConfig
from hr_db import (
    MAX_BULK_ROWS,
    MIN_SCHEMA_VERSION,
    ConcurrentModification,
    ConnectionFailed,
    DatabaseAdapter,
    InvalidRange,
    LockTimeout,
    NotFound,
)
from hr_models import (
    BulkCorrectionResult,
    BulkPreview,
    Correction,
    Entity,
    HealthReport,
    Quality,
    SensorType,
    StatePoint,
    StateSeries,
    StatisticsMetadata,
    StatisticsRow,
)
from hr_sql_utils import escape_like, to_float
from hr_statistics import (
    HOURLY_SECONDS,
    SHORT_TERM_SECONDS,
    CounterBucket,
    MeasurementBucket,
    Reading,
    bucket_start,
    cascade_sums,
    recompute_short_term,
    summarise_hourly,
)

_LOGGER = logging.getLogger(__name__)

# SQLite error codes translated into domain errors. SQLITE_BUSY: another
# connection holds the lock right now. SQLITE_LOCKED: this connection's own
# earlier statement conflicts with itself within the same transaction — rarer
# here since every write is a single short transaction, but treated the same.
_SQLITE_BUSY = 5
_SQLITE_LOCKED = 6

# The connection's own `timeout=` parameter (below) already makes sqlite3
# block and retry internally on a locked database before raising — so this
# module's own retry loop deliberately sets that connection-level timeout
# very small (SQLITE_CONNECT_TIMEOUT) and does its own backoff on top,
# rather than composing two independent retry loops. Nesting them multiplies
# the worst-case wait by the outer loop's attempt count: an earlier version
# of this file set the connection timeout to the full lock_wait_timeout AND
# retried five times around it, so a persistently locked database took nearly
# 30 seconds to report LockTimeout — far too long for a web request to hang.
#
# The single retry loop's total budget is SQLiteConfig.lock_wait_timeout
# (5s by default, matching the MariaDB adapter's innodb_lock_wait_timeout),
# spent as exponential backoff capped per-sleep so the loop stays responsive
# rather than making one long final sleep.
_SQLITE_CONNECT_TIMEOUT = 0.1
_RETRY_INITIAL_DELAY = 0.05
_RETRY_MAX_DELAY = 1.0

AUDIT_TABLE_DDL = """
CREATE TABLE IF NOT EXISTS state_corrections (
  id                    INTEGER PRIMARY KEY AUTOINCREMENT,
  entity_id             TEXT NOT NULL,
  sensor_type           TEXT NOT NULL DEFAULT 'unknown'
                            CHECK (sensor_type IN ('measurement','counter','unknown')),

  -- Primary key of the corrected states row. Corrections are targeted by this
  -- rather than by a timestamp comparison: last_updated_ts is a float epoch.
  state_id              INTEGER,
  state_ts              REAL NOT NULL,
  state_dt              TEXT,

  -- Originals captured before the change. Measurement corrections fill only
  -- the mean/min/max columns; counter corrections fill state/sum instead.
  orig_state_value      TEXT,
  orig_sst_mean         REAL,
  orig_sst_min          REAL,
  orig_sst_max          REAL,
  orig_sst_state        REAL,
  orig_sst_sum          REAL,
  orig_stat_mean        REAL,
  orig_stat_min         REAL,
  orig_stat_max         REAL,
  orig_stat_state       REAL,
  orig_stat_sum         REAL,

  corrected_value       TEXT NOT NULL,

  sum_delta_applied     REAL,
  cascade_rows_updated  INTEGER,

  -- False until the statistics tables have been rebuilt for this row, so a
  -- later pass can find and backfill any correction still missing that.
  stats_corrected       INTEGER NOT NULL DEFAULT 0,

  quality               TEXT NOT NULL DEFAULT 'uncertain'
                            CHECK (quality IN
                              ('bad_comm','out_of_range','spike','frozen','uncertain')),
  note                  TEXT,
  created_at            TEXT NOT NULL,
  created_by            TEXT NOT NULL DEFAULT 'user',
  restored_at           TEXT,
  restored_by           TEXT,

  -- Set when a user dismisses an orphaned correction (9.11) rather than
  -- re-applying it. Added after the table's first release, so
  -- ensure_audit_table also ALTERs any table created before this existed.
  dismissed_at          TEXT,
  dismissed_by          TEXT
)
"""

# Columns added after the table's first release. SQLite has no
# "ADD COLUMN IF NOT EXISTS", so ensure_audit_table tries each and swallows
# only the duplicate-column error, letting a genuine problem still surface.
_AUDIT_TABLE_MIGRATIONS = (
    "ALTER TABLE state_corrections ADD COLUMN dismissed_at TEXT",
    "ALTER TABLE state_corrections ADD COLUMN dismissed_by TEXT",
)

_AUDIT_TABLE_INDEXES = (
    "CREATE INDEX IF NOT EXISTS idx_state_corrections_entity_ts "
    + "ON state_corrections (entity_id, state_ts)",
    "CREATE INDEX IF NOT EXISTS idx_state_corrections_state_id "
    + "ON state_corrections (state_id)",
    "CREATE INDEX IF NOT EXISTS idx_state_corrections_active "
    + "ON state_corrections (entity_id, restored_at)",
)

_CORRECTION_COLUMNS = """
    id, entity_id, sensor_type, state_id, state_ts, orig_state_value,
    corrected_value, quality, note, created_at, created_by,
    restored_at, restored_by, stats_corrected, dismissed_at, dismissed_by
"""

# Same columns, qualified for the join in find_orphaned_corrections.
_CORRECTION_COLUMNS_QUALIFIED = """
    c.id, c.entity_id, c.sensor_type, c.state_id, c.state_ts, c.orig_state_value,
    c.corrected_value, c.quality, c.note, c.created_at, c.created_by,
    c.restored_at, c.restored_by, c.stats_corrected, c.dismissed_at, c.dismissed_by
"""


def _utc_now_iso() -> str:
    """The current time as the ISO 8601 text this module stores timestamps as.

    Not relying on sqlite3's datetime adapters: Python 3.12 deprecated the
    default ones, so timestamps are handled explicitly here instead of on
    driver behaviour that is being removed.
    """
    return datetime.now(timezone.utc).isoformat()


def _is_lock_error(err: sqlite3.OperationalError) -> bool:
    code = getattr(err, "sqlite_errorcode", None)
    if code is not None:
        return code in (_SQLITE_BUSY, _SQLITE_LOCKED)
    # Fallback for a sqlite3 build without the errorcode attribute
    # (Python < 3.11, or a driver that does not populate it): match the
    # message text SQLite itself uses for both conditions.
    text = str(err).lower()
    return "database is locked" in text or "database table is locked" in text


class SQLiteAdapter(DatabaseAdapter):
    """Talks directly to Home Assistant's home-assistant_v2.db file.

    Unlike MariaDBAdapter, a single connection is held open for the adapter's
    lifetime rather than one per operation: opening a SQLite file is not free,
    there is no server to pool connections for, and Home Assistant's own
    recorder holds its own connection open the same way.
    """

    def __init__(self, config: SQLiteConfig) -> None:
        self._config = config
        self._path = config.path
        self._conn: sqlite3.Connection | None = None
        self._stats_meta_columns: set[str] | None = None
        self._statistics_columns_cache: dict[str, set[str]] = {}

    # -- connection ----------------------------------------------------------

    def _connection(self) -> sqlite3.Connection:
        if self._conn is None:
            # sqlite3.connect() silently creates an empty file at a path that
            # does not exist — there is no server to refuse the connection.
            # A wrong db_path, or an install whose recorder is actually
            # MariaDB, would otherwise leave a stray empty database sitting in
            # the user's real Home Assistant config directory and report only
            # a confusing "could not read the recorder schema version" further
            # down, with no clue that the file itself never existed.
            if not os.path.exists(self._path):
                raise ConnectionFailed(
                    f"No database file exists at '{self._path}'. If your Home "
                    "Assistant recorder is configured for MariaDB rather than "
                    "the default SQLite database, set db_type to mariadb "
                    "instead. Otherwise check that db_path points at your "
                    "actual home-assistant_v2.db."
                )
            try:
                # isolation_level=None: autocommit mode, so this module issues
                # its own BEGIN/COMMIT/ROLLBACK explicitly rather than the
                # driver's implicit ones — needed to use BEGIN IMMEDIATE.
                # check_same_thread=False: a WSGI worker may serve requests
                # for the same adapter instance from more than one thread.
                conn = sqlite3.connect(
                    self._path,
                    # Deliberately small — see the comment on
                    # _SQLITE_CONNECT_TIMEOUT above. This module's own retry
                    # loop in _transaction is what actually honours
                    # lock_wait_timeout.
                    timeout=_SQLITE_CONNECT_TIMEOUT,
                    isolation_level=None,
                    check_same_thread=False,
                )
                conn.row_factory = sqlite3.Row
            except sqlite3.Error as err:
                raise ConnectionFailed(str(err)) from err
            self._conn = conn
        return self._conn

    @contextmanager
    def _cursor(self) -> Iterator[Any]:
        """A read-only cursor. No transaction is opened for a plain SELECT."""
        conn = self._connection()
        try:
            yield conn.cursor()
        except sqlite3.Error as err:
            raise ConnectionFailed(str(err)) from err

    @contextmanager
    def _transaction(self) -> Iterator[Any]:
        """A write transaction, retried with backoff if the file is locked.

        BEGIN IMMEDIATE claims the write lock at the start of the transaction
        rather than on the first write, which is what avoids the classic
        SQLite failure mode of a transaction that read first and then could
        not upgrade to a writer because another connection got there first.

        Retries with backoff apply only to BEGIN IMMEDIATE itself failing
        (another writer holds the lock before this transaction starts) —
        continuing until `lock_wait_timeout` seconds have elapsed in total,
        not for a fixed number of attempts, since the attempt count that
        fits depends on how quickly each one fails, which depends on
        _SQLITE_CONNECT_TIMEOUT, and hardcoding both independently is how the
        30-second regression above happened. A lock error discovered only
        once the transaction is already open (typically at COMMIT, upgrading
        past a concurrent reader's SHARED lock) is reported as LockTimeout
        immediately instead: retrying it would require this generator to
        yield a second time after catching an exception thrown into it at
        the first yield, which contextlib's own context-manager protocol
        rejects outright.
        """
        conn = self._connection()
        deadline = time.monotonic() + self._config.lock_wait_timeout
        delay = _RETRY_INITIAL_DELAY

        while True:
            try:
                conn.execute("BEGIN IMMEDIATE")
            except sqlite3.OperationalError as err:
                # Not a lock at all: something else is wrong, and that error
                # is more informative than a manufactured LockTimeout — let
                # it propagate as itself rather than retrying or masking it.
                if not _is_lock_error(err):
                    raise
                if time.monotonic() >= deadline:
                    raise LockTimeout(
                        "The recorder is currently holding this database. Please try again."
                    ) from err
                time.sleep(min(delay, _RETRY_MAX_DELAY))
                delay *= 2
                continue

            try:
                cur = conn.cursor()
                yield cur
                conn.execute("COMMIT")
                return
            except sqlite3.OperationalError as err:
                conn.execute("ROLLBACK")
                # A lock error can still surface here even though BEGIN
                # IMMEDIATE already succeeded: it claims the write lock up
                # front, but COMMIT still needs to upgrade past any reader
                # holding a SHARED lock (Home Assistant's own recorder, most
                # realistically, reading this same file concurrently).
                #
                # This cannot be retried by looping back to a second `yield`
                # in this same generator: a @contextmanager generator that
                # catches an exception thrown into it at the yield point and
                # then yields again, instead of stopping, is a protocol
                # violation contextlib actively detects and turns into
                # `RuntimeError("generator didn't stop after throw()")` —
                # confirmed directly, and unconditionally, independent of
                # whether the retried attempt would have gone on to succeed.
                # Reporting LockTimeout immediately is the only correct
                # option once execution has already reached this point.
                if not _is_lock_error(err):
                    raise
                raise LockTimeout(
                    "The recorder is currently holding this database. Please try again."
                ) from err
            except Exception:
                conn.execute("ROLLBACK")
                raise

    def close(self) -> None:
        if self._conn is not None:
            self._conn.close()
            self._conn = None

    # -- startup gates ---------------------------------------------------

    def check_health(self) -> HealthReport:
        errors: list[str] = []
        warnings: list[str] = []
        schema_version: int | None = None
        server_version: str | None = None
        can_update = False
        audit_ready = False

        try:
            with self._cursor() as cur:
                cur.execute("SELECT sqlite_version() AS v")
                row = cur.fetchone()
                server_version = f"SQLite {row['v']}" if row else None

                schema_version = self._read_schema_version(cur)
                if schema_version is None:
                    errors.append(
                        "Could not read the recorder schema version. Is this the "
                        "Home Assistant database?"
                    )
                elif schema_version < MIN_SCHEMA_VERSION:
                    errors.append(
                        f"Recorder schema {schema_version} is too old. This add-on needs "
                        f"schema {MIN_SCHEMA_VERSION} or newer (Home Assistant 2022.4 and "
                        "later). Please update Home Assistant first."
                    )

                cur.execute(
                    "SELECT name FROM sqlite_master WHERE type='table' AND name='state_corrections'"
                )
                audit_ready = cur.fetchone() is not None

            can_update = self._can_write()
            if not can_update:
                errors.append(
                    f"The add-on cannot write to '{self._path}'. Check the file's "
                    "permissions, and that it is mounted read-write."
                )
        except ConnectionFailed as err:
            return HealthReport(connected=False, errors=[str(err)])

        return HealthReport(
            connected=True,
            schema_version=schema_version,
            server_version=server_version,
            can_update=can_update,
            audit_table_ready=audit_ready,
            errors=errors,
            warnings=warnings,
        )

    @staticmethod
    def _read_schema_version(cur: Any) -> int | None:
        try:
            cur.execute("SELECT MAX(schema_version) AS v FROM schema_changes")
            row = cur.fetchone()
        except sqlite3.Error:
            return None
        if row and row["v"] is not None:
            return int(row["v"])
        return None

    def _can_write(self) -> bool:
        """Attempt a real, harmless write as the privilege check.

        SQLite has no GRANTS to inspect; a scratch table is created and
        dropped inside its own transaction, mirroring the MariaDB adapter's
        "cannot introspect, so let a real write fail loudly instead" fallback
        for exactly the same reason — better than blocking the user on a
        guess.
        """
        try:
            with self._transaction() as cur:
                cur.execute("CREATE TABLE IF NOT EXISTS _hr_write_check (x INTEGER)")
                cur.execute("DROP TABLE _hr_write_check")
            return True
        except (sqlite3.Error, LockTimeout):
            return False

    def ensure_audit_table(self) -> None:
        with self._transaction() as cur:
            cur.execute(AUDIT_TABLE_DDL)
            for statement in _AUDIT_TABLE_INDEXES:
                cur.execute(statement)
            for migration in _AUDIT_TABLE_MIGRATIONS:
                try:
                    cur.execute(migration)
                except sqlite3.OperationalError as err:
                    if "duplicate column name" not in str(err):
                        raise

    # -- sensor type -------------------------------------------------------

    def _statistics_meta_columns(self) -> set[str]:
        if self._stats_meta_columns is None:
            with self._cursor() as cur:
                cur.execute("PRAGMA table_info(statistics_meta)")
                self._stats_meta_columns = {r["name"].lower() for r in cur.fetchall()}
        return self._stats_meta_columns

    def _sensor_type_expressions(self) -> tuple[str, str]:
        """Raw (has_sum, has_mean) SQL expressions, for both SELECT and WHERE use.

        Same reasoning as the MariaDB adapter: mean_type is authoritative
        wherever the column exists, because Home Assistant stopped populating
        has_mean once mean_type arrived. Kept separate from
        _sensor_type_columns's "AS has_sum" aliasing because a WHERE clause at
        the same query level cannot reference a SELECT-list alias.
        """
        cols = self._statistics_meta_columns()
        has_sum = "stm.has_sum" if "has_sum" in cols else "0"

        if "mean_type" in cols and "has_mean" in cols:
            has_mean = (
                "CASE WHEN stm.mean_type IS NOT NULL THEN "
                "(CASE WHEN stm.mean_type > 0 THEN 1 ELSE 0 END) "
                "ELSE stm.has_mean END"
            )
        elif "mean_type" in cols:
            has_mean = "CASE WHEN stm.mean_type > 0 THEN 1 ELSE 0 END"
        elif "has_mean" in cols:
            has_mean = "stm.has_mean"
        else:
            has_mean = "0"
        return has_sum, has_mean

    def _sensor_type_columns(self) -> str:
        """SELECT-list fragment exposing the sensor-type inputs as has_sum/has_mean."""
        has_sum, has_mean = self._sensor_type_expressions()
        return f"{has_sum} AS has_sum, {has_mean} AS has_mean"

    def _sensor_type_where(self, sensor_type: SensorType) -> str:
        """WHERE fragment restricting to entities classified as exactly this type."""
        has_sum, has_mean = self._sensor_type_expressions()
        if sensor_type is SensorType.COUNTER:
            return f"COALESCE({has_sum}, 0) = 1"
        if sensor_type is SensorType.MEASUREMENT:
            return f"COALESCE({has_sum}, 0) = 0 AND COALESCE({has_mean}, 0) = 1"
        return f"COALESCE({has_sum}, 0) = 0 AND COALESCE({has_mean}, 0) = 0"

    @staticmethod
    def _entity_sort_expr(sort: str, sort_dir: str) -> str:
        """ORDER BY fragment for list_entities, from an allowlisted key/direction.

        Never built from the raw request values directly: an unrecognised
        sort or sort_dir falls back to the default rather than ever reaching
        SQL as text.
        """
        direction = "DESC" if sort_dir == "desc" else "ASC"
        if sort == "last_updated":
            return f"(latest_ts IS NULL) ASC, latest_ts {direction}"
        if sort == "corrections":
            return f"correction_count {direction}"
        return f"sm.entity_id {direction}"

    @staticmethod
    def sensor_type_from_flags(has_sum: Any, has_mean: Any) -> SensorType:
        if has_sum:
            return SensorType.COUNTER
        if has_mean:
            return SensorType.MEASUREMENT
        return SensorType.UNKNOWN

    @classmethod
    def _sensor_type(cls, row: sqlite3.Row) -> SensorType:
        keys = row.keys()
        has_sum = row["has_sum"] if "has_sum" in keys else None
        has_mean = row["has_mean"] if "has_mean" in keys else None
        return cls.sensor_type_from_flags(has_sum, has_mean)

    # -- entity browser ----------------------------------------------------

    def _entity_filter_clause(
        self, search: str | None, sensor_type: SensorType | None
    ) -> tuple[str, list[Any]]:
        clauses: list[str] = []
        params: list[Any] = []
        if search:
            clauses.append("sm.entity_id LIKE ? ESCAPE '\\'")
            params.append(f"%{escape_like(search)}%")
        if sensor_type is not None:
            clauses.append(self._sensor_type_where(sensor_type))
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        return where, params

    def count_entities(
        self, search: str | None = None, sensor_type: SensorType | None = None
    ) -> int:
        where, params = self._entity_filter_clause(search, sensor_type)
        # nosec B608 - where is built from _entity_filter_clause, whose only
        # interpolations are the fixed LIKE fragment and _sensor_type_where's
        # introspected column allowlist; the search term is bound.
        sql = f"""
            SELECT COUNT(*) AS n
            FROM states_meta sm
            LEFT JOIN statistics_meta stm ON stm.statistic_id = sm.entity_id
            {where}
        """  # nosec B608
        with self._cursor() as cur:
            cur.execute(sql, params)
            return int(cur.fetchone()["n"])

    def _list_entities_sql(self, where: str, order_by: str, correction_expr: str) -> str:
        # nosec B608 - the only interpolations are the fixed WHERE fragment
        # from _entity_filter_clause, _sensor_type_columns()'s introspected
        # column allowlist, _entity_sort_expr's own allowlisted mapping, and
        # correction_expr, which is either the fixed subquery below or the
        # literal fallback list_entities substitutes when state_corrections
        # does not exist yet; the search term is bound as a parameter.
        return f"""
            SELECT
              m.metadata_id,
              m.entity_id,
              s.state                 AS last_value,
              m.latest_ts             AS last_updated_ts,
              m.unit                  AS unit,
              m.has_sum               AS has_sum,
              m.has_mean              AS has_mean,
              m.correction_count      AS correction_count,
              sa.shared_attrs         AS shared_attrs
            FROM (
              SELECT sm.metadata_id, sm.entity_id,
                stm.unit_of_measurement AS unit,
                {self._sensor_type_columns()},
                (SELECT s2.state_id FROM states s2
                  WHERE s2.metadata_id = sm.metadata_id
                  ORDER BY s2.last_updated_ts DESC LIMIT 1) AS last_state_id,
                (SELECT s2.last_updated_ts FROM states s2
                  WHERE s2.metadata_id = sm.metadata_id
                  ORDER BY s2.last_updated_ts DESC LIMIT 1) AS latest_ts,
                {correction_expr} AS correction_count
              FROM states_meta sm
              LEFT JOIN statistics_meta stm ON stm.statistic_id = sm.entity_id
              {where}
              ORDER BY {order_by}
              LIMIT ? OFFSET ?
            ) m
            LEFT JOIN states s            ON s.state_id = m.last_state_id
            LEFT JOIN state_attributes sa ON sa.attributes_id = s.attributes_id
        """  # nosec B608

    def list_entities(
        self,
        search: str | None = None,
        sensor_type: SensorType | None = None,
        sort: str = "entity_id",
        sort_dir: str = "asc",
        limit: int = 100,
        offset: int = 0,
    ) -> list[Entity]:
        where, params = self._entity_filter_clause(search, sensor_type)
        order_by = self._entity_sort_expr(sort, sort_dir)
        params.extend([limit, offset])

        # Same shape as the MariaDB adapter: filtering, sorting, and paging
        # all happen inside the subquery this builds, before any per-entity
        # lookup runs, so the cost is proportional to the page size rather
        # than to the size of states. latest_ts and correction_count are each
        # one correlated, indexed lookup per states_meta row.
        correction_expr = (
            "(SELECT COUNT(*) FROM state_corrections sc "
            "WHERE sc.entity_id = sm.entity_id AND sc.restored_at IS NULL)"
        )
        sql = self._list_entities_sql(where, order_by, correction_expr)
        with self._cursor() as cur:
            try:
                cur.execute(sql, params)
                rows = cur.fetchall()
            except sqlite3.OperationalError as err:
                if not self._is_missing_audit_table(err):
                    raise
                # state_corrections is created during onboarding, so this path
                # has to work without it too. No audit table yet means no
                # corrections yet either.
                sql = self._list_entities_sql(where, order_by, "0")
                cur.execute(sql, params)
                rows = cur.fetchall()

        return [
            self._row_to_entity(r, {r["entity_id"]: r["correction_count"] for r in rows})
            for r in rows
        ]

    def get_entity(self, entity_id: str) -> Entity:
        sql = f"""
            SELECT
              sm.metadata_id,
              sm.entity_id,
              s.state                 AS last_value,
              s.last_updated_ts       AS last_updated_ts,
              stm.unit_of_measurement AS unit,
              {self._sensor_type_columns()},
              sa.shared_attrs         AS shared_attrs
            FROM states_meta sm
            LEFT JOIN states s ON s.state_id = (
              SELECT s2.state_id FROM states s2
               WHERE s2.metadata_id = sm.metadata_id
               ORDER BY s2.last_updated_ts DESC LIMIT 1)
            LEFT JOIN state_attributes sa ON sa.attributes_id = s.attributes_id
            LEFT JOIN statistics_meta stm ON stm.statistic_id = sm.entity_id
            WHERE sm.entity_id = ?
        """  # nosec B608 - interpolates only _sensor_type_columns(); entity_id is bound.
        with self._cursor() as cur:
            cur.execute(sql, (entity_id,))
            row = cur.fetchone()
        if not row:
            raise NotFound(f"Unknown entity: {entity_id}")
        return self._row_to_entity(row, self._correction_counts([entity_id]))

    @staticmethod
    def _friendly_name(row: sqlite3.Row) -> str | None:
        """Pull friendly_name out of the shared_attrs JSON blob, in Python.

        Not every SQLite build has the JSON1 extension compiled in, so this
        avoids depending on json_extract() being available at all.
        """
        # `"x" in row` is not `"x" in row.keys()` for sqlite3.Row: the object
        # supports `in` as a sequence, checking values, not column names — a
        # confirmed, deliberate deviation from ruff's usual dict-membership
        # advice, not an oversight.
        raw = row["shared_attrs"] if "shared_attrs" in row.keys() else None  # noqa: SIM118
        if not raw:
            return None
        try:
            attrs = json.loads(raw)
        except (TypeError, ValueError):
            return None
        name = attrs.get("friendly_name") if isinstance(attrs, dict) else None
        return name if isinstance(name, str) else None

    @classmethod
    def _row_to_entity(cls, row: sqlite3.Row, counts: dict[str, int]) -> Entity:
        return Entity(
            metadata_id=row["metadata_id"],
            entity_id=row["entity_id"],
            friendly_name=cls._friendly_name(row),
            sensor_type=cls._sensor_type(row),
            unit=row["unit"],
            last_value=row["last_value"],
            last_updated_ts=row["last_updated_ts"],
            correction_count=counts.get(row["entity_id"], 0),
        )

    @staticmethod
    def _is_missing_audit_table(err: sqlite3.OperationalError) -> bool:
        """Is this the audit table simply not existing yet?

        It is created during onboarding, so every read path has to work
        without it: before onboarding, and whenever the adapter is pointed at
        a recorder database purely to look.
        """
        return "no such table: state_corrections" in str(err)

    def _correction_counts(self, entity_ids: list[str]) -> dict[str, int]:
        if not entity_ids:
            return {}
        placeholders = ", ".join(["?"] * len(entity_ids))
        with self._cursor() as cur:
            try:
                cur.execute(
                    "SELECT entity_id, COUNT(*) AS n FROM state_corrections "
                    f"WHERE restored_at IS NULL AND entity_id IN ({placeholders}) "  # nosec B608
                    "GROUP BY entity_id",
                    entity_ids,
                )
            except sqlite3.OperationalError as err:
                if self._is_missing_audit_table(err):
                    return {}
                raise
            return {r["entity_id"]: int(r["n"]) for r in cur.fetchall()}

    # -- history -------------------------------------------------------------

    def fetch_states(
        self, entity_id: str, start_ts: float, end_ts: float, limit: int = 20000
    ) -> StateSeries:
        with self._cursor() as cur:
            cur.execute(
                """
                SELECT s.state_id, s.last_updated_ts AS ts, s.state
                  FROM states s
                  JOIN states_meta sm ON sm.metadata_id = s.metadata_id
                 WHERE sm.entity_id = ?
                   AND s.last_updated_ts >= ?
                   AND s.last_updated_ts < ?
                 ORDER BY s.last_updated_ts
                 LIMIT ?
                """,
                # One more than asked for, purely to detect truncation; the
                # extra row is discarded below.
                (entity_id, start_ts, end_ts, limit + 1),
            )
            rows = cur.fetchall()

            try:
                cur.execute(
                    """
                    SELECT id, state_id, orig_state_value
                      FROM state_corrections
                     WHERE entity_id = ? AND restored_at IS NULL
                       AND state_ts >= ? AND state_ts < ?
                    """,
                    (entity_id, start_ts, end_ts),
                )
                corrected = {
                    r["state_id"]: (r["id"], r["orig_state_value"])
                    for r in cur.fetchall()
                    if r["state_id"] is not None
                }
            except sqlite3.OperationalError as err:
                if not self._is_missing_audit_table(err):
                    raise
                corrected = {}

        truncated = len(rows) > limit
        points: list[StatePoint] = []
        for r in rows[:limit]:
            mark = corrected.get(r["state_id"])
            points.append(
                StatePoint(
                    state_id=r["state_id"],
                    ts=float(r["ts"]),
                    value=r["state"],
                    numeric_value=to_float(r["state"]),
                    correction_id=mark[0] if mark else None,
                    original_value=mark[1] if mark else None,
                )
            )
        return StateSeries(points=points, truncated=truncated, limit=limit)

    # -- statistics ------------------------------------------------------

    def get_statistics_metadata(self, entity_id: str) -> StatisticsMetadata | None:
        cols = self._statistics_meta_columns()
        mean_type_expr = "stm.mean_type" if "mean_type" in cols else "NULL"
        with self._cursor() as cur:
            cur.execute(
                f"""
                SELECT stm.id, stm.statistic_id, stm.has_sum,
                       stm.unit_of_measurement, {mean_type_expr} AS mean_type,
                       {"stm.has_mean" if "has_mean" in cols else "NULL"} AS has_mean
                  FROM statistics_meta stm
                 WHERE stm.statistic_id = ?
                """,  # nosec B608 - column names come from an introspected allowlist
                (entity_id,),
            )
            row = cur.fetchone()
        if not row:
            return None

        mean_type = row["mean_type"]
        if mean_type is None:
            mean_type = 1 if row["has_mean"] else 0

        return StatisticsMetadata(
            id=int(row["id"]),
            statistic_id=row["statistic_id"],
            mean_type=int(mean_type),
            has_sum=bool(row["has_sum"]),
            unit_of_measurement=row["unit_of_measurement"],
        )

    def fetch_readings(self, entity_id: str, start_ts: float, end_ts: float) -> list[Reading]:
        with self._cursor() as cur:
            return self._readings_with(cur, entity_id, start_ts, end_ts)

    @staticmethod
    def _readings_with(cur: Any, entity_id: str, start_ts: float, end_ts: float) -> list[Reading]:
        """Readings for a window, on a caller-supplied cursor.

        Taking the cursor matters: during a correction this has to run inside
        the same transaction as the states UPDATE, so it sees the corrected
        value rather than the one being replaced.
        """
        cur.execute(
            """
            SELECT s.state, s.last_updated_ts
              FROM states s
              JOIN states_meta sm ON sm.metadata_id = s.metadata_id
             WHERE sm.entity_id = ?
               AND s.last_updated_ts < ?
               AND s.state NOT IN ('unknown', 'unavailable')
             ORDER BY s.last_updated_ts DESC
             LIMIT 1
            """,
            (entity_id, start_ts),
        )
        rows = list(cur.fetchall())

        cur.execute(
            """
            SELECT s.state, s.last_updated_ts
              FROM states s
              JOIN states_meta sm ON sm.metadata_id = s.metadata_id
             WHERE sm.entity_id = ?
               AND s.last_updated_ts >= ?
               AND s.last_updated_ts < ?
               AND s.state NOT IN ('unknown', 'unavailable')
             ORDER BY s.last_updated_ts
            """,
            (entity_id, start_ts, end_ts),
        )
        rows.extend(cur.fetchall())

        readings: list[Reading] = []
        for row in rows:
            value = to_float(row["state"])
            if value is None:
                continue
            readings.append(Reading(value=value, ts=float(row["last_updated_ts"])))
        return readings

    def fetch_statistics(
        self,
        metadata_id: int,
        start_ts: float,
        end_ts: float,
        short_term: bool = False,
    ) -> list[StatisticsRow]:
        with self._cursor() as cur:
            return self._statistics_with(cur, metadata_id, start_ts, end_ts, short_term)

    def _statistics_with(
        self,
        cur: Any,
        metadata_id: int,
        start_ts: float,
        end_ts: float,
        short_term: bool = False,
    ) -> list[StatisticsRow]:
        table = "statistics_short_term" if short_term else "statistics"
        cols = self._statistics_columns(table)
        mean_weight = "mean_weight" if "mean_weight" in cols else "NULL AS mean_weight"

        cur.execute(
            f"""
            SELECT id, metadata_id, start_ts, mean, {mean_weight},
                   min, max, state, sum
              FROM {table}
             WHERE metadata_id = ? AND start_ts >= ? AND start_ts < ?
             ORDER BY start_ts
            """,  # nosec B608 - table and columns come from fixed values above
            (metadata_id, start_ts, end_ts),
        )
        return [
            StatisticsRow(
                id=int(r["id"]),
                metadata_id=int(r["metadata_id"]),
                start_ts=float(r["start_ts"]),
                mean=r["mean"],
                mean_weight=r["mean_weight"],
                min=r["min"],
                max=r["max"],
                state=r["state"],
                sum=r["sum"],
            )
            for r in cur.fetchall()
        ]

    def _statistics_columns(self, table: str) -> set[str]:
        if table not in self._statistics_columns_cache:
            with self._cursor() as cur:
                cur.execute(f"PRAGMA table_info({table})")  # nosec B608 - fixed table names only
                self._statistics_columns_cache[table] = {r["name"].lower() for r in cur.fetchall()}
        return self._statistics_columns_cache[table]

    # -- audit trail -----------------------------------------------------

    @staticmethod
    def _row_to_correction(r: sqlite3.Row) -> Correction:
        return Correction(
            id=r["id"],
            entity_id=r["entity_id"],
            sensor_type=SensorType(r["sensor_type"]),
            state_id=r["state_id"],
            state_ts=float(r["state_ts"]),
            original_value=r["orig_state_value"],
            corrected_value=r["corrected_value"],
            quality=Quality(r["quality"]),
            note=r["note"],
            created_at=r["created_at"] or "",
            created_by=r["created_by"],
            restored_at=r["restored_at"],
            restored_by=r["restored_by"],
            stats_corrected=bool(r["stats_corrected"]),
            dismissed_at=r["dismissed_at"],
            dismissed_by=r["dismissed_by"],
        )

    def list_corrections(
        self,
        entity_id: str | None = None,
        include_restored: bool = True,
        limit: int = 500,
    ) -> list[Correction]:
        clauses: list[str] = []
        params: list[Any] = []
        if entity_id:
            clauses.append("entity_id = ?")
            params.append(entity_id)
        if not include_restored:
            clauses.append("restored_at IS NULL")
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        params.append(limit)

        with self._cursor() as cur:
            cur.execute(
                f"SELECT {_CORRECTION_COLUMNS} FROM state_corrections "  # nosec B608
                f"{where} ORDER BY created_at DESC, id DESC LIMIT ?",
                params,
            )
            return [self._row_to_correction(r) for r in cur.fetchall()]

    def get_correction(self, correction_id: int) -> Correction:
        with self._cursor() as cur:
            cur.execute(
                f"SELECT {_CORRECTION_COLUMNS} FROM state_corrections WHERE id = ?",  # nosec B608
                (correction_id,),
            )
            row = cur.fetchone()
        if not row:
            raise NotFound(f"No correction with id {correction_id}")
        return self._row_to_correction(row)

    # -- statistics recomputation ------------------------------------------
    #
    # Identical in structure and reasoning to hr_mariadb.py's methods of the
    # same name — see their docstrings there. Only the SQL text differs.

    def _recompute_measurement_statistics(
        self, cur: Any, entity_id: str, stats_metadata_id: int, state_ts: float
    ) -> tuple[dict[str, Any], dict[str, Any], int]:
        short_start = bucket_start(state_ts, SHORT_TERM_SECONDS)
        affected_starts = [short_start, short_start + SHORT_TERM_SECONDS]

        originals_short: dict[str, Any] = {}
        updated = 0

        for index, start in enumerate(affected_starts):
            existing = self._statistics_with(
                cur, stats_metadata_id, start, start + SHORT_TERM_SECONDS, short_term=True
            )
            if not existing:
                continue

            readings = self._readings_with(cur, entity_id, start, start + SHORT_TERM_SECONDS)
            rebuilt = recompute_short_term(readings, start)
            row = existing[0]
            if index == 0:
                originals_short = {"mean": row.mean, "min": row.min, "max": row.max}
            cur.execute(
                "UPDATE statistics_short_term SET mean = ?, min = ?, max = ? WHERE id = ?",
                (rebuilt.mean, rebuilt.min, rebuilt.max, row.id),
            )
            updated += cur.rowcount

        hours = sorted({bucket_start(s, HOURLY_SECONDS) for s in affected_starts})
        originals_hourly: dict[str, Any] = {}

        for index, hour_start in enumerate(hours):
            existing_hourly = self._statistics_with(
                cur, stats_metadata_id, hour_start, hour_start + HOURLY_SECONDS
            )
            if not existing_hourly:
                continue
            hourly_row = existing_hourly[0]
            if index == 0:
                originals_hourly = {
                    "mean": hourly_row.mean,
                    "min": hourly_row.min,
                    "max": hourly_row.max,
                }

            siblings = self._statistics_with(
                cur, stats_metadata_id, hour_start, hour_start + HOURLY_SECONDS, short_term=True
            )
            if siblings:
                buckets = [
                    MeasurementBucket(
                        start_ts=row.start_ts,
                        mean=None if row.mean is None else float(row.mean),
                        min=None if row.min is None else float(row.min),
                        max=None if row.max is None else float(row.max),
                    )
                    for row in siblings
                ]
            else:
                buckets = []
                offset = 0.0
                while offset < HOURLY_SECONDS:
                    sub_start = hour_start + offset
                    sub_readings = self._readings_with(
                        cur, entity_id, sub_start, sub_start + SHORT_TERM_SECONDS
                    )
                    if sub_readings:
                        buckets.append(recompute_short_term(sub_readings, sub_start))
                    offset += SHORT_TERM_SECONDS

            summary = summarise_hourly(buckets, hour_start)
            cur.execute(
                "UPDATE statistics SET mean = ?, min = ?, max = ? WHERE id = ?",
                (summary.mean, summary.min, summary.max, hourly_row.id),
            )
            updated += cur.rowcount

        return originals_short, originals_hourly, updated

    # -- counter cascade -------------------------------------------------

    def _last_reading_in(
        self, cur: Any, entity_id: str, start_ts: float, end_ts: float
    ) -> float | None:
        readings = self._readings_with(cur, entity_id, start_ts, end_ts)
        inside = [r for r in readings if r.ts >= start_ts]
        return inside[-1].value if inside else None

    def _cascade_table(
        self,
        cur: Any,
        entity_id: str,
        stats_metadata_id: int,
        from_ts: float,
        width: float,
        table: str,
        total_increasing: bool,
    ) -> tuple[dict[str, Any], int]:
        short_term = table == "statistics_short_term"

        previous = self._statistics_with(
            cur, stats_metadata_id, from_ts - width, from_ts, short_term=short_term
        )
        previous_state = (
            float(previous[-1].state) if previous and previous[-1].state is not None else None
        )
        previous_sum = float(previous[-1].sum) if previous and previous[-1].sum is not None else 0.0

        rows = self._statistics_with(
            cur, stats_metadata_id, from_ts, 2_000_000_000, short_term=short_term
        )
        if not rows:
            return {}, 0

        corrected_reading = self._last_reading_in(cur, entity_id, from_ts, from_ts + width)
        originals = {"state": rows[0].state, "sum": rows[0].sum}

        buckets: list[CounterBucket] = []
        for index, row in enumerate(rows):
            state = row.state
            if index == 0 and corrected_reading is not None:
                state = corrected_reading
            buckets.append(
                CounterBucket(
                    start_ts=row.start_ts,
                    state=None if state is None else float(state),
                    sum=None,
                )
            )

        rebuilt = cascade_sums(
            buckets, previous_state, previous_sum, total_increasing=total_increasing
        )

        updates = [
            (bucket.state, bucket.sum, row.id) for bucket, row in zip(rebuilt, rows, strict=True)
        ]
        cur.executemany(
            f"UPDATE {table} SET state = ?, sum = ? WHERE id = ?",  # nosec B608
            updates,
        )
        return originals, len(updates)

    def _recompute_counter_statistics(
        self,
        cur: Any,
        entity_id: str,
        stats_metadata_id: int,
        state_ts: float,
        total_increasing: bool = True,
    ) -> tuple[dict[str, Any], dict[str, Any], int, float | None]:
        short_start = bucket_start(state_ts, SHORT_TERM_SECONDS)
        hour_start = bucket_start(state_ts, HOURLY_SECONDS)

        originals_short, short_rows = self._cascade_table(
            cur,
            entity_id,
            stats_metadata_id,
            short_start,
            SHORT_TERM_SECONDS,
            "statistics_short_term",
            total_increasing,
        )

        hourly = self._statistics_with(cur, stats_metadata_id, hour_start, 2_000_000_000)
        if not hourly:
            return originals_short, {}, short_rows, None

        originals_hourly: dict[str, Any] = {"state": hourly[0].state, "sum": hourly[0].sum}
        original_final_sum = hourly[-1].sum

        short_term = self._statistics_with(
            cur, stats_metadata_id, hour_start, 2_000_000_000, short_term=True
        )
        last_of_hour: dict[float, StatisticsRow] = {}
        for row in short_term:
            last_of_hour[bucket_start(row.start_ts, HOURLY_SECONDS)] = row

        updates: list[tuple[Any, Any, int]] = []
        fallback: list[StatisticsRow] = []
        for row in hourly:
            source = last_of_hour.get(row.start_ts)
            if source is not None:
                updates.append((source.state, source.sum, row.id))
            else:
                fallback.append(row)

        if fallback:
            previous = self._statistics_with(
                cur, stats_metadata_id, hour_start - HOURLY_SECONDS, hour_start
            )
            rebuilt = cascade_sums(
                [
                    CounterBucket(
                        start_ts=r.start_ts,
                        state=None if r.state is None else float(r.state),
                        sum=None,
                    )
                    for r in fallback
                ],
                float(previous[-1].state) if previous and previous[-1].state is not None else None,
                float(previous[-1].sum) if previous and previous[-1].sum is not None else 0.0,
                total_increasing=total_increasing,
            )
            updates.extend(
                (bucket.state, bucket.sum, row.id)
                for bucket, row in zip(rebuilt, fallback, strict=True)
            )

        cur.executemany("UPDATE statistics SET state = ?, sum = ? WHERE id = ?", updates)

        refreshed = self._statistics_with(cur, stats_metadata_id, hour_start, 2_000_000_000)
        delta: float | None = None
        if refreshed and refreshed[-1].sum is not None and original_final_sum is not None:
            delta = float(original_final_sum) - float(refreshed[-1].sum)

        return originals_short, originals_hourly, short_rows + len(updates), delta

    def counter_cascade_scope(self, entity_id: str, state_ts: float) -> dict[str, int]:
        metadata = self.get_statistics_metadata(entity_id)
        if metadata is None:
            return {"short_term": 0, "hourly": 0}

        with self._cursor() as cur:
            short = self._statistics_with(
                cur,
                metadata.id,
                bucket_start(state_ts, SHORT_TERM_SECONDS),
                2_000_000_000,
                short_term=True,
            )
            hourly = self._statistics_with(
                cur, metadata.id, bucket_start(state_ts, HOURLY_SECONDS), 2_000_000_000
            )
        return {"short_term": len(short), "hourly": len(hourly)}

    # -- writes ------------------------------------------------------------

    def _apply_one_correction(
        self,
        cur: Any,
        *,
        entity_id: str,
        sensor_type: SensorType,
        stats_metadata: StatisticsMetadata | None,
        state_id: int,
        expected_original: str | None,
        new_value: str,
        quality: Quality,
        note: str | None,
        created_by: str,
    ) -> int:
        """Correct one row on an already-open transaction. Returns the audit id.

        There is no SQLite equivalent of `FOR UPDATE` to request here: the
        surrounding `BEGIN IMMEDIATE` transaction already holds the database's
        one write lock for its whole duration, which is a stronger guarantee
        than a single row lock, not a weaker one.
        """
        cur.execute(
            """
            SELECT s.state_id, s.state, s.last_updated_ts, sm.entity_id
              FROM states s
              JOIN states_meta sm ON sm.metadata_id = s.metadata_id
             WHERE s.state_id = ?
            """,
            (state_id,),
        )
        row = cur.fetchone()
        if not row:
            raise NotFound(f"No states row with id {state_id}")
        if row["entity_id"] != entity_id:
            raise NotFound(f"States row {state_id} belongs to {row['entity_id']}, not {entity_id}")
        if expected_original is not None and row["state"] != expected_original:
            raise ConcurrentModification(
                f"This value changed since you loaded it: it now reads "
                f"'{row['state']}', not '{expected_original}'. Reload the graph "
                "and try again."
            )

        ts = float(row["last_updated_ts"])
        state_dt = datetime.fromtimestamp(ts, tz=timezone.utc).isoformat()

        # Audit first: if the UPDATE below fails, ROLLBACK in _transaction
        # takes this with it, leaving the database untouched (design
        # document, 9.9).
        cur.execute(
            """
            INSERT INTO state_corrections
              (entity_id, sensor_type, state_id, state_ts, state_dt,
               orig_state_value, corrected_value, quality, note,
               created_by, stats_corrected, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0, ?)
            """,
            (
                entity_id,
                sensor_type.value,
                state_id,
                ts,
                state_dt,
                row["state"],
                new_value,
                quality.value,
                note,
                created_by,
                _utc_now_iso(),
            ),
        )
        correction_id = cur.lastrowid

        cur.execute("UPDATE states SET state = ? WHERE state_id = ?", (new_value, state_id))
        if cur.rowcount != 1:
            raise NotFound(f"States row {state_id} disappeared mid-transaction")

        if stats_metadata is not None:
            cascade_delta: float | None = None
            cascade_rows: int | None = None
            if sensor_type is SensorType.COUNTER:
                (
                    originals_short,
                    originals_hourly,
                    cascade_rows,
                    cascade_delta,
                ) = self._recompute_counter_statistics(cur, entity_id, stats_metadata.id, ts)
            else:
                originals_short, originals_hourly, _ = self._recompute_measurement_statistics(
                    cur, entity_id, stats_metadata.id, ts
                )
            cur.execute(
                """
                UPDATE state_corrections SET
                  orig_sst_mean = ?, orig_sst_min = ?, orig_sst_max = ?,
                  orig_sst_state = ?, orig_sst_sum = ?,
                  orig_stat_mean = ?, orig_stat_min = ?, orig_stat_max = ?,
                  orig_stat_state = ?, orig_stat_sum = ?,
                  sum_delta_applied = ?, cascade_rows_updated = ?,
                  stats_corrected = 1
                WHERE id = ?
                """,
                (
                    originals_short.get("mean"),
                    originals_short.get("min"),
                    originals_short.get("max"),
                    originals_short.get("state"),
                    originals_short.get("sum"),
                    originals_hourly.get("mean"),
                    originals_hourly.get("min"),
                    originals_hourly.get("max"),
                    originals_hourly.get("state"),
                    originals_hourly.get("sum"),
                    cascade_delta,
                    cascade_rows,
                    correction_id,
                ),
            )

        return int(correction_id)

    def apply_correction(
        self,
        *,
        entity_id: str,
        state_id: int,
        expected_original: str | None,
        new_value: str,
        quality: Quality,
        note: str | None,
        created_by: str,
        recompute_statistics: bool = False,
    ) -> Correction:
        sensor_type = self.get_entity(entity_id).sensor_type
        stats_metadata = self.get_statistics_metadata(entity_id) if recompute_statistics else None

        with self._transaction() as cur:
            correction_id = self._apply_one_correction(
                cur,
                entity_id=entity_id,
                sensor_type=sensor_type,
                stats_metadata=stats_metadata,
                state_id=state_id,
                expected_original=expected_original,
                new_value=new_value,
                quality=quality,
                note=note,
                created_by=created_by,
            )

        return self.get_correction(correction_id)

    def _restore_statistics(self, cur: Any, correction: sqlite3.Row) -> None:
        stats_metadata = self.get_statistics_metadata(correction["entity_id"])
        if stats_metadata is None:
            return

        state_ts = float(correction["state_ts"])

        cur.execute(
            "UPDATE statistics_short_term SET mean = ?, min = ?, max = ? "
            "WHERE metadata_id = ? AND start_ts = ?",
            (
                correction["orig_sst_mean"],
                correction["orig_sst_min"],
                correction["orig_sst_max"],
                stats_metadata.id,
                bucket_start(state_ts, SHORT_TERM_SECONDS),
            ),
        )
        cur.execute(
            "UPDATE statistics SET mean = ?, min = ?, max = ? "
            "WHERE metadata_id = ? AND start_ts = ?",
            (
                correction["orig_stat_mean"],
                correction["orig_stat_min"],
                correction["orig_stat_max"],
                stats_metadata.id,
                bucket_start(state_ts, HOURLY_SECONDS),
            ),
        )

    def restore_correction(self, correction_id: int, restored_by: str) -> Correction:
        with self._transaction() as cur:
            cur.execute(
                "SELECT id, entity_id, state_id, state_ts, orig_state_value, "
                "corrected_value, restored_at, stats_corrected, "
                "orig_sst_mean, orig_sst_min, orig_sst_max, "
                "orig_stat_mean, orig_stat_min, orig_stat_max "
                "FROM state_corrections WHERE id = ?",
                (correction_id,),
            )
            correction = cur.fetchone()
            if not correction:
                raise NotFound(f"No correction with id {correction_id}")
            if correction["restored_at"] is not None:
                raise ConcurrentModification("This correction has already been restored.")
            if correction["state_id"] is None:
                raise NotFound(
                    "This correction has no states row recorded and cannot be "
                    "restored automatically."
                )

            cur.execute("SELECT state FROM states WHERE state_id = ?", (correction["state_id"],))
            current = cur.fetchone()
            if not current:
                raise NotFound(
                    "The corrected states row no longer exists — it was most likely "
                    "purged by the recorder."
                )
            if current["state"] != correction["corrected_value"]:
                raise ConcurrentModification(
                    f"This row now holds '{current['state']}', not the corrected value "
                    f"'{correction['corrected_value']}'. It was changed outside this "
                    "add-on, so it will not be overwritten."
                )

            cur.execute(
                "UPDATE states SET state = ? WHERE state_id = ?",
                (correction["orig_state_value"], correction["state_id"]),
            )

            if correction["stats_corrected"]:
                metadata = self.get_statistics_metadata(correction["entity_id"])
                if metadata is not None and metadata.has_sum:
                    self._recompute_counter_statistics(
                        cur,
                        correction["entity_id"],
                        metadata.id,
                        float(correction["state_ts"]),
                    )
                else:
                    self._restore_statistics(cur, correction)

            cur.execute(
                "UPDATE state_corrections SET restored_at = ?, restored_by = ? WHERE id = ?",
                (_utc_now_iso(), restored_by, correction_id),
            )

        return self.get_correction(correction_id)

    def find_orphaned_corrections(self) -> list[Correction]:
        # LEFT JOIN so a states row purged by the recorder (not merely
        # reverted by a backup) also counts as orphaned rather than silently
        # disappearing from the check.
        with self._cursor() as cur:
            cur.execute(
                f"SELECT {_CORRECTION_COLUMNS_QUALIFIED} "  # nosec B608
                "FROM state_corrections c "
                "LEFT JOIN states s ON s.state_id = c.state_id "
                "WHERE c.restored_at IS NULL AND c.dismissed_at IS NULL "
                "AND c.state_id IS NOT NULL "
                "AND (s.state_id IS NULL OR s.state != c.corrected_value) "
                "ORDER BY c.created_at DESC, c.id DESC"
            )
            return [self._row_to_correction(r) for r in cur.fetchall()]

    def dismiss_correction(self, correction_id: int, dismissed_by: str) -> Correction:
        with self._transaction() as cur:
            cur.execute(
                "SELECT id, restored_at, dismissed_at FROM state_corrections WHERE id = ?",
                (correction_id,),
            )
            correction = cur.fetchone()
            if not correction:
                raise NotFound(f"No correction with id {correction_id}")
            if correction["restored_at"] is not None:
                raise ConcurrentModification(
                    "This correction has already been restored and cannot be dismissed."
                )
            if correction["dismissed_at"] is not None:
                raise ConcurrentModification("This correction has already been dismissed.")

            cur.execute(
                "UPDATE state_corrections SET dismissed_at = ?, dismissed_by = ? WHERE id = ?",
                (_utc_now_iso(), dismissed_by, correction_id),
            )

        return self.get_correction(correction_id)

    # -- bulk correction ---------------------------------------------------

    def _bulk_target_rows(
        self, cur: Any, entity_id: str, start_ts: float, end_ts: float
    ) -> list[dict[str, Any]]:
        cur.execute(
            """
            SELECT s.state_id, s.state, s.last_updated_ts AS ts
              FROM states s
              JOIN states_meta sm ON sm.metadata_id = s.metadata_id
             WHERE sm.entity_id = ?
               AND s.last_updated_ts >= ?
               AND s.last_updated_ts < ?
             ORDER BY s.last_updated_ts
            """,
            (entity_id, start_ts, end_ts),
        )
        rows: list[dict[str, Any]] = [dict(r) for r in cur.fetchall()]

        try:
            cur.execute(
                "SELECT state_id FROM state_corrections "
                "WHERE entity_id = ? AND restored_at IS NULL "
                "AND state_ts >= ? AND state_ts < ? AND state_id IS NOT NULL",
                (entity_id, start_ts, end_ts),
            )
            already_corrected = {r["state_id"] for r in cur.fetchall()}
        except sqlite3.OperationalError as err:
            if not self._is_missing_audit_table(err):
                raise
            already_corrected = set()

        for row in rows:
            row["already_corrected"] = row["state_id"] in already_corrected
        return rows

    def _interpolation_anchors(
        self, cur: Any, entity_id: str, start_ts: float, end_ts: float
    ) -> tuple[Reading | None, Reading | None]:
        cur.execute(
            """
            SELECT s.state, s.last_updated_ts
              FROM states s
              JOIN states_meta sm ON sm.metadata_id = s.metadata_id
             WHERE sm.entity_id = ?
               AND s.last_updated_ts < ?
               AND s.state NOT IN ('unknown', 'unavailable')
             ORDER BY s.last_updated_ts DESC
             LIMIT 1
            """,
            (entity_id, start_ts),
        )
        before_row = cur.fetchone()

        cur.execute(
            """
            SELECT s.state, s.last_updated_ts
              FROM states s
              JOIN states_meta sm ON sm.metadata_id = s.metadata_id
             WHERE sm.entity_id = ?
               AND s.last_updated_ts >= ?
               AND s.state NOT IN ('unknown', 'unavailable')
             ORDER BY s.last_updated_ts
             LIMIT 1
            """,
            (entity_id, end_ts),
        )
        after_row = cur.fetchone()

        before = None
        if before_row is not None:
            value = to_float(before_row["state"])
            if value is not None:
                before = Reading(value=value, ts=float(before_row["last_updated_ts"]))

        after = None
        if after_row is not None:
            value = to_float(after_row["state"])
            if value is not None:
                after = Reading(value=value, ts=float(after_row["last_updated_ts"]))

        return before, after

    def bulk_correction_preview(
        self, entity_id: str, start_ts: float, end_ts: float
    ) -> BulkPreview:
        with self._cursor() as cur:
            rows = self._bulk_target_rows(cur, entity_id, start_ts, end_ts)
            before, after = self._interpolation_anchors(cur, entity_id, start_ts, end_ts)

        already_corrected = sum(1 for r in rows if r["already_corrected"])
        return BulkPreview(
            total=len(rows),
            correctable=len(rows) - already_corrected,
            already_corrected=already_corrected,
            can_interpolate=before is not None and after is not None,
        )

    def apply_bulk_correction(
        self,
        *,
        entity_id: str,
        start_ts: float,
        end_ts: float,
        strategy: str,
        value: str | None,
        quality: Quality,
        note: str | None,
        created_by: str,
    ) -> BulkCorrectionResult:
        sensor_type = self.get_entity(entity_id).sensor_type
        stats_metadata = self.get_statistics_metadata(entity_id)

        with self._transaction() as cur:
            rows = self._bulk_target_rows(cur, entity_id, start_ts, end_ts)
            targets = [r for r in rows if not r["already_corrected"]]

            if len(targets) > MAX_BULK_ROWS:
                raise InvalidRange(
                    f"This range holds {len(targets)} correctable rows, more than the "
                    f"{MAX_BULK_ROWS}-row limit for a single bulk correction. Choose a "
                    "narrower range."
                )

            if strategy == "interpolate":
                before, after = self._interpolation_anchors(cur, entity_id, start_ts, end_ts)
                if before is None or after is None:
                    raise InvalidRange(
                        "Cannot interpolate: this range needs a numeric reading on both "
                        "sides of it, and at least one side has none."
                    )
                anchor_before, anchor_after = before, after

                def value_for(ts: float) -> str:
                    span = anchor_after.ts - anchor_before.ts
                    fraction = (ts - anchor_before.ts) / span if span else 0.0
                    interpolated = (
                        anchor_before.value + (anchor_after.value - anchor_before.value) * fraction
                    )
                    return repr(interpolated)
            elif value is not None:
                constant_value = value

                def value_for(ts: float) -> str:
                    return constant_value
            else:
                raise InvalidRange("A value is required for a constant bulk correction.")

            correction_ids: list[int] = []
            for row in targets:
                row_value = value_for(float(row["ts"]))
                correction_ids.append(
                    self._apply_one_correction(
                        cur,
                        entity_id=entity_id,
                        sensor_type=sensor_type,
                        stats_metadata=stats_metadata,
                        state_id=row["state_id"],
                        expected_original=None,
                        new_value=row_value,
                        quality=quality,
                        note=note,
                        created_by=created_by,
                    )
                )

        return BulkCorrectionResult(
            entity_id=entity_id,
            correction_ids=correction_ids,
            applied=len(correction_ids),
            skipped=len(rows) - len(targets),
        )
