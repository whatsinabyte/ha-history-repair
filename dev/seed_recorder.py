"""Fill the development recorder database with realistic sensor history.

The point is not volume but variety: every shape the add-on has to cope with
appears here, including the ones that are easy to get wrong.

  sensor.living_room_temperature  measurement, with three injected outliers
  sensor.outdoor_humidity         measurement, clean
  sensor.energy_total             counter, with a spike that offsets its sum
  sensor.flaky_pressure           measurement, peppered with unknown/unavailable
  binary_sensor.front_door        no statistics at all, so sensor type UNKNOWN

Run with the project venv, which has PyMySQL:

    .venv-check/bin/python dev/seed_recorder.py

Or, for a SQLite target (schema must already exist — see dev/build_schema.py):

    .venv-check/bin/python dev/seed_recorder.py --sqlite-path .devdb/sqlite/test.db

Or PostgreSQL (schema must already exist likewise):

    .venv-check/bin/python dev/seed_recorder.py \\
      --postgres-dsn postgresql://hatest:hatest@127.0.0.1:5442/ha_test

Existing recorder rows are cleared first, so repeated runs are idempotent. The
add-on's own state_corrections table is left alone.
"""

from __future__ import annotations

import argparse
import json
import math
import pathlib
import random
import re
import sqlite3
import sys
import time
from dataclasses import dataclass
from typing import Any

import pymysql
from pymysql.cursors import DictCursor

_PLACEHOLDER = re.compile(r"%s")


class _SQLiteCursor:
    """Just enough of the PyMySQL cursor surface for seed()/_build_statistics().

    Translates the `%s` placeholders those functions already use into SQLite's
    `?`, and DictCursor-style row access into sqlite3.Row, so the seeding logic
    itself never has to know which dialect it is talking to.
    """

    def __init__(self, conn: sqlite3.Connection) -> None:
        self._conn = conn
        self._cur = conn.cursor()
        self.lastrowid: int | None = None

    def execute(self, sql: str, params: tuple[Any, ...] = ()) -> None:
        self._cur.execute(_PLACEHOLDER.sub("?", sql), params)
        self.lastrowid = self._cur.lastrowid

    def executemany(self, sql: str, seq_of_params: list[tuple[Any, ...]]) -> None:
        self._cur.executemany(_PLACEHOLDER.sub("?", sql), seq_of_params)

    def fetchall(self) -> list[sqlite3.Row]:
        return self._cur.fetchall()


# Primary key each seeded INSERT needs handed back. PostgreSQL has no
# lastrowid at all — the value comes from a RETURNING clause instead, and
# which column to return depends on the table being inserted into.
_PG_RETURNING_PK = {
    "state_attributes": "attributes_id",
    "states_meta": "metadata_id",
    "statistics_meta": "id",
}
_PG_INSERT_TABLE = re.compile(r"INSERT\s+INTO\s+(\w+)", re.IGNORECASE)


class _PostgresCursor:
    """Just enough of the PyMySQL cursor surface for seed()/_build_statistics().

    psycopg already speaks `%s`, so unlike _SQLiteCursor there is nothing to
    translate there. What it does not have is `lastrowid`: this appends a
    RETURNING clause to the three INSERTs that need their new id, and exposes
    the result under the name the seeding logic already uses.
    """

    def __init__(self, conn: Any) -> None:
        self._cur = conn.cursor()
        self.lastrowid: int | None = None

    def execute(self, sql: str, params: tuple[Any, ...] = ()) -> None:
        self.lastrowid = None
        match = _PG_INSERT_TABLE.search(sql)
        pk = _PG_RETURNING_PK.get(match.group(1).lower()) if match else None
        if pk and "RETURNING" not in sql.upper():
            self._cur.execute(f"{sql.rstrip().rstrip(';')} RETURNING {pk}", params)
            row = self._cur.fetchone()
            self.lastrowid = int(row[pk]) if row else None
            return
        self._cur.execute(sql, params)

    def executemany(self, sql: str, seq_of_params: list[tuple[Any, ...]]) -> None:
        self._cur.executemany(sql, seq_of_params)

    def fetchall(self) -> list[Any]:
        return self._cur.fetchall()


INTERVAL_SECONDS = 300
DEFAULT_DAYS = 7

# A fixed seed keeps the generated history identical between runs, so an
# outlier a developer is looking at does not move when they reseed.
RANDOM_SEED = 20260828


