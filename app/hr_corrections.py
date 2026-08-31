"""Correction rules that hold regardless of which database is underneath.

Pure module apart from the adapter it is handed: no SQL, no HTTP, no state.
Everything here is a decision about whether a correction is allowed and what
it should contain, which makes it the natural home for property tests.
"""

from __future__ import annotations

import logging
import math

from hr_db import DatabaseAdapter
from hr_ha_api import HomeAssistantApiError, HomeAssistantClient
from hr_models import (
    BulkCorrectionResult,
    Correction,
    Quality,
    SensorType,
    StatisticsMetadata,
)
from hr_statistics import HOURLY_SECONDS, bucket_start

# A bulk correction that touches this many distinct hours or more is not
# refreshed through the API row by row — one WebSocket round trip per hour
# would make a large, already-slow bulk save slower still for a benefit that
# is cosmetic (a cache refresh, not a correctness one). Past this the user
# sees the usual "restart Home Assistant" guidance instead.
MAX_BULK_CACHE_REFRESH_HOURS = 24

# The states.state column is VARCHAR(255) in every supported schema.
MAX_STATE_LENGTH = 255

# The note column is likewise VARCHAR(255).
MAX_NOTE_LENGTH = 255

# All three sensor types are correctable. A counter took longer because its
# `sum` is cumulative: correcting one reading means rebuilding every running
# total after it, and a spike can additionally look like a meter reset, which
# changes the arithmetic rather than merely offsetting it. UNKNOWN has no
# statistics_meta row at all — Home Assistant recorded no state_class for it —
# so `apply`/`apply_bulk` below find no metadata and simply skip the cascade;
# the correction is states-only.
CORRECTABLE_SENSOR_TYPES = frozenset(
    {SensorType.MEASUREMENT, SensorType.COUNTER, SensorType.UNKNOWN}
)

_LOGGER = logging.getLogger(__name__)


class ValidationError(Exception):
    """The requested correction is not one this phase is willing to make."""


def parse_quality(raw: str | None) -> Quality:
    """Map the UI's quality code to the enum, defaulting to 'uncertain'."""
    if not raw:
        return Quality.UNCERTAIN
    try:
        return Quality(raw)
    except ValueError:
        raise ValidationError(f"Unknown quality code: {raw}") from None


def validate_new_value(raw: object) -> str:
    """Corrections must be a finite number that fits the states.state column."""
    if raw is None:
        raise ValidationError("A corrected value is required.")
    value = str(raw).strip()
    if not value:
        raise ValidationError("A corrected value is required.")
    try:
        number = float(value)
    except ValueError:
        raise ValidationError(
            f"'{value}' is not a number. This add-on corrects numeric sensor values only."
        ) from None
    if math.isnan(number) or math.isinf(number):
        raise ValidationError("The corrected value must be a finite number.")
    if len(value) > MAX_STATE_LENGTH:
        raise ValidationError(
            f"The corrected value is longer than the {MAX_STATE_LENGTH} characters "
            "the states table can hold."
        )
    return value


def validate_sensor_type(sensor_type: SensorType) -> None:
    """Refuse sensor types whose statistics this phase cannot keep consistent."""
    if sensor_type in CORRECTABLE_SENSOR_TYPES:
        return
    raise ValidationError(
        "This entity has no measurement statistics, so its sensor type cannot be "
        "confirmed. Only measurement sensors can be corrected in this version."
    )


def normalise_note(note: str | None) -> str | None:
    """Trim a note to what the column holds; empty notes are stored as NULL."""
    if note is None:
        return None
    trimmed = note.strip()[:MAX_NOTE_LENGTH]
    return trimmed or None


def apply(
    adapter: DatabaseAdapter,
    *,
    entity_id: str,
    state_id: int,
    expected_original: str | None,
    new_value: object,
    quality: str | None,
    note: str | None,
    created_by: str,
) -> Correction:
    """Validate a correction request, then hand it to the adapter to commit."""
    entity = adapter.get_entity(entity_id)
    validate_sensor_type(entity.sensor_type)
    value = validate_new_value(new_value)
    parsed_quality = parse_quality(quality)

    try:
        target_row = int(state_id)
    except (TypeError, ValueError):
        raise ValidationError(f"'{state_id}' is not a valid state row id.") from None

    # A sensor with statistics needs its 5-minute and hourly buckets rebuilt
    # in the same transaction, or the statistics graph and the energy
    # dashboard keep showing the outlier the history graph no longer has. A
    # sensor without them has nothing to keep consistent.
    has_statistics = adapter.get_statistics_metadata(entity_id) is not None

    return adapter.apply_correction(
        entity_id=entity_id,
        state_id=target_row,
        expected_original=expected_original,
        new_value=value,
        quality=parsed_quality,
        note=normalise_note(note),
        created_by=created_by,
        recompute_statistics=has_statistics,
    )


VALID_BULK_STRATEGIES = frozenset({"constant", "interpolate"})


