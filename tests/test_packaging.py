"""Tests that keep the add-on manifest, the code, and the docs in step.

None of these exercise behaviour; they exist because config.yaml, the
translations, and the Python constants are three hand-maintained copies of the
same facts, and nothing else notices when one drifts.
"""

from __future__ import annotations

import os
from typing import Any

import pytest
import yaml

import history_repair

_REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


@pytest.fixture(scope="module")
def config() -> dict[str, Any]:
    with open(os.path.join(_REPO, "config.yaml"), encoding="utf-8") as handle:
        loaded: dict[str, Any] = yaml.safe_load(handle)
    return loaded


@pytest.fixture(scope="module")
def translations() -> dict[str, Any]:
    with open(os.path.join(_REPO, "translations", "en.yaml"), encoding="utf-8") as handle:
        loaded: dict[str, Any] = yaml.safe_load(handle)
    return loaded


class TestVersionParity:
    def test_addon_version_matches_the_manifest(self, config: dict[str, Any]) -> None:
        assert config["version"] == history_repair.ADDON_VERSION


class TestVendoredAssets:
    def test_no_vendored_script_references_a_sourcemap_that_is_not_shipped(self) -> None:
        # chart.umd.min.js as downloaded carries a trailing
        # `//# sourceMappingURL=chart.umd.js.map` comment, but the .map file
        # itself was never vendored alongside it (it is large and only useful
        # for debugging Chart.js's own source, not this add-on's) — every
        # browser DevTools session was requesting it and logging a 404 for a
        # file that was never going to exist. Guards against a future
        # `vendor` update silently reintroducing the same reference.
        vendor_dir = os.path.join(_REPO, "app", "static", "vendor")
        for name in os.listdir(vendor_dir):
            if not name.endswith(".js"):
                continue
            with open(os.path.join(vendor_dir, name), encoding="utf-8") as handle:
                content = handle.read()
            assert "sourceMappingURL" not in content, f"{name} references a sourcemap"


