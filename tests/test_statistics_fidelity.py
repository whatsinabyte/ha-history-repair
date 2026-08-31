"""Do our recomputed buckets match what Home Assistant actually stored?

This is the test the whole statistics phase rests on. Every other test checks
that the code does what *we* think Home Assistant does; this one checks that
belief against a real recorder, by reading the states behind each bucket,
recomputing it, and comparing with the value Home Assistant wrote.

If this passes, a corrected bucket will agree with the rest of the user's
statistics. If it fails, corrections would leave the statistics graph quietly
disagreeing with the history graph, which is the failure this project exists
to prevent.

It needs a live development Home Assistant, so it is skipped unless HR_HA_DSN
points at one:

    ./dev/ha_core.sh start
    # let it record for a few minutes
    HR_HA_DSN=mysql://hatest:hatest@127.0.0.1:3402/ha_test_core \\
      .venv-check/bin/python -m pytest tests/test_statistics_fidelity.py

Never point HR_HA_DSN at a production instance. These tests only read, but the
guard below refuses anything not named ha_test* regardless.
"""

from __future__ import annotations

import os
import time
from itertools import pairwise
from urllib.parse import urlparse

import pytest

from hr_config import DatabaseConfig
from hr_mariadb import MariaDBAdapter
from hr_statistics import (
    HOURLY_SECONDS,
    SHORT_TERM_SECONDS,
    MeasurementBucket,
    recompute_short_term,
    summarise_hourly,
)

# Epoch timestamps near 1.7e9 carry roughly 2e-7 of absolute precision in a
# float64, so recomputed values differ from stored ones in the last bits.
TOLERANCE = 1e-6

# Home Assistant's arithmetic mean type. Circular means (wind direction) use a
# different formula and are out of scope for this phase.
ARITHMETIC = 1

_DSN = os.environ.get("HR_HA_DSN")

pytestmark = [
    pytest.mark.skipif(not _DSN, reason="HR_HA_DSN is not set; see this module's docstring"),
    # ha_test_core (HR_HA_DSN) is one live, un-cloned Home Assistant instance
    # shared with test_ha_api_live.py — unlike ha_test (HR_TEST_DSN), which
    # every xdist worker clones for itself. That other file's hourly-import
    # test briefly writes a known value into this same database to verify it
    # was applied, then deletes it; without this group, a worker running
    # these read-only fidelity checks at that exact moment can read the
    # transient value and report a real, but spurious, mismatch. Grouping
    # both files onto one worker serialises them instead.
    pytest.mark.xdist_group(name="live_home_assistant"),
]


@pytest.fixture(scope="session")
def ha_db() -> MariaDBAdapter:
    parsed = urlparse(_DSN or "")
    name = (parsed.path or "/").lstrip("/")
    if not name.startswith("ha_test"):
        raise AssertionError(
            f"HR_HA_DSN points at '{name}', which is not a disposable test "
            "database. Refusing to run."
        )
    return MariaDBAdapter(
        DatabaseConfig(
            host=parsed.hostname or "127.0.0.1",
            port=parsed.port or 3306,
            name=name,
            user=parsed.username or "",
            password=parsed.password or "",
        )
    )


@pytest.fixture(scope="session")
def measurement_sensors(ha_db: MariaDBAdapter) -> list[str]:
    """Entity ids Home Assistant is producing arithmetic-mean statistics for."""
    found = [
        entity.entity_id
        for entity in ha_db.list_entities(limit=200)
        if (meta := ha_db.get_statistics_metadata(entity.entity_id)) is not None
        and meta.mean_type == ARITHMETIC
        and not meta.has_sum
    ]
    if not found:
        pytest.skip("the development instance has no measurement statistics yet")
    return found


