"""Tests for hr_state — the onboarding flag that must survive restarts.

The failure this module guards against is subtle: if the state file cannot be
written, the add-on must still serve its UI. Onboarding reappearing after a
restart is a small annoyance; a crash loop on a read-only /config is not.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from hr_state import STATE_FILENAME, AddonState


class TestPersistence:
    def test_starts_with_onboarding_incomplete(self, tmp_path: Path) -> None:
        assert AddonState(str(tmp_path)).onboarding_complete is False

    def test_survives_a_restart(self, tmp_path: Path) -> None:
        AddonState(str(tmp_path)).update(onboarding_complete=True)
        assert AddonState(str(tmp_path)).onboarding_complete is True

    def test_writes_a_readable_json_file(self, tmp_path: Path) -> None:
        AddonState(str(tmp_path)).update(onboarding_complete=True, backup_acknowledged_by="m")
        stored = json.loads((tmp_path / STATE_FILENAME).read_text(encoding="utf-8"))
        assert stored["onboarding_complete"] is True
        assert stored["backup_acknowledged_by"] == "m"

    def test_unknown_keys_in_the_file_are_preserved(self, tmp_path: Path) -> None:
        # A file written by a newer version must not lose its extra keys when
        # an older one saves over it.
        (tmp_path / STATE_FILENAME).write_text(
            json.dumps({"onboarding_complete": True, "future_key": 1}), encoding="utf-8"
        )
        state = AddonState(str(tmp_path))
        state.update(backup_acknowledged_by="m")
        stored = json.loads((tmp_path / STATE_FILENAME).read_text(encoding="utf-8"))
        assert stored["future_key"] == 1


class TestResilience:
    def test_a_corrupt_file_falls_back_to_defaults(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        (tmp_path / STATE_FILENAME).write_text("{not json", encoding="utf-8")
        with caplog.at_level("WARNING"):
            state = AddonState(str(tmp_path))
        assert state.onboarding_complete is False
        message = caplog.records[-1].getMessage()
        # Deliberately not just a prefix check: a corrupted log call that
        # drops the real exception (substitutes None, or drops the argument
        # entirely and leaves a literal "%s" unformatted) would still start
        # with the same fixed text.
        assert message.startswith("Could not read add-on state, starting fresh: ")
        assert "%s" not in message
        assert not message.endswith(": None")

    def test_a_non_dict_file_falls_back_to_defaults(self, tmp_path: Path) -> None:
        (tmp_path / STATE_FILENAME).write_text("[1, 2, 3]", encoding="utf-8")
        assert AddonState(str(tmp_path)).onboarding_complete is False

    def test_an_unwritable_directory_does_not_raise(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        state = AddonState(str(tmp_path / "does-not-exist"))
        with caplog.at_level("WARNING"):
            state.update(onboarding_complete=True)
        # The value is held in memory for this process even though the save
        # failed, so the user's session continues normally.
        assert state.onboarding_complete is True
        message = caplog.records[-1].getMessage()
        assert message.startswith("Could not persist add-on state: ")
        assert "%s" not in message
        assert not message.endswith(": None")

    def test_get_falls_back_to_the_declared_default(self, tmp_path: Path) -> None:
        state = AddonState(str(tmp_path))
        assert state.get("backup_acknowledged_at") is None
        assert state.get("nonexistent") is None

    def test_get_returns_the_real_value_for_a_known_key(self, tmp_path: Path) -> None:
        state = AddonState(str(tmp_path))
        state.update(onboarding_complete=True)
        assert state.get("onboarding_complete") is True

    def test_get_falls_back_to_the_keys_own_default_not_none(self, tmp_path: Path) -> None:
        # onboarding_complete's declared default is False, not None — the
        # only way to tell "fell back to _DEFAULTS" apart from "fell back to
        # a bare None" is a default that isn't None itself.
        state = AddonState(str(tmp_path))
        del state._data["onboarding_complete"]
        assert state.get("onboarding_complete") is False

    def test_the_saved_file_is_indented_for_humans(self, tmp_path: Path) -> None:
        AddonState(str(tmp_path)).update(onboarding_complete=True)
        raw = (tmp_path / STATE_FILENAME).read_text(encoding="utf-8")
        assert raw == json.dumps(
            {
                "onboarding_complete": True,
                "backup_acknowledged_at": None,
                "backup_acknowledged_by": None,
            },
            indent=2,
        )


class TestSnapshot:
    def test_as_dict_returns_a_copy(self, tmp_path: Path) -> None:
        state = AddonState(str(tmp_path))
        snapshot: dict[str, Any] = state.as_dict()
        snapshot["onboarding_complete"] = True
        assert state.onboarding_complete is False
