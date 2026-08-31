"""Tests for hr_statistics — Home Assistant's bucket arithmetic.

This module is pure, and the properties it must satisfy are mathematical, so
most of these are property tests. The specific examples pin the behaviour that
distinguishes a time-weighted mean from a plain average, because getting that
wrong is silent: the number still looks plausible, it is simply not the one
Home Assistant would have produced.

The formulas themselves were verified against a running Home Assistant by
dev/statistics_truth.py; these tests then keep them from drifting.
"""

from __future__ import annotations

from itertools import pairwise

import pytest
from hypothesis import assume, example, given
from hypothesis import strategies as st

from hr_models import StatePoint, StatisticsRow
from hr_statistics import (
    HOURLY_SECONDS,
    SHORT_TERM_SECONDS,
    CounterBucket,
    MeasurementBucket,
    Reading,
    backfill_from_statistics,
    bucket_start,
    cascade_sums,
    is_reset,
    recompute_short_term,
    summarise_hourly,
    time_weighted_mean,
)

START = 1_700_000_000.0
END = START + SHORT_TERM_SECONDS


def _readings(*pairs: tuple[float, float]) -> list[Reading]:
    """Build readings from (value, seconds after the bucket start) pairs."""
    return [Reading(value=value, ts=START + offset) for value, offset in pairs]


class TestTimeWeightedMean:
    def test_a_single_reading_holding_all_period_is_its_own_mean(self) -> None:
        assert time_weighted_mean(_readings((20.0, 0)), START, END) == 20.0

    def test_weights_by_duration_not_by_count(self) -> None:
        # 10 for the first 240s, 20 for the last 60s. A plain average would
        # say 15; the honest answer is much closer to 10.
        readings = _readings((10.0, 0), (20.0, 240))
        expected = (10.0 * 240 + 20.0 * 60) / 300
        assert time_weighted_mean(readings, START, END) == pytest.approx(expected)
        assert expected == pytest.approx(12.0)

    def test_differs_from_a_plain_average_when_spacing_is_uneven(self) -> None:
        # The case the design document's AVG(state) gets wrong. Recording on
        # change rather than on a timer makes this the normal case.
        readings = _readings((0.0, 0), (100.0, 290))
        weighted = time_weighted_mean(readings, START, END)
        plain = (0.0 + 100.0) / 2
        assert weighted == pytest.approx((0.0 * 290 + 100.0 * 10) / 300)
        assert weighted != pytest.approx(plain)

    def test_a_reading_from_before_the_bucket_is_weighted_from_the_start(self) -> None:
        # Home Assistant carries in the last reading before the bucket: it is
        # the value that was in effect when the bucket opened.
        readings = [Reading(value=5.0, ts=START - 1000), Reading(value=15.0, ts=START + 150)]
        expected = (5.0 * 150 + 15.0 * 150) / 300
        assert time_weighted_mean(readings, START, END) == pytest.approx(expected)

    def test_the_window_shrinks_when_nothing_was_in_effect_at_the_start(self) -> None:
        # With no carried-in reading, averaging begins at the first reading
        # rather than counting the leading gap as zero.
        readings = _readings((10.0, 120))
        assert time_weighted_mean(readings, START, END) == pytest.approx(10.0)

    def test_no_readings_gives_no_mean(self) -> None:
        assert time_weighted_mean([], START, END) is None

    def test_a_zero_width_window_returns_the_last_value_without_dividing(self) -> None:
        # span == end - window_start == 0 here: the shortcut that avoids a
        # division by zero. A weakened "< 0" instead of "<= 0" would fall
        # through to that division and crash instead of taking it.
        readings = [Reading(value=42.0, ts=START)]
        assert time_weighted_mean(readings, START, START) == 42.0

    def test_a_short_but_nonzero_window_is_still_a_real_average(self) -> None:
        # span == 0.5, strictly positive: must still divide for real rather
        # than falling into the zero-width shortcut (which would wrongly
        # return the last reading's value, 20.0, instead of the weighted
        # average, 14.0).
        readings = [Reading(value=10.0, ts=START), Reading(value=20.0, ts=START + 0.3)]
        assert time_weighted_mean(readings, START, START + 0.5) == pytest.approx(14.0)

    @given(
        value=st.floats(min_value=-1e6, max_value=1e6, allow_nan=False, allow_infinity=False),
        count=st.integers(min_value=1, max_value=20),
    )
    def test_a_constant_series_averages_to_that_constant(self, value: float, count: int) -> None:
        step = SHORT_TERM_SECONDS / (count + 1)
        readings = [Reading(value=value, ts=START + i * step) for i in range(count)]
        result = time_weighted_mean(readings, START, END)
        assert result == pytest.approx(value, rel=1e-9, abs=1e-9)

    @given(
        values=st.lists(
            st.floats(min_value=-1000, max_value=1000, allow_nan=False, allow_infinity=False),
            min_size=1,
            max_size=15,
        )
    )
    def test_the_mean_never_leaves_the_range_of_its_readings(self, values: list[float]) -> None:
        step = SHORT_TERM_SECONDS / (len(values) + 1)
        readings = [Reading(value=v, ts=START + i * step) for i, v in enumerate(values)]
        result = time_weighted_mean(readings, START, END)
        assert result is not None
        assert min(values) - 1e-9 <= result <= max(values) + 1e-9

    @given(
        values=st.lists(
            st.floats(min_value=-1000, max_value=1000, allow_nan=False, allow_infinity=False),
            min_size=2,
            max_size=12,
        )
    )
    @example([0.0, 0.0, 0.0, 243.0, 261.0, 634.0, 282.0, -213.0, -479.0, -480.0, -247.0])
    def test_evenly_spaced_readings_agree_with_a_plain_average(self, values: list[float]) -> None:
        # A useful sanity check on the whole idea: when every reading is in
        # effect for the same duration, weighting by duration must collapse to
        # the ordinary average. This is also why an evenly-sampled fixture
        # cannot tell the two formulas apart.
        count = len(values)
        step = SHORT_TERM_SECONDS / count
        readings = [Reading(value=v, ts=START + i * step) for i, v in enumerate(values)]
        result = time_weighted_mean(readings, START, END)
        # The tolerance is not slack, it is the resolution of the input: an
        # epoch timestamp near 1.7e9 has about 2e-7 of absolute precision in a
        # float64, so "evenly spaced" readings are not exactly evenly spaced
        # once added to it, and the durations differ in their last bits. Any
        # comparison against Home Assistant's stored values has to allow for
        # the same thing — dev/statistics_truth.py uses 1e-6 for this reason.
        #
        # The absolute half is scaled by the input's own magnitude, not a
        # fixed 1e-9: found by a 12-run parallel-safety check to fail
        # intermittently on inputs summing large, cancelling values (e.g.
        # [..., 634.0, -480.0, ...] averaging near 0.09) — the rounding error
        # from many float additions of the large terms is proportional to
        # their magnitude, not to the tiny cancelled result, so a tolerance
        # keyed to the result alone was too tight for exactly that case.
        scale = max((abs(v) for v in values), default=1.0) or 1.0
        assert result == pytest.approx(sum(values) / count, rel=1e-6, abs=scale * 1e-6)


