"""Tests for hr_outliers — automatic candidate detection.

Pure module, so these are mostly property tests: the detector should never
crash on degenerate input (too few readings, a flat series, all-identical
values), and its flags should track the sensitivity threshold monotonically.
"""

from __future__ import annotations

from hypothesis import given
from hypothesis import strategies as st

from hr_outliers import (
    _neighbourhood,
    _robust_deviation,
    _robust_ratio,
    detect_counter_outliers,
    detect_measurement_outliers,
)
from hr_statistics import Reading

START = 1_700_000_000.0


def _readings(*values: float) -> list[Reading]:
    return [Reading(value=v, ts=START + i * 300) for i, v in enumerate(values)]


class TestNeighbourhood:
    # WINDOW_HALF_WIDTH is 5, so the whole-window/local-slice boundary sits
    # at exactly 2*5+1 = 11 points. Found by mutation testing: the existing
    # detector-level tests never exercised a series sitting exactly on that
    # boundary, or an index far enough from the edge to make the slice
    # bounds themselves observable.
    def test_a_series_of_exactly_the_window_size_is_returned_whole(self) -> None:
        values = [float(i) for i in range(11)]
        assert _neighbourhood(values, 0) == values

    def test_a_series_one_longer_than_the_window_is_sliced_around_the_edge(self) -> None:
        values = [float(i) for i in range(12)]
        assert _neighbourhood(values, 0) == values[0:6]

    def test_a_series_one_longer_than_the_window_is_sliced_around_the_middle(self) -> None:
        values = [float(i) for i in range(12)]
        assert _neighbourhood(values, 6) == values[1:12]


class TestRobustDeviation:
    def test_a_lone_differing_value_among_identical_neighbours_is_infinite(self) -> None:
        # MAD and standard deviation both collapse to 0 when every neighbour
        # agrees, so the only signal left is whether the value under test
        # matches them exactly.
        assert _robust_deviation(5.0, [1.0, 1.0, 1.0, 1.0]) == float("inf")
        assert _robust_deviation(1.0, [1.0, 1.0, 1.0, 1.0]) == 0.0


class TestRobustRatio:
    def test_all_zero_neighbours_scores_infinite_ratio_for_any_movement(self) -> None:
        assert _robust_ratio(5.0, [0.0, 0.0, 0.0, 0.0]) == float("inf")
        assert _robust_ratio(0.0, [0.0, 0.0, 0.0, 0.0]) == 0.0

    def test_zero_median_baseline_divides_by_the_standard_deviation(self) -> None:
        import statistics as _statistics

        neighbours = [0.0, 0.0, 0.0, 10.0]
        expected_stdev = _statistics.pstdev(neighbours)
        assert _robust_ratio(5.0, neighbours) == 5.0 / expected_stdev


