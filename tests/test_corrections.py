"""Tests for hr_corrections — the rules that decide whether a correction is
allowed and what it should contain.

This module is pure, which makes it the natural home for property tests: the
validators must accept every finite number a user could plausibly type and
reject everything else, without the test having to enumerate cases.
"""

from __future__ import annotations

import math

import pytest
from hypothesis import assume, example, given
from hypothesis import strategies as st

import hr_corrections
from hr_corrections import (
    MAX_BULK_CACHE_REFRESH_HOURS,
    MAX_NOTE_LENGTH,
    MAX_STATE_LENGTH,
    ValidationError,
    normalise_note,
    parse_quality,
    validate_new_value,
    validate_sensor_type,
)
from hr_fakes import SEED_TIMESTAMPS, FakeAdapter
from hr_ha_api import HomeAssistantApiError
from hr_models import BulkCorrectionResult, Correction, Quality, SensorType
from hr_statistics import HOURLY_SECONDS, bucket_start


class _FakeHomeAssistantClient:
    """A duck-typed stand-in for HomeAssistantClient.

    Only import_statistics is ever called by hr_corrections, so that is all
    this fakes — the same injected-transport pattern as hr_mqtt.py's
    MqttPublisher, which lets the orchestration around the real client be
    tested without a live Home Assistant.
    """

    def __init__(self, *, fail: bool = False) -> None:
        self.calls: list[dict[str, object]] = []
        self.fail = fail

    def import_statistics(self, **kwargs: object) -> None:
        if self.fail:
            raise HomeAssistantApiError("simulated API failure")
        self.calls.append(kwargs)


class TestValidateNewValue:
    @given(st.floats(allow_nan=False, allow_infinity=False, width=32))
    @example(0.0)
    @example(-2000.0)  # the impossible temperature from the design document
    @example(20.5)
    def test_accepts_any_finite_number(self, value: float) -> None:
        text = repr(value)
        assume(len(text) <= MAX_STATE_LENGTH)
        assert validate_new_value(text) == text

    @given(st.text())
    def test_rejects_anything_not_a_number(self, text: str) -> None:
        try:
            float(text.strip())
        except ValueError:
            with pytest.raises(ValidationError):
                validate_new_value(text)

    @pytest.mark.parametrize("value", ["", "   ", None])
    def test_rejects_empty(self, value: object) -> None:
        with pytest.raises(ValidationError, match="required") as excinfo:
            validate_new_value(value)
        assert str(excinfo.value) == "A corrected value is required."

    @pytest.mark.parametrize("value", ["nan", "inf", "-inf", "Infinity"])
    def test_rejects_non_finite(self, value: str) -> None:
        # float() parses these happily, so they need their own guard: a NaN in
        # states would poison every later aggregate silently.
        with pytest.raises(ValidationError, match="finite") as excinfo:
            validate_new_value(value)
        assert str(excinfo.value) == "The corrected value must be a finite number."

    def test_rejects_a_non_numeric_string_by_name(self) -> None:
        with pytest.raises(ValidationError) as excinfo:
            validate_new_value("banana")
        assert str(excinfo.value) == (
            "'banana' is not a number. This add-on corrects numeric sensor values only."
        )

    def test_rejects_value_longer_than_the_column(self) -> None:
        too_long = "1" + "0" * MAX_STATE_LENGTH
        with pytest.raises(ValidationError, match="longer than") as excinfo:
            validate_new_value(too_long)
        assert str(excinfo.value) == (
            f"The corrected value is longer than the {MAX_STATE_LENGTH} characters "
            "the states table can hold."
        )

    def test_a_value_of_exactly_the_column_length_is_accepted(self) -> None:
        exactly_fitting = "1" * MAX_STATE_LENGTH
        assert validate_new_value(exactly_fitting) == exactly_fitting

    @given(st.floats(allow_nan=False, allow_infinity=False, width=32))
    def test_surrounding_whitespace_is_stripped(self, value: float) -> None:
        text = repr(value)
        assume(len(text) <= MAX_STATE_LENGTH)
        assert validate_new_value(f"  {text}  ") == text

    def test_result_always_reparses_as_the_same_number(self) -> None:
        assert float(validate_new_value(" 20.50 ")) == 20.5


