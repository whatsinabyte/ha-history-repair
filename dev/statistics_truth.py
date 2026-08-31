"""Check our understanding of how Home Assistant computes statistics.

Phase 2 has to recompute a statistics bucket after a state inside it is
corrected, and the recomputed value must match what Home Assistant itself
would have produced. There is no documentation of that arithmetic, so this
script derives it from a running instance: it reads the raw states behind each
bucket, computes the candidate formulas, and compares them to the values Home
Assistant actually stored.

Run it against the development Home Assistant, never a real one:

    ./dev/ha_core.sh start
    .venv-check/bin/python dev/statistics_truth.py

What it establishes, and why each matters:

  short-term mean   Time-weighted by how long each reading was in effect, NOT
                    a plain average of the readings. The design document's
                    recomputation SQL uses AVG(state), which is a different
                    number whenever readings are unevenly spaced.
  short-term min/max
                    Plain min/max, over the readings in the bucket plus the
                    last reading before it — the value that was in effect when
                    the bucket opened counts.
  hourly mean       A plain unweighted AVG over the 5-minute means, so the
                    design document is right about this one.
  hourly min/max    MIN/MAX over the 5-minute min/max.
  hourly state/sum  Taken from the LAST 5-minute row of the hour, not summed.
"""

from __future__ import annotations

import argparse
from typing import Any

import pymysql
from pymysql.cursors import DictCursor

SHORT_TERM_SECONDS = 300
HOURLY_SECONDS = 3600

# Floating point accumulation differs in the last bits between Python and the
# database; anything within this is the same number for our purposes.
TOLERANCE = 1e-6


def time_weighted_mean(
    readings: list[tuple[float, float]], start: float, end: float
) -> float | None:
    """Reimplementation of Home Assistant's _time_weighted_arithmetic_mean.

    `readings` is (value, timestamp) ascending, and must include the last
    reading before `start` — Home Assistant carries that one in, because it is
    the value that was in effect when the bucket opened. There is no
    interpolation: each reading is weighted by the seconds until the next one.
    """
    if not readings:
        return None

    old_value: float | None = None
    old_start: float | None = None
    accumulated = 0.0
    window_start = start

    for value, ts in readings:
        state_start = max(ts, window_start)
        if old_start is None:
            # No reading was in effect at the start of the bucket, so the
            # window itself begins later and the denominator shrinks.
            window_start = state_start
        else:
            accumulated += old_value * (state_start - old_start)  # type: ignore[operator]
        old_value, old_start = value, state_start

    if old_value is not None and old_start is not None:
        accumulated += old_value * (end - old_start)

    span = end - window_start
    return accumulated / span if span else None


def plain_mean(readings: list[tuple[float, float]], start: float) -> float | None:
    """The design document's formula: a plain average of the readings."""
    inside = [value for value, ts in readings if ts >= start]
    return sum(inside) / len(inside) if inside else None


def _readings_for(
    cur: Any, metadata_id: int, start: float, end: float
) -> list[tuple[float, float]]:
    """Readings inside the bucket, preceded by the last one before it."""
    cur.execute(
        "SELECT state, last_updated_ts FROM states "
        "WHERE metadata_id = %s AND last_updated_ts < %s "
        "AND state NOT IN ('unknown', 'unavailable') "
        "ORDER BY last_updated_ts DESC LIMIT 1",
        (metadata_id, start),
    )
    carried = cur.fetchall()

    cur.execute(
        "SELECT state, last_updated_ts FROM states "
        "WHERE metadata_id = %s AND last_updated_ts >= %s AND last_updated_ts < %s "
        "AND state NOT IN ('unknown', 'unavailable') "
        "ORDER BY last_updated_ts",
        (metadata_id, start, end),
    )
    inside = cur.fetchall()

    readings: list[tuple[float, float]] = []
    for row in list(carried) + list(inside):
        try:
            readings.append((float(row["state"]), float(row["last_updated_ts"])))
        except (TypeError, ValueError):
            continue
    return readings


