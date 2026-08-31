"""
conftest.py
===========
Shared fixtures, Hypothesis strategies, and profile configuration for the
History Repair test suite.

Profiles
--------
  ci       (default) — 20 examples, fast feedback during development
  thorough            — 500 examples, run manually before releases
  nightly             — 500 examples + stateful_step_count=50

Select a profile via the HYPOTHESIS_PROFILE environment variable:
  HYPOTHESIS_PROFILE=thorough pytest tests/

database=None
    The Hypothesis example database is disabled in all profiles, matching the
    sibling nibe-smo-mqtt-bridge repo: st.text() can generate Unicode
    surrogates that hash non-deterministically on Python 3.12+, making replay
    of cached examples unreliable.
"""

from __future__ import annotations

import os
from typing import Any

import pytest

from hr_fakes import SEED_TIMESTAMPS, FakeAdapter
from hr_models import SensorType

try:
    from hypothesis import HealthCheck, settings

    _suppress = [HealthCheck.too_slow]
    if hasattr(HealthCheck, "differing_executors"):
        _suppress.append(HealthCheck.differing_executors)

    settings.register_profile(
        "ci", max_examples=20, deadline=None, suppress_health_check=_suppress, database=None
    )
    settings.register_profile(
        "thorough",
        max_examples=500,
        deadline=None,
        suppress_health_check=_suppress,
        database=None,
    )
    settings.register_profile(
        "nightly",
        max_examples=500,
        stateful_step_count=50,
        deadline=None,
        suppress_health_check=_suppress,
        database=None,
    )
    settings.load_profile(os.environ.get("HYPOTHESIS_PROFILE", "ci"))
except ImportError:  # pragma: no cover - hypothesis is a declared dependency
    pass


@pytest.fixture
def adapter() -> FakeAdapter:
    """An in-memory recorder with one measurement sensor and three readings."""
    fake = FakeAdapter()
    fake.add_entity("sensor.living_room_temperature", SensorType.MEASUREMENT)
    fake.add_state("sensor.living_room_temperature", 1, SEED_TIMESTAMPS[0], "20.1")
    fake.add_state("sensor.living_room_temperature", 2, SEED_TIMESTAMPS[1], "-2000.0")
    fake.add_state("sensor.living_room_temperature", 3, SEED_TIMESTAMPS[2], "20.3")
    return fake


@pytest.fixture
def app(adapter: FakeAdapter, tmp_path: Any) -> Any:
    """A Flask app wired to the fake adapter, with onboarding already done."""
    from hr_config import AppConfig, DatabaseConfig
    from hr_web import create_app

    config = AppConfig(
        db_type="mariadb",
        database=DatabaseConfig(
            host="localhost", port=3306, name="homeassistant", user="ha", password=""
        ),
        sqlite=None,
        log_level="INFO",
        state_dir=str(tmp_path),
        port=8099,
        time_format="24",
    )
    application = create_app(config, adapter=adapter)
    application.extensions["hr_state"].update(onboarding_complete=True)
    application.config.update(TESTING=True)
    return application


@pytest.fixture
def client(app: Any) -> Any:
    return app.test_client()


@pytest.fixture
def fresh_app(adapter: FakeAdapter, tmp_path: Any) -> Any:
    """Same app, but with onboarding not yet completed."""
    from hr_config import AppConfig, DatabaseConfig
    from hr_web import create_app

    config = AppConfig(
        db_type="mariadb",
        database=DatabaseConfig(
            host="localhost", port=3306, name="homeassistant", user="ha", password=""
        ),
        sqlite=None,
        log_level="INFO",
        state_dir=str(tmp_path),
        port=8099,
        time_format="24",
    )
    application = create_app(config, adapter=adapter)
    application.config.update(TESTING=True)
    return application
