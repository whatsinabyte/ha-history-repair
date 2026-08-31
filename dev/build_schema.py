"""Create an authentic Home Assistant recorder schema in the development database.

Rather than hand-writing DDL that would drift from the real thing, this asks
Home Assistant's own SQLAlchemy models to emit their CREATE TABLE statements.
The result is exactly the schema a real installation would have, at whatever
version the installed homeassistant package declares.

Works against every backend Home Assistant supports:

    .venv-ha-schema/bin/python dev/build_schema.py \\
      --dsn mysql+pymysql://hatest:hatest@127.0.0.1:3399/ha_test
    .venv-ha-schema/bin/python dev/build_schema.py \\
      --dsn sqlite:///.devdb/sqlite/ha_test.db
    .venv-ha-schema/bin/python dev/build_schema.py \\
      --dsn postgresql+psycopg://hatest:hatest@127.0.0.1:5442/ha_test

The target database is dropped and recreated, so this is always idempotent and
never accumulates state from a previous run.
"""

from __future__ import annotations

import argparse
import os
import sys

DEFAULT_DSN = "mysql+pymysql://hatest:hatest@127.0.0.1:3399/ha_test"


def _reset_mysql(dsn: str, database: str) -> None:
    from sqlalchemy import create_engine, text

    server_dsn = dsn.rsplit("/", 1)[0]
    server = create_engine(f"{server_dsn}/", future=True)
    try:
        with server.begin() as conn:
            conn.execute(text(f"DROP DATABASE IF EXISTS `{database}`"))
            conn.execute(
                text(
                    f"CREATE DATABASE `{database}` CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci"
                )
            )
    finally:
        server.dispose()


def _reset_postgres(dsn: str, database: str) -> None:
    from sqlalchemy import create_engine, text

    # DROP DATABASE cannot run inside a transaction block, and cannot target
    # the database the connection is itself using — so this connects to the
    # server's built-in "postgres" maintenance database with AUTOCOMMIT
    # instead, which is the standard way to recreate a database from SQL.
    server_dsn = dsn.rsplit("/", 1)[0]
    server = create_engine(f"{server_dsn}/postgres", future=True, isolation_level="AUTOCOMMIT")
    try:
        with server.connect() as conn:
            # Any other session still holding a connection would make DROP
            # fail with "database is being accessed by other users".
            conn.execute(
                text(
                    "SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
                    "WHERE datname = :name AND pid <> pg_backend_pid()"
                ),
                {"name": database},
            )
            conn.execute(text(f'DROP DATABASE IF EXISTS "{database}"'))
            conn.execute(text(f'CREATE DATABASE "{database}" ENCODING \'UTF8\''))
    finally:
        server.dispose()


def _reset_sqlite(path: str) -> None:
    # A SQLite "database" is just a file (plus -wal/-shm siblings under WAL
    # mode); dropping and recreating it is deleting and letting create_all()
    # start fresh, not a DROP DATABASE statement.
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    for suffix in ("", "-wal", "-shm", "-journal"):
        candidate = path + suffix
        if os.path.exists(candidate):
            os.remove(candidate)


def _record_schema_version(engine: object, schema_version: int) -> None:
    # SchemaChanges.changed has a Python-side default (default=dt_util.utcnow
    # on the mapped_column, not a server_default), which only fires when the
    # insert goes through SQLAlchemy's own Table.insert() construct — a raw
    # parameterised INSERT skips column defaults entirely, on any dialect, and
    # fails NOT NULL on SQLite (MySQL happened to work before only because the
    # old version of this script named NOW() explicitly).
    from homeassistant.components.recorder.db_schema import SchemaChanges

    with engine.begin() as conn:  # type: ignore[attr-defined]
        conn.execute(SchemaChanges.__table__.insert().values(schema_version=schema_version))


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dsn", default=DEFAULT_DSN, help="SQLAlchemy URL of the target")
    args = parser.parse_args()

    try:
        from homeassistant.components.recorder.db_schema import SCHEMA_VERSION, Base
        from homeassistant.const import __version__ as ha_version
    except ImportError:
        print(
            "homeassistant is not importable. Run this with .venv-ha-schema/bin/python,\n"
            "or create that venv first:\n"
            "  uv venv --python 3.13 .venv-ha-schema\n"
            "  VIRTUAL_ENV=.venv-ha-schema uv pip install homeassistant",
            file=sys.stderr,
        )
        return 1

    from sqlalchemy import create_engine, inspect

    dialect = args.dsn.split(":", 1)[0].split("+", 1)[0]
    print(
        f"Home Assistant {ha_version}, recorder schema version {SCHEMA_VERSION}, dialect {dialect}"
    )

    if dialect == "sqlite":
        path = args.dsn.removeprefix("sqlite:///")
        _reset_sqlite(path)
    elif dialect == "mysql":
        database = args.dsn.rsplit("/", 1)[1]
        _reset_mysql(args.dsn, database)
    elif dialect == "postgresql":
        database = args.dsn.rsplit("/", 1)[1]
        _reset_postgres(args.dsn, database)
    else:
        print(f"Unsupported dialect: {dialect}", file=sys.stderr)
        return 1

    engine = create_engine(args.dsn, future=True)
    Base.metadata.create_all(engine)

    # Home Assistant records every migration it has applied here, and the
    # add-on reads MAX(schema_version) from it to decide whether it can
    # operate. create_all() builds the table but not its contents.
    _record_schema_version(engine, SCHEMA_VERSION)

    tables = sorted(inspect(engine).get_table_names())
    engine.dispose()

    print(f"Created {len(tables)} tables: {', '.join(tables)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