@dataclass
class SensorSpec:
    entity_id: str
    friendly_name: str
    unit: str | None
    device_class: str | None
    has_mean: bool
    has_sum: bool
    # Outliers as (fraction through the series, value written).
    outliers: tuple[tuple[float, float], ...] = ()


SENSORS = (
    SensorSpec(
        entity_id="sensor.living_room_temperature",
        friendly_name="Living Room Temperature",
        unit="°C",
        device_class="temperature",
        has_mean=True,
        has_sum=False,
        # The -2000 is the exact case the design document opens with.
        outliers=((0.30, -2000.0), (0.55, 851.3), (0.80, -999.0)),
    ),
    SensorSpec(
        entity_id="sensor.outdoor_humidity",
        friendly_name="Outdoor Humidity",
        unit="%",
        device_class="humidity",
        has_mean=True,
        has_sum=False,
    ),
    SensorSpec(
        entity_id="sensor.energy_total",
        friendly_name="Energy Total",
        unit="kWh",
        device_class="energy",
        has_mean=False,
        has_sum=True,
        outliers=((0.45, 99999.0),),
    ),
    SensorSpec(
        entity_id="sensor.flaky_pressure",
        friendly_name="Flaky Pressure",
        unit="hPa",
        device_class="pressure",
        has_mean=True,
        has_sum=False,
    ),
    SensorSpec(
        entity_id="binary_sensor.front_door",
        friendly_name="Front Door",
        unit=None,
        device_class="door",
        has_mean=False,
        has_sum=False,
    ),
)

RECORDER_TABLES = (
    "states",
    "state_attributes",
    "states_meta",
    "statistics",
    "statistics_short_term",
    "statistics_meta",
)


def _value_for(spec: SensorSpec, index: int, total: int, rng: random.Random) -> str | None:
    """The value this sensor would plausibly have reported at this point."""
    progress = index / max(total - 1, 1)
    # One full day is 288 samples at five-minute resolution.
    day_phase = math.sin(2 * math.pi * index / 288)

    if spec.entity_id == "sensor.living_room_temperature":
        return f"{20.0 + 2.5 * day_phase + rng.gauss(0, 0.15):.1f}"
    if spec.entity_id == "sensor.outdoor_humidity":
        return f"{65.0 - 12.0 * day_phase + rng.gauss(0, 1.2):.1f}"
    if spec.entity_id == "sensor.energy_total":
        # Monotonically increasing, as a total_increasing sensor must be.
        return f"{1000.0 + progress * 180.0 + rng.uniform(0, 0.05):.3f}"
    if spec.entity_id == "sensor.flaky_pressure":
        # Roughly one reading in twenty is missing, which is what a sensor on
        # a marginal radio link actually looks like.
        roll = rng.random()
        if roll < 0.03:
            return "unavailable"
        if roll < 0.05:
            return "unknown"
        return f"{1013.0 + 6.0 * day_phase + rng.gauss(0, 0.8):.1f}"
    if spec.entity_id == "binary_sensor.front_door":
        return "on" if rng.random() < 0.04 else "off"
    raise AssertionError(f"no generator for {spec.entity_id}")


def _clear(cur: Any, dialect: str) -> None:
    if dialect == "postgres":
        # One statement, so the foreign keys between these tables never see an
        # inconsistent intermediate state — PostgreSQL has no equivalent of
        # MySQL's FOREIGN_KEY_CHECKS toggle. RESTART IDENTITY also resets the
        # identity sequences, keeping ids stable between reseeds the way
        # MySQL's AUTO_INCREMENT reset does.
        tables = ", ".join(RECORDER_TABLES)
        cur.execute(f"TRUNCATE TABLE {tables} RESTART IDENTITY CASCADE")  # nosec B608
        return
    if dialect == "mysql":
        cur.execute("SET FOREIGN_KEY_CHECKS = 0")
    for table in RECORDER_TABLES:
        cur.execute(f"DELETE FROM {table}")  # nosec B608 - fixed table name list
    if dialect == "mysql":
        cur.execute("SET FOREIGN_KEY_CHECKS = 1")


def _insert_attributes(cur: Any, spec: SensorSpec) -> int:
    attrs: dict[str, object] = {"friendly_name": spec.friendly_name}
    if spec.unit:
        attrs["unit_of_measurement"] = spec.unit
    if spec.device_class:
        attrs["device_class"] = spec.device_class
    if spec.has_mean:
        attrs["state_class"] = "measurement"
    elif spec.has_sum:
        attrs["state_class"] = "total_increasing"

    shared = json.dumps(attrs, sort_keys=True)
    cur.execute(
        "INSERT INTO state_attributes (hash, shared_attrs) VALUES (%s, %s)",
        (hash(shared) & 0x7FFFFFFF, shared),
    )
    return int(cur.lastrowid)


