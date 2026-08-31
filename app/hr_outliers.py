"""Automatic outlier candidate detection.

This flags points on the graph for a user to look at — it never corrects
anything itself. A candidate is a suggestion; the user still makes the call
via the normal correction flow.

The design document's §6 draft SQL has a mismatch worth noting: its comment
says "deviating > threshold × stddev" but the formula it uses is
`threshold * (max - min)`, a range-based check, not a standard-deviation one.

Neither range nor a classical mean/standard-deviation z-score works well here,
and testing this module found why: **a single dramatic outlier inflates its
own detector.** A -2000 spike among readings normally varying by 0.1-0.2 pulls
the mean down to -232 and the standard deviation up to 714 — so the spike's own
z-score drops to about 2.5, below a default threshold of 3.0, and it goes
undetected. This is the standard statistical phenomenon called masking: the
statistics used to judge the outlier are themselves computed from data that
includes it.

This module uses the median and the median absolute deviation (MAD) instead.
Both are robust: half of all readings would have to be outliers before either
one moved. The same reasoning applies to counter sensors, where the document's
`threshold * AVG(increment)` baseline is skewed by the very spike being
searched for, and by ordinary meter resets (large negative increments)
pulling the average down.

A whole-window MAD has its own masking case, though: a slowly creeping or
sloped series (a battery draining over days, a temperature ramping through a
heating cycle) spreads the *whole* window's values across a wide range, which
inflates the window's own MAD and can hide a real anomaly that stands out
clearly against its immediate neighbours but not against the full range of
the trend. Deciding whether a series is stationary or trending first (an ADF
or KPSS test, then reaching for STL decomposition or ARIMA residuals only if
it is not) is how a general-purpose time-series library would resolve this —
but it is more machinery than this tool's job calls for: a Hampel filter
(comparing each reading to the median and MAD of a small local neighbourhood
around it, rather than the whole window) adapts to a local trend without
having to classify the series first, and it collapses to exactly the
whole-window behaviour above when the series is no longer than the
neighbourhood — a strict generalisation, not a separate mode.

Pure module: no I/O, no SQL, no state.
"""

from __future__ import annotations

import statistics as _statistics
from dataclasses import dataclass

from hr_statistics import Reading

# A candidate needs at least this many neighbours to compute a meaningful
# spread; below it, "outlier" is not a well-formed question.
MIN_READINGS_FOR_MEASUREMENT = 3
MIN_READINGS_FOR_COUNTER = 3

# Points on each side of a reading included in its local neighbourhood, so a
# neighbourhood spans up to 2 * WINDOW_HALF_WIDTH + 1 points including the
# reading itself. Small enough to track a slow trend (the neighbourhood is a
# short enough span of the series that the trend within it is close to
# linear, so it does not by itself look like spread) while still leaving
# enough points either side for the median and MAD to mean something. Not
# tied to wall-clock time: home-recorder sensors report on change rather than
# on a fixed schedule, so a point count adapts to however densely a given
# entity happens to report, unlike a fixed time window would.
WINDOW_HALF_WIDTH = 5

DEFAULT_THRESHOLD = 3.0


@dataclass(frozen=True)
class Candidate:
    """One reading flagged as worth a user's attention."""

    ts: float
    value: float
    deviation: float


def _neighbourhood(values: list[float], index: int) -> list[float]:
    """The local window around `index`, clipped at either end of the series.

    A series no longer than the window is returned whole regardless of
    `index`, reproducing the original whole-window behaviour exactly rather
    than merely approximately: a clipped, position-dependent window would
    still give a point near either edge a smaller, skewed neighbourhood even
    when the whole series would comfortably fit inside one window. Only once
    the series is longer than that does a point get the genuinely local,
    position-dependent slice around it.
    """
    if len(values) <= 2 * WINDOW_HALF_WIDTH + 1:
        return values
    lo = max(0, index - WINDOW_HALF_WIDTH)
    hi = min(len(values), index + WINDOW_HALF_WIDTH + 1)
    return values[lo:hi]


