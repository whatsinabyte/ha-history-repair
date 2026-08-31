"""Value-parsing helpers shared by every concrete database adapter.

These are not SQL — they are pure Python functions over values a query
returns or a search box supplies — so they live outside any one adapter and
are imported by each of them (hr_mariadb.py, hr_sqlite.py). Every actual SQL
string still lives inside a concrete adapter; nothing here assumes a dialect.
"""

from __future__ import annotations

from typing import Any


def to_float(value: Any) -> float | None:
    """Parse a states.state string, which may be 'unknown', 'unavailable', ''."""
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def escape_like(term: str) -> str:
    """Escape the wildcards a user's search term must not smuggle into LIKE."""
    return term.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
