"""Corrected command lines for refused writes.

A refused write changed nothing, so it is safe to offer the full corrected command. Each failed
check may carry an ``Edit``; the edits are applied to the caller's own argument list, so long
``--input`` content is referenced by its source rather than echoed. Placeholders appear only where
the caller must supply content. Writes whose outcome is unknown or partial never get a retry.
"""

from __future__ import annotations

import shlex
from dataclasses import dataclass

PROGRAM = "yojo"
POSIX_NOTE = (
    "Commands are quoted for POSIX shells (sh, bash, zsh); in cmd.exe, PowerShell, or a program, "
    "pass the matching argv list as separate arguments."
)


@dataclass(frozen=True)
class Edit:
    """One change to the caller's arguments.

    ``set``: give the ``--set`` named ``name`` the value ``value`` (added if absent).
    ``rename``: keep the value but name the field ``value`` instead of ``name``.
    ``drop``: remove the ``--set`` named ``name``.
    ``flag``: give option ``name`` (for example ``--to``) the value ``value``.
    ``source`` is where the original value came from; values from ``--input`` cannot be edited
    on the command line, so no retry is offered for them. A ``set`` whose source is ``missing``
    only adds a field the command does not already name.
    """

    kind: str
    name: str
    value: str | None = None
    source: str = "flag"


def placeholder(what: str = "value") -> str:
    return f"<{what}>"


def _split_sets(argv: list[str]) -> list[tuple[int, str, str]]:
    """Locate each ``--set`` item: (index of the token holding NAME=VALUE, name, value)."""
    found: list[tuple[int, str, str]] = []
    index = 0
    while index < len(argv):
        word = argv[index]
        if word == "--":
            break
        if word == "--set" and index + 1 < len(argv):
            item, at = argv[index + 1], index + 1
            index += 2
        elif word.startswith("--set="):
            item, at = word[len("--set=") :], index
            index += 1
        else:
            index += 1
            continue
        name, _, value = item.partition("=")
        found.append((at, name, value))
    return found


def _set_token(argv: list[str], at: int, item: str) -> None:
    argv[at] = f"--set={item}" if argv[at].startswith("--set=") else item


def apply(argv: list[str], edits: list[Edit]) -> list[str] | None:
    """Return ``yojo`` plus ``argv`` with ``edits`` applied, or None if an edit is impossible."""
    out = list(argv)
    for edit in edits:
        if edit.source == "input":
            return None
        sets = {name.strip().casefold(): (at, name, value) for at, name, value in _split_sets(out)}
        hit = sets.get(edit.name.strip().casefold())
        if edit.kind == "flag":
            out = _with_flag(out, edit.name, edit.value or "")
        elif edit.kind == "set":
            if hit and edit.source == "missing":
                # Another edit (such as a corrected name) already supplies this field's value.
                continue
            item = f"{hit[1] if hit else edit.name}={edit.value or ''}"
            if hit:
                _set_token(out, hit[0], item)
            else:
                out = _insert(out, ["--set", item])
        elif edit.kind == "rename":
            if hit is None:
                return None
            _set_token(out, hit[0], f"{edit.value}={hit[2]}")
        elif edit.kind == "drop":
            if hit is None:
                return None
            at = hit[0]
            if out[at].startswith("--set="):
                del out[at]
            else:
                del out[at - 1 : at + 1]
        else:
            return None
    return [PROGRAM, *out]


def _insert(argv: list[str], tokens: list[str]) -> list[str]:
    """Insert before any ``--`` separator so the new option stays an option."""
    if "--" in argv:
        at = argv.index("--")
        return [*argv[:at], *tokens, *argv[at:]]
    return [*argv, *tokens]


def _with_flag(argv: list[str], flag: str, value: str) -> list[str]:
    out = list(argv)
    for index, word in enumerate(out):
        if word == "--":
            break
        if word == flag and index + 1 < len(out):
            out[index + 1] = value
            return out
        if word.startswith(flag + "="):
            out[index] = f"{flag}={value}"
            return out
    return _insert(out, [flag, value])


def posix(argv: list[str]) -> str:
    return shlex.join(argv)