class TestValidateSensorType:
    def test_measurement_is_allowed(self) -> None:
        validate_sensor_type(SensorType.MEASUREMENT)

    def test_counter_is_allowed_now_that_the_cascade_exists(self) -> None:
        # Refused until the running-total cascade was implemented, because
        # correcting the reading alone would have left every later total wrong.
        validate_sensor_type(SensorType.COUNTER)

    def test_unknown_is_allowed_states_only(self) -> None:
        # No statistics_meta row at all, so there is no cascade to keep in
        # sync — but the state value itself can still be corrected.
        validate_sensor_type(SensorType.UNKNOWN)

    def test_a_refused_type_names_the_reason(self) -> None:
        # Every real SensorType value is correctable (CORRECTABLE_SENSOR_TYPES
        # covers all three), so the refusal path can only be reached with a
        # value outside the enum entirely — exactly what a bug that widened
        # or shrank the allow-list would still need this to catch.
        with pytest.raises(ValidationError) as excinfo:
            validate_sensor_type("bogus")  # type: ignore[arg-type]
        assert str(excinfo.value) == (
            "This entity has no measurement statistics, so its sensor type cannot be "
            "confirmed. Only measurement sensors can be corrected in this version."
        )


class TestParseQuality:
    @given(st.sampled_from(list(Quality)))
    def test_round_trips_every_quality(self, quality: Quality) -> None:
        assert parse_quality(quality.value) is quality

    @pytest.mark.parametrize("value", [None, ""])
    def test_defaults_to_uncertain(self, value: str | None) -> None:
        assert parse_quality(value) is Quality.UNCERTAIN

    @given(st.text())
    def test_rejects_codes_outside_the_enum(self, raw: str) -> None:
        assume(raw)
        assume(raw not in {q.value for q in Quality})
        with pytest.raises(ValidationError):
            parse_quality(raw)

    def test_the_rejection_message_names_the_bad_code(self) -> None:
        with pytest.raises(ValidationError) as excinfo:
            parse_quality("not-a-real-code")
        assert str(excinfo.value) == "Unknown quality code: not-a-real-code"


class TestNormaliseNote:
    @given(st.text())
    def test_never_exceeds_the_column(self, note: str) -> None:
        result = normalise_note(note)
        assert result is None or len(result) <= MAX_NOTE_LENGTH

    @pytest.mark.parametrize("note", [None, "", "   ", "\n\t "])
    def test_blank_notes_become_null(self, note: str | None) -> None:
        assert normalise_note(note) is None


