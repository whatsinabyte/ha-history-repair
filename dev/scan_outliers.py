"""Read-only outlier scan against a real MariaDB recorder database.

This is deliberately NOT the add-on. It opens its own connection, issues only
SELECT statements, and never imports hr_mariadb.py or anything else capable of
writing to `states`. The point is to survey a real database's real history —
including one you actually care about, like your live ODROID setup — with
zero risk of a correction being applied, before you ever point the add-on
itself at it.

It reuses hr_outliers.py's median/MAD detection, which is the same code the
add-on's own graph uses to flag candidates, and hr_statistics.Reading, which
is a plain (value, ts) pair with no I/O. Nothing about running this script
writes to your database, checks a correctable sensor type, or requires the
add-on's own audit table to exist.

Usage:

    .venv-check/bin/python dev/scan_outliers.py \\
        --host <your-mariadb-host> --port 3306 \\
        --database homeassistant --user homeassistant --password <pw> \\
        --days 90 --threshold 3.0 --top 15

Add --entity to scan just one entity_id instead of every one with numeric
history. Add --dry-run-check to print the exact SQL this script issues and
exit without connecting, if you want to review it first.
"""

from __future__ import annotations

import argparse
import pathlib
import sys
import time
from dataclasses import dataclass
from typing import Any

import pymysql
from pymysql.cursors import DictCursor

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent / "app"))

from hr_outliers import (
    Candidate,
    detect_counter_outliers,
    detect_measurement_outliers,
)
from hr_statistics import Reading

# Every query this script ever issues. Kept as a literal tuple, not built up
# dynamically, so --dry-run-check can print exactly what will run and a
# reader does not have to trust that claim — they can check it in one place.
_QUERIES = {
    "entities": "SELECT metadata_id, entity_id FROM states_meta ORDER BY entity_id",
    "statistics_meta": (
        "SELECT statistic_id, has_sum FROM statistics_meta WHERE statistic_id = %s"
    ),
    "readings": (
        "SELECT state, last_updated_ts FROM states "
        "WHERE metadata_id = %s AND state NOT IN ('unknown', 'unavailable', '') "
        "AND last_updated_ts >= %s "
        "ORDER BY last_updated_ts"
    ),
}


@dataclass
class EntityScanResult:
    entity_id: str
    sensor_type: str
    candidates: list[Candidate]


def _connect_readonly(host: str, port: int, database: str, user: str, password: str) -> Any:
    # autocommit=True and no transaction ever opened: nothing here holds a
    # lock or has anything to roll back. read_timeout is generous because a
    # full-history scan of a large table can legitimately take a while.
    return pymysql.connect(
        host=host,
        port=port,
        user=user,
        password=password,
        database=database,
        cursorclass=DictCursor,
        autocommit=True,
        connect_timeout=10,
        read_timeout=300,
    )


def _is_counter(cur: Any, entity_id: str) -> bool:
    cur.execute(_QUERIES["statistics_meta"], (entity_id,))
    row = cur.fetchone()
    return bool(row and row["has_sum"])


def _readings(cur: Any, metadata_id: int, since_ts: float) -> list[Reading]:
    cur.execute(_QUERIES["readings"], (metadata_id, since_ts))
    readings: list[Reading] = []
    for row in cur.fetchall():
        try:
            readings.append(Reading(value=float(row["state"]), ts=float(row["last_updated_ts"])))
        except (TypeError, ValueError):
            continue
    return readings


def scan(
    conn: Any,
    since_ts: float,
    threshold: float,
    only_entity: str | None = None,
) -> list[EntityScanResult]:
    results: list[EntityScanResult] = []
    with conn.cursor() as cur:
        cur.execute(_QUERIES["entities"])
        entities = cur.fetchall()

        for row in entities:
            entity_id = row["entity_id"]
            if only_entity and entity_id != only_entity:
                continue

            readings = _readings(cur, row["metadata_id"], since_ts)
            if not readings:
                continue

            is_counter = _is_counter(cur, entity_id)
            if is_counter:
                candidates = detect_counter_outliers(readings, threshold=threshold)
                sensor_type = "counter"
            else:
                candidates = detect_measurement_outliers(readings, threshold=threshold)
                sensor_type = "measurement"

            if candidates:
                results.append(
                    EntityScanResult(
                        entity_id=entity_id, sensor_type=sensor_type, candidates=candidates
                    )
                )
    return results


def _print_dry_run() -> None:
    print("This script issues only these SELECT statements — nothing else, ever:\n")
    for name, sql in _QUERIES.items():
        print(f"  [{name}]\n    {sql}\n")
    print("No INSERT, UPDATE, DELETE, or DDL statement appears anywhere in this script.")


def main() -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawTextHelpFormatter
    )
    parser.add_argument("--host", help="MariaDB hostname or IP")
    parser.add_argument("--port", type=int, default=3306)
    parser.add_argument("--database", default="homeassistant")
    parser.add_argument("--user", default="homeassistant")
    parser.add_argument("--password", default="")
    parser.add_argument("--days", type=int, default=90, help="How far back to scan")
    parser.add_argument("--threshold", type=float, default=3.0, help="MAD multiple to flag")
    parser.add_argument("--top", type=int, default=15, help="Worst candidates shown per entity")
    parser.add_argument("--entity", default=None, help="Scan only this entity_id")
    parser.add_argument(
        "--dry-run-check",
        action="store_true",
        help="Print the exact SQL this script can issue and exit without connecting.",
    )
    args = parser.parse_args()

    if args.dry_run_check:
        _print_dry_run()
        return 0

    if not args.host:
        parser.error("--host is required unless --dry-run-check is given")

    since_ts = time.time() - args.days * 24 * 3600

    print(f"Connecting read-only to {args.user}@{args.host}:{args.port}/{args.database}")
    print(f"Scanning the last {args.days} days, threshold={args.threshold}\n")

    conn = _connect_readonly(args.host, args.port, args.database, args.user, args.password)
    try:
        results = scan(conn, since_ts, args.threshold, only_entity=args.entity)
    finally:
        conn.close()

    if not results:
        print("No candidates found at this threshold.")
        return 0

    for result in sorted(
        results, key=lambda r: max(c.deviation for c in r.candidates), reverse=True
    ):
        print(f"=== {result.entity_id} ({result.sensor_type}) ===")
        worst = sorted(result.candidates, key=lambda c: c.deviation, reverse=True)[: args.top]
        for candidate in worst:
            when = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(candidate.ts))
            print(
                f"  {when}  value={candidate.value!r:>14}  deviation={candidate.deviation:.1f}x MAD"
            )
        print()

    total = sum(len(r.candidates) for r in results)
    print(f"{total} candidate(s) across {len(results)} entit(y/ies). Nothing was written.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
