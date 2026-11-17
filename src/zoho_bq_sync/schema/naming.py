"""Identifier normalization for BigQuery.

BigQuery column names are case-insensitive, limited to 300 characters, and names with
a leading underscore are reserved here for pipeline metadata (_synced_at, _is_deleted)
and by BigQuery for pseudo-columns (_PARTITIONTIME, _TABLE_...).
"""

from __future__ import annotations

import re
from typing import Iterable

MAX_COLUMN_LENGTH = 300
_INVALID_CHARS = re.compile(r"[^a-z0-9_]")
_SAFE_IDENTIFIER = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


def normalize_identifier(name: str) -> str:
    value = _INVALID_CHARS.sub("_", name.strip().lower()).lstrip("_")
    if not value:
        value = "field"
    if value[0].isdigit():
        value = f"f_{value}"
    return value[:MAX_COLUMN_LENGTH]


def assert_safe_identifier(name: str) -> str:
    """Guard for every identifier interpolated into SQL (they cannot be query parameters)."""
    if not _SAFE_IDENTIFIER.match(name) or len(name) > 1024:
        raise ValueError(f"Unsafe SQL identifier: {name!r}")
    return name


class NameAllocator:
    """Deterministic, collision-free column names.

    Fields are processed in a stable order (by Zoho field id), so the first field keeps the
    plain name and a later colliding field gets a suffix derived from its own field id,
    which does not change between runs.
    """

    def __init__(self, reserved: Iterable[str] = ()) -> None:
        self._taken = {r.lower() for r in reserved}

    def allocate(self, desired: str, disambiguator: str) -> str:
        name = desired
        if name in self._taken:
            name = f"{desired}_{disambiguator}"[:MAX_COLUMN_LENGTH]
            counter = 2
            while name in self._taken:
                name = f"{desired}_{disambiguator}_{counter}"[:MAX_COLUMN_LENGTH]
                counter += 1
        self._taken.add(name)
        return name
