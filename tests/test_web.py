"""Tests for hr_web — routing, the JSON API, Ingress plumbing, and the way
adapter errors are turned into HTTP status codes.

The status codes matter more than they look: the browser distinguishes a
conflict (reload and retry) from a lock timeout (try again in a moment) from
a validation error (fix the input), and shows the user a different message
for each.
"""

from __future__ import annotations

from typing import Any

import pytest

from hr_fakes import SEED_WINDOW, FakeAdapter
from hr_models import HealthReport, SensorType
from hr_web import IngressMiddleware, current_user, serialise


class TestOnboardingGate:
    def test_root_redirects_to_onboarding_until_it_is_completed(self, fresh_app: Any) -> None:
        response = fresh_app.test_client().get("/")
        assert response.status_code == 302
        assert "/onboarding" in response.headers["Location"]

    def test_root_renders_the_entity_browser_once_onboarded(self, client: Any) -> None:
        response = client.get("/")
        assert response.status_code == 200
        assert b"Entities" in response.data

    def test_onboarding_requires_the_backup_acknowledgement(self, fresh_app: Any) -> None:
        response = fresh_app.test_client().post("/api/onboarding", json={})
        assert response.status_code == 400
        assert "backup" in response.get_json()["error"].lower()

    def test_onboarding_refuses_when_the_database_checks_fail(
        self, fresh_app: Any, adapter: FakeAdapter
    ) -> None:
        adapter.health = HealthReport(
            connected=True, schema_version=20, errors=["Recorder schema 20 is too old."]
        )
        response = fresh_app.test_client().post(
            "/api/onboarding", json={"backup_acknowledged": True}
        )
        assert response.status_code == 400
        assert adapter.audit_table_created is False

    def test_completing_onboarding_creates_the_audit_table(
        self, fresh_app: Any, adapter: FakeAdapter
    ) -> None:
        client = fresh_app.test_client()
        response = client.post("/api/onboarding", json={"backup_acknowledged": True})
        assert response.status_code == 200
        assert adapter.audit_table_created is True
        assert client.get("/").status_code == 200

    def test_corrections_are_refused_before_onboarding(self, fresh_app: Any) -> None:
        response = fresh_app.test_client().post(
            "/api/corrections",
            json={
                "entity_id": "sensor.living_room_temperature",
                "state_id": 2,
                "new_value": "20.2",
            },
        )
        assert response.status_code == 400

    def test_the_entity_page_redirects_to_onboarding_until_it_is_completed(
        self, fresh_app: Any
    ) -> None:
        response = fresh_app.test_client().get("/entity/sensor.living_room_temperature")
        assert response.status_code == 302
        assert "/onboarding" in response.headers["Location"]

    def test_the_audit_page_redirects_to_onboarding_until_it_is_completed(
        self, fresh_app: Any
    ) -> None:
        response = fresh_app.test_client().get("/audit")
        assert response.status_code == 302
        assert "/onboarding" in response.headers["Location"]


