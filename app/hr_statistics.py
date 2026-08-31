"""Home Assistant's statistics arithmetic, reimplemented for recomputation.

When a state inside a statistics bucket is corrected, that bucket's stored
mean, min and max no longer describe the data underneath it. Putting them
right means reproducing Home Assistant's own arithmetic exactly — a value that
merely looks reasonable would leave the statistics graph disagreeing with the
history graph forever.

There is no documentation of that arithmetic. What is implemented here was
derived from Home Assistant's source and then verified empirically against a
running instance by dev/statistics_truth.py, which compares these formulas to
the values a real recorder stored.

The important finding, and the reason this module exists rather than a line of
SQL: **the 5-minute mean is time-weighted, not a plain average.** Each reading
counts for as long as it was in effect. The design document's

    UPDATE statistics_short_term SET mean = (SELECT AVG(CAST(state AS FLOAT)) ...)

produces a different number whenever readings are unevenly spaced, which is
almost always — Home Assistant records on change, not on a timer.

Pure module: no I/O, no state, no SQL. Imports hr_models for its shared,
equally pure StatePoint/StatisticsRow types — used by backfill_from_statistics
below — rather than defining a third, redundant pair of data classes.
"""

from __future__ import annotations

from dataclasses import dataclass

from hr_models import StatePoint, StatisticsRow

# Home Assistant's bucket widths. statistics_short_term rows cover five
# minutes; statistics rows cover an hour and summarise twelve of them.
SHORT_TERM_SECONDS = 300
HOURLY_SECONDS = 3600


@dataclass(frozen=True)
class Reading:
    """One numeric state value and the moment it took effect."""

    value: float
    ts: float


@dataclass(frozen=True)
class MeasurementBucket:
    """The measurement columns of a statistics row.

    All three are None for a bucket with nothing to summarise, which is what
    Home Assistant stores when a sensor reported nothing and nothing was in
    effect from before.
    """

    start_ts: float
    mean: float | None
    min: float | None
    max: float | None


def time_weighted_mean(readings: list[Reading], start: float, end: float) -> float | None:
    """Average the readings by how long each was in effect.

    `readings` must be ordered by timestamp and should include the last
    reading before `start`: Home Assistant carries that one in, because it is
    the value that was in effect when the bucket opened. Values are never
    interpolated between readings — each simply holds until the next.

    When no reading was in effect at the start of the bucket, the averaging
    window begins at the first reading instead, so the denominator shrinks
    rather than the leading gap counting as zero.
    """
    if not readings:
        return None

    # Value and the moment it took effect are carried together so the two can
    # never be half-set — which is also what lets this avoid an assert, since
    # asserts are stripped under python -O and would leave the invariant
    # unchecked in exactly the builds that ship.
    previous: tuple[float, float] | None = None
    accumulated = 0.0
    window_start = start

    for reading in readings:
        effective_from = max(reading.ts, window_start)
        if previous is None:
            window_start = effective_from
        else:
            previous_value, previous_from = previous
            accumulated += previous_value * (effective_from - previous_from)
        previous = (reading.value, effective_from)

    if previous is None:
        return None

    last_value, last_from = previous
    accumulated += last_value * (end - last_from)

    span = end - window_start
    if span <= 0:
        # A zero-width window carries no duration to weight by; the single
        # value in effect is the only sensible answer.
        return last_value
    return accumulated / span


def recompute_short_term(
    readings: list[Reading], start_ts: float, duration: float = SHORT_TERM_SECONDS
) -> MeasurementBucket:
    """Rebuild one 5-minute bucket from the readings behind it.

    min and max are plain extremes over every reading Home Assistant considers
    for the bucket — including the carried-in one, because the value in effect
    when the bucket opened genuinely occurred during it.
    """
    if not readings:
        return MeasurementBucket(start_ts=start_ts, mean=None, min=None, max=None)

    values = [reading.value for reading in readings]
    return MeasurementBucket(
        start_ts=start_ts,
        mean=time_weighted_mean(readings, start_ts, start_ts + duration),
        min=min(values),
        max=max(values),
    )


def summarise_hourly(buckets: list[MeasurementBucket], start_ts: float) -> MeasurementBucket:
    """Summarise the 5-minute buckets of one hour into the hourly row.

    Unlike the 5-minute mean, this one really is a plain unweighted average of
    the bucket means — Home Assistant's hourly query is AVG(short_term.mean),
    so an hour with fewer buckets than usual is not compensated for. The
    design document is right about this one.
    """
    means = [b.mean for b in buckets if b.mean is not None]
    mins = [b.min for b in buckets if b.min is not None]
    maxes = [b.max for b in buckets if b.max is not None]

    return MeasurementBucket(
        start_ts=start_ts,
        mean=sum(means) / len(means) if means else None,
        min=min(mins) if mins else None,
        max=max(maxes) if maxes else None,
    )


