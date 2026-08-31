"""Flask application: Ingress plumbing, pages, and the JSON API."""

from __future__ import annotations

import csv
import io
import logging
import time
from collections.abc import Callable, Iterable
from dataclasses import asdict, is_dataclass
from datetime import datetime, timezone
from enum import Enum
from typing import Any

from flask import Flask, jsonify, redirect, render_template, request, url_for
from werkzeug.wrappers import Response

import hr_corrections
from hr_config import AppConfig
from hr_corrections import CORRECTABLE_SENSOR_TYPES, ValidationError
from hr_db import (
    ENTITY_SORT_DIRECTIONS,
    ENTITY_SORT_KEYS,
    AdapterError,
    ConcurrentModification,
    ConnectionFailed,
    DatabaseAdapter,
    InvalidRange,
    LockTimeout,
    NotFound,
)
from hr_ha_api import HomeAssistantClient, client_from_env
from hr_mariadb import MariaDBAdapter
from hr_models import SensorType
from hr_outliers import DEFAULT_THRESHOLD, detect_counter_outliers, detect_measurement_outliers
from hr_postgres import PostgresAdapter
from hr_sqlite import SQLiteAdapter
from hr_state import AddonState
from hr_statistics import Reading, backfill_from_statistics

_LOGGER = logging.getLogger(__name__)

DEFAULT_RANGE_SECONDS = 24 * 3600
MAX_PAGE_SIZE = 500


class IngressMiddleware:
    """Honour the base path Supervisor's Ingress proxy serves the add-on under.

    Without this, every URL Flask generates would be absolute from the domain
    root and would miss the /api/hassio_ingress/<token>/ prefix.
    """

    def __init__(self, wsgi_app: Callable[..., Any]) -> None:
        self._app = wsgi_app

    def __call__(self, environ: dict[str, Any], start_response: Any) -> Iterable[bytes]:
        ingress_path = environ.get("HTTP_X_INGRESS_PATH")
        if ingress_path:
            environ["SCRIPT_NAME"] = ingress_path
            path = environ.get("PATH_INFO", "")
            if path.startswith(ingress_path):
                environ["PATH_INFO"] = path[len(ingress_path) :] or "/"
        result: Iterable[bytes] = self._app(environ, start_response)
        return result


def serialise(value: Any) -> Any:
    """Turn dataclasses and enums into something jsonify can handle."""
    if is_dataclass(value) and not isinstance(value, type):
        return {k: serialise(v) for k, v in asdict(value).items()}
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, (list, tuple)):
        return [serialise(v) for v in value]
    if isinstance(value, dict):
        return {k: serialise(v) for k, v in value.items()}
    return value


def _csv_timestamp(value: str | float | None) -> str:
    """One consistent, human-readable timestamp for every CSV column.

    Before this, the exported audit trail mixed three shapes on the same
    row: `state_ts` as a raw epoch float (no date at all — "1756472313.7"),
    and `created_at`/`restored_at`/`dismissed_at` as ISO strings with
    microseconds ("2026-08-29T13:06:09.453177"). Neither matches the
    friendly on-screen date the UI shows for the same correction, and the
    epoch column has no date a person can read at a glance. `state_ts` is a
    float epoch (see hr_models.py); the other three are already ISO strings
    from `.isoformat()`. Not locale- or time_format-dependent, unlike the
    on-screen display: a CSV may be opened by anyone, so an unambiguous
    "YYYY-MM-DD HH:MM:SS" that also sorts correctly as plain text is safer
    here than reproducing the viewer's own 12/24-hour preference.
    """
    if value is None or value == "":
        return ""
    if isinstance(value, (int, float)):
        dt = datetime.fromtimestamp(value, tz=timezone.utc)
    else:
        dt = datetime.fromisoformat(value)
    return dt.strftime("%Y-%m-%d %H:%M:%S")