class TestEntityApi:
    def test_lists_entities_with_a_total(self, client: Any) -> None:
        body = client.get("/api/entities").get_json()
        assert body["total"] == 1
        assert body["entities"][0]["entity_id"] == "sensor.living_room_temperature"
        # The enum must serialise as its value, not as "SensorType.MEASUREMENT".
        assert body["entities"][0]["sensor_type"] == "measurement"

    def test_search_filters_the_list(self, client: Any, adapter: FakeAdapter) -> None:
        adapter.add_entity("sensor.garden_humidity")
        assert client.get("/api/entities?search=garden").get_json()["total"] == 1
        assert client.get("/api/entities?search=nothing").get_json()["total"] == 0

    def test_page_size_is_capped(self, client: Any) -> None:
        body = client.get("/api/entities?limit=99999").get_json()
        assert body["limit"] == 500

    def test_filters_by_sensor_type(self, client: Any, adapter: FakeAdapter) -> None:
        adapter.add_entity("sensor.energy_total", SensorType.COUNTER)
        adapter.add_statistics_metadata("sensor.energy_total", mean_type=0, has_sum=True)

        counters = client.get("/api/entities?type=counter").get_json()
        assert [e["entity_id"] for e in counters["entities"]] == ["sensor.energy_total"]

        measurements = client.get("/api/entities?type=measurement").get_json()
        assert [e["entity_id"] for e in measurements["entities"]] == [
            "sensor.living_room_temperature"
        ]

    def test_an_unrecognised_type_is_ignored_rather_than_rejected(self, client: Any) -> None:
        # A display filter, not correctness-critical input.
        body = client.get("/api/entities?type=not-a-real-type").get_json()
        assert body["total"] == 1

    def test_sorts_by_corrections_descending(self, client: Any, adapter: FakeAdapter) -> None:
        adapter.add_entity("sensor.garden_humidity")
        client.post(
            "/api/corrections",
            json={
                "entity_id": "sensor.living_room_temperature",
                "state_id": 2,
                "expected_original": "-2000.0",
                "new_value": "20.2",
            },
        )
        body = client.get("/api/entities?sort=corrections&dir=desc").get_json()
        assert body["entities"][0]["entity_id"] == "sensor.living_room_temperature"
        assert body["entities"][0]["correction_count"] == 1
        assert body["sort"] == "corrections"
        assert body["dir"] == "desc"

    def test_an_unrecognised_sort_falls_back_to_the_default(self, client: Any) -> None:
        body = client.get("/api/entities?sort=not-a-real-column").get_json()
        assert body["sort"] == "entity_id"

    def test_an_unrecognised_sort_direction_falls_back_to_ascending(self, client: Any) -> None:
        body = client.get("/api/entities?dir=sideways").get_json()
        assert body["dir"] == "asc"

    def test_unknown_entity_is_a_404(self, client: Any) -> None:
        response = client.get("/api/entities/sensor.nope/states")
        assert response.status_code == 404
        assert response.get_json()["kind"] == "not_found"

    def test_states_include_the_correction_marker(self, client: Any, adapter: FakeAdapter) -> None:
        client.post(
            "/api/corrections",
            json={
                "entity_id": "sensor.living_room_temperature",
                "state_id": 2,
                "expected_original": "-2000.0",
                "new_value": "20.2",
            },
        )
        body = client.get(
            f"/api/entities/sensor.living_room_temperature/states"
            f"?start={SEED_WINDOW[0]}&end={SEED_WINDOW[1]}"
        ).get_json()

        corrected = [p for p in body["points"] if p["correction_id"]]
        assert len(corrected) == 1
        assert corrected[0]["value"] == "20.2"
        assert corrected[0]["original_value"] == "-2000.0"

    def test_an_inverted_range_is_rejected(self, client: Any) -> None:
        response = client.get(
            "/api/entities/sensor.living_room_temperature/states?start=200&end=100"
        )
        assert response.status_code == 400
        assert response.get_json()["kind"] == "validation"

    def test_the_seeded_outlier_is_flagged_as_a_candidate(self, client: Any) -> None:
        body = client.get(
            f"/api/entities/sensor.living_room_temperature/states"
            f"?start={SEED_WINDOW[0]}&end={SEED_WINDOW[1]}"
        ).get_json()
        flagged = {p["value"] for p in body["points"] if p["is_candidate"]}
        assert flagged == {"-2000.0"}
        assert body["threshold"] == pytest.approx(3.0)

    def test_a_high_threshold_flags_nothing(self, client: Any) -> None:
        body = client.get(
            f"/api/entities/sensor.living_room_temperature/states"
            f"?start={SEED_WINDOW[0]}&end={SEED_WINDOW[1]}&threshold=1000000"
        ).get_json()
        assert not any(p["is_candidate"] for p in body["points"])
        assert body["threshold"] == pytest.approx(1_000_000.0)

    def test_a_counter_entity_uses_counter_detection(
        self, client: Any, adapter: FakeAdapter
    ) -> None:
        adapter.add_entity("sensor.energy_total", SensorType.COUNTER)
        adapter.add_statistics_metadata("sensor.energy_total", mean_type=0, has_sum=True)
        adapter.add_state("sensor.energy_total", 20, SEED_WINDOW[0] + 10, "100.0")
        adapter.add_state("sensor.energy_total", 21, SEED_WINDOW[0] + 20, "105.0")
        adapter.add_state("sensor.energy_total", 22, SEED_WINDOW[0] + 30, "110.0")
        adapter.add_state("sensor.energy_total", 23, SEED_WINDOW[0] + 40, "115.0")
        adapter.add_state("sensor.energy_total", 24, SEED_WINDOW[0] + 50, "99999.0")
        adapter.add_state("sensor.energy_total", 25, SEED_WINDOW[0] + 60, "125.0")
        adapter.add_state("sensor.energy_total", 26, SEED_WINDOW[0] + 70, "130.0")
        body = client.get(
            f"/api/entities/sensor.energy_total/states?start={SEED_WINDOW[0]}&end={SEED_WINDOW[1]}"
        ).get_json()
        flagged = {p["value"] for p in body["points"] if p["is_candidate"]}
        assert "99999.0" in flagged

    def test_an_unclassifiable_entity_flags_nothing(
        self, client: Any, adapter: FakeAdapter
    ) -> None:
        # No statistics metadata at all: sensor type is UNKNOWN, and no
        # detection method applies to it.
        adapter.add_entity("sensor.mystery", SensorType.UNKNOWN)
        adapter.add_state("sensor.mystery", 30, SEED_WINDOW[0] + 10, "1.0")
        adapter.add_state("sensor.mystery", 31, SEED_WINDOW[0] + 20, "99999.0")
        body = client.get(
            f"/api/entities/sensor.mystery/states?start={SEED_WINDOW[0]}&end={SEED_WINDOW[1]}"
        ).get_json()
        assert not any(p["is_candidate"] for p in body["points"])


class TestStatesApiBackfill:
    """The graph reaching further back than raw states survive — see
    long-term-statistics-graph-design.md. hr_statistics.py's
    TestBackfillFromStatistics already covers the merge logic itself in
    isolation; these confirm the web layer actually calls it, only when
    needed, and keeps backfilled points out of outlier detection."""

    def test_a_range_within_raw_states_is_not_backfilled(
        self, client: Any, adapter: FakeAdapter
    ) -> None:
        # The seeded fixture's own states already cover this window — no
        # statistics were even seeded, so a backfill attempt would 500 if one
        # were wrongly triggered.
        body = client.get(
            f"/api/entities/sensor.living_room_temperature/states"
            f"?start={SEED_WINDOW[0]}&end={SEED_WINDOW[1]}"
        ).get_json()
        assert all(p["source"] == "state" for p in body["points"])

    def test_a_gap_before_the_earliest_state_is_backfilled(
        self, client: Any, adapter: FakeAdapter
    ) -> None:
        from hr_statistics import HOURLY_SECONDS

        entity_id = "sensor.long_history"
        adapter.add_entity(entity_id, SensorType.MEASUREMENT)
        adapter.add_statistics_metadata(entity_id, mean_type=1, has_sum=False)
        earliest_state_ts = SEED_WINDOW[0]
        adapter.add_state(entity_id, 500, earliest_state_ts, "20.0")
        adapter.add_hourly_statistics(entity_id, earliest_state_ts - HOURLY_SECONDS, mean=15.0)
        adapter.add_hourly_statistics(entity_id, earliest_state_ts - 2 * HOURLY_SECONDS, mean=14.0)

        body = client.get(
            f"/api/entities/{entity_id}/states"
            f"?start={earliest_state_ts - 3 * HOURLY_SECONDS}&end={earliest_state_ts + 10}"
        ).get_json()

        sources = [(p["ts"], p["source"]) for p in body["points"]]
        assert sources == [
            (earliest_state_ts - 2 * HOURLY_SECONDS, "statistics"),
            (earliest_state_ts - HOURLY_SECONDS, "statistics"),
            (earliest_state_ts, "state"),
        ]

    def test_backfilled_points_have_no_state_id(self, client: Any, adapter: FakeAdapter) -> None:
        from hr_statistics import HOURLY_SECONDS

        entity_id = "sensor.long_history"
        adapter.add_entity(entity_id, SensorType.MEASUREMENT)
        adapter.add_statistics_metadata(entity_id, mean_type=1, has_sum=False)
        earliest_state_ts = SEED_WINDOW[0]
        adapter.add_state(entity_id, 500, earliest_state_ts, "20.0")
        adapter.add_hourly_statistics(entity_id, earliest_state_ts - HOURLY_SECONDS, mean=15.0)

        body = client.get(
            f"/api/entities/{entity_id}/states"
            f"?start={earliest_state_ts - 2 * HOURLY_SECONDS}&end={earliest_state_ts + 10}"
        ).get_json()

        backfilled = [p for p in body["points"] if p["source"] == "statistics"]
        assert backfilled
        assert all(p["state_id"] is None for p in backfilled)

    def test_backfilled_points_are_never_outlier_candidates(
        self, client: Any, adapter: FakeAdapter
    ) -> None:
        from hr_statistics import HOURLY_SECONDS

        entity_id = "sensor.long_history"
        adapter.add_entity(entity_id, SensorType.MEASUREMENT)
        adapter.add_statistics_metadata(entity_id, mean_type=1, has_sum=False)
        earliest_state_ts = SEED_WINDOW[0]
        # A dramatic value, exactly the shape that would otherwise flag —
        # but it has no real row underneath it to correct.
        adapter.add_hourly_statistics(entity_id, earliest_state_ts - HOURLY_SECONDS, mean=-2000.0)
        adapter.add_state(entity_id, 500, earliest_state_ts, "20.0")
        adapter.add_state(entity_id, 501, earliest_state_ts + 10, "20.1")
        adapter.add_state(entity_id, 502, earliest_state_ts + 20, "19.9")

        body = client.get(
            f"/api/entities/{entity_id}/states"
            f"?start={earliest_state_ts - 2 * HOURLY_SECONDS}&end={earliest_state_ts + 30}"
        ).get_json()

        assert not any(p["is_candidate"] for p in body["points"])

    def test_no_statistics_metadata_skips_backfill_without_erroring(
        self, client: Any, adapter: FakeAdapter
    ) -> None:
        # An UNKNOWN entity has no statistics_meta row at all — the lookup
        # must return None cleanly, not raise, and the graph just shows
        # whatever states exist with no attempt to fill the gap.
        entity_id = "sensor.mystery"
        adapter.add_entity(entity_id, SensorType.UNKNOWN)
        adapter.add_state(entity_id, 500, SEED_WINDOW[0], "1.0")

        response = client.get(
            f"/api/entities/{entity_id}/states?start={SEED_WINDOW[0] - 3600}&end={SEED_WINDOW[1]}"
        )
        assert response.status_code == 200
        assert all(p["source"] == "state" for p in response.get_json()["points"])


