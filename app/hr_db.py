"""The database abstraction the whole application talks to.

The design document (9.7) asks for this interface from the first version so an
SQLiteAdapter can be added later without a rewrite. Every SQL string lives
inside a concrete adapter; nothing above this layer may assume a dialect.

Pure module: an interface plus its error taxonomy, no I/O of its own.
"""

from __future__ import annotations

from abc import ABC, abstractmethod

from hr_models import (
    BulkCorrectionResult,
    BulkPreview,
    Correction,
    Entity,
    HealthReport,
    Quality,
    SensorType,
    StateSeries,
    StatisticsMetadata,
    StatisticsRow,
)
from hr_statistics import Reading

# A bulk correction runs its per-row logic in a loop inside one transaction,
# and a counter's cascade rescans every statistics row from the correction
# point to the present for each row it touches. This bounds that cost and the
# time a set of row locks is held, rather than because a real range is likely
# to need more.
MAX_BULK_ROWS = 500

# The oldest recorder schema this tool understands. Before 28 there is no
# states_meta table and entity_id lives directly on states, so every query in
# the MariaDB adapter would fail. See the design document, 9.1.
MIN_SCHEMA_VERSION = 28

# Columns the entity browser can sort by. A closed set rather than an
# arbitrary column name, so a request's sort value can reach SQL safely
# without ever being interpolated directly — each adapter maps these to a
# fixed, known-safe expression instead.
ENTITY_SORT_KEYS = ("entity_id", "last_updated", "corrections")
ENTITY_SORT_DIRECTIONS = ("asc", "desc")


class AdapterError(Exception):
    """Base class for every failure an adapter reports upward."""


class ConnectionFailed(AdapterError):
    """The database could not be reached or authenticated against."""


class SchemaUnsupported(AdapterError):
    """The recorder schema is older than this tool supports."""


class PermissionDenied(AdapterError):
    """The database user lacks a privilege the operation needs."""


class ConcurrentModification(AdapterError):
    """The row changed between the user loading it and submitting a correction."""


class LockTimeout(AdapterError):
    """The recorder held the row longer than lock_wait_timeout allows."""


class NotFound(AdapterError):
    """The requested entity, state row, or correction does not exist."""


class InvalidRange(AdapterError):
    """A bulk correction's range cannot be carried out as asked.

    Distinct from ValidationError (hr_corrections): that one catches bad
    input before touching the database; this one is raised only once the
    database has been read, because whether a range is too large or has
    usable interpolation anchors on both sides cannot be known without
    querying it.
    """