def current_user() -> str:
    """The Home Assistant user behind the Ingress request.

    Supervisor sets these headers for authenticated Ingress traffic; the
    fallback covers direct access during development.
    """
    return (
        request.headers.get("X-Remote-User-Display-Name")
        or request.headers.get("X-Remote-User-Name")
        or "unknown"
    )


def build_adapter(config: AppConfig) -> DatabaseAdapter:
    """The concrete adapter for whichever recorder database this add-on faces.

    These are the three backends Home Assistant's recorder itself supports.
    SQLite is its default; MariaDB is what most of this project was built and
    verified against first; PostgreSQL is the third. All satisfy the same
    DatabaseAdapter contract, so nothing above this line needs to know which
    one is running.
    """
    if config.db_type == "sqlite":
        if config.sqlite is None:
            raise ValueError("db_type is 'sqlite' but no sqlite configuration was provided.")
        return SQLiteAdapter(config.sqlite)
    if config.database is None:
        raise ValueError(
            f"db_type is '{config.db_type}' but no database configuration was provided."
        )
    if config.db_type == "postgres":
        return PostgresAdapter(config.database)
    return MariaDBAdapter(config.database)


def create_app(
    config: AppConfig | None = None,
    adapter: DatabaseAdapter | None = None,
    ha_client: HomeAssistantClient | None = None,
) -> Flask:
    """Build the Flask app. Tests inject a fake adapter instead of a real one."""
    config = config or AppConfig.from_env()

    app = Flask(__name__)
    app.wsgi_app = IngressMiddleware(app.wsgi_app)  # type: ignore[method-assign]
    app.config["HR_CONFIG"] = config

    db: DatabaseAdapter = adapter or build_adapter(config)
    # Absent outside an add-on, and absent when API access was not granted.
    # Its absence costs a restart after a correction, never correctness.
    home_assistant = ha_client if ha_client is not None else client_from_env()
    state = AddonState(config.state_dir)
    app.extensions["hr_db"] = db
    app.extensions["hr_state"] = state

    @app.context_processor
    def _inject_time_format() -> dict[str, str]:
        return {"time_format": config.time_format}

    # -- error handling ----------------------------------------------------

    @app.errorhandler(ValidationError)
    def _on_validation_error(err: ValidationError) -> tuple[Response, int]:
        return jsonify(error=str(err), kind="validation"), 400

    @app.errorhandler(ConcurrentModification)
    def _on_conflict(err: ConcurrentModification) -> tuple[Response, int]:
        return jsonify(error=str(err), kind="conflict"), 409

    @app.errorhandler(LockTimeout)
    def _on_lock_timeout(err: LockTimeout) -> tuple[Response, int]:
        return jsonify(error=str(err), kind="lock_timeout"), 503

    @app.errorhandler(NotFound)
    def _on_not_found(err: NotFound) -> tuple[Response, int]:
        return jsonify(error=str(err), kind="not_found"), 404

    @app.errorhandler(InvalidRange)
    def _on_invalid_range(err: InvalidRange) -> tuple[Response, int]:
        return jsonify(error=str(err), kind="invalid_range"), 400

    @app.errorhandler(ConnectionFailed)
    def _on_connection_failed(err: ConnectionFailed) -> tuple[Response, int]:
        return jsonify(error=str(err), kind="connection"), 503

    @app.errorhandler(AdapterError)
    def _on_adapter_error(err: AdapterError) -> tuple[Response, int]:
        _LOGGER.exception("Adapter error")
        return jsonify(error=str(err), kind="database"), 500

    # -- pages -------------------------------------------------------------

    @app.route("/")
    def index() -> Any:
        if not state.onboarding_complete:
            return redirect(url_for("onboarding"))
        return render_template("entities.html", active="entities")

    @app.route("/onboarding")
    def onboarding() -> Any:
        return render_template("onboarding.html", active="onboarding", state=state.as_dict())

    @app.route("/entity/<path:entity_id>")
    def entity_page(entity_id: str) -> Any:
        if not state.onboarding_complete:
            return redirect(url_for("onboarding"))
        entity = db.get_entity(entity_id)
        return render_template(
            "entity.html",
            active="entities",
            entity=serialise(entity),
            correctable=entity.sensor_type in CORRECTABLE_SENSOR_TYPES,
            is_counter=entity.sensor_type is SensorType.COUNTER,
        )

    @app.route("/audit")
    def audit_page() -> Any:
        if not state.onboarding_complete:
            return redirect(url_for("onboarding"))
        return render_template("audit.html", active="audit")

    # -- API ---------------------------------------------------------------

    @app.get("/api/health")
    def api_health() -> Response:
        report = db.check_health()
        payload = serialise(report)
        payload["ok"] = report.ok
        payload["onboarding_complete"] = state.onboarding_complete
        return jsonify(payload)

    @app.post("/api/onboarding")
    def api_onboarding() -> Any:
        body = request.get_json(silent=True) or {}
        if not body.get("backup_acknowledged"):
            raise ValidationError("Please confirm that you have a backup before continuing.")
        report = db.check_health()
        if not report.ok:
            return (
                jsonify(
                    error="The database checks did not pass.",
                    kind="health",
                    health=serialise(report),
                ),
                400,
            )
        db.ensure_audit_table()
        state.update(
            onboarding_complete=True,
            backup_acknowledged_at=time.time(),
            backup_acknowledged_by=current_user(),
        )
        return jsonify(ok=True)

    @app.get("/api/entities")
    def api_entities() -> Response:
        search = request.args.get("search") or None
        limit = min(request.args.get("limit", default=100, type=int), MAX_PAGE_SIZE)
        offset = max(request.args.get("offset", default=0, type=int), 0)

        # A display ordering and a browse filter, not correctness-critical
        # input — an unrecognised value falls back to the default rather than
        # failing the request.
        sensor_type: SensorType | None = None
        raw_type = request.args.get("type")
        if raw_type:
            try:
                sensor_type = SensorType(raw_type)
            except ValueError:
                sensor_type = None
        sort = request.args.get("sort", "entity_id")
        if sort not in ENTITY_SORT_KEYS:
            sort = "entity_id"
        sort_dir = request.args.get("dir", "asc")
        if sort_dir not in ENTITY_SORT_DIRECTIONS:
            sort_dir = "asc"

        entities = db.list_entities(
            search=search,
            sensor_type=sensor_type,
            sort=sort,
            sort_dir=sort_dir,
            limit=limit,
            offset=offset,
        )
        return jsonify(
            entities=serialise(entities),
            total=db.count_entities(search=search, sensor_type=sensor_type),
            limit=limit,
            offset=offset,
            sort=sort,
            dir=sort_dir,
        )

    @app.get("/api/entities/<path:entity_id>/states")
    def api_states(entity_id: str) -> Response:
        end_ts = request.args.get("end", default=time.time(), type=float)
        start_ts = request.args.get("start", default=end_ts - DEFAULT_RANGE_SECONDS, type=float)
        if start_ts >= end_ts:
            raise ValidationError("The start of the range must come before its end.")
        threshold = request.args.get("threshold", default=DEFAULT_THRESHOLD, type=float)

        entity = db.get_entity(entity_id)
        series = db.fetch_states(entity_id, start_ts, end_ts)
        points = series.points

        # The requested range reaches further back than raw states survive
        # (the recorder purges them; long-term statistics never are — see
        # long-term-statistics-graph-design.md). Backfilled points are never
        # candidates for outlier detection below: there is nothing to correct
        # on an hourly average whose source row is already gone, so flagging
        # one would invite a click that goes nowhere.
        earliest_raw_ts = points[0].ts if points else end_ts
        if earliest_raw_ts > start_ts:
            metadata = db.get_statistics_metadata(entity_id)
            if metadata is not None:
                statistics_rows = db.fetch_statistics(metadata.id, start_ts, earliest_raw_ts)
                points = backfill_from_statistics(
                    points, statistics_rows, is_counter=entity.sensor_type is SensorType.COUNTER
                )

        readings = [
            Reading(value=p.numeric_value, ts=p.ts)
            for p in points
            if p.numeric_value is not None and p.source == "state"
        ]
        if entity.sensor_type is SensorType.COUNTER:
            candidates = detect_counter_outliers(readings, threshold=threshold)
        elif entity.sensor_type is SensorType.MEASUREMENT:
            candidates = detect_measurement_outliers(readings, threshold=threshold)
        else:
            candidates = []
        candidate_ts = {c.ts for c in candidates}

        serialised_points = []
        for point, serialised in zip(points, serialise(points), strict=True):
            serialised["is_candidate"] = point.ts in candidate_ts
            serialised_points.append(serialised)

        return jsonify(
            entity=serialise(entity),
            start=start_ts,
            end=end_ts,
            points=serialised_points,
            # The browser needs to know it is looking at part of the range, so
            # an absent outlier cannot be mistaken for a clean sensor.
            truncated=series.truncated,
            limit=series.limit,
            threshold=threshold,
        )

    @app.get("/api/entities/<path:entity_id>/cascade-scope")
    def api_cascade_scope(entity_id: str) -> Response:
        """How many statistics rows correcting at this moment would rewrite.

        Shown before a counter correction is committed. The hourly table is
        never purged, so an old energy reading can carry its error through
        years of running totals, and the user is entitled to know that first.
        """
        state_ts = request.args.get("state_ts", type=float)
        if state_ts is None:
            raise ValidationError("state_ts is required.")
        return jsonify(scope=db.counter_cascade_scope(entity_id, state_ts))

    @app.get("/api/corrections")
    def api_list_corrections() -> Response:
        entity_id = request.args.get("entity_id") or None
        include_restored = request.args.get("include_restored", "1") != "0"
        return jsonify(
            corrections=serialise(
                db.list_corrections(entity_id=entity_id, include_restored=include_restored)
            )
        )

    @app.post("/api/corrections")
    def api_create_correction() -> tuple[Response, int]:
        if not state.onboarding_complete:
            raise ValidationError("Please complete onboarding first.")
        body = request.get_json(silent=True) or {}
        for field in ("entity_id", "state_id", "new_value"):
            if body.get(field) in (None, ""):
                raise ValidationError(f"Missing required field: {field}")

        correction = hr_corrections.apply(
            db,
            entity_id=body["entity_id"],
            state_id=body["state_id"],
            expected_original=body.get("expected_original"),
            new_value=body["new_value"],
            quality=body.get("quality"),
            note=body.get("note"),
            created_by=current_user(),
        )
        # The database is already correct. This only refreshes Home
        # Assistant's in-memory statistics, and its failure is not the
        # correction's failure — the UI turns it into "restart to see the
        # change on statistics cards".
        cache_refreshed = hr_corrections.refresh_statistics_cache(db, home_assistant, correction)
        return (
            jsonify(
                correction=serialise(correction),
                statistics_cache_refreshed=cache_refreshed,
            ),
            201,
        )

    @app.post("/api/corrections/<int:correction_id>/restore")
    def api_restore_correction(correction_id: int) -> Response:
        correction = db.restore_correction(correction_id, current_user())
        return jsonify(correction=serialise(correction))

    @app.get("/api/corrections/orphaned")
    def api_orphaned_corrections() -> Response:
        """Active corrections a backup restore has silently invalidated.

        A restore reverts states without touching this add-on's audit table
        (design document, 9.11), so a correction can claim to be active while
        no longer describing what is actually stored. Checked on every load of
        the corrections page rather than only at startup, since a restore can
        happen at any time the add-on is running.
        """
        return jsonify(corrections=serialise(db.find_orphaned_corrections()))

    @app.post("/api/corrections/<int:correction_id>/dismiss")
    def api_dismiss_correction(correction_id: int) -> Response:
        correction = db.dismiss_correction(correction_id, current_user())
        return jsonify(correction=serialise(correction))

    @app.get("/api/corrections/export.csv")
    def api_export_corrections_csv() -> Response:
        """The audit trail as CSV, for record-keeping outside this add-on.

        Takes the same filters as GET /api/corrections, so exporting respects
        whatever the user was already looking at (e.g. one entity, or active
        corrections only) rather than always dumping everything.
        """
        entity_id = request.args.get("entity_id") or None
        include_restored = request.args.get("include_restored", "1") != "0"
        corrections = db.list_corrections(entity_id=entity_id, include_restored=include_restored)

        buffer = io.StringIO()
        writer = csv.writer(buffer)
        writer.writerow(
            [
                "id",
                "entity_id",
                "sensor_type",
                "state_id",
                "recorded_at",
                "original_value",
                "corrected_value",
                "quality",
                "note",
                "created_at",
                "created_by",
                "restored_at",
                "restored_by",
                "dismissed_at",
                "dismissed_by",
                "stats_corrected",
            ]
        )
        for c in corrections:
            writer.writerow(
                [
                    c.id,
                    c.entity_id,
                    c.sensor_type.value,
                    c.state_id,
                    _csv_timestamp(c.state_ts),
                    c.original_value,
                    c.corrected_value,
                    c.quality.value,
                    c.note,
                    _csv_timestamp(c.created_at),
                    c.created_by,
                    _csv_timestamp(c.restored_at),
                    c.restored_by,
                    _csv_timestamp(c.dismissed_at),
                    c.dismissed_by,
                    c.stats_corrected,
                ]
            )

        filename = f"history-repair-audit-{time.strftime('%Y%m%d-%H%M%S')}.csv"
        return Response(
            buffer.getvalue(),
            mimetype="text/csv",
            headers={"Content-Disposition": f'attachment; filename="{filename}"'},
        )

    @app.get("/api/entities/<path:entity_id>/bulk-preview")
    def api_bulk_preview(entity_id: str) -> Response:
        """How many rows a drag-selected range would touch, before saving.

        Read-only: nothing here is written, so the browser can call it freely
        while the user is still adjusting their selection.
        """
        start_ts = request.args.get("start", type=float)
        end_ts = request.args.get("end", type=float)
        if start_ts is None or end_ts is None:
            raise ValidationError("start and end are required.")
        if start_ts >= end_ts:
            raise ValidationError("The start of the range must come before its end.")
        preview = db.bulk_correction_preview(entity_id, start_ts, end_ts)
        return jsonify(preview=serialise(preview))

    @app.post("/api/entities/<path:entity_id>/bulk-correction")
    def api_bulk_correction(entity_id: str) -> tuple[Response, int]:
        if not state.onboarding_complete:
            raise ValidationError("Please complete onboarding first.")
        body = request.get_json(silent=True) or {}
        for field in ("start", "end", "strategy"):
            if body.get(field) in (None, ""):
                raise ValidationError(f"Missing required field: {field}")

        result = hr_corrections.apply_bulk(
            db,
            entity_id=entity_id,
            start_ts=float(body["start"]),
            end_ts=float(body["end"]),
            strategy=body["strategy"],
            value=body.get("value"),
            quality=body.get("quality"),
            note=body.get("note"),
            created_by=current_user(),
        )
        cache_refreshed_hours = hr_corrections.refresh_statistics_cache_bulk(
            db, home_assistant, result
        )
        return (
            jsonify(
                result=serialise(result),
                statistics_cache_refreshed_hours=cache_refreshed_hours,
            ),
            201,
        )

    return app