def apply_bulk(
    adapter: DatabaseAdapter,
    *,
    entity_id: str,
    start_ts: float,
    end_ts: float,
    strategy: str,
    value: object,
    quality: str | None,
    note: str | None,
    created_by: str,
) -> BulkCorrectionResult:
    """Validate a bulk correction request, then hand it to the adapter.

    Everything that can be checked without reading the range itself is
    checked here — the strategy name, the range bounds, a constant value's
    shape. Whether the range is too large, or has readings on both sides to
    interpolate between, can only be known once the adapter reads it, so
    those checks live there and surface as InvalidRange.
    """
    entity = adapter.get_entity(entity_id)
    validate_sensor_type(entity.sensor_type)

    if start_ts >= end_ts:
        raise ValidationError("The start of the range must come before its end.")

    if strategy not in VALID_BULK_STRATEGIES:
        raise ValidationError(f"Unknown strategy '{strategy}'. Use 'constant' or 'interpolate'.")

    parsed_value = validate_new_value(value) if strategy == "constant" else None
    parsed_quality = parse_quality(quality)

    return adapter.apply_bulk_correction(
        entity_id=entity_id,
        start_ts=start_ts,
        end_ts=end_ts,
        strategy=strategy,
        value=parsed_value,
        quality=parsed_quality,
        note=normalise_note(note),
        created_by=created_by,
    )


def _refresh_hour(
    adapter: DatabaseAdapter,
    client: HomeAssistantClient,
    entity_id: str,
    metadata: StatisticsMetadata,
    hour_start: float,
) -> bool:
    """Push one already-corrected hourly bucket back through the API."""
    rows = adapter.fetch_statistics(metadata.id, hour_start, hour_start + HOURLY_SECONDS)
    if not rows:
        return False

    row = rows[0]
    try:
        client.import_statistics(
            statistic_id=entity_id,
            unit_of_measurement=metadata.unit_of_measurement,
            mean_type=metadata.mean_type,
            has_sum=metadata.has_sum,
            unit_class=None,
            stats=[
                {
                    "start_ts": row.start_ts,
                    "mean": row.mean,
                    "min": row.min,
                    "max": row.max,
                }
            ],
        )
    except HomeAssistantApiError as err:
        # Expected often enough to be routine: the add-on may not have API
        # access, or Home Assistant may be restarting. The correction stands.
        _LOGGER.info("Could not refresh Home Assistant's statistics cache: %s", err)
        return False
    return True


def refresh_statistics_cache(
    adapter: DatabaseAdapter,
    client: HomeAssistantClient | None,
    correction: Correction,
) -> bool:
    """Tell Home Assistant about one corrected hourly bucket.

    The database is already correct — the correction transaction saw to both
    statistics tables. What is not yet correct is Home Assistant's *in-memory*
    copy, which is what a statistics card and the energy dashboard actually
    render. Feeding the corrected hourly row back through
    recorder/import_statistics makes Home Assistant rewrite it itself, which
    refreshes that cache and removes the need to restart.

    Two things constrain the design, both established by experiment:

    * The API accepts only timestamps on an hour boundary, so
      statistics_short_term has no API at all and can never be refreshed this
      way. It is written by SQL inside the transaction and nowhere else.
    * The write is asynchronous and outside our transaction, so it happens
      after the commit. Doing it inside would hold row locks across a network
      call, which is exactly how a database ends up deadlocked against the
      recorder.

    Returns True when Home Assistant accepted the update. False means the
    database is still correct but the user may need to restart Home Assistant
    for the change to appear — never that the correction failed.
    """
    if client is None or not correction.stats_corrected:
        return False

    metadata = adapter.get_statistics_metadata(correction.entity_id)
    if metadata is None:
        return False

    hour_start = bucket_start(correction.state_ts, HOURLY_SECONDS)
    return _refresh_hour(adapter, client, correction.entity_id, metadata, hour_start)


def refresh_statistics_cache_bulk(
    adapter: DatabaseAdapter,
    client: HomeAssistantClient | None,
    result: BulkCorrectionResult,
) -> int:
    """Tell Home Assistant about every hour a bulk correction touched.

    Each distinct hour is pushed through the API once, however many of its
    corrected rows share it — a WiFi outage spanning ninety 5-minute readings
    might still only touch two or three hourly buckets. Returns how many hours
    were refreshed; the rest, like a single correction's failure, cost a
    restart rather than correctness.
    """
    if client is None or result.applied == 0:
        return 0

    metadata = adapter.get_statistics_metadata(result.entity_id)
    if metadata is None:
        return 0

    hours: set[float] = set()
    for correction_id in result.correction_ids:
        correction = adapter.get_correction(correction_id)
        if correction.stats_corrected:
            hours.add(bucket_start(correction.state_ts, HOURLY_SECONDS))

    if len(hours) > MAX_BULK_CACHE_REFRESH_HOURS:
        _LOGGER.info(
            "Bulk correction touched %d hours, above the %d-hour cache-refresh cap; "
            "skipping the API refresh for %s.",
            len(hours),
            MAX_BULK_CACHE_REFRESH_HOURS,
            result.entity_id,
        )
        return 0

    return sum(_refresh_hour(adapter, client, result.entity_id, metadata, hour) for hour in hours)
