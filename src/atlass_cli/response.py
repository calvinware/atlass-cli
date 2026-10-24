"""Safe accessors for untrusted Jira and Confluence response values."""

from __future__ import annotations

from typing import Any


def nested_str(value: Any, key: str = "name") -> str | None:
    """Return ``value[key]`` when ``value`` is an object and that entry is a string."""
    if isinstance(value, dict) and isinstance(value.get(key), str):
        return str(value[key])
    return None