class TestRecomputeShortTerm:
    def test_produces_mean_min_and_max(self) -> None:
        bucket = recompute_short_term(_readings((10.0, 0), (30.0, 150)), START)
        assert bucket.start_ts == START
        assert bucket.min == 10.0
        assert bucket.max == 30.0
        assert bucket.mean == pytest.approx(20.0)

    def test_min_and_max_include_the_carried_in_reading(self) -> None:
        # The value in effect when the bucket opened genuinely occurred during
        # it, so it counts towards the extremes.
        readings = [Reading(value=-50.0, ts=START - 60), Reading(value=10.0, ts=START + 60)]
        bucket = recompute_short_term(readings, START)
        assert bucket.min == -50.0

    def test_an_empty_bucket_has_no_values(self) -> None:
        bucket = recompute_short_term([], START)
        assert (bucket.mean, bucket.min, bucket.max) == (None, None, None)
        assert bucket.start_ts == START

    def test_correcting_an_outlier_moves_the_bucket(self) -> None:
        # The whole point of Phase 2, in miniature: with the spike present the
        # bucket is dominated by it; once corrected the bucket describes the
        # real data.
        with_spike = recompute_short_term(_readings((20.0, 0), (-2000.0, 100), (20.5, 200)), START)
        corrected = recompute_short_term(_readings((20.0, 0), (20.2, 100), (20.5, 200)), START)
        assert with_spike.min == -2000.0
        assert corrected.min == 20.0
        assert corrected.mean is not None and 20.0 <= corrected.mean <= 20.5