def check_short_term(conn: pymysql.connections.Connection, limit: int) -> None:
    print("\n=== statistics_short_term: how a 5-minute bucket is built ===")
    with conn.cursor() as cur:
        cur.execute(
            "SELECT sm.id, sm.statistic_id, sm.mean_type "
            "FROM statistics_meta sm WHERE sm.mean_type = 1"
        )
        sensors = cur.fetchall()

        for sensor in sensors:
            cur.execute(
                "SELECT metadata_id, start_ts, mean, mean_weight, min, max "
                "FROM statistics_short_term WHERE metadata_id = %s "
                "ORDER BY start_ts LIMIT %s",
                (sensor["id"], limit),
            )
            buckets = cur.fetchall()
            if not buckets:
                continue

            cur.execute(
                "SELECT metadata_id FROM states_meta WHERE entity_id = %s",
                (sensor["statistic_id"],),
            )
            row = cur.fetchone()
            if not row:
                continue
            states_metadata_id = row["metadata_id"]

            weighted_hits = plain_hits = minmax_hits = 0
            compared = 0
            for bucket in buckets:
                start = float(bucket["start_ts"])
                end = start + SHORT_TERM_SECONDS
                readings = _readings_for(cur, states_metadata_id, start, end)
                if not readings or bucket["mean"] is None:
                    continue
                compared += 1

                stored = float(bucket["mean"])
                weighted = time_weighted_mean(readings, start, end)
                plain = plain_mean(readings, start)

                if weighted is not None and abs(weighted - stored) < TOLERANCE:
                    weighted_hits += 1
                if plain is not None and abs(plain - stored) < TOLERANCE:
                    plain_hits += 1

                values = [v for v, _ in readings]
                if (
                    abs(min(values) - float(bucket["min"])) < TOLERANCE
                    and abs(max(values) - float(bucket["max"])) < TOLERANCE
                ):
                    minmax_hits += 1

            if not compared:
                continue
            print(f"\n  {sensor['statistic_id']}  ({compared} buckets compared)")
            print(f"    time-weighted mean matches   {weighted_hits}/{compared}")
            print(f"    plain average matches        {plain_hits}/{compared}")
            print(f"    min/max over readings match  {minmax_hits}/{compared}")
            print(f"    mean_weight stored           {buckets[0]['mean_weight']!r}")


def check_hourly(conn: pymysql.connections.Connection) -> None:
    print("\n=== statistics: how an hourly bucket summarises the 5-minute ones ===")
    with conn.cursor() as cur:
        cur.execute("SELECT id, statistic_id, mean_type, has_sum FROM statistics_meta")
        sensors = cur.fetchall()

        for sensor in sensors:
            cur.execute(
                "SELECT start_ts, mean, min, max, state, sum FROM statistics "
                "WHERE metadata_id = %s ORDER BY start_ts",
                (sensor["id"],),
            )
            hours = cur.fetchall()
            if not hours:
                continue

            matches = 0
            for hour in hours:
                start = float(hour["start_ts"])
                cur.execute(
                    "SELECT AVG(mean) AS avg_mean, MIN(min) AS min_min, "
                    "MAX(max) AS max_max FROM statistics_short_term "
                    "WHERE metadata_id = %s AND start_ts >= %s AND start_ts < %s",
                    (sensor["id"], start, start + HOURLY_SECONDS),
                )
                summary = cur.fetchone()
                if hour["mean"] is None or summary["avg_mean"] is None:
                    continue
                if abs(float(summary["avg_mean"]) - float(hour["mean"])) < TOLERANCE:
                    matches += 1

            print(f"\n  {sensor['statistic_id']}  ({len(hours)} hourly rows)")
            if sensor["mean_type"] == 1:
                print(f"    AVG(short_term.mean) matches hourly mean  {matches}/{len(hours)}")
            if sensor["has_sum"]:
                cur.execute(
                    "SELECT state, sum FROM statistics_short_term "
                    "WHERE metadata_id = %s AND start_ts >= %s AND start_ts < %s "
                    "ORDER BY start_ts DESC LIMIT 1",
                    (
                        sensor["id"],
                        float(hours[-1]["start_ts"]),
                        float(hours[-1]["start_ts"]) + HOURLY_SECONDS,
                    ),
                )
                last = cur.fetchone()
                if last:
                    print(
                        f"    last short-term state/sum {last['state']}/{last['sum']} "
                        f"vs hourly {hours[-1]['state']}/{hours[-1]['sum']}"
                    )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=3402)
    parser.add_argument("--database", default="ha_test_core")
    parser.add_argument("--user", default="hatest")
    parser.add_argument("--password", default="hatest")
    parser.add_argument("--buckets", type=int, default=25)
    args = parser.parse_args()

    conn = pymysql.connect(
        host=args.host,
        port=args.port,
        user=args.user,
        password=args.password,
        database=args.database,
        cursorclass=DictCursor,
    )
    try:
        check_short_term(conn, args.buckets)
        check_hourly(conn)
    finally:
        conn.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