def seed(
    dsn: dict[str, object] | None,
    days: int,
    sqlite_path: str | None = None,
    postgres_dsn: str | None = None,
) -> None:
    rng = random.Random(RANDOM_SEED)  # nosec B311 - test fixture data, not security
    total = days * 24 * 3600 // INTERVAL_SECONDS
    now = time.time()
    start = now - days * 24 * 3600

    if sqlite_path is not None:
        dialect = "sqlite"
    elif postgres_dsn is not None:
        dialect = "postgres"
    else:
        dialect = "mysql"

    conn: Any
    if dialect == "sqlite":
        assert sqlite_path is not None
        conn = sqlite3.connect(sqlite_path)
        conn.row_factory = sqlite3.Row
    elif dialect == "postgres":
        import psycopg
        from psycopg.rows import dict_row

        conn = psycopg.connect(postgres_dsn, row_factory=dict_row, autocommit=False)
    else:
        assert dsn is not None
        conn = pymysql.connect(cursorclass=DictCursor, autocommit=False, **dsn)
    try:
        cur: Any
        if dialect == "sqlite":
            cur = _SQLiteCursor(conn)
        elif dialect == "postgres":
            cur = _PostgresCursor(conn)
        else:
            cur = conn.cursor()
        _clear(cur, dialect)

        for spec in SENSORS:
            cur.execute("INSERT INTO states_meta (entity_id) VALUES (%s)", (spec.entity_id,))
            metadata_id = int(cur.lastrowid)
            attributes_id = _insert_attributes(cur, spec)

            outliers = {int(f * total): v for f, v in spec.outliers}

            rows: list[tuple[object, ...]] = []
            for index in range(total):
                ts = start + index * INTERVAL_SECONDS
                if index in outliers:
                    value: str | None = f"{outliers[index]}"
                else:
                    value = _value_for(spec, index, total, rng)
                rows.append((metadata_id, value, ts, ts, attributes_id))

            cur.executemany(
                "INSERT INTO states "
                "(metadata_id, state, last_updated_ts, last_reported_ts, attributes_id) "
                "VALUES (%s, %s, %s, %s, %s)",
                rows,
            )

            if not (spec.has_mean or spec.has_sum):
                print(f"  {spec.entity_id}: {total} states, no statistics")
                continue

            # has_mean is written as NULL on purpose: that is what a
            # current Home Assistant does now that mean_type carries the
            # meaning. Seeding it as 1 — as this script originally did —
            # hid a real bug where every measurement sensor was classified
            # UNKNOWN and refused correction on any modern install.
            cur.execute(
                "INSERT INTO statistics_meta "
                "(statistic_id, source, unit_of_measurement, has_mean, has_sum, "
                " name, mean_type) "
                "VALUES (%s, 'recorder', %s, NULL, %s, NULL, %s)",
                (
                    # bool, not int: has_sum is a real BOOLEAN column on
                    # PostgreSQL, which rejects an integer there. MySQL and
                    # SQLite both accept a bool for their own tinyint/integer
                    # equivalents, so this is correct on all three.
                    spec.entity_id,
                    spec.unit,
                    bool(spec.has_sum),
                    1 if spec.has_mean else 0,
                ),
            )
            stats_metadata_id = int(cur.lastrowid)
            buckets = _build_statistics(cur, spec, metadata_id, stats_metadata_id)
            print(f"  {spec.entity_id}: {total} states, {buckets} hourly + short-term buckets")

        conn.commit()
    finally:
        conn.close()