def _robust_deviation(value: float, neighbours: list[float]) -> float:
    """How many spread-units `value` sits from its neighbourhood's median.

    MAD is preferred for the same masking reason as the module docstring:
    it barely moves for one bad reading, however extreme. But when more than
    half the neighbourhood shares one exact value — common for a quantized or
    rounded sensor — MAD collapses to 0, and without a fallback every
    differing reading, however small the difference, would score an infinite
    deviation regardless of the sensitivity threshold. Standard deviation
    still has spread to measure in that case, since it is not pinned to the
    majority value the way MAD is, so it takes over as the yardstick. If even
    that is 0, every neighbour is genuinely identical, and any difference at
    all really is the only anomaly there is to find.
    """
    median = _statistics.median(neighbours)
    spread = _statistics.median(abs(v - median) for v in neighbours)
    if spread == 0:
        spread = _statistics.pstdev(neighbours)
    if spread == 0:
        return float("inf") if value != median else 0.0
    return abs(value - median) / spread


def detect_measurement_outliers(
    readings: list[Reading], threshold: float = DEFAULT_THRESHOLD
) -> list[Candidate]:
    """Flag readings whose local deviation exceeds the threshold.

    Each reading is judged against the median and MAD of its own local
    neighbourhood (see WINDOW_HALF_WIDTH), not the whole series — see the
    module docstring for why a whole-window baseline can mask a real anomaly
    on a slowly trending series.
    """
    if len(readings) < MIN_READINGS_FOR_MEASUREMENT:
        return []

    values = [r.value for r in readings]
    candidates = []
    for i, reading in enumerate(readings):
        deviation = _robust_deviation(reading.value, _neighbourhood(values, i))
        if deviation > threshold:
            candidates.append(Candidate(ts=reading.ts, value=reading.value, deviation=deviation))
    return candidates


def _robust_ratio(magnitude: float, neighbours: list[float]) -> float:
    """How many multiples of the local baseline magnitude `magnitude` is.

    Unlike _robust_deviation, this is not centred on the neighbourhood's
    median: an increment's magnitude is already a distance (how much the
    counter moved), so what matters is its ratio to the *typical* movement
    nearby, not its distance from that typical movement. The median is
    preferred as the baseline for the same masking reason as elsewhere in
    this module — a handful of huge spikes, or a meter reset's large
    negative increment, would drag a mean-based baseline toward themselves.
    When more than half the neighbourhood's increments are 0 (a counter idle
    for most of the window), the median collapses to 0, and standard
    deviation of the magnitudes takes over so an ordinary small movement is
    not scored as an infinite multiple of nothing.
    """
    baseline = _statistics.median(neighbours)
    if baseline == 0:
        stdev = _statistics.pstdev(neighbours)
        if stdev == 0:
            return float("inf") if magnitude != 0 else 0.0
        return magnitude / stdev
    return magnitude / baseline


def detect_counter_outliers(
    readings: list[Reading], threshold: float = DEFAULT_THRESHOLD
) -> list[Candidate]:
    """Flag meter readings whose increment's local deviation exceeds the
    threshold.

    Increments are compared to their own local neighbourhood's median
    magnitude, not the whole window's, for the same reason as
    detect_measurement_outliers — and because a counter's consumption *rate*
    can itself trend (heating ramping up through the day), which is exactly
    the kind of drift a local neighbourhood tracks and a whole-window
    baseline would flatten into extra apparent spread.

    Every reading after the first can be flagged; the first has no prior
    reading to form an increment from.
    """
    if len(readings) < MIN_READINGS_FOR_COUNTER:
        return []

    increments = [readings[i].value - readings[i - 1].value for i in range(1, len(readings))]
    magnitudes = [abs(x) for x in increments]

    candidates = []
    for i, magnitude in enumerate(magnitudes):
        deviation = _robust_ratio(magnitude, _neighbourhood(magnitudes, i))
        if deviation > threshold:
            candidates.append(
                Candidate(ts=readings[i + 1].ts, value=readings[i + 1].value, deviation=deviation)
            )
    return candidates