class TestShortTermFidelity:
    def test_recomputed_buckets_match_home_assistant(
        self, ha_db: MariaDBAdapter, measurement_sensors: list[str]
    ) -> None:
        compared = 0
        mismatches: list[str] = []

        for entity_id in measurement_sensors:
            meta = ha_db.get_statistics_metadata(entity_id)
            assert meta is not None
            stored_rows = ha_db.fetch_statistics(meta.id, 0, 2_000_000_000, short_term=True)

            for row in stored_rows:
                if row.mean is None:
                    continue
                readings = ha_db.fetch_readings(
                    entity_id, row.start_ts, row.start_ts + SHORT_TERM_SECONDS
                )
                if not readings:
                    continue

                ours = recompute_short_term(readings, row.start_ts)
                compared += 1

                for label, mine, theirs in (
                    ("mean", ours.mean, row.mean),
                    ("min", ours.min, row.min),
                    ("max", ours.max, row.max),
                ):
                    if mine is None or theirs is None:
                        continue
                    if abs(mine - float(theirs)) > TOLERANCE:
                        mismatches.append(
                            f"{entity_id} @ {row.start_ts:.0f} {label}: "
                            f"ours={mine!r} home_assistant={theirs!r}"
                        )

        if compared == 0:
            pytest.skip("no completed statistics buckets to compare yet")
        assert not mismatches, (
            f"{len(mismatches)} of {compared} recomputed buckets disagree with "
            "Home Assistant:\n  " + "\n  ".join(mismatches[:10])
        )

    def test_a_plain_average_would_disagree(
        self, ha_db: MariaDBAdapter, measurement_sensors: list[str]
    ) -> None:
        """The design document's formula must actually be wrong, not equivalent.

        If a plain average matched too, the whole time-weighting argument would
        be unfalsifiable and this suite would prove nothing.
        """
        disagreements = 0
        compared = 0

        for entity_id in measurement_sensors:
            meta = ha_db.get_statistics_metadata(entity_id)
            assert meta is not None
            for row in ha_db.fetch_statistics(meta.id, 0, 2_000_000_000, short_term=True):
                if row.mean is None:
                    continue
                readings = ha_db.fetch_readings(
                    entity_id, row.start_ts, row.start_ts + SHORT_TERM_SECONDS
                )
                inside = [r.value for r in readings if r.ts >= row.start_ts]
                if len(inside) < 2:
                    continue
                compared += 1
                plain = sum(inside) / len(inside)
                if abs(plain - float(row.mean)) > TOLERANCE:
                    disagreements += 1

        if compared == 0:
            pytest.skip("not enough multi-reading buckets to compare yet")
        assert disagreements > 0, (
            "a plain average matched every bucket, so this fixture cannot "
            "distinguish the two formulas — check that readings are unevenly "
            "spaced"
        )


class TestHourlyFidelity:
    def test_hourly_rows_summarise_the_short_term_ones(
        self, ha_db: MariaDBAdapter, measurement_sensors: list[str]
    ) -> None:
        compared = 0
        mismatches: list[str] = []
        # The current, still-accumulating hour is a moving target: Home
        # Assistant keeps writing new 5-minute buckets into it in the
        # background while this test reads the hourly row and its children as
        # two separate queries with no shared snapshot. An hour that has not
        # fully elapsed yet is therefore excluded — this is a fidelity check
        # on Home Assistant's settled arithmetic, not a claim that a bucket
        # mid-compilation must already equal its final value.
        now = time.time()

        for entity_id in measurement_sensors:
            meta = ha_db.get_statistics_metadata(entity_id)
            assert meta is not None
            hourly = ha_db.fetch_statistics(meta.id, 0, 2_000_000_000)

            for row in hourly:
                if row.mean is None or row.start_ts + HOURLY_SECONDS > now:
                    continue
                short_term = ha_db.fetch_statistics(
                    meta.id,
                    row.start_ts,
                    row.start_ts + HOURLY_SECONDS,
                    short_term=True,
                )
                if not short_term:
                    continue

                ours = summarise_hourly(
                    [
                        MeasurementBucket(
                            start_ts=b.start_ts,
                            mean=None if b.mean is None else float(b.mean),
                            min=None if b.min is None else float(b.min),
                            max=None if b.max is None else float(b.max),
                        )
                        for b in short_term
                    ],
                    row.start_ts,
                )
                compared += 1

                for label, mine, theirs in (
                    ("mean", ours.mean, row.mean),
                    ("min", ours.min, row.min),
                    ("max", ours.max, row.max),
                ):
                    if mine is None or theirs is None:
                        continue
                    if abs(mine - float(theirs)) > TOLERANCE:
                        mismatches.append(
                            f"{entity_id} @ {row.start_ts:.0f} {label}: "
                            f"ours={mine!r} home_assistant={theirs!r}"
                        )

        if compared == 0:
            pytest.skip("Home Assistant has not compiled an hourly bucket yet")
        assert not mismatches, (
            f"{len(mismatches)} of {compared} hourly rows disagree:\n  "
            + "\n  ".join(mismatches[:10])
        )


class TestCounterShape:
    def test_hourly_state_and_sum_come_from_the_last_short_term_row(
        self, ha_db: MariaDBAdapter
    ) -> None:
        """Not summed — copied from the final 5-minute row of the hour.

        This is what makes the counter cascade tractable: an hourly sum is a
        running total carried forward, so an error in one bucket offsets every
        later one by a constant.
        """
        counters = [
            entity.entity_id
            for entity in ha_db.list_entities(limit=200)
            if (meta := ha_db.get_statistics_metadata(entity.entity_id)) is not None
            and meta.has_sum
        ]
        if not counters:
            pytest.skip("no counter statistics in the development instance")

        compared = 0
        now = time.time()
        for entity_id in counters:
            meta = ha_db.get_statistics_metadata(entity_id)
            assert meta is not None
            for row in ha_db.fetch_statistics(meta.id, 0, 2_000_000_000):
                if row.start_ts + HOURLY_SECONDS > now:
                    # Still accumulating in the background; see the note in
                    # TestHourlyFidelity above.
                    continue
                short_term = ha_db.fetch_statistics(
                    meta.id,
                    row.start_ts,
                    row.start_ts + HOURLY_SECONDS,
                    short_term=True,
                )
                if not short_term or row.sum is None:
                    continue
                last = short_term[-1]
                if last.sum is None:
                    continue
                compared += 1
                assert abs(float(row.sum) - float(last.sum)) < TOLERANCE
                if row.state is not None and last.state is not None:
                    assert abs(float(row.state) - float(last.state)) < TOLERANCE

        if compared == 0:
            pytest.skip("no completed hourly counter rows yet")