class TestCascadeScopeApi:
    def test_reports_how_many_rows_a_counter_correction_would_touch(
        self, client: Any, adapter: FakeAdapter
    ) -> None:
        adapter.add_entity("sensor.energy_total", SensorType.COUNTER)
        adapter.add_statistics_metadata("sensor.energy_total", mean_type=0, has_sum=True)
        body = client.get(
            "/api/entities/sensor.energy_total/cascade-scope?state_ts=1700000000.0"
        ).get_json()
        assert "short_term" in body["scope"]
        assert "hourly" in body["scope"]

    def test_a_missing_state_ts_is_a_validation_error(self, client: Any) -> None:
        response = client.get("/api/entities/sensor.living_room_temperature/cascade-scope")
        assert response.status_code == 400
        assert response.get_json()["kind"] == "validation"


class TestCorrectionApi:
    def _correct(self, client: Any, **overrides: Any) -> Any:
        payload = {
            "entity_id": "sensor.living_room_temperature",
            "state_id": 2,
            "expected_original": "-2000.0",
            "new_value": "20.2",
            "quality": "spike",
            "note": "modem reboot",
        }
        payload.update(overrides)
        return client.post("/api/corrections", json=payload)

    def test_creating_a_correction_returns_201_and_the_audit_record(self, client: Any) -> None:
        response = self._correct(client)
        assert response.status_code == 201
        correction = response.get_json()["correction"]
        assert correction["original_value"] == "-2000.0"
        assert correction["corrected_value"] == "20.2"
        assert correction["quality"] == "spike"
        assert correction["stats_corrected"] is False

    @pytest.mark.parametrize("field", ["entity_id", "state_id", "new_value"])
    def test_missing_required_fields_are_rejected(self, client: Any, field: str) -> None:
        response = self._correct(client, **{field: None})
        assert response.status_code == 400
        assert field in response.get_json()["error"]

    def test_a_stale_value_is_a_409_conflict(self, client: Any, adapter: FakeAdapter) -> None:
        adapter.states[2] = ("sensor.living_room_temperature", 1_700_000_300.0, "19.9")
        response = self._correct(client)
        assert response.status_code == 409
        assert response.get_json()["kind"] == "conflict"

    def test_a_counter_sensor_is_accepted_and_its_totals_rebuilt(
        self, client: Any, adapter: FakeAdapter
    ) -> None:
        adapter.add_entity("sensor.energy_total", SensorType.COUNTER)
        adapter.add_statistics_metadata("sensor.energy_total", mean_type=0, has_sum=True)
        adapter.add_state("sensor.energy_total", 9, 1_700_000_000.0, "999999.0")
        response = self._correct(
            client,
            entity_id="sensor.energy_total",
            state_id=9,
            expected_original="999999.0",
        )
        assert response.status_code == 201
        assert response.get_json()["correction"]["stats_corrected"] is True

    def test_an_unknown_sensor_type_is_accepted_states_only(
        self, client: Any, adapter: FakeAdapter
    ) -> None:
        # No statistics_meta row at all — Home Assistant recorded no
        # state_class for this entity — so there is nothing to cascade.
        adapter.add_entity("sensor.mystery", SensorType.UNKNOWN)
        adapter.add_state("sensor.mystery", 30, 1_700_000_000.0, "5.0")
        response = self._correct(
            client,
            entity_id="sensor.mystery",
            state_id=30,
            expected_original="5.0",
            new_value="6.0",
        )
        assert response.status_code == 201
        assert response.get_json()["correction"]["stats_corrected"] is False
        assert adapter.states[30][2] == "6.0"

    def test_a_lock_timeout_becomes_a_503(
        self, client: Any, adapter: FakeAdapter, monkeypatch: Any
    ) -> None:
        from hr_db import LockTimeout

        def _timeout(**kwargs: Any) -> None:
            raise LockTimeout("The recorder is currently holding this row.")

        monkeypatch.setattr(adapter, "apply_correction", _timeout)
        response = self._correct(client)
        assert response.status_code == 503
        assert response.get_json()["kind"] == "lock_timeout"

    def test_a_connection_failure_becomes_a_503(
        self, client: Any, adapter: FakeAdapter, monkeypatch: Any
    ) -> None:
        from hr_db import ConnectionFailed

        def _fail(**kwargs: Any) -> None:
            raise ConnectionFailed("Could not reach the recorder database.")

        monkeypatch.setattr(adapter, "apply_correction", _fail)
        response = self._correct(client)
        assert response.status_code == 503
        assert response.get_json()["kind"] == "connection"

    def test_an_unrecognised_adapter_error_becomes_a_500(
        self, client: Any, adapter: FakeAdapter, monkeypatch: Any
    ) -> None:
        # The catch-all for any AdapterError subclass without its own
        # handler — a database problem the browser cannot act on beyond
        # "something is wrong," unlike the more specific 503/409/404 cases.
        from hr_db import AdapterError

        def _fail(**kwargs: Any) -> None:
            raise AdapterError("Something the other handlers don't know about.")

        monkeypatch.setattr(adapter, "apply_correction", _fail)
        response = self._correct(client)
        assert response.status_code == 500
        assert response.get_json()["kind"] == "database"

    def test_restore_puts_the_original_value_back(self, client: Any, adapter: FakeAdapter) -> None:
        correction_id = self._correct(client).get_json()["correction"]["id"]
        assert adapter.states[2][2] == "20.2"

        response = client.post(f"/api/corrections/{correction_id}/restore")
        assert response.status_code == 200
        assert response.get_json()["correction"]["restored_at"] is not None
        assert adapter.states[2][2] == "-2000.0"

    def test_restoring_twice_is_a_conflict(self, client: Any) -> None:
        correction_id = self._correct(client).get_json()["correction"]["id"]
        client.post(f"/api/corrections/{correction_id}/restore")
        response = client.post(f"/api/corrections/{correction_id}/restore")
        assert response.status_code == 409

    def test_restore_refuses_a_row_changed_outside_the_addon(
        self, client: Any, adapter: FakeAdapter
    ) -> None:
        # This is the backup-restore scenario from the design document, 9.11:
        # the states row no longer holds what the add-on wrote, so putting the
        # "original" back would destroy whatever is there now.
        correction_id = self._correct(client).get_json()["correction"]["id"]
        adapter.states[2] = ("sensor.living_room_temperature", 1_700_000_300.0, "18.0")

        response = client.post(f"/api/corrections/{correction_id}/restore")
        assert response.status_code == 409
        assert adapter.states[2][2] == "18.0"

    def test_the_audit_list_can_hide_restored_corrections(self, client: Any) -> None:
        correction_id = self._correct(client).get_json()["correction"]["id"]
        client.post(f"/api/corrections/{correction_id}/restore")

        assert len(client.get("/api/corrections").get_json()["corrections"]) == 1
        hidden = client.get("/api/corrections?include_restored=0").get_json()
        assert hidden["corrections"] == []