class TestApply:
    def test_writes_an_audit_record_and_updates_the_state(self, adapter: FakeAdapter) -> None:
        correction = hr_corrections.apply(
            adapter,
            entity_id="sensor.living_room_temperature",
            state_id=2,
            expected_original="-2000.0",
            new_value="20.2",
            quality="spike",
            note="  modem reboot  ",
            created_by="marcel",
        )

        assert correction.original_value == "-2000.0"
        assert correction.corrected_value == "20.2"
        assert correction.quality is Quality.SPIKE
        assert correction.note == "modem reboot"
        assert correction.created_by == "marcel"
        assert adapter.states[2][2] == "20.2"

    def test_the_audit_row_records_that_statistics_are_untouched(
        self, adapter: FakeAdapter
    ) -> None:
        # This phase corrects states only. The flag is what lets a later phase
        # find these corrections and bring the statistics tables up to date.
        correction = hr_corrections.apply(
            adapter,
            entity_id="sensor.living_room_temperature",
            state_id=2,
            expected_original="-2000.0",
            new_value="20.2",
            quality=None,
            note=None,
            created_by="marcel",
        )
        assert correction.stats_corrected is False

    def test_counter_sensors_are_corrected_with_their_statistics(
        self, adapter: FakeAdapter
    ) -> None:
        adapter.add_entity("sensor.energy_total", SensorType.COUNTER)
        adapter.add_statistics_metadata("sensor.energy_total", mean_type=0, has_sum=True)
        adapter.add_state("sensor.energy_total", 9, 1_700_000_000.0, "999999.0")

        correction = hr_corrections.apply(
            adapter,
            entity_id="sensor.energy_total",
            state_id=9,
            expected_original="999999.0",
            new_value="1234.0",
            quality="spike",
            note=None,
            created_by="marcel",
        )

        assert adapter.states[9][2] == "1234.0"
        assert correction.sensor_type is SensorType.COUNTER
        # Its running totals were rebuilt too, which is what took a phase.
        assert correction.stats_corrected is True

    def test_an_entity_without_statistics_is_corrected_states_only(
        self, adapter: FakeAdapter
    ) -> None:
        adapter.add_entity("sensor.mystery", SensorType.UNKNOWN)
        adapter.add_state("sensor.mystery", 11, 1_700_000_000.0, "5.0")

        correction = hr_corrections.apply(
            adapter,
            entity_id="sensor.mystery",
            state_id=11,
            expected_original="5.0",
            new_value="6.0",
            quality=None,
            note=None,
            created_by="marcel",
        )

        assert adapter.states[11][2] == "6.0"
        assert correction.sensor_type is SensorType.UNKNOWN
        # No statistics_meta row exists for this entity, so there is nothing
        # to cascade — only the state value itself changes.
        assert correction.stats_corrected is False

    def test_an_unparseable_state_id_names_the_bad_value(self, adapter: FakeAdapter) -> None:
        with pytest.raises(ValidationError) as excinfo:
            hr_corrections.apply(
                adapter,
                entity_id="sensor.living_room_temperature",
                state_id="not-an-id",  # type: ignore[arg-type]
                expected_original="-2000.0",
                new_value="20.2",
                quality=None,
                note=None,
                created_by="marcel",
            )
        assert str(excinfo.value) == "'not-an-id' is not a valid state row id."

    def test_an_invalid_value_leaves_the_database_untouched(self, adapter: FakeAdapter) -> None:
        with pytest.raises(ValidationError):
            hr_corrections.apply(
                adapter,
                entity_id="sensor.living_room_temperature",
                state_id=2,
                expected_original="-2000.0",
                new_value="not a number",
                quality=None,
                note=None,
                created_by="marcel",
            )
        assert adapter.states[2][2] == "-2000.0"
        assert adapter.corrections == {}

    def test_a_stale_expected_original_is_refused(self, adapter: FakeAdapter) -> None:
        from hr_db import ConcurrentModification

        # The recorder replaced the row after the user loaded the graph.
        adapter.states[2] = ("sensor.living_room_temperature", 1_700_000_300.0, "19.9")

        with pytest.raises(ConcurrentModification):
            hr_corrections.apply(
                adapter,
                entity_id="sensor.living_room_temperature",
                state_id=2,
                expected_original="-2000.0",
                new_value="20.2",
                quality=None,
                note=None,
                created_by="marcel",
            )

    @given(
        value=st.floats(
            min_value=-1e6, max_value=1e6, allow_nan=False, allow_infinity=False, width=32
        )
    )
    def test_any_finite_correction_round_trips_through_restore(self, value: float) -> None:
        # A fresh adapter per example: @given runs many times per test method,
        # so shared fixture state would leak between examples.
        adapter = FakeAdapter()
        adapter.add_entity("sensor.t", SensorType.MEASUREMENT)
        adapter.add_state("sensor.t", 1, 1_700_000_000.0, "-2000.0")

        text = repr(value)
        assume(len(text) <= MAX_STATE_LENGTH)
        assume(not math.isnan(value))

        correction = hr_corrections.apply(
            adapter,
            entity_id="sensor.t",
            state_id=1,
            expected_original="-2000.0",
            new_value=text,
            quality=None,
            note=None,
            created_by="marcel",
        )
        assert adapter.states[1][2] == text

        adapter.restore_correction(correction.id, "marcel")
        assert adapter.states[1][2] == "-2000.0"