def _build_statistics(
    cur: Any,
    spec: SensorSpec,
    metadata_id: int,
    stats_metadata_id: int,
) -> int:
    """Aggregate the seeded states into both statistics tables.

    This deliberately uses the project's own hr_statistics module rather than a
    SQL approximation. Two earlier fixtures were built by hand and each hid a
    real bug — one classified every measurement sensor as unknown, the other
    made a counter's running totals independent of each other so a cascade had
    nothing to cascade through. A fixture that does not behave like Home
    Assistant cannot catch code that does not behave like Home Assistant.

    The arithmetic here is the arithmetic verified against a live recorder by
    dev/statistics_truth.py.
    """
    sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent / "app"))
    from hr_statistics import (
        CounterBucket,
        Reading,
        cascade_sums,
        recompute_short_term,
    )

    cur.execute(
        "SELECT state, last_updated_ts FROM states WHERE metadata_id = %s "
        "AND state NOT IN ('unknown', 'unavailable') ORDER BY last_updated_ts",
        (metadata_id,),
    )
    readings: list[Reading] = []
    for row in cur.fetchall():
        try:
            readings.append(Reading(value=float(row["state"]), ts=float(row["last_updated_ts"])))
        except (TypeError, ValueError):
            continue
    if not readings:
        return 0

    hourly_written = 0

    if spec.has_sum:
        # Counters: build the 5-minute chain first, then take each hour's last
        # 5-minute row as its hourly row. That is exactly how Home Assistant
        # derives hourly state and sum — never by summing — and it is what
        # carries a spike's damage forward into the hourly totals.
        starts = sorted({int(r.ts // 300) * 300 for r in readings})
        buckets = []
        for start_ts in starts:
            inside = [r for r in readings if start_ts <= r.ts < start_ts + 300]
            buckets.append(
                CounterBucket(
                    start_ts=float(start_ts),
                    state=inside[-1].value if inside else None,
                    sum=None,
                )
            )
        chained = [CounterBucket(buckets[0].start_ts, buckets[0].state, 0.0)]
        chained.extend(cascade_sums(buckets[1:], buckets[0].state, 0.0))

        cur.executemany(
            "INSERT INTO statistics_short_term "
            "(created_ts, metadata_id, start_ts, state, sum) VALUES (%s, %s, %s, %s, %s)",
            [(b.start_ts, stats_metadata_id, b.start_ts, b.state, b.sum) for b in chained],
        )

        hourly: dict[float, CounterBucket] = {}
        for bucket in chained:
            hour = float(int(bucket.start_ts // 3600) * 3600)
            hourly[hour] = bucket  # ordered ascending, so the last one wins
        cur.executemany(
            "INSERT INTO statistics (created_ts, metadata_id, start_ts, state, sum) "
            "VALUES (%s, %s, %s, %s, %s)",
            [(h, stats_metadata_id, h, b.state, b.sum) for h, b in sorted(hourly.items())],
        )
        hourly_written = len(hourly)
    else:
        for table, width in (("statistics_short_term", 300), ("statistics", 3600)):
            starts = sorted({int(r.ts // width) * width for r in readings})
            computed = []
            for start_ts in starts:
                window = [r for r in readings if r.ts < start_ts + width]
                relevant = [r for r in window if r.ts >= start_ts]
                carried = [r for r in window if r.ts < start_ts]
                computed.append(
                    recompute_short_term(
                        (carried[-1:] if carried else []) + relevant, float(start_ts), width
                    )
                )
            cur.executemany(
                f"INSERT INTO {table} (created_ts, metadata_id, start_ts, mean, min, max) "  # nosec B608
                "VALUES (%s, %s, %s, %s, %s, %s)",
                [
                    (b.start_ts, stats_metadata_id, b.start_ts, b.mean, b.min, b.max)
                    for b in computed
                ],
            )
            if table == "statistics":
                hourly_written = len(starts)

    return hourly_written


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=3399)
    parser.add_argument("--database", default="ha_test")
    parser.add_argument("--user", default="hatest")
    parser.add_argument("--password", default="hatest")
    parser.add_argument("--days", type=int, default=DEFAULT_DAYS)
    parser.add_argument(
        "--sqlite-path",
        default=None,
        help="Seed a SQLite recorder file instead of MariaDB (schema must already exist).",
    )
    parser.add_argument(
        "--postgres-dsn",
        default=None,
        help="Seed a PostgreSQL recorder instead of MariaDB (schema must already exist).",
    )
    args = parser.parse_args()

    if args.sqlite_path:
        print(f"Seeding {args.days} days into {args.sqlite_path} (SQLite)")
        seed(None, args.days, sqlite_path=args.sqlite_path)
    elif args.postgres_dsn:
        print(f"Seeding {args.days} days into {args.postgres_dsn} (PostgreSQL)")
        seed(None, args.days, postgres_dsn=args.postgres_dsn)
    else:
        print(f"Seeding {args.days} days into {args.database} at {args.host}:{args.port}")
        seed(
            {
                "host": args.host,
                "port": args.port,
                "database": args.database,
                "user": args.user,
                "password": args.password,
            },
            args.days,
        )
    print("Done.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