class TestOrphanedCorrections:
    """Backup consistency (design document, 9.11).

    Restoring a Home Assistant backup reverts states without touching this
    add-on's audit table, so an active correction can silently stop matching
    what is actually stored. No automatic re-application — the user is shown
    the mismatch and decides whether to re-apply it or dismiss it.
    """

    def _correct(self, client: Any) -> int:
        payload = {
            "entity_id": "sensor.living_room_temperature",
            "state_id": 2,
            "expected_original": "-2000.0",
            "new_value": "20.2",
            "quality": "spike",
            "note": None,
        }
        response = client.post("/api/corrections", json=payload)
        return int(response.get_json()["correction"]["id"])

    def test_a_correction_still_matching_the_database_is_not_orphaned(self, client: Any) -> None:
        self._correct(client)
        response = client.get("/api/corrections/orphaned")
        assert response.status_code == 200
        assert response.get_json()["corrections"] == []

    def test_a_backup_restore_is_detected_as_orphaned(
        self, client: Any, adapter: FakeAdapter
    ) -> None:
        correction_id = self._correct(client)
        # Simulate a backup restore putting the pre-correction value back
        # without going through this add-on at all.
        adapter.states[2] = ("sensor.living_room_temperature", 1_700_000_300.0, "-2000.0")

        response = client.get("/api/corrections/orphaned")
        assert response.status_code == 200
        orphaned = response.get_json()["corrections"]
        assert [c["id"] for c in orphaned] == [correction_id]

    def test_a_restored_correction_is_never_orphaned(
        self, client: Any, adapter: FakeAdapter
    ) -> None:
        correction_id = self._correct(client)
        client.post(f"/api/corrections/{correction_id}/restore")
        # The row now holds the original value again, same shape as a backup
        # restore — but this add-on itself put it there, so it is not orphaned.
        assert client.get("/api/corrections/orphaned").get_json()["corrections"] == []

    def test_dismissing_an_orphaned_correction_stops_it_reappearing(
        self, client: Any, adapter: FakeAdapter
    ) -> None:
        correction_id = self._correct(client)
        adapter.states[2] = ("sensor.living_room_temperature", 1_700_000_300.0, "-2000.0")

        response = client.post(f"/api/corrections/{correction_id}/dismiss")
        assert response.status_code == 200
        assert response.get_json()["correction"]["dismissed_at"] is not None
        assert client.get("/api/corrections/orphaned").get_json()["corrections"] == []

    def test_dismissing_twice_is_a_conflict(self, client: Any, adapter: FakeAdapter) -> None:
        correction_id = self._correct(client)
        adapter.states[2] = ("sensor.living_room_temperature", 1_700_000_300.0, "-2000.0")
        client.post(f"/api/corrections/{correction_id}/dismiss")

        response = client.post(f"/api/corrections/{correction_id}/dismiss")
        assert response.status_code == 409

    def test_dismissing_a_restored_correction_is_refused(
        self, client: Any, adapter: FakeAdapter
    ) -> None:
        correction_id = self._correct(client)
        client.post(f"/api/corrections/{correction_id}/restore")

        response = client.post(f"/api/corrections/{correction_id}/dismiss")
        assert response.status_code == 409

    def test_dismissing_an_unknown_correction_is_not_found(self, client: Any) -> None:
        response = client.post("/api/corrections/999999/dismiss")
        assert response.status_code == 404