class DatabaseAdapter(ABC):
    """Read and correct a Home Assistant recorder database."""

    # -- lifecycle ---------------------------------------------------------

    @abstractmethod
    def check_health(self) -> HealthReport:
        """Run the startup gates: connect, schema version, UPDATE privilege."""

    @abstractmethod
    def ensure_audit_table(self) -> None:
        """Create the state_corrections table if absent. Idempotent."""

    @abstractmethod
    def close(self) -> None:
        """Release any pooled resources."""

    # -- reads -------------------------------------------------------------

    @abstractmethod
    def list_entities(
        self,
        search: str | None = None,
        sensor_type: SensorType | None = None,
        sort: str = "entity_id",
        sort_dir: str = "asc",
        limit: int = 100,
        offset: int = 0,
    ) -> list[Entity]:
        """One page of entities that have recorded history.

        `sort` is one of ENTITY_SORT_KEYS and `sort_dir` one of
        ENTITY_SORT_DIRECTIONS; an adapter falls back to entity_id/asc for
        anything else rather than raising, since this is a display ordering,
        not a correctness-critical input. Filtering and sorting both happen
        before the page is selected, not after, so the result is still one
        page's worth of per-entity lookups regardless of how large `states`
        is — the same flat-at-scale shape as the unsorted, unfiltered case.
        """

    @abstractmethod
    def count_entities(
        self, search: str | None = None, sensor_type: SensorType | None = None
    ) -> int:
        """How many entities the same filter would match in total."""

    @abstractmethod
    def get_entity(self, entity_id: str) -> Entity:
        """One entity, including its sensor type and correction count."""

    @abstractmethod
    def fetch_states(
        self, entity_id: str, start_ts: float, end_ts: float, limit: int = 20000
    ) -> StateSeries:
        """Raw state rows in a time window, oldest first.

        Points carrying an un-restored correction are annotated with that
        correction's id and the original value. The result reports whether the
        window held more rows than the limit allowed, so callers can say so
        rather than presenting a partial graph as a complete one.
        """

    # -- statistics ---------------------------------------------------------
    #
    # Reads only. Recomputing a bucket means reading the states underneath it,
    # running them through hr_statistics, and comparing against what is stored
    # — so the read side has to come first and be trustworthy on its own.

    @abstractmethod
    def get_statistics_metadata(self, entity_id: str) -> StatisticsMetadata | None:
        """The statistics_meta row for an entity, or None if it has none.

        An entity without one produces no statistics at all, so there is
        nothing for a correction to keep consistent.
        """

    @abstractmethod
    def fetch_readings(self, entity_id: str, start_ts: float, end_ts: float) -> list[Reading]:
        """Numeric readings for a bucket, ready for hr_statistics.

        Includes the last reading *before* start_ts, because Home Assistant
        carries it in: it was the value in effect when the bucket opened, and
        both the time-weighted mean and the extremes depend on it.
        Non-numeric states are excluded, exactly as the recorder excludes them.
        """

    @abstractmethod
    def fetch_statistics(
        self,
        metadata_id: int,
        start_ts: float,
        end_ts: float,
        short_term: bool = False,
    ) -> list[StatisticsRow]:
        """Statistics rows in a time window, oldest first.

        `short_term` selects statistics_short_term (5-minute buckets) rather
        than statistics (hourly).
        """

    @abstractmethod
    def counter_cascade_scope(self, entity_id: str, state_ts: float) -> dict[str, int]:
        """How many statistics rows a counter correction here would rewrite.

        Shown before the user commits: the hourly statistics table is never
        purged, so correcting an old energy reading can carry through years of
        running totals. The design document asks for this in 9.5.
        """

    @abstractmethod
    def list_corrections(
        self,
        entity_id: str | None = None,
        include_restored: bool = True,
        limit: int = 500,
    ) -> list[Correction]:
        """The audit trail, newest first."""

    @abstractmethod
    def get_correction(self, correction_id: int) -> Correction:
        """One audit record."""

    # -- writes ------------------------------------------------------------

    @abstractmethod
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
        """Write the audit row and update states in one transaction.

        When `recompute_statistics` is set, the 5-minute and hourly statistics
        buckets containing the corrected state are rebuilt in the same
        transaction, and their previous values are recorded on the audit row so
        a restore can put them back. Without it, only `states` changes and the
        audit row's `stats_corrected` stays false.

        The audit INSERT happens first so a failed UPDATE rolls both back and
        leaves the database untouched (design document, 9.9). Raises
        ConcurrentModification when the stored value no longer matches
        expected_original.
        """

    @abstractmethod
    def restore_correction(self, correction_id: int, restored_by: str) -> Correction:
        """Write the original value back and stamp restored_at, atomically."""

    # -- backup consistency --------------------------------------------------
    #
    # Restoring a Home Assistant backup taken before a correction reverts the
    # states row underneath it without touching the audit table, leaving a
    # correction that claims to be active when it no longer describes what is
    # in the database (design document, 9.11).

    @abstractmethod
    def find_orphaned_corrections(self) -> list[Correction]:
        """Active corrections whose states row no longer holds corrected_value.

        Run on startup. Never applied automatically — the design document is
        explicit that the user decides whether to re-apply (an ordinary new
        correction) or dismiss each one.
        """

    @abstractmethod
    def dismiss_correction(self, correction_id: int, dismissed_by: str) -> Correction:
        """Acknowledge an orphaned correction without touching states.

        Stamps dismissed_at so find_orphaned_corrections stops reporting it.
        Refuses a correction that has already been restored or dismissed.
        """

    # -- bulk correction -----------------------------------------------------
    #
    # One correction applied across every row in a time range — a WiFi outage
    # that produced many bad readings in a row, rather than a single spike.
    # Every row still gets its own audit record and, where applicable, its own
    # statistics recomputation; what is new is that they all commit together,
    # in chronological order, in one transaction.

    @abstractmethod
    def bulk_correction_preview(
        self, entity_id: str, start_ts: float, end_ts: float
    ) -> BulkPreview:
        """How many rows a bulk correction over this range would touch.

        Read-only, so the user can see the scope — and whether interpolation
        is even possible — before committing to anything.
        """

    @abstractmethod
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
        """Correct every eligible row in [start_ts, end_ts) in one transaction.

        `strategy` is "constant" (every row becomes `value`) or "interpolate"
        (each row is linearly interpolated between the nearest numeric reading
        before the range and the nearest one after it). Rows that already
        carry an active correction are skipped rather than failing the whole
        batch — the audit table is the record of what happened either way.
        """
