"""Persistent add-on state: whether onboarding has been completed.

Kept in the Home Assistant configuration directory so it survives restarts,
and deliberately not in the recorder database — this is tool state, not user
history, and it must remain readable even when the database is unreachable.
"""

from __future__ import annotations

import json
import logging
import os
import threading
from typing import Any

_LOGGER = logging.getLogger(__name__)

STATE_FILENAME = "history_repair_state.json"

_DEFAULTS: dict[str, Any] = {
    "onboarding_complete": False,
    "backup_acknowledged_at": None,
    "backup_acknowledged_by": None,
}


class AddonState:
    """A tiny JSON-backed key/value store, safe to use from several threads."""

    def __init__(self, state_dir: str) -> None:
        self._path = os.path.join(state_dir, STATE_FILENAME)
        self._lock = threading.Lock()
        self._data: dict[str, Any] = dict(_DEFAULTS)
        self._load()

    def _load(self) -> None:
        try:
            with open(self._path, encoding="utf-8") as handle:
                stored = json.load(handle)
        except FileNotFoundError:
            return
        except (OSError, ValueError) as err:
            _LOGGER.warning("Could not read add-on state, starting fresh: %s", err)
            return
        if isinstance(stored, dict):
            self._data.update(stored)

    def _save(self) -> None:
        tmp = f"{self._path}.tmp"
        try:
            with open(tmp, "w", encoding="utf-8") as handle:
                json.dump(self._data, handle, indent=2)
            os.replace(tmp, self._path)
        except OSError as err:
            # A read-only or missing state directory must not take the UI
            # down; onboarding simply reappears after the next restart.
            _LOGGER.warning("Could not persist add-on state: %s", err)

    def get(self, key: str) -> Any:
        return self._data.get(key, _DEFAULTS.get(key))

    def update(self, **values: Any) -> None:
        with self._lock:
            self._data.update(values)
            self._save()

    @property
    def onboarding_complete(self) -> bool:
        return bool(self._data.get("onboarding_complete"))

    def as_dict(self) -> dict[str, Any]:
        return dict(self._data)