class TestCsvExport:
    def _correct(self, client: Any, **overrides: Any) -> Any:
        payload = {
            "entity_id": "sensor.living_room_temperature",
            "state_id": 2,
            "expected_original": "-2000.0",
            "new_value": "20.2",
            "quality": "spike",
            "note": "modem reboot",
        }
        payload.update(overrides)
        return client.post("/api/corrections", json=payload)

    def test_exports_a_header_and_one_row_per_correction(self, client: Any) -> None:
        import csv
        import io

        self._correct(client)
        response = client.get("/api/corrections/export.csv")

        assert response.status_code == 200
        assert response.content_type.startswith("text/csv")
        assert "attachment" in response.headers["Content-Disposition"]

        rows = list(csv.reader(io.StringIO(response.get_data(as_text=True))))
        assert rows[0][:4] == ["id", "entity_id", "sensor_type", "state_id"]
        assert len(rows) == 2
        data_row = dict(zip(rows[0], rows[1], strict=True))
        assert data_row["entity_id"] == "sensor.living_room_temperature"
        assert data_row["original_value"] == "-2000.0"
        assert data_row["corrected_value"] == "20.2"

    def test_every_timestamp_column_is_the_same_readable_format(self, client: Any) -> None:
        # Before this, recorded_at (state_ts) was a raw epoch float with no
        # date at all, while created_at/restored_at/dismissed_at were ISO
        # strings with microseconds — three different shapes on one row, none
        # matching the friendly date the on-screen UI shows for the same
        # correction. All four now share one "YYYY-MM-DD HH:MM:SS" format.
        import csv
        import io
        import re

        correction_id = self._correct(client).get_json()["correction"]["id"]
        client.post(f"/api/corrections/{correction_id}/restore")

        rows = list(
            csv.reader(
                io.StringIO(client.get("/api/corrections/export.csv").get_data(as_text=True))
            )
        )
        header, data_row = rows[0], dict(zip(rows[0], rows[1], strict=True))
        assert "recorded_at" in header

        pattern = re.compile(r"^\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}$")
        for column in ("recorded_at", "created_at", "restored_at"):
            assert pattern.match(data_row[column]), f"{column}={data_row[column]!r}"
        # Never set on this row, and must stay blank rather than becoming
        # "1970-01-01 00:00:00" or some other artifact of formatting None.
        assert data_row["dismissed_at"] == ""

    def test_an_empty_audit_trail_exports_just_the_header(self, client: Any) -> None:
        import csv
        import io

        response = client.get("/api/corrections/export.csv")
        rows = list(csv.reader(io.StringIO(response.get_data(as_text=True))))
        assert len(rows) == 1

    def test_respects_the_hide_restored_filter(self, client: Any) -> None:
        import csv
        import io

        correction_id = self._correct(client).get_json()["correction"]["id"]
        client.post(f"/api/corrections/{correction_id}/restore")

        full = list(
            csv.reader(
                io.StringIO(client.get("/api/corrections/export.csv").get_data(as_text=True))
            )
        )
        assert len(full) == 2

        active_only = list(
            csv.reader(
                io.StringIO(
                    client.get("/api/corrections/export.csv?include_restored=0").get_data(
                        as_text=True
                    )
                )
            )
        )
        assert len(active_only) == 1

    def test_respects_the_entity_filter(self, client: Any, adapter: FakeAdapter) -> None:
        import csv
        import io

        adapter.add_entity("sensor.other")
        adapter.add_state("sensor.other", 99, 1_700_000_000.0, "5.0")
        self._correct(client)
        self._correct(
            client,
            entity_id="sensor.other",
            state_id=99,
            expected_original="5.0",
            new_value="6.0",
        )

        rows = list(
            csv.reader(
                io.StringIO(
                    client.get("/api/corrections/export.csv?entity_id=sensor.other").get_data(
                        as_text=True
                    )
                )
            )
        )
        assert len(rows) == 2
        assert rows[1][1] == "sensor.other"