class TestSummariseHourly:
    def test_mean_is_a_plain_average_of_the_bucket_means(self) -> None:
        # Deliberately unweighted: Home Assistant's hourly query is
        # AVG(short_term.mean), so a short hour is not compensated for.
        buckets = [
            MeasurementBucket(start_ts=START, mean=10.0, min=5.0, max=15.0),
            MeasurementBucket(start_ts=START + 300, mean=20.0, min=18.0, max=25.0),
        ]
        hourly = summarise_hourly(buckets, START)
        assert hourly.start_ts == START
        assert hourly.mean == pytest.approx(15.0)
        assert hourly.min == 5.0
        assert hourly.max == 25.0

    def test_buckets_without_a_mean_are_ignored(self) -> None:
        buckets = [
            MeasurementBucket(start_ts=START, mean=10.0, min=10.0, max=10.0),
            MeasurementBucket(start_ts=START + 300, mean=None, min=None, max=None),
        ]
        assert summarise_hourly(buckets, START).mean == pytest.approx(10.0)

    def test_an_empty_hour_has_no_values(self) -> None:
        hourly = summarise_hourly([], START)
        assert (hourly.mean, hourly.min, hourly.max) == (None, None, None)

    @given(
        means=st.lists(
            st.floats(min_value=-1000, max_value=1000, allow_nan=False, allow_infinity=False),
            min_size=1,
            max_size=12,
        )
    )
    def test_the_hourly_mean_stays_within_the_bucket_means(self, means: list[float]) -> None:
        buckets = [
            MeasurementBucket(start_ts=START + i * 300, mean=m, min=m, max=m)
            for i, m in enumerate(means)
        ]
        hourly = summarise_hourly(buckets, START)
        assert hourly.mean is not None
        assert min(means) - 1e-9 <= hourly.mean <= max(means) + 1e-9


class TestBucketStart:
    @given(ts=st.floats(min_value=0, max_value=2e9, allow_nan=False, allow_infinity=False))
    def test_short_term_boundaries_are_multiples_of_five_minutes(self, ts: float) -> None:
        start = bucket_start(ts, SHORT_TERM_SECONDS)
        assert start % SHORT_TERM_SECONDS == 0
        assert start <= ts < start + SHORT_TERM_SECONDS

    @given(ts=st.floats(min_value=0, max_value=2e9, allow_nan=False, allow_infinity=False))
    def test_hourly_boundaries_are_multiples_of_an_hour(self, ts: float) -> None:
        start = bucket_start(ts, HOURLY_SECONDS)
        assert start % HOURLY_SECONDS == 0
        assert start <= ts < start + HOURLY_SECONDS

    @given(ts=st.floats(min_value=0, max_value=2e9, allow_nan=False, allow_infinity=False))
    def test_an_hourly_bucket_starts_on_a_short_term_boundary(self, ts: float) -> None:
        # Twelve short-term buckets tile one hour exactly, which the
        # recomputation relies on when it walks an hour's buckets.
        assume(ts > 0)
        hourly = bucket_start(ts, HOURLY_SECONDS)
        assert bucket_start(hourly, SHORT_TERM_SECONDS) == hourly


