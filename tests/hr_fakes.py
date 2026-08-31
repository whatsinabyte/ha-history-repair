"""In-memory stand-in for the recorder database.

Deliberately reimplements MariaDBAdapter's *rules* — audit before update,
optimistic concurrency, refusing to restore a row someone else changed —
rather than its SQL, so tests of the service and web layers exercise real
behaviour without a database. Any rule added to MariaDBAdapter belongs here
too, or the two drift apart silently.
"""

from __future__ import annotations

import time
from dataclasses import replace

from hr_db import MAX_BULK_ROWS, ConcurrentModification, DatabaseAdapter, InvalidRange, NotFound
from hr_models import (
    BulkCorrectionResult,
    BulkPreview,
    Correction,
    Entity,
    HealthReport,
    Quality,
    SensorType,
    StatePoint,
    StateSeries,
    StatisticsMetadata,
    StatisticsRow,
)
from hr_statistics import Reading


class FakeAdapter(DatabaseAdapter):
    """An in-memory recorder that mirrors MariaDBAdapter's observable contract.

    Deliberately reimplements the *rules* (audit before update, optimistic
    concurrency, refusing to restore a row someone else changed) rather than
    the SQL, so tests of the service and web layers exercise real behaviour
    without a database. Any rule added to MariaDBAdapter belongs here too.
    """

    def __init__(self) -> None:
        self.entities: dict[str, Entity] = {}
        # state_id -> (entity_id, ts, value)
        self.states: dict[int, tuple[str, float, str | None]] = {}
        self.corrections: dict[int, Correction] = {}
        self.statistics_metadata: dict[str, StatisticsMetadata] = {}
        self.short_term_rows: list[StatisticsRow] = []
        self.hourly_rows: list[StatisticsRow] = []
        self.audit_table_created = False
        self.health = HealthReport(
            connected=True,
            schema_version=48,
            server_version="10.11.6-MariaDB",
            can_update=True,
            audit_table_ready=True,
        )
        self._next_correction_id = 1

    # -- helpers used by tests --------------------------------------------

    def add_entity(
        self,
        entity_id: str,
        sensor_type: SensorType = SensorType.MEASUREMENT,
        unit: str | None = "°C",
    ) -> Entity:
        entity = Entity(
            metadata_id=len(self.entities) + 1,
            entity_id=entity_id,
            friendly_name=None,
            sensor_type=sensor_type,
            unit=unit,
            last_value=None,
            last_updated_ts=None,
        )
        self.entities[entity_id] = entity
        return entity

    def add_state(self, entity_id: str, state_id: int, ts: float, value: str) -> None:
        self.states[state_id] = (entity_id, ts, value)

    # -- lifecycle ---------------------------------------------------------

    def check_health(self) -> HealthReport:
        return self.health

    def ensure_audit_table(self) -> None:
        self.audit_table_created = True

    def close(self) -> None:
        return None

    # -- reads -------------------------------------------------------------

    def _latest_state(self, entity_id: str) -> tuple[str | None, float | None]:
        rows = [(ts, value) for owner, ts, value in self.states.values() if owner == entity_id]
        if not rows:
            return None, None
        ts, value = max(rows, key=lambda r: r[0])
        return value, ts

    def _correction_count(self, entity_id: str) -> int:
        return len(
            [
                c
                for c in self.corrections.values()
                if c.entity_id == entity_id and c.restored_at is None
            ]
        )

    def _resolved_entity(self, entity: Entity) -> Entity:
        """The stored Entity, with the fields that change over time refreshed.

        add_entity only sets the fields known at creation; last_value,
        last_updated_ts, and correction_count are recomputed from `states` and
        `corrections` on every read here, the same way a real adapter's own
        query always reflects the current data rather than a cached snapshot.
        """
        value, ts = self._latest_state(entity.entity_id)
        return replace(
            entity,
            last_value=value,
            last_updated_ts=ts,
            correction_count=self._correction_count(entity.entity_id),
        )

    def _matching(self, search: str | None, sensor_type: SensorType | None) -> list[Entity]:
        values = [self._resolved_entity(e) for e in self.entities.values()]
        if search:
            values = [e for e in values if search.lower() in e.entity_id.lower()]
        if sensor_type is not None:
            values = [e for e in values if e.sensor_type is sensor_type]
        return values

    @staticmethod
    def _sorted(values: list[Entity], sort: str, sort_dir: str) -> list[Entity]:
        reverse = sort_dir == "desc"
        if sort == "last_updated":
            # NULLS-last regardless of direction, matching the real adapters:
            # an entity with no states at all should not jump to the top of a
            # "most recently updated" sort.
            with_ts = [e for e in values if e.last_updated_ts is not None]
            without_ts = [e for e in values if e.last_updated_ts is None]
            with_ts.sort(key=lambda e: e.last_updated_ts, reverse=reverse)  # type: ignore[arg-type,return-value]
            return with_ts + without_ts
        if sort == "corrections":
            return sorted(values, key=lambda e: e.correction_count, reverse=reverse)
        return sorted(values, key=lambda e: e.entity_id, reverse=reverse)

    def list_entities(
        self,
        search: str | None = None,
        sensor_type: SensorType | None = None,
        sort: str = "entity_id",
        sort_dir: str = "asc",
        limit: int = 100,
        offset: int = 0,
    ) -> list[Entity]:
        values = self._sorted(self._matching(search, sensor_type), sort, sort_dir)
        return values[offset : offset + limit]

    def count_entities(
        self, search: str | None = None, sensor_type: SensorType | None = None
    ) -> int:
        return len(self._matching(search, sensor_type))

    def get_entity(self, entity_id: str) -> Entity:
        try:
            return self._resolved_entity(self.entities[entity_id])
        except KeyError:
            raise NotFound(f"Unknown entity: {entity_id}") from None

    def fetch_states(
        self, entity_id: str, start_ts: float, end_ts: float, limit: int = 20000
    ) -> StateSeries:
        active = {
            c.state_id: c
            for c in self.corrections.values()
            if c.entity_id == entity_id and c.restored_at is None
        }
        rows = [
            (state_id, ts, value)
            for state_id, (owner, ts, value) in self.states.items()
            if owner == entity_id and start_ts <= ts < end_ts
        ]
        ordered = sorted(rows, key=lambda r: r[1])
        truncated = len(ordered) > limit
        points: list[StatePoint] = []
        for state_id, ts, value in ordered[:limit]:
            correction = active.get(state_id)
            try:
                numeric = float(value) if value is not None else None
            except ValueError:
                numeric = None
            points.append(
                StatePoint(
                    state_id=state_id,
                    ts=ts,
                    value=value,
                    numeric_value=numeric,
                    correction_id=correction.id if correction else None,
                    original_value=correction.original_value if correction else None,
                )
            )
        return StateSeries(points=points, truncated=truncated, limit=limit)

    def list_corrections(
        self,
        entity_id: str | None = None,
        include_restored: bool = True,
        limit: int = 500,
    ) -> list[Correction]:
        values = sorted(self.corrections.values(), key=lambda c: c.id, reverse=True)
        if entity_id:
            values = [c for c in values if c.entity_id == entity_id]
        if not include_restored:
            values = [c for c in values if c.restored_at is None]
        return values[:limit]

    def get_correction(self, correction_id: int) -> Correction:
        try:
            return self.corrections[correction_id]
        except KeyError:
            raise NotFound(f"No correction with id {correction_id}") from None

    # -- statistics --------------------------------------------------------

    def add_statistics_metadata(
        self, entity_id: str, mean_type: int = 1, has_sum: bool = False
    ) -> StatisticsMetadata:
        meta = StatisticsMetadata(
            id=len(self.statistics_metadata) + 1,
            statistic_id=entity_id,
            mean_type=mean_type,
            has_sum=has_sum,
            unit_of_measurement="°C",
        )
        self.statistics_metadata[entity_id] = meta
        return meta

    def get_statistics_metadata(self, entity_id: str) -> StatisticsMetadata | None:
        return self.statistics_metadata.get(entity_id)

    def add_hourly_statistics(
        self,
        entity_id: str,
        start_ts: float,
        mean: float | None = None,
        state: float | None = None,
    ) -> None:
        """Seed one hourly statistics row, independent of any states rows.

        For testing the graph's backfill from long-term statistics
        (hr_statistics.backfill_from_statistics), which needs statistics that
        exist further back than any seeded states row — the situation a real
        installation is in once the recorder purges old states.
        """
        meta = self.statistics_metadata[entity_id]
        self.hourly_rows.append(
            StatisticsRow(
                id=len(self.hourly_rows) + 1,
                metadata_id=meta.id,
                start_ts=start_ts,
                mean=mean,
                state=state,
            )
        )

    def fetch_readings(self, entity_id: str, start_ts: float, end_ts: float) -> list[Reading]:
        numeric: list[tuple[float, float]] = []
        for owner, ts, value in self.states.values():
            if owner != entity_id or value is None:
                continue
            try:
                numeric.append((float(value), ts))
            except ValueError:
                continue
        numeric.sort(key=lambda item: item[1])

        # The last reading before the window, then everything inside it —
        # matching what the recorder considers for a bucket.
        before = [item for item in numeric if item[1] < start_ts]
        inside = [item for item in numeric if start_ts <= item[1] < end_ts]
        selected = (before[-1:] if before else []) + inside
        return [Reading(value=value, ts=ts) for value, ts in selected]

    def counter_cascade_scope(self, entity_id: str, state_ts: float) -> dict[str, int]:
        meta = self.statistics_metadata.get(entity_id)
        if meta is None:
            return {"short_term": 0, "hourly": 0}
        return {
            "short_term": len([r for r in self.short_term_rows if r.metadata_id == meta.id]),
            "hourly": len([r for r in self.hourly_rows if r.metadata_id == meta.id]),
        }

    def fetch_statistics(
        self,
        metadata_id: int,
        start_ts: float,
        end_ts: float,
        short_term: bool = False,
    ) -> list[StatisticsRow]:
        rows = self.short_term_rows if short_term else self.hourly_rows
        return sorted(
            (
                row
                for row in rows
                if row.metadata_id == metadata_id and start_ts <= row.start_ts < end_ts
            ),
            key=lambda row: row.start_ts,
        )

    # -- writes ------------------------------------------------------------

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
        if state_id not in self.states:
            raise NotFound(f"No states row with id {state_id}")
        owner, ts, current = self.states[state_id]
        if owner != entity_id:
            raise NotFound(f"States row {state_id} belongs to {owner}, not {entity_id}")
        if expected_original is not None and current != expected_original:
            raise ConcurrentModification(
                f"This value changed since you loaded it: it now reads '{current}', "
                f"not '{expected_original}'."
            )

        correction = Correction(
            id=self._next_correction_id,
            entity_id=entity_id,
            sensor_type=self.entities[entity_id].sensor_type,
            state_id=state_id,
            state_ts=ts,
            original_value=current,
            corrected_value=new_value,
            quality=quality,
            note=note,
            created_at="2026-01-01T00:00:00",
            created_by=created_by,
            restored_at=None,
            restored_by=None,
            stats_corrected=recompute_statistics and entity_id in self.statistics_metadata,
        )
        self._next_correction_id += 1
        self.corrections[correction.id] = correction
        self.states[state_id] = (owner, ts, new_value)
        return correction

    def restore_correction(self, correction_id: int, restored_by: str) -> Correction:
        correction = self.get_correction(correction_id)
        if correction.restored_at is not None:
            raise ConcurrentModification("This correction has already been restored.")
        if correction.state_id is None or correction.state_id not in self.states:
            raise NotFound("The corrected states row no longer exists.")
        owner, ts, current = self.states[correction.state_id]
        if current != correction.corrected_value:
            raise ConcurrentModification(
                f"This row now holds '{current}', not the corrected value "
                f"'{correction.corrected_value}'."
            )
        self.states[correction.state_id] = (owner, ts, correction.original_value)
        restored = Correction(
            **{
                **correction.__dict__,
                "restored_at": "2026-01-02T00:00:00",
                "restored_by": restored_by,
            }
        )
        self.corrections[correction_id] = restored
        return restored

    def find_orphaned_corrections(self) -> list[Correction]:
        orphaned = []
        for correction in self.corrections.values():
            if correction.restored_at is not None or correction.dismissed_at is not None:
                continue
            if correction.state_id is None:
                continue
            current = self.states.get(correction.state_id)
            if current is None or current[2] != correction.corrected_value:
                orphaned.append(correction)
        return sorted(orphaned, key=lambda c: c.id, reverse=True)

    def dismiss_correction(self, correction_id: int, dismissed_by: str) -> Correction:
        correction = self.get_correction(correction_id)
        if correction.restored_at is not None:
            raise ConcurrentModification(
                "This correction has already been restored and cannot be dismissed."
            )
        if correction.dismissed_at is not None:
            raise ConcurrentModification("This correction has already been dismissed.")
        dismissed = Correction(
            **{
                **correction.__dict__,
                "dismissed_at": "2026-01-02T00:00:00",
                "dismissed_by": dismissed_by,
            }
        )
        self.corrections[correction_id] = dismissed
        return dismissed

    # -- bulk correction -----------------------------------------------------

    def _bulk_targets(
        self, entity_id: str, start_ts: float, end_ts: float
    ) -> list[tuple[int, float, str | None, bool]]:
        """(state_id, ts, current_value, already_corrected), oldest first."""
        active_state_ids = {
            c.state_id
            for c in self.corrections.values()
            if c.restored_at is None
            and c.entity_id == entity_id
            and c.state_id is not None
            and start_ts <= c.state_ts < end_ts
        }
        rows = [
            (state_id, ts, value, state_id in active_state_ids)
            for state_id, (owner, ts, value) in self.states.items()
            if owner == entity_id and start_ts <= ts < end_ts
        ]
        rows.sort(key=lambda r: r[1])
        return rows

    def _interpolation_anchors(
        self, entity_id: str, start_ts: float, end_ts: float
    ) -> tuple[Reading | None, Reading | None]:
        before: Reading | None = None
        after: Reading | None = None
        for owner, ts, value in self.states.values():
            if owner != entity_id or value is None:
                continue
            try:
                numeric = float(value)
            except ValueError:
                continue
            if ts < start_ts and (before is None or ts > before.ts):
                before = Reading(value=numeric, ts=ts)
            elif ts >= end_ts and (after is None or ts < after.ts):
                after = Reading(value=numeric, ts=ts)
        return before, after

    def bulk_correction_preview(
        self, entity_id: str, start_ts: float, end_ts: float
    ) -> BulkPreview:
        rows = self._bulk_targets(entity_id, start_ts, end_ts)
        already_corrected = sum(1 for r in rows if r[3])
        before, after = self._interpolation_anchors(entity_id, start_ts, end_ts)
        return BulkPreview(
            total=len(rows),
            correctable=len(rows) - already_corrected,
            already_corrected=already_corrected,
            can_interpolate=before is not None and after is not None,
        )

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
        rows = self._bulk_targets(entity_id, start_ts, end_ts)
        targets = [r for r in rows if not r[3]]

        if len(targets) > MAX_BULK_ROWS:
            raise InvalidRange(
                f"This range holds {len(targets)} correctable rows, more than the "
                f"{MAX_BULK_ROWS}-row limit for a single bulk correction."
            )

        if strategy == "interpolate":
            before, after = self._interpolation_anchors(entity_id, start_ts, end_ts)
            if before is None or after is None:
                raise InvalidRange(
                    "Cannot interpolate: this range needs a numeric reading on both "
                    "sides of it, and at least one side has none."
                )
            anchor_before, anchor_after = before, after

            def value_for(ts: float) -> str:
                span = anchor_after.ts - anchor_before.ts
                fraction = (ts - anchor_before.ts) / span if span else 0.0
                return repr(
                    anchor_before.value + (anchor_after.value - anchor_before.value) * fraction
                )
        elif value is not None:
            constant_value = value

            def value_for(ts: float) -> str:
                return constant_value
        else:
            raise InvalidRange("A value is required for a constant bulk correction.")

        correction_ids = []
        for state_id, ts, _current, _already in targets:
            correction = self.apply_correction(
                entity_id=entity_id,
                state_id=state_id,
                expected_original=None,
                new_value=value_for(ts),
                quality=quality,
                note=note,
                created_by=created_by,
                recompute_statistics=entity_id in self.statistics_metadata,
            )
            correction_ids.append(correction.id)

        return BulkCorrectionResult(
            entity_id=entity_id,
            correction_ids=correction_ids,
            applied=len(correction_ids),
            skipped=len(rows) - len(targets),
        )


# Seeded readings sit shortly before "now" rather than at a fixed epoch. The
# graph defaults to the last 7 days, so fixed 2023 timestamps produced an
# empty chart in the browser tests — the fixture, not the product, was wrong.
_NOW = time.time()
SEED_TIMESTAMPS = (_NOW - 1800.0, _NOW - 1500.0, _NOW - 1200.0)
SEED_WINDOW = (_NOW - 3600.0, _NOW + 60.0)