class TestDetectMeasurementOutliers:
    def test_exactly_the_minimum_reading_count_still_detects(self) -> None:
        readings = _readings(20.0, 20.0, -2000.0)
        candidates = detect_measurement_outliers(readings, threshold=2.0)
        assert {c.value for c in candidates} == {-2000.0}

    def test_a_deviation_exactly_at_threshold_is_not_flagged(self) -> None:
        # neighbourhood [0, 0, 4, 4, 8]: median 4, MAD 4, so both the 0s and
        # the 8 sit at deviation exactly 1.0 — a boundary the strict ">"
        # excludes.
        readings = _readings(0.0, 0.0, 4.0, 4.0, 8.0)
        assert detect_measurement_outliers(readings, threshold=1.0) == []

    def test_candidate_ts_matches_the_flagged_reading(self) -> None:
        readings = _readings(20.0, 20.1, 19.9, 20.2, -2000.0, 20.0, 19.8, 20.1)
        candidates = detect_measurement_outliers(readings)
        assert candidates[0].ts == readings[4].ts

    def test_an_obvious_spike_is_flagged(self) -> None:
        readings = _readings(20.0, 20.1, 19.9, 20.2, -2000.0, 20.0, 19.8, 20.1)
        candidates = detect_measurement_outliers(readings)
        assert {c.value for c in candidates} == {-2000.0}

    def test_normal_variation_is_not_flagged(self) -> None:
        readings = _readings(20.0, 20.2, 19.8, 20.1, 19.9, 20.3, 19.7)
        assert detect_measurement_outliers(readings) == []

    def test_too_few_readings_returns_nothing(self) -> None:
        assert detect_measurement_outliers(_readings(20.0, 21.0)) == []
        assert detect_measurement_outliers([]) == []

    def test_identical_readings_have_no_deviation(self) -> None:
        # Zero standard deviation: every reading agrees, so nothing can be an
        # outlier and the division-by-zero case must not raise.
        assert detect_measurement_outliers(_readings(20.0, 20.0, 20.0, 20.0)) == []

    def test_a_quantized_sensor_with_ordinary_jitter_is_not_flooded(self) -> None:
        # More than half the readings share one exact value (a rounded or
        # quantized sensor), so the median absolute deviation collapses to 0.
        # Before the standard-deviation fallback, every one of the jittered
        # readings below got an infinite deviation regardless of threshold —
        # raising the sensitivity slider did nothing to quiet them down.
        readings = _readings(20.0, 20.0, 20.0, 20.0, 20.0, 20.0, 20.3, 19.8, 20.2)
        assert detect_measurement_outliers(readings, threshold=3.0) == []

    def test_a_real_spike_is_still_caught_despite_a_quantized_majority(self) -> None:
        readings = _readings(20.0, 20.0, 20.0, 20.0, 20.0, 20.0, 20.3, 19.8, -2000.0)
        candidates = detect_measurement_outliers(readings, threshold=3.0)
        assert {c.value for c in candidates} == {-2000.0}

    def test_a_higher_threshold_flags_no_more_than_a_lower_one(self) -> None:
        readings = _readings(20.0, 20.1, 19.9, 25.0, 20.0, 19.8, -50.0)
        loose = {c.value for c in detect_measurement_outliers(readings, threshold=1.0)}
        strict = {c.value for c in detect_measurement_outliers(readings, threshold=5.0)}
        assert strict <= loose

    def test_deviation_grows_with_distance_from_the_median(self) -> None:
        readings = _readings(10.0, 10.0, 10.0, 10.0, 30.0)
        candidates = detect_measurement_outliers(readings, threshold=0.5)
        assert candidates
        assert candidates[0].deviation > 0.5

    def test_a_local_anomaly_on_a_slow_ramp_is_not_masked_by_the_trend(self) -> None:
        # A whole-window MAD has its own masking case: a slowly rising series
        # (a battery draining, a temperature ramping through a heating cycle)
        # spans a wide range on its own, inflating the whole-window MAD
        # enough to hide a real local anomaly. Judged against its own local
        # neighbourhood instead, the same anomaly stands out clearly. Values
        # confirmed by hand: a 21-point ramp from 20.0 to 25.0 with a +3.0
        # bump at the midpoint scores deviation 1.83 against the whole
        # window's MAD (masked below the default threshold of 3.0) but 3.67
        # against its local neighbourhood's MAD (correctly flagged).
        values = [20.0 + i * 0.25 for i in range(21)]
        values[10] += 3.0
        readings = _readings(*values)
        candidates = detect_measurement_outliers(readings, threshold=3.0)
        assert {c.value for c in candidates} == {25.5}

    def test_a_dramatic_spike_is_not_masked_by_its_own_inflated_spread(self) -> None:
        # The exact scenario a naive mean/stdev z-score fails on: the spike
        # drags the mean and standard deviation toward itself, dropping its
        # own z-score below threshold. Median/MAD barely move.
        readings = _readings(20.0, 20.1, 19.9, 20.2, -2000.0, 20.0, 19.8, 20.1)
        candidates = detect_measurement_outliers(readings, threshold=3.0)
        assert -2000.0 in {c.value for c in candidates}

    @given(
        values=st.lists(
            st.floats(min_value=-1e4, max_value=1e4, allow_nan=False, allow_infinity=False),
            min_size=0,
            max_size=30,
        )
    )
    def test_never_raises_on_arbitrary_input(self, values: list[float]) -> None:
        detect_measurement_outliers(_readings(*values))

    @given(
        values=st.lists(
            st.floats(min_value=-1e4, max_value=1e4, allow_nan=False, allow_infinity=False),
            min_size=3,
            max_size=30,
        )
    )
    def test_every_candidate_is_one_of_the_input_readings(self, values: list[float]) -> None:
        readings = _readings(*values)
        candidates = detect_measurement_outliers(readings)
        reading_values = {r.value for r in readings}
        assert all(c.value in reading_values for c in candidates)