class TestCascadeSums:
    """The running total of a counter, and what a correction does to it.

    Verified against a real recorder: for an ordinary rising counter, each
    bucket's sum increases by exactly the increase in its meter reading.
    """

    def _buckets(self, *states: float | None) -> list[CounterBucket]:
        return [
            CounterBucket(start_ts=START + i * SHORT_TERM_SECONDS, state=s, sum=None)
            for i, s in enumerate(states)
        ]

    def test_sum_follows_the_meter_reading(self) -> None:
        buckets = self._buckets(110.0, 120.0, 135.0)
        rebuilt = cascade_sums(buckets, previous_state=100.0, previous_sum=50.0)
        assert [b.sum for b in rebuilt] == [60.0, 70.0, 85.0]
        assert [b.start_ts for b in rebuilt] == [b.start_ts for b in buckets]
        assert [b.state for b in rebuilt] == [110.0, 120.0, 135.0]

    def test_a_gap_keeps_the_original_buckets_own_timestamp(self) -> None:
        buckets = self._buckets(110.0, None, 130.0)
        rebuilt = cascade_sums(buckets, previous_state=100.0, previous_sum=0.0)
        assert [b.start_ts for b in rebuilt] == [b.start_ts for b in buckets]

    def test_the_increments_match_the_state_increments(self) -> None:
        # The relationship measured on a live Home Assistant counter.
        states = [1005.61, 1011.25, 1016.7, 1022.83]
        rebuilt = cascade_sums(
            self._buckets(*states[1:]), previous_state=states[0], previous_sum=5.5
        )
        sums = [b.sum for b in rebuilt]
        assert sums[0] == pytest.approx(5.5 + (states[1] - states[0]))
        assert sums[-1] == pytest.approx(5.5 + (states[-1] - states[0]))

    def test_a_reset_starts_a_fresh_cycle_from_zero(self) -> None:
        # A meter replaced or rolled over: the new reading is all new
        # consumption, not a negative difference from a reading that no longer
        # means anything.
        rebuilt = cascade_sums(self._buckets(5.0), previous_state=1000.0, previous_sum=900.0)
        assert rebuilt[0].sum == pytest.approx(905.0)

    def test_a_small_dip_is_not_a_reset(self) -> None:
        # Above 90% of the previous reading, Home Assistant only warns.
        rebuilt = cascade_sums(self._buckets(95.0), previous_state=100.0, previous_sum=50.0)
        assert rebuilt[0].sum == pytest.approx(45.0)

    def test_the_reset_threshold_is_exactly_ninety_percent(self) -> None:
        assert is_reset(89.9, 100.0, True) is True
        assert is_reset(90.0, 100.0, True) is False

    def test_a_total_sensor_never_resets_on_a_drop(self) -> None:
        # Reset detection by value applies to total_increasing only. A `total`
        # sensor signals its cycles with last_reset instead.
        rebuilt = cascade_sums(
            self._buckets(5.0), previous_state=1000.0, previous_sum=900.0, total_increasing=False
        )
        assert rebuilt[0].sum == pytest.approx(900.0 + (5.0 - 1000.0))

    def test_a_gap_carries_the_total_forward(self) -> None:
        rebuilt = cascade_sums(
            self._buckets(110.0, None, 130.0), previous_state=100.0, previous_sum=0.0
        )
        assert [b.sum for b in rebuilt] == [10.0, 10.0, 30.0]

    def test_the_first_ever_reading_is_the_zero_point(self) -> None:
        rebuilt = cascade_sums(self._buckets(500.0, 510.0), previous_state=None, previous_sum=0.0)
        assert rebuilt[0].sum == 0.0
        assert rebuilt[1].sum == pytest.approx(10.0)

    def test_correcting_a_spike_changes_the_total_by_more_than_the_spike(self) -> None:
        """Why the design document's constant-delta model is unsafe.

        A spike followed by a return to normal reads as a meter reset, so the
        running total is inflated twice: once by the spike itself, and again
        when the next reading starts a fresh cycle. Removing the spike removes
        both, and the difference between the two chains is not the difference
        between the two readings.
        """
        with_spike = cascade_sums(
            self._buckets(1010.0, 99999.0, 1020.0), previous_state=1000.0, previous_sum=0.0
        )
        corrected = cascade_sums(
            self._buckets(1010.0, 1015.0, 1020.0), previous_state=1000.0, previous_sum=0.0
        )

        naive_delta = 99999.0 - 1015.0
        actual_difference = with_spike[-1].sum - corrected[-1].sum  # type: ignore[operator]
        assert actual_difference != pytest.approx(naive_delta)
        # The spike also triggered a reset, so the real damage is larger still.
        assert actual_difference > naive_delta

    @given(
        increments=st.lists(
            st.floats(min_value=0.0, max_value=100.0, allow_nan=False, allow_infinity=False),
            min_size=1,
            max_size=20,
        ),
        start=st.floats(min_value=1.0, max_value=10_000.0, allow_nan=False, allow_infinity=False),
    )
    def test_a_rising_counter_totals_its_increments(
        self, increments: list[float], start: float
    ) -> None:
        # No reset can fire while the reading only ever rises, so the final sum
        # is simply everything that was consumed.
        states: list[float | None] = []
        value = start
        for increment in increments:
            value += increment
            states.append(value)

        rebuilt = cascade_sums(self._buckets(*states), previous_state=start, previous_sum=0.0)
        assert rebuilt[-1].sum == pytest.approx(sum(increments), rel=1e-6, abs=1e-6)

    def test_a_small_dip_reduces_the_running_total(self) -> None:
        """Surprising, and Home Assistant's real behaviour.

        A drop of less than ten percent is not a reset, so the difference is
        applied as it stands — a negative contribution to a total that is
        supposed to only ever rise. Home Assistant logs a "dip" warning and
        leaves it. This was found by a property test asserting the total never
        decreases; the assertion was wrong, not the code.

        It is also a reason this project exists: a dip like that is exactly the
        kind of glitch a user would want to correct.
        """
        rebuilt = cascade_sums(self._buckets(18.0), previous_state=19.0, previous_sum=100.0)
        assert rebuilt[0].sum == pytest.approx(99.0)

    @given(
        increments=st.lists(
            st.floats(min_value=0.0, max_value=100.0, allow_nan=False, allow_infinity=False),
            min_size=1,
            max_size=15,
        ),
        start=st.floats(min_value=1.0, max_value=1000.0, allow_nan=False, allow_infinity=False),
    )
    def test_the_total_never_decreases_while_the_meter_only_rises(
        self, increments: list[float], start: float
    ) -> None:
        # The property that makes an energy dashboard meaningful, stated
        # correctly: it holds while readings rise, which is what a
        # total_increasing sensor is supposed to do.
        states: list[float | None] = []
        value = start
        for increment in increments:
            value += increment
            states.append(value)

        rebuilt = cascade_sums(self._buckets(*states), previous_state=start, previous_sum=0.0)
        sums = [b.sum for b in rebuilt if b.sum is not None]
        assert all(later >= earlier - 1e-9 for earlier, later in pairwise(sums))


