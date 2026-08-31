"""Plain data structures shared between the database adapters and the web layer.

Pure module: no I/O, no state, no imports from other hr_* modules. These types
are the contract an SQLiteAdapter will have to satisfy in a later phase, so
nothing here may assume a SQL dialect.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum


class SensorType(str, Enum):
    """How a sensor's statistics are aggregated.

    Derived from statistics_meta: a sensor carrying a sum is a counter, one
    carrying a mean is a measurement. Sensors with no statistics row at all
    are UNKNOWN — Home Assistant recorded no state_class for them, so there
    is no cascade to keep in sync, but their states are still correctable.
    """

    MEASUREMENT = "measurement"
    COUNTER = "counter"
    UNKNOWN = "unknown"


class Quality(str, Enum):
    """Why a value was wrong. Mirrors the audit table's ENUM."""

    BAD_COMM = "bad_comm"
    OUT_OF_RANGE = "out_of_range"
    SPIKE = "spike"
    FROZEN = "frozen"
    UNCERTAIN = "uncertain"


@dataclass(frozen=True)
class Entity:
    metadata_id: int
    entity_id: str
    friendly_name: str | None
    sensor_type: SensorType
    unit: str | None
    last_value: str | None
    last_updated_ts: float | None
    correction_count: int = 0


@dataclass(frozen=True)
class StatePoint:
    """One point on the graph — an exact reading, or an hourly average.

    state_id is the primary key of a real states row, and is what corrections
    target. The design document keys corrections on a DATETIME comparison
    against last_updated_ts, but that column is a float epoch: equality after
    a DATETIME round trip is unreliable and cannot use the index.

    state_id is None, and source is "statistics", for a point backfilled from
    Home Assistant's long-term statistics rather than read from states
    directly — see hr_statistics.backfill_from_statistics. This happens when
    a requested range reaches further back than raw states survive (the
    recorder purges them; long-term statistics never are). Such a point has
    no real row to correct: it is an hourly average, not a single reading,
    and even if it were, the states row it was computed from is already gone.
    """

    state_id: int | None
    ts: float
    value: str | None
    numeric_value: float | None
    correction_id: int | None = None
    original_value: str | None = None
    source: str = "state"


@dataclass(frozen=True)
class StateSeries:
    """A window of state rows, and whether the window held more than were read.

    Truncation has to be reported rather than hidden. A 30-day range on a
    sensor reporting every 30 seconds holds far more rows than any sane limit,
    and silently returning the oldest slice would show a user a clean graph
    while the outlier they came to find sat in the part that was dropped.
    """

    points: list[StatePoint]
    truncated: bool = False
    limit: int = 0

    def __len__(self) -> int:
        return len(self.points)


@dataclass(frozen=True)
class StatisticsMetadata:
    """A row of statistics_meta, describing how a sensor is aggregated.

    mean_type is authoritative: Home Assistant stopped populating has_mean once
    it arrived, so a current install has NULL there for every row.
    """

    id: int
    statistic_id: str
    mean_type: int
    has_sum: bool
    unit_of_measurement: str | None


@dataclass(frozen=True)
class StatisticsRow:
    """One row of statistics or statistics_short_term.

    Both tables share these columns; which are populated depends on the sensor
    type. Measurement sensors carry mean/min/max, counters carry state/sum.
    mean_weight is used only for circular means and is otherwise NULL.
    """

    id: int
    metadata_id: int
    start_ts: float
    mean: float | None = None
    mean_weight: float | None = None
    min: float | None = None
    max: float | None = None
    state: float | None = None
    sum: float | None = None


@dataclass(frozen=True)
class Correction:
    id: int
    entity_id: str
    sensor_type: SensorType
    state_id: int | None
    state_ts: float
    original_value: str | None
    corrected_value: str
    quality: Quality
    note: str | None
    created_at: str
    created_by: str
    restored_at: str | None
    restored_by: str | None
    # False for every correction this phase writes: the statistics tables are
    # not touched yet. A later phase uses this flag to find corrections whose
    # statistics buckets still need recomputing.
    stats_corrected: bool = False
    # Set when a user dismisses an orphaned correction (design document 9.11):
    # a backup restore put the states row back to something other than what
    # this correction wrote, and the user chose to accept that rather than
    # re-apply. Distinct from restored_at, which means this add-on itself put
    # the original value back.
    dismissed_at: str | None = None
    dismissed_by: str | None = None


@dataclass(frozen=True)
class BulkPreview:
    """A read-only look at what a bulk correction would touch, before saving.

    Shown to the user so a drag-selected range never becomes a surprise: how
    many rows are in it, how many are already corrected (and would be
    skipped), and whether the range has usable readings on both sides for an
    interpolated fill.
    """

    total: int
    correctable: int
    already_corrected: int
    can_interpolate: bool


@dataclass(frozen=True)
class BulkCorrectionResult:
    """What a bulk correction actually did."""

    entity_id: str
    correction_ids: list[int]
    applied: int
    skipped: int


@dataclass(frozen=True)
class HealthReport:
    """Result of the startup gates described in the design document, 9.1/9.14."""

    connected: bool
    schema_version: int | None = None
    server_version: str | None = None
    can_update: bool = False
    audit_table_ready: bool = False
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return self.connected and not self.errors