def bucket_start(ts: float, width: float) -> float:
    """The start of the bucket of `width` seconds that contains `ts`."""
    return (ts // width) * width


# Home Assistant treats a total_increasing sensor as having been reset when a
# reading falls below this fraction of the previous one — a meter replaced, or
# rolled over. A smaller dip is only warned about, not treated as a reset.
# From homeassistant.components.sensor.recorder.reset_detected.
RESET_RATIO = 0.9


@dataclass(frozen=True)
class CounterBucket:
    """The counter columns of a statistics row.

    `state` is the meter reading at the end of the bucket. `sum` is the total
    increase since the sensor's first record — a running total, which is why
    an error in one bucket travels forward through every later one.
    """

    start_ts: float
    state: float | None
    sum: float | None


def is_reset(state: float, previous_state: float | None, total_increasing: bool) -> bool:
    """Would Home Assistant read this transition as a meter reset?"""
    if not total_increasing or previous_state is None:
        return False
    return state < RESET_RATIO * previous_state


def cascade_sums(
    buckets: list[CounterBucket],
    previous_state: float | None,
    previous_sum: float,
    total_increasing: bool = True,
) -> list[CounterBucket]:
    """Rebuild the running totals of consecutive buckets.

    Verified against a real recorder: for an ordinary rising counter each
    bucket's sum increases by exactly the increase in its meter reading, so the
    chain is `sum[n] = sum[n-1] + (state[n] - state[n-1])`.

    A reset breaks that. When Home Assistant decides the meter has restarted it
    begins a fresh cycle from zero, and the bucket contributes its whole
    reading rather than the difference. This is why the design document's model
    — subtract a constant delta from every later sum — is not safe in general:
    a spike followed by a return to normal looks exactly like a reset, so a
    correction can change *whether* a reset fires, and the offset between the
    old and new chains is then not constant.

    Buckets with no reading carry the running total forward unchanged, which is
    what a gap in the data means: nothing was consumed that we know of.
    """
    rebuilt: list[CounterBucket] = []
    running_state = previous_state
    running_sum = previous_sum

    for bucket in buckets:
        if bucket.state is None:
            rebuilt.append(CounterBucket(bucket.start_ts, None, running_sum))
            continue

        if running_state is None:
            # No earlier reading to measure against: this is the zero point,
            # exactly as Home Assistant treats the first record of a sensor.
            increase = 0.0
        elif is_reset(bucket.state, running_state, total_increasing):
            # The cycle restarts at zero, so the whole reading is new
            # consumption rather than the difference from a reading that no
            # longer means anything.
            increase = bucket.state
        else:
            increase = bucket.state - running_state

        running_sum += increase
        running_state = bucket.state
        rebuilt.append(CounterBucket(bucket.start_ts, bucket.state, running_sum))

    return rebuilt


def backfill_from_statistics(
    points: list[StatePoint],
    statistics_rows: list[StatisticsRow],
    is_counter: bool,
) -> list[StatePoint]:
    """Fill the gap before the earliest raw reading with hourly averages.

    A requested graph range can reach further back than raw `states` survive
    — the recorder purges them (10 days by default); long-term statistics
    never are, the same distinction Home Assistant's own history graph
    already makes (see long-term-statistics-graph-design.md). Only
    statistics rows strictly before the earliest raw point are used, so the
    raw and aggregated segments never overlap: a period where both exist is
    always shown as the exact reading, never the average standing in for it.

    Each backfilled point carries `state_id=None` and `source="statistics"`
    — there is no real row underneath it. It is an hourly mean or a meter
    reading Home Assistant already computed, not a single event with one
    true value to restore, and even if it were, the states row it came from
    is already gone. Both are why it cannot be corrected, and both are
    stated explicitly to the user (entity.js), not left implied by its
    different appearance alone.
    """
    boundary = points[0].ts if points else float("inf")
    backfilled = [
        _statistics_row_to_point(row, is_counter)
        for row in statistics_rows
        if row.start_ts < boundary
    ]
    return backfilled + points


def _statistics_row_to_point(row: StatisticsRow, is_counter: bool) -> StatePoint:
    # A counter's own stored value is what it read at the bucket's end — the
    # meter reading, not a mean of anything (CLAUDE.md's statistics-arithmetic
    # notes: the hourly state/sum are copied from the last 5-minute row of the
    # hour, never summed or averaged). A measurement sensor's representative
    # value is its time-weighted mean over the hour.
    raw = row.state if is_counter else row.mean
    numeric_value = float(raw) if raw is not None else None
    return StatePoint(
        state_id=None,
        ts=row.start_ts,
        value=repr(numeric_value) if numeric_value is not None else None,
        numeric_value=numeric_value,
        source="statistics",
    )