class TestApplyBulk:
    RANGE = (SEED_TIMESTAMPS[0] - 1, SEED_TIMESTAMPS[-1] + 1)

    def test_start_must_come_before_end(self, adapter: FakeAdapter) -> None:
        with pytest.raises(ValidationError) as excinfo:
            hr_corrections.apply_bulk(
                adapter,
                entity_id="sensor.living_room_temperature",
                start_ts=100.0,
                end_ts=100.0,
                strategy="constant",
                value="20.0",
                quality=None,
                note=None,
                created_by="marcel",
            )
        assert str(excinfo.value) == "The start of the range must come before its end."

    def test_an_unknown_strategy_names_itself(self, adapter: FakeAdapter) -> None:
        with pytest.raises(ValidationError) as excinfo:
            hr_corrections.apply_bulk(
                adapter,
                entity_id="sensor.living_room_temperature",
                start_ts=self.RANGE[0],
                end_ts=self.RANGE[1],
                strategy="bogus",
                value="20.0",
                quality=None,
                note=None,
                created_by="marcel",
            )
        assert str(excinfo.value) == "Unknown strategy 'bogus'. Use 'constant' or 'interpolate'."

    def test_quality_note_and_creator_reach_every_corrected_row(self, adapter: FakeAdapter) -> None:
        result = hr_corrections.apply_bulk(
            adapter,
            entity_id="sensor.living_room_temperature",
            start_ts=self.RANGE[0],
            end_ts=self.RANGE[1],
            strategy="constant",
            value="20.0",
            quality="spike",
            note="  modem reboot  ",
            created_by="marcel",
        )
        assert result.applied > 0
        for correction_id in result.correction_ids:
            correction = adapter.get_correction(correction_id)
            assert correction.quality is Quality.SPIKE
            assert correction.note == "modem reboot"
            assert correction.created_by == "marcel"


def _raw_correction(**overrides: object) -> Correction:
    """A Correction built directly, for scenarios apply() cannot produce —
    e.g. stats_corrected True with no matching statistics_meta row, which
    only happens if the metadata disappears between the correction and the
    later cache-refresh call."""
    fields: dict[str, object] = {
        "id": 1,
        "entity_id": "sensor.energy_total",
        "sensor_type": SensorType.COUNTER,
        "state_id": 9,
        "state_ts": 1_700_000_000.0,
        "original_value": "999999.0",
        "corrected_value": "1234.0",
        "quality": Quality.SPIKE,
        "note": None,
        "created_at": "2026-01-01T00:00:00",
        "created_by": "marcel",
        "restored_at": None,
        "restored_by": None,
        "stats_corrected": True,
    }
    fields.update(overrides)
    return Correction(**fields)  # type: ignore[arg-type]