class TestReadOnlyAgainstAnyRecorder:
    """Reads must work on a database that has never seen this add-on.

    Found here: list_entities and fetch_states both join state_corrections,
    which is created during onboarding. Against a recorder database without it
    — before onboarding, or when simply pointing the adapter at one to look —
    every read raised "Table 'state_corrections' doesn't exist" instead of
    returning data.
    """

    def test_the_development_instance_has_no_audit_table(self, ha_db: MariaDBAdapter) -> None:
        assert ha_db.check_health().audit_table_ready is False

    def test_entities_still_list(self, ha_db: MariaDBAdapter) -> None:
        entities = ha_db.list_entities(limit=50)
        assert entities
        assert all(entity.correction_count == 0 for entity in entities)

    def test_history_still_loads_with_nothing_marked(
        self, ha_db: MariaDBAdapter, measurement_sensors: list[str]
    ) -> None:
        series = ha_db.fetch_states(measurement_sensors[0], 0, 2_000_000_000)
        assert series.points
        assert all(point.correction_id is None for point in series.points)


class TestCounterChainFidelity:
    """Does our running-total chain reproduce what Home Assistant stored?

    The design document models a counter correction as subtracting a constant
    delta from every later sum. That is only safe while no reset fires. This
    checks the chain we actually implement against a real recorder's numbers.
    """

    def _counters(self, ha_db: MariaDBAdapter) -> list[str]:
        found = [
            entity.entity_id
            for entity in ha_db.list_entities(limit=200)
            if (meta := ha_db.get_statistics_metadata(entity.entity_id)) is not None
            and meta.has_sum
        ]
        if not found:
            pytest.skip("the development instance has no counter statistics yet")
        return found

    def test_the_chain_reproduces_home_assistants_sums(self, ha_db: MariaDBAdapter) -> None:
        from hr_statistics import CounterBucket, cascade_sums

        compared = 0
        mismatches: list[str] = []

        for entity_id in self._counters(ha_db):
            meta = ha_db.get_statistics_metadata(entity_id)
            assert meta is not None
            rows = ha_db.fetch_statistics(meta.id, 0, 2_000_000_000, short_term=True)
            usable = [r for r in rows if r.state is not None and r.sum is not None]
            if len(usable) < 3:
                continue

            # Start from the first row Home Assistant wrote and rebuild every
            # later total from the meter readings alone.
            first = usable[0]
            rebuilt = cascade_sums(
                [
                    CounterBucket(start_ts=r.start_ts, state=float(r.state), sum=None)  # type: ignore[arg-type]
                    for r in usable[1:]
                ],
                previous_state=float(first.state),  # type: ignore[arg-type]
                previous_sum=float(first.sum),  # type: ignore[arg-type]
            )

            for ours, theirs in zip(rebuilt, usable[1:], strict=True):
                compared += 1
                assert ours.sum is not None and theirs.sum is not None
                if abs(ours.sum - float(theirs.sum)) > TOLERANCE:
                    mismatches.append(
                        f"{entity_id} @ {theirs.start_ts:.0f}: "
                        f"ours={ours.sum!r} home_assistant={theirs.sum!r}"
                    )

        if compared == 0:
            pytest.skip("not enough counter rows to compare yet")
        assert not mismatches, (
            f"{len(mismatches)} of {compared} rebuilt totals disagree with Home "
            "Assistant:\n  " + "\n  ".join(mismatches[:10])
        )

    def test_sum_increments_equal_state_increments_while_rising(
        self, ha_db: MariaDBAdapter
    ) -> None:
        """The relationship the whole cascade rests on, checked on real data."""
        for entity_id in self._counters(ha_db):
            meta = ha_db.get_statistics_metadata(entity_id)
            assert meta is not None
            rows = [
                r
                for r in ha_db.fetch_statistics(meta.id, 0, 2_000_000_000, short_term=True)
                if r.state is not None and r.sum is not None
            ]
            checked = 0
            for earlier, later in pairwise(rows):
                if float(later.state) < float(earlier.state):  # type: ignore[arg-type]
                    continue  # a dip or reset; covered by the unit tests
                checked += 1
                assert float(later.sum) - float(earlier.sum) == pytest.approx(  # type: ignore[arg-type]
                    float(later.state) - float(earlier.state),  # type: ignore[arg-type]
                    abs=TOLERANCE,
                )
            if checked:
                return
        pytest.skip("no rising counter rows to compare yet")