class TestDetectCounterOutliers:
    def test_exactly_the_minimum_reading_count_still_detects(self) -> None:
        readings = _readings(0.0, 0.0, 1000.0)
        candidates = detect_counter_outliers(readings, threshold=1.0)
        assert {c.value for c in candidates} == {1000.0}

    def test_a_ratio_exactly_at_threshold_is_not_flagged(self) -> None:
        # increments [0, 4, 0, 4]: median baseline 2, so the two 4s sit at
        # ratio exactly 2.0 — a boundary the strict ">" excludes.
        readings = _readings(0.0, 0.0, 4.0, 4.0, 8.0)
        assert detect_counter_outliers(readings, threshold=2.0) == []

    def test_increments_are_differences_not_sums(self) -> None:
        readings = _readings(100.0, 105.0, 110.0, 99999.0, 115.0, 120.0)
        candidates = detect_counter_outliers(readings)
        by_value = {c.value: c.deviation for c in candidates}
        # A correct increment of 110 -> 99999 is 99889; a corrupted "sum"
        # increment of 110 + 99999 would give a completely different
        # deviation while still happening to flag the same reading, so the
        # exact deviation value is what actually pins the arithmetic down.
        assert by_value[99999.0] == 99889 / 5

    def test_candidate_ts_matches_the_reading_after_the_increment(self) -> None:
        readings = _readings(100.0, 105.0, 110.0, 99999.0, 115.0, 120.0)
        candidates = detect_counter_outliers(readings)
        by_value = {c.value: c.ts for c in candidates}
        assert by_value[99999.0] == readings[3].ts
        assert by_value[115.0] == readings[4].ts

    def test_a_series_longer_than_the_window_still_uses_each_points_own_index(self) -> None:
        # With more than WINDOW_HALF_WIDTH*2+1 increments, _neighbourhood
        # takes the local-slice branch, which actually reads the caller's
        # index — a series this short never reaches that branch, so a bug
        # that stopped threading the real index through would go unnoticed.
        readings = _readings(
            100.0,
            105.0,
            110.0,
            115.0,
            120.0,
            125.0,
            130.0,
            135.0,
            140.0,
            145.0,
            150.0,
            155.0,
            160.0,
            99999.0,
            165.0,
        )
        candidates = detect_counter_outliers(readings)
        assert 99999.0 in {c.value for c in candidates}

    def test_a_spike_then_recovery_is_flagged(self) -> None:
        # Both the jump up and the jump back down are large increments.
        readings = _readings(100.0, 105.0, 110.0, 99999.0, 115.0, 120.0)
        candidates = detect_counter_outliers(readings)
        assert {c.value for c in candidates} == {99999.0, 115.0}

    def test_steady_consumption_is_not_flagged(self) -> None:
        readings = _readings(100.0, 105.0, 110.2, 114.8, 120.1, 125.0)
        assert detect_counter_outliers(readings) == []

    def test_too_few_readings_returns_nothing(self) -> None:
        assert detect_counter_outliers(_readings(100.0, 105.0)) == []

    def test_a_meter_reset_does_not_poison_the_baseline(self) -> None:
        # A legitimate reset (value drops to near zero) must not drag the
        # median so far that a real spike elsewhere goes unflagged.
        readings = _readings(500.0, 505.0, 5.0, 10.0, 15.0, 90000.0, 20.0)
        candidates = detect_counter_outliers(readings)
        assert 90000.0 in {c.value for c in candidates}

    def test_a_mostly_idle_counter_is_not_flooded_by_small_movement(self) -> None:
        # Zero baseline increment: the fallback path. Before the
        # standard-deviation fallback, every nonzero increment here got an
        # infinite deviation regardless of threshold — raising the
        # sensitivity slider did nothing to quiet ordinary small movement.
        readings = _readings(50.0, 50.0, 50.0, 50.0, 50.0, 50.0, 52.0, 50.0, 51.0)
        assert detect_counter_outliers(readings, threshold=3.0) == []

    def test_a_flat_counter_still_flags_a_real_jump(self) -> None:
        # Zero baseline increment: the fallback path. A jump large enough
        # relative to the ordinary small movement is still caught.
        readings = _readings(50.0, 50.0, 50.0, 50.0, 50.0, 50.0, 52.0, 50.0, 90000.0)
        candidates = detect_counter_outliers(readings, threshold=3.0)
        assert 90000.0 in {c.value for c in candidates}

    def test_a_perfectly_flat_counter_flags_nothing(self) -> None:
        assert detect_counter_outliers(_readings(50.0, 50.0, 50.0, 50.0)) == []

    @given(
        values=st.lists(
            st.floats(min_value=0.0, max_value=1e5, allow_nan=False, allow_infinity=False),
            min_size=0,
            max_size=30,
        )
    )
    def test_never_raises_on_arbitrary_input(self, values: list[float]) -> None:
        detect_counter_outliers(_readings(*values))

    def test_the_first_reading_is_never_itself_a_candidate(self) -> None:
        # It has no prior reading to form an increment against.
        readings = _readings(99999.0, 100.0, 105.0, 110.0)
        candidates = detect_counter_outliers(readings)
        assert readings[0].ts not in {c.ts for c in candidates}