class TestRefreshStatisticsCache:
    ENTITY = "sensor.energy_total"

    def _corrected(self, adapter: FakeAdapter) -> Correction:
        adapter.add_entity(self.ENTITY, SensorType.COUNTER)
        adapter.add_statistics_metadata(self.ENTITY, mean_type=0, has_sum=True)
        adapter.add_state(self.ENTITY, 9, 1_700_000_000.0, "999999.0")
        correction = hr_corrections.apply(
            adapter,
            entity_id=self.ENTITY,
            state_id=9,
            expected_original="999999.0",
            new_value="1234.0",
            quality=None,
            note=None,
            created_by="marcel",
        )
        hour_start = bucket_start(correction.state_ts, HOURLY_SECONDS)
        adapter.add_hourly_statistics(self.ENTITY, hour_start, state=1234.0)
        return correction

    def test_no_client_means_no_refresh(self, adapter: FakeAdapter) -> None:
        correction = self._corrected(adapter)
        assert hr_corrections.refresh_statistics_cache(adapter, None, correction) is False

    def test_a_client_with_no_stats_correction_is_never_called(self, adapter: FakeAdapter) -> None:
        # stats_corrected False: there is nothing in the statistics tables for
        # Home Assistant's cache to catch up on, so the client must not even
        # be asked — the "client is None or not stats_corrected" guard must
        # short-circuit on either half independently, not require both.
        correction = _raw_correction(stats_corrected=False)
        client = _FakeHomeAssistantClient()
        assert hr_corrections.refresh_statistics_cache(adapter, client, correction) is False
        assert client.calls == []

    def test_a_client_and_a_stats_correction_together_push_the_hour(
        self, adapter: FakeAdapter
    ) -> None:
        correction = self._corrected(adapter)
        client = _FakeHomeAssistantClient()
        assert hr_corrections.refresh_statistics_cache(adapter, client, correction) is True
        assert len(client.calls) == 1
        hour_start = bucket_start(correction.state_ts, HOURLY_SECONDS)
        assert client.calls[0] == {
            "statistic_id": self.ENTITY,
            "unit_of_measurement": "°C",
            "mean_type": 0,
            "has_sum": True,
            "unit_class": None,
            "stats": [{"start_ts": hour_start, "mean": None, "min": None, "max": None}],
        }

    def test_metadata_missing_at_refresh_time_is_not_pushed(self, adapter: FakeAdapter) -> None:
        # No add_statistics_metadata call: get_statistics_metadata returns
        # None even though the correction itself claims stats_corrected.
        correction = _raw_correction()
        client = _FakeHomeAssistantClient()
        assert hr_corrections.refresh_statistics_cache(adapter, client, correction) is False
        assert client.calls == []

    def test_an_api_failure_is_reported_as_no_refresh_not_an_exception(
        self, adapter: FakeAdapter, caplog: pytest.LogCaptureFixture
    ) -> None:
        correction = self._corrected(adapter)
        client = _FakeHomeAssistantClient(fail=True)
        with caplog.at_level("INFO"):
            result = hr_corrections.refresh_statistics_cache(adapter, client, correction)
        assert result is False
        assert caplog.records[-1].getMessage() == (
            "Could not refresh Home Assistant's statistics cache: simulated API failure"
        )

    def test_a_bucket_with_no_statistics_row_is_not_pushed(self, adapter: FakeAdapter) -> None:
        # _corrected() always seeds the hourly row itself; here it is left
        # out, mirroring a bucket recomputation that has not happened yet.
        adapter.add_entity(self.ENTITY, SensorType.COUNTER)
        adapter.add_statistics_metadata(self.ENTITY, mean_type=0, has_sum=True)
        adapter.add_state(self.ENTITY, 9, 1_700_000_000.0, "999999.0")
        correction = hr_corrections.apply(
            adapter,
            entity_id=self.ENTITY,
            state_id=9,
            expected_original="999999.0",
            new_value="1234.0",
            quality=None,
            note=None,
            created_by="marcel",
        )
        client = _FakeHomeAssistantClient()
        assert hr_corrections.refresh_statistics_cache(adapter, client, correction) is False
        assert client.calls == []