class TestBulkCorrectionApi:
    """A single fix applied across a whole range, in one transaction.

    Uses its own entity rather than the shared fixture, so a range and its
    boundary anchors can be laid out exactly for each scenario.
    """

    ENTITY = "sensor.bulk_test"
    BASE = 1_700_000_000.0

    def _seed(self, adapter: FakeAdapter) -> None:
        adapter.add_entity(self.ENTITY, SensorType.MEASUREMENT)
        adapter.add_statistics_metadata(self.ENTITY, mean_type=1, has_sum=False)
        # An anchor before the range, three bad readings inside it, an anchor
        # after. Timestamps 300s apart, matching a real 5-minute cadence.
        adapter.add_state(self.ENTITY, 200, self.BASE, "10.0")
        adapter.add_state(self.ENTITY, 201, self.BASE + 300, "-999.0")
        adapter.add_state(self.ENTITY, 202, self.BASE + 600, "-999.0")
        adapter.add_state(self.ENTITY, 203, self.BASE + 900, "-999.0")
        adapter.add_state(self.ENTITY, 204, self.BASE + 1200, "30.0")

    def _range(self) -> dict[str, float]:
        # Covers exactly the three bad readings, not the two anchors either
        # side of them.
        return {"start": self.BASE + 150, "end": self.BASE + 1050}

    def test_preview_counts_rows_and_reports_interpolation_is_possible(
        self, client: Any, adapter: FakeAdapter
    ) -> None:
        self._seed(adapter)
        r = self._range()
        body = client.get(
            f"/api/entities/{self.ENTITY}/bulk-preview?start={r['start']}&end={r['end']}"
        ).get_json()
        assert body["preview"]["total"] == 3
        assert body["preview"]["correctable"] == 3
        assert body["preview"]["already_corrected"] == 0
        assert body["preview"]["can_interpolate"] is True

    def test_preview_without_start_or_end_is_a_validation_error(
        self, client: Any, adapter: FakeAdapter
    ) -> None:
        self._seed(adapter)
        response = client.get(f"/api/entities/{self.ENTITY}/bulk-preview")
        assert response.status_code == 400
        assert response.get_json()["kind"] == "validation"

    def test_preview_with_an_inverted_range_is_a_validation_error(
        self, client: Any, adapter: FakeAdapter
    ) -> None:
        self._seed(adapter)
        response = client.get(
            f"/api/entities/{self.ENTITY}/bulk-preview?start={self.BASE + 1050}&end={self.BASE}"
        )
        assert response.status_code == 400
        assert response.get_json()["kind"] == "validation"

    def test_preview_without_an_outside_anchor_cannot_interpolate(
        self, client: Any, adapter: FakeAdapter
    ) -> None:
        self._seed(adapter)
        # A range reaching to the very first reading has no "before" anchor.
        body = client.get(
            f"/api/entities/{self.ENTITY}/bulk-preview?start={self.BASE - 1}&end={self.BASE + 1050}"
        ).get_json()
        assert body["preview"]["can_interpolate"] is False

    def test_constant_strategy_sets_every_row_in_range(
        self, client: Any, adapter: FakeAdapter
    ) -> None:
        self._seed(adapter)
        r = self._range()
        response = client.post(
            f"/api/entities/{self.ENTITY}/bulk-correction",
            json={**r, "strategy": "constant", "value": "20.0", "quality": "bad_comm"},
        )
        assert response.status_code == 201
        result = response.get_json()["result"]
        assert result["applied"] == 3
        assert result["skipped"] == 0
        assert len(result["correction_ids"]) == 3

        for state_id in (201, 202, 203):
            assert adapter.states[state_id][2] == "20.0"
        # The anchors outside the range are untouched.
        assert adapter.states[200][2] == "10.0"
        assert adapter.states[204][2] == "30.0"

    def test_interpolate_strategy_ramps_between_the_anchors(
        self, client: Any, adapter: FakeAdapter
    ) -> None:
        self._seed(adapter)
        r = self._range()
        response = client.post(
            f"/api/entities/{self.ENTITY}/bulk-correction",
            json={**r, "strategy": "interpolate", "quality": "frozen"},
        )
        assert response.status_code == 201

        # Anchors are 10.0 at t=0 and 30.0 at t=1200; the three corrected
        # readings sit at t=300, 600, 900 — a fifth, half, three-quarters of
        # the way across, so a straight line gives 15, 20, 25.
        assert float(adapter.states[201][2]) == pytest.approx(15.0)
        assert float(adapter.states[202][2]) == pytest.approx(20.0)
        assert float(adapter.states[203][2]) == pytest.approx(25.0)

    def test_a_correction_id_is_recorded_per_row(self, client: Any, adapter: FakeAdapter) -> None:
        self._seed(adapter)
        r = self._range()
        response = client.post(
            f"/api/entities/{self.ENTITY}/bulk-correction",
            json={**r, "strategy": "constant", "value": "20.0"},
        )
        ids = response.get_json()["result"]["correction_ids"]
        assert len(ids) == 3
        # Each is a real, individually restorable correction.
        restore = client.post(f"/api/corrections/{ids[0]}/restore")
        assert restore.status_code == 200
        assert adapter.states[201][2] == "-999.0"

    def test_already_corrected_rows_are_skipped_not_overwritten(
        self, client: Any, adapter: FakeAdapter
    ) -> None:
        self._seed(adapter)
        # Correct the middle row individually first.
        client.post(
            "/api/corrections",
            json={
                "entity_id": self.ENTITY,
                "state_id": 202,
                "expected_original": "-999.0",
                "new_value": "21.0",
            },
        )
        r = self._range()
        response = client.post(
            f"/api/entities/{self.ENTITY}/bulk-correction",
            json={**r, "strategy": "constant", "value": "20.0"},
        )
        result = response.get_json()["result"]
        assert result["applied"] == 2
        assert result["skipped"] == 1
        # The already-corrected row keeps its own value, not the bulk one.
        assert adapter.states[202][2] == "21.0"
        assert adapter.states[201][2] == "20.0"

    def test_missing_value_for_constant_strategy_is_rejected(
        self, client: Any, adapter: FakeAdapter
    ) -> None:
        self._seed(adapter)
        r = self._range()
        response = client.post(
            f"/api/entities/{self.ENTITY}/bulk-correction",
            json={**r, "strategy": "constant"},
        )
        assert response.status_code == 400
        assert response.get_json()["kind"] == "validation"

    @pytest.mark.parametrize("field", ["start", "end", "strategy"])
    def test_a_missing_required_field_is_rejected(
        self, client: Any, adapter: FakeAdapter, field: str
    ) -> None:
        self._seed(adapter)
        r = self._range()
        body = {**r, "strategy": "constant", "value": "20.0"}
        body[field] = None
        response = client.post(
            f"/api/entities/{self.ENTITY}/bulk-correction",
            json=body,
        )
        assert response.status_code == 400
        assert response.get_json()["kind"] == "validation"

    def test_an_unknown_strategy_is_rejected(self, client: Any, adapter: FakeAdapter) -> None:
        self._seed(adapter)
        r = self._range()
        response = client.post(
            f"/api/entities/{self.ENTITY}/bulk-correction",
            json={**r, "strategy": "delete_everything"},
        )
        assert response.status_code == 400
        assert response.get_json()["kind"] == "validation"

    def test_interpolating_without_both_anchors_is_an_invalid_range(
        self, client: Any, adapter: FakeAdapter
    ) -> None:
        self._seed(adapter)
        response = client.post(
            f"/api/entities/{self.ENTITY}/bulk-correction",
            json={
                "start": self.BASE - 1,
                "end": self.BASE + 1050,
                "strategy": "interpolate",
            },
        )
        assert response.status_code == 400
        assert response.get_json()["kind"] == "invalid_range"

    def test_an_inverted_range_is_rejected(self, client: Any, adapter: FakeAdapter) -> None:
        self._seed(adapter)
        response = client.post(
            f"/api/entities/{self.ENTITY}/bulk-correction",
            json={
                "start": self.BASE + 1050,
                "end": self.BASE + 150,
                "strategy": "constant",
                "value": "1.0",
            },
        )
        assert response.status_code == 400
        assert response.get_json()["kind"] == "validation"

    def test_a_counter_sensor_can_be_bulk_corrected_too(
        self, client: Any, adapter: FakeAdapter
    ) -> None:
        adapter.add_entity("sensor.energy_bulk", SensorType.COUNTER)
        adapter.add_statistics_metadata("sensor.energy_bulk", mean_type=0, has_sum=True)
        adapter.add_state("sensor.energy_bulk", 300, self.BASE, "100.0")
        adapter.add_state("sensor.energy_bulk", 301, self.BASE + 300, "99999.0")
        adapter.add_state("sensor.energy_bulk", 302, self.BASE + 600, "99999.0")
        adapter.add_state("sensor.energy_bulk", 303, self.BASE + 900, "115.0")

        response = client.post(
            "/api/entities/sensor.energy_bulk/bulk-correction",
            json={
                "start": self.BASE + 150,
                "end": self.BASE + 750,
                "strategy": "interpolate",
                "quality": "spike",
            },
        )
        assert response.status_code == 201
        result = response.get_json()["result"]
        assert result["applied"] == 2
        assert adapter.states[301][2] != "99999.0"
        assert adapter.states[302][2] != "99999.0"

    def test_response_reports_a_cache_refresh_attempt(
        self, client: Any, adapter: FakeAdapter
    ) -> None:
        # No Home Assistant API client is configured in tests, so this is 0 —
        # the field existing and being well-formed is what matters here.
        self._seed(adapter)
        r = self._range()
        response = client.post(
            f"/api/entities/{self.ENTITY}/bulk-correction",
            json={**r, "strategy": "constant", "value": "20.0"},
        )
        assert response.get_json()["statistics_cache_refreshed_hours"] == 0

    def test_bulk_correction_requires_onboarding(
        self, fresh_app: Any, adapter: FakeAdapter
    ) -> None:
        self._seed(adapter)
        r = self._range()
        response = fresh_app.test_client().post(
            f"/api/entities/{self.ENTITY}/bulk-correction",
            json={**r, "strategy": "constant", "value": "20.0"},
        )
        assert response.status_code == 400

    def test_the_row_cap_is_enforced(
        self, client: Any, adapter: FakeAdapter, monkeypatch: Any
    ) -> None:
        import hr_fakes

        monkeypatch.setattr(hr_fakes, "MAX_BULK_ROWS", 2)
        self._seed(adapter)
        r = self._range()  # 3 correctable rows, above the patched cap of 2
        response = client.post(
            f"/api/entities/{self.ENTITY}/bulk-correction",
            json={**r, "strategy": "constant", "value": "20.0"},
        )
        assert response.status_code == 400
        assert response.get_json()["kind"] == "invalid_range"
        # Nothing was written — the whole batch is refused, not truncated.
        for state_id in (201, 202, 203):
            assert adapter.states[state_id][2] == "-999.0"


