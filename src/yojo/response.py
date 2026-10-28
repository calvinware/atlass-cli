"""Safe accessors for untrusted Jira, Confluence, and Bitbucket response values."""

from __future__ import annotations

import json
from typing import Any

from yojo.contracts import MAX_TEXT_CHARS

# Encoded JSON bytes all trimmed text in one read response may use; non-ASCII text is escaped
# (``\uXXXX``) in JSON output, so a character budget alone would not bound the response size.
MAX_TEXT_BYTES = 2400


def nested_str(value: Any, key: str = "name") -> str | None:
    """Return ``value[key]`` when ``value`` is an object and that entry is a string."""
    if isinstance(value, dict) and isinstance(value.get(key), str):
        return str(value[key])
    return None


def encoded_size(value: Any) -> int:
    return len(json.dumps(value, ensure_ascii=True))


class TextBudget:
    """Trim long values so one read response stays small; ``full`` disables trimming."""

    def __init__(self, full: bool, limit: int = MAX_TEXT_BYTES) -> None:
        self.full = full
        self.left = limit

    def fit(self, value: Any) -> tuple[Any, bool]:
        """Return ``value`` or a trimmed string prefix of it, and whether it was trimmed."""
        if self.full or value is None:
            return value, False
        text = value if isinstance(value, str) else None
        size = encoded_size(value)
        if text is not None and len(text) <= MAX_TEXT_CHARS and size <= self.left:
            self.left -= size
            return value, False
        if text is None:
            if size <= self.left:
                self.left -= size
                return value, False
            text = json.dumps(value, ensure_ascii=False)
        kept, used = [], 2
        for char in text[:MAX_TEXT_CHARS]:
            cost = encoded_size(char) - 2
            if used + cost > self.left:
                break
            kept.append(char)
            used += cost
        self.left -= used
        return "".join(kept), True