class TestRefreshStatisticsCacheBulk:
    ENTITY = "sensor.energy_total"

    def _setup(self, adapter: FakeAdapter, *, hours: int) -> BulkCorrectionResult:
        adapter.add_entity(self.ENTITY, SensorType.COUNTER)
        adapter.add_statistics_metadata(self.ENTITY, mean_type=0, has_sum=True)
        correction_ids = []
        for i in range(hours):
            state_id = 100 + i
            ts = 1_700_000_000.0 + i * HOURLY_SECONDS
            adapter.add_state(self.ENTITY, state_id, ts, "100.0")
            correction = hr_corrections.apply(
                adapter,
                entity_id=self.ENTITY,
                state_id=state_id,
                expected_original="100.0",
                new_value="105.0",
                quality=None,
                note=None,
                created_by="marcel",
            )
            adapter.add_hourly_statistics(
                self.ENTITY, bucket_start(ts, HOURLY_SECONDS), state=105.0
            )
            correction_ids.append(correction.id)
        return BulkCorrectionResult(
            entity_id=self.ENTITY,
            correction_ids=correction_ids,
            applied=len(correction_ids),
            skipped=0,
        )

    def test_no_client_means_no_refresh(self, adapter: FakeAdapter) -> None:
        result = self._setup(adapter, hours=1)
        assert hr_corrections.refresh_statistics_cache_bulk(adapter, None, result) == 0

    def test_nothing_applied_means_no_refresh_even_with_a_client(
        self, adapter: FakeAdapter
    ) -> None:
        # applied == 0 must skip on its own, independent of whether a client
        # was supplied — the two halves of the guard are an "or", not an
        # "and".
        adapter.add_entity(self.ENTITY, SensorType.COUNTER)
        adapter.add_statistics_metadata(self.ENTITY, mean_type=0, has_sum=True)
        result = BulkCorrectionResult(
            entity_id=self.ENTITY, correction_ids=[], applied=0, skipped=3
        )
        client = _FakeHomeAssistantClient()
        assert hr_corrections.refresh_statistics_cache_bulk(adapter, client, result) == 0
        assert client.calls == []

    def test_exactly_one_applied_row_still_refreshes(self, adapter: FakeAdapter) -> None:
        result = self._setup(adapter, hours=1)
        client = _FakeHomeAssistantClient()
        assert hr_corrections.refresh_statistics_cache_bulk(adapter, client, result) == 1

    def test_metadata_missing_at_refresh_time_refreshes_nothing(self, adapter: FakeAdapter) -> None:
        adapter.add_entity(self.ENTITY, SensorType.COUNTER)
        # No add_statistics_metadata call.
        result = BulkCorrectionResult(
            entity_id=self.ENTITY, correction_ids=[1], applied=1, skipped=0
        )
        client = _FakeHomeAssistantClient()
        assert hr_corrections.refresh_statistics_cache_bulk(adapter, client, result) == 0

    def test_each_distinct_hour_is_pushed_exactly_once(self, adapter: FakeAdapter) -> None:
        result = self._setup(adapter, hours=3)
        client = _FakeHomeAssistantClient()
        refreshed = hr_corrections.refresh_statistics_cache_bulk(adapter, client, result)
        assert refreshed == 3
        assert len(client.calls) == 3
        assert all(call["statistic_id"] == self.ENTITY for call in client.calls)

    def test_a_correction_with_no_stats_correction_contributes_no_hour(
        self, adapter: FakeAdapter
    ) -> None:
        # An entity with no statistics metadata at correction time never sets
        # stats_corrected, so it must not count towards the hours pushed even
        # when the bulk result reports it as applied.
        adapter.add_entity(self.ENTITY, SensorType.COUNTER)
        adapter.add_statistics_metadata(self.ENTITY, mean_type=0, has_sum=True)
        adapter.add_state(self.ENTITY, 200, 1_700_000_000.0, "100.0")
        correction = hr_corrections.apply(
            adapter,
            entity_id=self.ENTITY,
            state_id=200,
            expected_original="100.0",
            new_value="105.0",
            quality=None,
            note=None,
            created_by="marcel",
        )
        adapter.corrections[correction.id] = Correction(
            **{**correction.__dict__, "stats_corrected": False}
        )
        result = BulkCorrectionResult(
            entity_id=self.ENTITY, correction_ids=[correction.id], applied=1, skipped=0
        )
        client = _FakeHomeAssistantClient()
        assert hr_corrections.refresh_statistics_cache_bulk(adapter, client, result) == 0
        assert client.calls == []

    def test_at_exactly_the_cap_every_hour_still_refreshes(self, adapter: FakeAdapter) -> None:
        result = self._setup(adapter, hours=MAX_BULK_CACHE_REFRESH_HOURS)
        client = _FakeHomeAssistantClient()
        refreshed = hr_corrections.refresh_statistics_cache_bulk(adapter, client, result)
        assert refreshed == MAX_BULK_CACHE_REFRESH_HOURS

    def test_one_hour_past_the_cap_refreshes_nothing(
        self, adapter: FakeAdapter, caplog: pytest.LogCaptureFixture
    ) -> None:
        hours = MAX_BULK_CACHE_REFRESH_HOURS + 1
        result = self._setup(adapter, hours=hours)
        client = _FakeHomeAssistantClient()
        with caplog.at_level("INFO"):
            refreshed = hr_corrections.refresh_statistics_cache_bulk(adapter, client, result)
        assert refreshed == 0
        assert client.calls == []
        assert caplog.records[-1].getMessage() == (
            f"Bulk correction touched {hours} hours, above the "
            f"{MAX_BULK_CACHE_REFRESH_HOURS}-hour cache-refresh cap; "
            f"skipping the API refresh for {self.ENTITY}."
        )