class TestBackfillFromStatistics:
    """backfill_from_statistics: filling the gap before the earliest raw
    reading with hourly averages, for a range that reaches further back than
    raw states survive — see long-term-statistics-graph-design.md."""

    @staticmethod
    def _row(
        start_ts: float, *, mean: float | None = None, state: float | None = None
    ) -> StatisticsRow:
        return StatisticsRow(id=1, metadata_id=1, start_ts=start_ts, mean=mean, state=state)

    @staticmethod
    def _point(ts: float, state_id: int = 1) -> StatePoint:
        return StatePoint(state_id=state_id, ts=ts, value="1.0", numeric_value=1.0)

    def test_no_statistics_rows_leaves_the_points_unchanged(self) -> None:
        points = [self._point(START), self._point(START + 300)]
        assert backfill_from_statistics(points, [], is_counter=False) == points

    def test_no_raw_points_backfills_everything(self) -> None:
        rows = [self._row(START, mean=1.0), self._row(START + HOURLY_SECONDS, mean=2.0)]
        result = backfill_from_statistics([], rows, is_counter=False)
        assert len(result) == 2
        assert all(p.source == "statistics" for p in result)
        assert all(p.state_id is None for p in result)

    def test_only_rows_before_the_earliest_raw_point_are_used(self) -> None:
        # A statistics row landing inside the raw segment's time range must
        # never appear too — the two segments never overlap, and the exact
        # reading always wins where both exist.
        raw = [self._point(START + 10 * HOURLY_SECONDS)]
        rows = [
            self._row(START, mean=1.0),
            self._row(START + 5 * HOURLY_SECONDS, mean=2.0),
            # Same instant as the earliest raw point — excluded, not just
            # anything strictly after it.
            self._row(START + 10 * HOURLY_SECONDS, mean=3.0),
        ]
        result = backfill_from_statistics(raw, rows, is_counter=False)
        assert [p.ts for p in result] == [
            START,
            START + 5 * HOURLY_SECONDS,
            START + 10 * HOURLY_SECONDS,
        ]
        assert [p.source for p in result] == ["statistics", "statistics", "state"]

    def test_backfilled_points_come_before_raw_ones_in_order(self) -> None:
        raw = [self._point(START + HOURLY_SECONDS)]
        rows = [self._row(START, mean=1.0)]
        result = backfill_from_statistics(raw, rows, is_counter=False)
        assert [p.ts for p in result] == [START, START + HOURLY_SECONDS]

    def test_measurement_sensors_use_the_mean(self) -> None:
        rows = [self._row(START, mean=42.5, state=999.0)]
        result = backfill_from_statistics([], rows, is_counter=False)
        assert result[0].numeric_value == 42.5
        assert result[0].value == repr(42.5)

    def test_counters_use_the_state_not_the_mean(self) -> None:
        # The hourly state/sum are what Home Assistant itself copies from the
        # last 5-minute row of the hour — never an average — per
        # CLAUDE.md's statistics-arithmetic notes.
        rows = [self._row(START, mean=42.5, state=999.0)]
        result = backfill_from_statistics([], rows, is_counter=True)
        assert result[0].numeric_value == 999.0

    def test_a_row_with_no_value_at_all_becomes_a_gap_not_a_crash(self) -> None:
        rows = [self._row(START)]  # mean and state both None
        result = backfill_from_statistics([], rows, is_counter=False)
        assert result[0].numeric_value is None
        assert result[0].value is None