class TestManifest:
    def test_is_served_through_ingress(self, config: dict[str, Any]) -> None:
        # Ingress is what puts Home Assistant's authentication in front of a
        # tool that can rewrite the recorder database. Publishing a plain port
        # instead would expose it to everyone on the LAN.
        assert config["ingress"] is True
        assert config["ingress_port"] == 8099

    def test_publishes_no_lan_port(self, config: dict[str, Any]) -> None:
        assert "ports" not in config

    def test_is_admin_only(self, config: dict[str, Any]) -> None:
        assert config["panel_admin"] is True

    def test_offers_every_backend_the_app_can_actually_build(self, config: dict[str, Any]) -> None:
        # These are the three Home Assistant's own recorder supports, and
        # build_adapter has a branch for each. A db_type offered here with no
        # adapter behind it would install fine and fail at connect time.
        assert config["schema"]["db_type"] == "list(sqlite|mariadb|postgres)"

    def test_the_default_port_is_the_backend_agnostic_sentinel(
        self, config: dict[str, Any]
    ) -> None:
        # 0 means "whichever port is standard for db_type" (hr_config.py's
        # _DEFAULT_PORTS). A literal 3306 here would silently point a
        # PostgreSQL user at MySQL's port.
        assert config["options"]["db_port"] == 0

    def test_is_granted_the_home_assistant_core_api(self, config: dict[str, Any]) -> None:
        # hr_ha_api.py authenticates against ws://supervisor/core/websocket
        # with the SUPERVISOR_TOKEN every add-on receives — but that token is
        # only valid there when this grant is present. Without it, Home
        # Assistant rejects the token (hr_ha_api._authenticate's own error
        # message anticipates exactly this), and refresh_statistics_cache can
        # never succeed on a real installation, silently falling back to
        # "restart Home Assistant to see the change" every time.
        assert config["homeassistant_api"] is True

    def test_wants_a_discoverable_mariadb_service(self, config: dict[str, Any]) -> None:
        # hr_discovery.py queries Supervisor's own API for a "mysql" service
        # (typically the official MariaDB add-on) at startup — this
        # declaration is what grants read access to it. "want", not "need":
        # SQLite is the default and MariaDB works fine from manual
        # configuration alone, so this add-on must be able to start with no
        # such service present.
        assert "mysql:want" in config["services"]

    def test_wants_a_discoverable_mqtt_broker(self, config: dict[str, Any]) -> None:
        # hr_mqtt.py publishes the "needs review" entity through whatever
        # broker this discovers (typically the official Mosquitto add-on).
        # "want", not "need": this add-on works identically with no MQTT
        # broker present at all.
        assert "mqtt:want" in config["services"]

    def test_has_a_watchdog_pointed_at_its_own_health_endpoint(
        self, config: dict[str, Any]
    ) -> None:
        # Lets Supervisor restart the add-on automatically if it stops
        # responding, using the /api/health endpoint that already exists for
        # exactly this. [PORT:8099] must match ingress_port so the URL
        # gunicorn actually binds to.
        assert config["watchdog"] == "http://[HOST]:[PORT:8099]/api/health"
        assert config["ingress_port"] == 8099

    def test_the_ingress_port_matches_the_default_the_app_binds(
        self, config: dict[str, Any], monkeypatch: Any
    ) -> None:
        from hr_config import AppConfig

        monkeypatch.delenv("HR_PORT", raising=False)
        assert AppConfig.from_env().port == config["ingress_port"]

    def test_maps_its_own_directory_and_the_ha_config_directory(
        self, config: dict[str, Any]
    ) -> None:
        # Phase 1 mapped only app_config (then still named addon_config —
        # Supervisor renamed it later; see CLAUDE.md's "Known departures"),
        # because the add-on wrote one small JSON file and MariaDB is reached
        # over TCP. Phase 3 added SQLite support, and around 80% of installs
        # use SQLite by default — a single file, not a server, so reaching it
        # means mounting the directory it lives in. Supervisor's map: list is
        # fixed at install time and cannot depend on the db_type option, so
        # both grants are requested unconditionally rather than splitting
        # this add-on into a MariaDB-only and a SQLite-capable listing. See
        # DOCS.md's Security section and ha-history-repair-design.md's
        # SQLite notes for the full reasoning; this is a deliberate
        # widening, not a regression.
        assert set(config["map"]) == {"app_config:rw", "homeassistant_config:rw"}


class TestOptionsSchemaParity:
    def test_every_option_has_a_schema_entry(self, config: dict[str, Any]) -> None:
        assert set(config["options"]) == set(config["schema"])

    def test_every_option_is_documented_in_english(
        self, config: dict[str, Any], translations: dict[str, Any]
    ) -> None:
        assert set(config["options"]) == set(translations["configuration"])

    def test_every_translation_has_a_name_and_description(
        self, translations: dict[str, Any]
    ) -> None:
        for key, entry in translations["configuration"].items():
            assert entry.get("name"), f"{key} has no name"
            assert entry.get("description"), f"{key} has no description"

    def test_the_password_option_uses_the_password_type(self, config: dict[str, Any]) -> None:
        # Supervisor masks and encrypts options declared as password; a plain
        # str would show the recorder credentials in the add-on UI.
        assert config["schema"]["db_password"] == "password"


@pytest.fixture(scope="module")
def run_sh() -> str:
    with open(os.path.join(_REPO, "run.sh"), encoding="utf-8") as handle:
        return handle.read()


class TestRunScriptParity:
    def test_exports_every_configured_option(self, run_sh: str, config: dict[str, Any]) -> None:
        # run.sh is the only bridge between options.json and the environment
        # AppConfig reads, so a new option that is never exported would
        # silently fall back to its default.
        for option in config["options"]:
            assert f"HR_{option.upper()}=" in run_sh

    def test_binds_the_ingress_port(self, run_sh: str, config: dict[str, Any]) -> None:
        assert f"HR_PORT={config['ingress_port']}" in run_sh