class TestAuthorAttribution:
    def test_the_ingress_username_is_recorded_as_the_author(self, client: Any) -> None:
        response = client.post(
            "/api/corrections",
            json={
                "entity_id": "sensor.living_room_temperature",
                "state_id": 2,
                "expected_original": "-2000.0",
                "new_value": "20.2",
            },
            headers={"X-Remote-User-Display-Name": "Marcel"},
        )
        assert response.get_json()["correction"]["created_by"] == "Marcel"

    def test_falls_back_to_unknown_without_ingress_headers(self, client: Any) -> None:
        response = client.post(
            "/api/corrections",
            json={
                "entity_id": "sensor.living_room_temperature",
                "state_id": 2,
                "expected_original": "-2000.0",
                "new_value": "20.2",
            },
        )
        assert response.get_json()["correction"]["created_by"] == "unknown"

    def test_display_name_wins_over_username(self, app: Any) -> None:
        with app.test_request_context(
            headers={
                "X-Remote-User-Display-Name": "Marcel B",
                "X-Remote-User-Name": "marcel",
            }
        ):
            assert current_user() == "Marcel B"


class TestIngressMiddleware:
    def _environ(self, path: str, ingress: str | None) -> dict[str, Any]:
        environ: dict[str, Any] = {"PATH_INFO": path}
        if ingress is not None:
            environ["HTTP_X_INGRESS_PATH"] = ingress
        return environ

    def _run(self, environ: dict[str, Any]) -> dict[str, Any]:
        seen: dict[str, Any] = {}

        def _app(env: dict[str, Any], _start: Any) -> list[bytes]:
            seen.update(env)
            return [b""]

        IngressMiddleware(_app)(environ, lambda *args: None)
        return seen

    def test_strips_the_ingress_prefix_from_the_path(self) -> None:
        env = self._run(
            self._environ("/api/hassio_ingress/tok/entity/sensor.x", "/api/hassio_ingress/tok")
        )
        assert env["PATH_INFO"] == "/entity/sensor.x"
        assert env["SCRIPT_NAME"] == "/api/hassio_ingress/tok"

    def test_a_bare_prefix_becomes_the_root_path(self) -> None:
        env = self._run(self._environ("/api/hassio_ingress/tok", "/api/hassio_ingress/tok"))
        assert env["PATH_INFO"] == "/"

    def test_direct_access_is_left_alone(self) -> None:
        env = self._run(self._environ("/entity/sensor.x", None))
        assert env["PATH_INFO"] == "/entity/sensor.x"
        assert "SCRIPT_NAME" not in env

    def test_generated_urls_carry_the_ingress_prefix(self, client: Any) -> None:
        response = client.get("/", headers={"X-Ingress-Path": "/api/hassio_ingress/tok"})
        assert b"/api/hassio_ingress/tok/static/app.css" in response.data

    def test_the_onboarding_embed_snippet_uses_the_real_ingress_path(self, fresh_app: Any) -> None:
        # Regression: this was a hardcoded placeholder (.../history_corrections)
        # that never matched the real, randomly-tokened Ingress path a
        # webpage card would actually need to reach the add-on.
        response = fresh_app.test_client().get(
            "/onboarding", headers={"X-Ingress-Path": "/api/hassio_ingress/tok"}
        )
        assert b"url: /api/hassio_ingress/tok/" in response.data


