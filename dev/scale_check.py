"""Measure the adapter's queries against a realistically large recorder database.

The entity browser's query carries a comment claiming it keeps work
proportional to the page size rather than to the size of states. On the seeded
development database — five entities, ten thousand rows — that claim is
untestable: everything is fast. A real installation has hundreds of entities
and tens of millions of state rows, which is where a correlated subquery per
displayed entity either holds up or falls over.

This builds such a database once and times the three queries a user waits on.

    ./dev/mariadb.sh start
    .venv-ha-schema/bin/python dev/build_schema.py --dsn mysql+pymysql://hatest:hatest@127.0.0.1:3399/ha_test_scale
    .venv-check/bin/python dev/scale_check.py

It is a benchmark, not a test: it prints timings and asserts nothing.
"""

from __future__ import annotations

import argparse
import json
import random
import time

import pymysql
from pymysql.cursors import DictCursor

BATCH = 20_000


def _build(conn: pymysql.connections.Connection, entities: int, per_entity: int) -> None:
    rng = random.Random(7)  # nosec B311 - benchmark fixture data
    now = time.time()

    with conn.cursor() as cur:
        cur.execute("SET FOREIGN_KEY_CHECKS = 0")
        for table in ("states", "state_attributes", "states_meta", "statistics_meta"):
            cur.execute(f"DELETE FROM {table}")  # nosec B608 - fixed table list
        cur.execute("SET FOREIGN_KEY_CHECKS = 1")

        for index in range(entities):
            entity_id = f"sensor.scale_test_{index:04d}"
            shared = json.dumps(
                {"friendly_name": f"Scale Test {index}", "unit_of_measurement": "°C"},
                sort_keys=True,
            )
            cur.execute(
                "INSERT INTO state_attributes (hash, shared_attrs) VALUES (%s, %s)",
                (hash(shared) & 0x7FFFFFFF, shared),
            )
            attributes_id = int(cur.lastrowid)
            cur.execute("INSERT INTO states_meta (entity_id) VALUES (%s)", (entity_id,))
            metadata_id = int(cur.lastrowid)
            cur.execute(
                "INSERT INTO statistics_meta "
                "(statistic_id, source, unit_of_measurement, has_mean, has_sum, mean_type) "
                "VALUES (%s, 'recorder', '°C', 1, 0, 1)",
                (entity_id,),
            )

            start_ts = now - per_entity * 30
            rows: list[tuple[object, ...]] = []
            for step in range(per_entity):
                ts = start_ts + step * 30
                rows.append((metadata_id, f"{20 + rng.gauss(0, 2):.1f}", ts, ts, attributes_id))
                if len(rows) >= BATCH:
                    cur.executemany(
                        "INSERT INTO states (metadata_id, state, last_updated_ts, "
                        "last_reported_ts, attributes_id) VALUES (%s, %s, %s, %s, %s)",
                        rows,
                    )
                    rows.clear()
            if rows:
                cur.executemany(
                    "INSERT INTO states (metadata_id, state, last_updated_ts, "
                    "last_reported_ts, attributes_id) VALUES (%s, %s, %s, %s, %s)",
                    rows,
                )
            if (index + 1) % 25 == 0:
                conn.commit()
                print(f"    {index + 1}/{entities} entities built")
        conn.commit()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=3399)
    parser.add_argument("--database", default="ha_test_scale")
    parser.add_argument("--user", default="hatest")
    parser.add_argument("--password", default="hatest")
    parser.add_argument("--entities", type=int, default=250)
    parser.add_argument("--per-entity", type=int, default=20_000)
    parser.add_argument("--skip-build", action="store_true")
    args = parser.parse_args()

    total = args.entities * args.per_entity
    conn = pymysql.connect(
        host=args.host,
        port=args.port,
        user=args.user,
        password=args.password,
        database=args.database,
        cursorclass=DictCursor,
        autocommit=False,
    )
    try:
        if not args.skip_build:
            print(f"Building {args.entities} entities x {args.per_entity} states = {total:,} rows")
            began = time.time()
            _build(conn, args.entities, args.per_entity)
            print(f"  built in {time.time() - began:.1f}s")
    finally:
        conn.close()

    from hr_config import DatabaseConfig
    from hr_mariadb import MariaDBAdapter

    adapter = MariaDBAdapter(
        DatabaseConfig(
            host=args.host,
            port=args.port,
            name=args.database,
            user=args.user,
            password=args.password,
        )
    )
    adapter.ensure_audit_table()

    def timed(label: str, fn: object, repeats: int = 5) -> None:
        samples = []
        for _ in range(repeats):
            began = time.perf_counter()
            result = fn()  # type: ignore[operator]
            samples.append(time.perf_counter() - began)
        size = len(result) if hasattr(result, "__len__") else result
        samples.sort()
        print(f"  {label:<44} median {samples[len(samples) // 2] * 1000:8.1f} ms   -> {size}")

    print(f"\nTimings against {total:,} state rows across {args.entities} entities:")
    timed("list_entities(limit=50) — first page", lambda: adapter.list_entities(limit=50))
    timed(
        "list_entities(limit=50, offset=200) — last page",
        lambda: adapter.list_entities(limit=50, offset=200),
    )
    timed("count_entities()", adapter.count_entities)
    timed(
        "count_entities(search='scale_test_01')",
        lambda: adapter.count_entities(search="scale_test_01"),
    )
    timed(
        "list_entities(search='scale_test_01')",
        lambda: adapter.list_entities(search="scale_test_01"),
    )
    timed("get_entity() — single", lambda: adapter.get_entity("sensor.scale_test_0000").entity_id)

    now = time.time()
    timed(
        "fetch_states — 24h window",
        lambda: adapter.fetch_states("sensor.scale_test_0000", now - 86400, now),
    )
    timed(
        "fetch_states — 30d window",
        lambda: adapter.fetch_states("sensor.scale_test_0000", now - 30 * 86400, now),
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
