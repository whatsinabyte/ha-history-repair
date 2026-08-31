"""Does Home Assistant actually accept and apply our statistics messages?

The unit tests pin the shape of the message. This checks a real Home Assistant
agrees, because the design document treats the WebSocket path as unreliable
(§2.6: "acknowledged but not applied") and that claim needed testing rather
than assuming.

Needs the development instance:

    ./dev/ha_core.sh start
    HR_HA_URL=ws://127.0.0.1:8123/api/websocket \\
    HR_HA_TOKEN="$(cat .devdb/ha-core/token.txt)" \\
    HR_HA_DSN=mysql://hatest:hatest@127.0.0.1:3402/ha_test_core \\
      .venv-check/bin/python -m pytest tests/test_ha_api_live.py

Never point these at a production instance: they write statistics.
"""

from __future__ import annotations

import os
import time
from collections.abc import Iterator
from typing import Any
from urllib.parse import urlparse

import pymysql
import pytest
from pymysql.cursors import DictCursor

from hr_ha_api import ApiConfig, HomeAssistantApiError, HomeAssistantClient
from hr_statistics import HOURLY_SECONDS, SHORT_TERM_SECONDS, bucket_start

_WS_URL = os.environ.get("HR_HA_URL")
_TOKEN = os.environ.get("HR_HA_TOKEN")
_DSN = os.environ.get("HR_HA_DSN")

pytestmark = [
    pytest.mark.skipif(
        not (_WS_URL and _TOKEN and _DSN),
        reason="HR_HA_URL, HR_HA_TOKEN and HR_HA_DSN are not all set",
    ),
    # This file's hourly-import test briefly writes a known value into
    # ha_test_core — one live, un-cloned Home Assistant instance also read by
    # test_statistics_fidelity.py — to verify Home Assistant actually applied
    # it, then deletes it. Grouped with that file so xdist never schedules
    # them onto different workers concurrently, which would let a read there
    # observe this write mid-flight and report a spurious mismatch.
    pytest.mark.xdist_group(name="live_home_assistant"),
]

# The recorder queues imports, so a write is not visible the moment the command
# is acknowledged.
APPLY_TIMEOUT = 30


@pytest.fixture(scope="session")
def client() -> HomeAssistantClient:
    return HomeAssistantClient(ApiConfig(url=_WS_URL or "", token=_TOKEN or ""))


@pytest.fixture(scope="session")
def db() -> Iterator[Any]:
    parsed = urlparse(_DSN or "")
    name = (parsed.path or "/").lstrip("/")
    if not name.startswith("ha_test"):
        raise AssertionError(f"HR_HA_DSN points at '{name}'; refusing to run.")
    conn = pymysql.connect(
        host=parsed.hostname or "127.0.0.1",
        port=parsed.port or 3306,
        user=parsed.username or "",
        password=parsed.password or "",
        database=name,
        cursorclass=DictCursor,
        autocommit=True,
    )
    try:
        yield conn
    finally:
        conn.close()


def _a_measurement_sensor(db: Any) -> dict[str, Any]:
    with db.cursor() as cur:
        cur.execute(
            "SELECT id, statistic_id, unit_of_measurement, unit_class, mean_type, "
            "has_sum FROM statistics_meta WHERE mean_type = 1 LIMIT 1"
        )
        row = cur.fetchone()
    if not row:
        pytest.skip("the development instance has no measurement statistics yet")
    return dict(row)


def _an_hour_with_data(db: Any, metadata_id: int) -> float:
    with db.cursor() as cur:
        cur.execute(
            "SELECT MIN(start_ts) AS t FROM statistics_short_term WHERE metadata_id = %s",
            (metadata_id,),
        )
        row = cur.fetchone()
    if not row or row["t"] is None:
        pytest.skip("no statistics recorded yet")
    return bucket_start(float(row["t"]), HOURLY_SECONDS)


class TestConnection:
    def test_authenticates_and_reports_a_version(self, client: HomeAssistantClient) -> None:
        version = client.get_version()
        assert version is not None
        assert version[0].isdigit()


class TestHourlyImport:
    def test_an_imported_hourly_row_is_actually_applied(
        self, client: HomeAssistantClient, db: Any
    ) -> None:
        """The claim in design document §2.6, tested rather than assumed."""
        meta = _a_measurement_sensor(db)
        hour = _an_hour_with_data(db, meta["id"])
        # Unmistakably fake rather than a plausible-looking reading: this
        # briefly becomes real data in ha_test_core, a live instance also
        # read by test_statistics_fidelity.py's checks (co-scheduled with
        # this file via the live_home_assistant xdist group so they never run
        # concurrently, but a value like this makes any gap in that grouping
        # obvious on sight rather than looking like a genuine mismatch).
        target = 918_273_645.0

        client.import_statistics(
            statistic_id=meta["statistic_id"],
            unit_of_measurement=meta["unit_of_measurement"],
            mean_type=int(meta["mean_type"]),
            has_sum=bool(meta["has_sum"]),
            unit_class=meta["unit_class"],
            stats=[{"start_ts": hour, "mean": target, "min": 1.0, "max": 99.0}],
        )

        deadline = time.time() + APPLY_TIMEOUT
        applied = None
        while time.time() < deadline:
            with db.cursor() as cur:
                cur.execute(
                    "SELECT mean FROM statistics WHERE metadata_id = %s AND start_ts = %s",
                    (meta["id"], hour),
                )
                row = cur.fetchone()
            if row and row["mean"] is not None and abs(float(row["mean"]) - target) < 1e-6:
                applied = float(row["mean"])
                break
            time.sleep(1)

        try:
            assert applied is not None, (
                "Home Assistant acknowledged the import but never wrote it. This "
                "is the unreliability the design document warns about; the SQL "
                "fallback exists for exactly this case."
            )
        finally:
            with db.cursor() as cur:
                cur.execute(
                    "DELETE FROM statistics WHERE metadata_id = %s AND start_ts = %s AND mean = %s",
                    (meta["id"], hour, target),
                )


class TestShortTermIsNotImportable:
    def test_sub_hourly_timestamps_are_refused(self, client: HomeAssistantClient, db: Any) -> None:
        """Why statistics_short_term must always be written with SQL.

        There is no API for 5-minute buckets: import_statistics accepts only
        timestamps on an hour boundary. The design document's §2.6 assumes the
        WebSocket path can correct both statistics tables, and it cannot.
        """
        meta = _a_measurement_sensor(db)
        hour = _an_hour_with_data(db, meta["id"])
        # Deliberately off the hour, by one 5-minute bucket.
        not_an_hour = hour + SHORT_TERM_SECONDS

        with pytest.raises(HomeAssistantApiError, match="top of the hour"):
            client.import_statistics(
                statistic_id=meta["statistic_id"],
                unit_of_measurement=meta["unit_of_measurement"],
                mean_type=int(meta["mean_type"]),
                has_sum=bool(meta["has_sum"]),
                unit_class=meta["unit_class"],
                stats=[{"start_ts": not_an_hour, "mean": 1.0}],
            )