class TestSerialise:
    def test_unpacks_dataclasses_and_enums(self) -> None:
        report = HealthReport(connected=True, schema_version=48)
        assert serialise(report)["schema_version"] == 48
        assert serialise(SensorType.COUNTER) == "counter"

    def test_recurses_through_containers(self) -> None:
        assert serialise({"types": [SensorType.COUNTER]}) == {"types": ["counter"]}


class TestPages:
    def test_the_entity_page_explains_what_happens_to_statistics(self, client: Any) -> None:
        # Statistics are rebuilt with the correction, but Home Assistant caches
        # them in memory, so the restart caveat has to be on the page.
        response = client.get("/entity/sensor.living_room_temperature")
        assert response.status_code == 200
        assert b"Statistics are corrected too" in response.data
        assert b"restart Home Assistant" in response.data

    def test_a_counter_entity_page_warns_about_the_cascade(
        self, client: Any, adapter: FakeAdapter
    ) -> None:
        # Correctable now, but the user is told a correction rewrites every
        # later running total before they make one.
        adapter.add_entity("sensor.energy_total", SensorType.COUNTER)
        response = client.get("/entity/sensor.energy_total")
        assert b"This is a counter" in response.data
        assert b"every total that follows it" in response.data

    def test_the_health_endpoint_reports_readiness(self, client: Any) -> None:
        body = client.get("/api/health").get_json()
        assert body["ok"] is True
        assert body["onboarding_complete"] is True
        assert body["schema_version"] == 48

    def test_the_time_format_option_is_injected_for_every_page(self, client: Any) -> None:
        # Not read from Home Assistant — there is no API for a backend add-on
        # to read a specific browsing user's personal date/time preference —
        # so it has to reach the frontend via this add-on's own config.
        assert b'window.HR_TIME_FORMAT = "24"' in client.get("/").data
        assert b'window.HR_TIME_FORMAT = "24"' in client.get("/audit").data

    def test_a_custom_time_format_is_injected_too(self, app: Any) -> None:
        from dataclasses import replace

        from hr_web import create_app

        custom_config = replace(app.config["HR_CONFIG"], time_format="12")
        custom_app = create_app(custom_config, adapter=app.extensions["hr_db"])
        custom_app.extensions["hr_state"].update(onboarding_complete=True)
        body = custom_app.test_client().get("/").data
        assert b'window.HR_TIME_FORMAT = "12"' in body


class TestTruncationIsSurfaced:
    """The API must tell the browser when it is showing part of a range."""

    def test_a_full_window_is_not_flagged(self, client: Any) -> None:
        body = client.get(
            f"/api/entities/sensor.living_room_temperature/states"
            f"?start={SEED_WINDOW[0]}&end={SEED_WINDOW[1]}"
        ).get_json()
        assert body["truncated"] is False

    def test_a_truncated_window_is_flagged_with_its_limit(
        self, client: Any, adapter: FakeAdapter, monkeypatch: Any
    ) -> None:
        from hr_models import StateSeries

        real = adapter.fetch_states

        def _small(entity_id: str, start: float, end: float, limit: int = 20000) -> StateSeries:
            return real(entity_id, start, end, limit=2)

        monkeypatch.setattr(adapter, "fetch_states", _small)
        body = client.get(
            f"/api/entities/sensor.living_room_temperature/states"
            f"?start={SEED_WINDOW[0]}&end={SEED_WINDOW[1]}"
        ).get_json()

        assert body["truncated"] is True
        assert body["limit"] == 2
        assert len(body["points"]) == 2

    def test_len_of_the_series_matches_its_points(self) -> None:
        from hr_models import StatePoint, StateSeries

        points = [
            StatePoint(state_id=1, ts=1.0, value="1.0", numeric_value=1.0),
            StatePoint(state_id=2, ts=2.0, value="2.0", numeric_value=2.0),
        ]
        assert len(StateSeries(points=points)) == 2


class TestBuildAdapter:
    """build_adapter picks the concrete adapter for whichever recorder
    backend Home Assistant's own config points at. Every real construction
    here is safe without a live database or file: all three adapters
    connect lazily, not in __init__."""

    def _config(self, **overrides: Any) -> Any:
        from hr_config import AppConfig

        defaults: dict[str, Any] = {
            "db_type": "mariadb",
            "database": None,
            "sqlite": None,
            "log_level": "INFO",
            "state_dir": "/config",
            "port": 8099,
            "time_format": "24",
        }
        defaults.update(overrides)
        return AppConfig(**defaults)

    def test_sqlite_builds_a_sqlite_adapter(self) -> None:
        from hr_config import SQLiteConfig
        from hr_sqlite import SQLiteAdapter
        from hr_web import build_adapter

        config = self._config(
            db_type="sqlite", sqlite=SQLiteConfig(path="/homeassistant/home-assistant_v2.db")
        )
        assert isinstance(build_adapter(config), SQLiteAdapter)

    def test_sqlite_without_a_sqlite_config_is_a_programming_error(self) -> None:
        from hr_web import build_adapter

        config = self._config(db_type="sqlite", sqlite=None)
        with pytest.raises(ValueError, match="no sqlite configuration"):
            build_adapter(config)

    def test_mariadb_without_a_database_config_is_a_programming_error(self) -> None:
        from hr_web import build_adapter

        config = self._config(db_type="mariadb", database=None)
        with pytest.raises(ValueError, match="no database configuration"):
            build_adapter(config)

    def test_postgres_builds_a_postgres_adapter(self) -> None:
        from hr_config import DatabaseConfig
        from hr_postgres import PostgresAdapter
        from hr_web import build_adapter

        config = self._config(
            db_type="postgres",
            database=DatabaseConfig(
                host="localhost", port=5432, name="homeassistant", user="ha", password="pw"
            ),
        )
        assert isinstance(build_adapter(config), PostgresAdapter)

    def test_mariadb_builds_a_mariadb_adapter(self) -> None:
        from hr_config import DatabaseConfig
        from hr_mariadb import MariaDBAdapter
        from hr_web import build_adapter

        config = self._config(
            db_type="mariadb",
            database=DatabaseConfig(
                host="localhost", port=3306, name="homeassistant", user="ha", password="pw"
            ),
        )
        assert isinstance(build_adapter(config), MariaDBAdapter)
