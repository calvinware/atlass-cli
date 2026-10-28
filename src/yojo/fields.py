"""Field resolution and value coercion driven by live Jira field metadata.

A caller names a field the way Jira shows it, by a ``yojo.yaml`` alias, or by its raw ID, and
gives a plain value. The live schema decides the REST shape; unsupported schemas fail closed.
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from yojo.config import FieldRef
from yojo.contracts import ISSUE_KEY_PATTERN
from yojo.resolve import Candidate, Unresolved, UserLookup, pick
from yojo.transport import sanitize_text

ISSUE_KEY_RE = re.compile(ISSUE_KEY_PATTERN)
DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
INT_RE = re.compile(r"^[+-]?\d{1,15}$")
MAX_LISTED_VALUES = 25
MAX_VALUE_ITEMS = 50
# Set by the command line itself, never through --set.
SYSTEM_FIELDS = {
    "project": "the PROJECT argument",
    "issuetype": "--type",
    "summary": "--summary",
}
OPTION_TYPES = {"option", "priority", "resolution", "version", "component", "securitylevel"}
ARRAY_ITEM_TYPES = {"option", "version", "component", "string", "user"}


@dataclass(frozen=True)
class FieldMeta:
    id: str
    name: str
    required: bool
    has_default: bool
    schema: dict[str, Any]
    allowed_values: list[dict[str, Any]] | None
    operations: list[str] | None

    @classmethod
    def parse(cls, field_id: str, raw: dict[str, Any]) -> FieldMeta:
        allowed = raw.get("allowedValues")
        operations = raw.get("operations")
        schema = raw.get("schema")
        return cls(
            id=field_id,
            name=str(raw.get("name") or field_id),
            required=raw.get("required") is True,
            has_default=raw.get("hasDefaultValue") is True,
            schema=schema if isinstance(schema, dict) else {},
            allowed_values=[v for v in allowed if isinstance(v, dict)]
            if isinstance(allowed, list)
            else None,
            operations=[str(o) for o in operations] if isinstance(operations, list) else None,
        )

    @property
    def shown(self) -> str:
        return self.name if self.name == self.id else f"{self.name} ({self.id})"

    @property
    def is_array(self) -> bool:
        return self.schema.get("type") == "array"


class FieldProblem(Exception):
    """A value cannot be used. ``status`` is ``fail`` or ``unverified`` (metadata missing)."""

    def __init__(
        self,
        message: str,
        fix: str,
        details: dict[str, Any] | None = None,
        *,
        status: str = "fail",
        replacement: str | None = None,
    ) -> None:
        super().__init__(message)
        self.message = message
        self.fix = fix
        self.details = details or {}
        self.status = status
        self.replacement = replacement


# --------------------------------------------------------------------------- resolution


def _field_candidates(pool: dict[str, FieldMeta]) -> list[Candidate]:
    """A field is picked by its name when no other field shares it (any case), else by ID."""
    counts: dict[str, int] = {}
    for meta in pool.values():
        counts[meta.name.casefold()] = counts.get(meta.name.casefold(), 0) + 1
    return [
        Candidate(
            label=m.name,
            value=m,
            names=(m.id,),
            token=m.name if counts[m.name.casefold()] == 1 else m.id,
        )
        for m in pool.values()
    ]


def resolve(
    given: str,
    pool: dict[str, FieldMeta],
    aliases: dict[str, FieldRef],
    scope: str = "",
) -> FieldMeta:
    """Resolve a raw ID, a configured alias, or a Jira field name (any case) within ``pool``.

    Raises ``Unresolved`` when nothing or more than one field matches.
    """
    wanted = given.strip()
    folded = wanted.casefold()
    by_id = {fid.casefold(): meta for fid, meta in pool.items()}
    if folded in by_id:
        return by_id[folded]
    alias = next((ref for name, ref in aliases.items() if name.casefold() == folded), None)
    if alias is not None:
        if alias.id is not None:
            meta = pool.get(alias.id)
            if meta is None:
                raise Unresolved("not_found", "field", f"{wanted} ({alias.id})", [], scope=scope)
            return meta
        assert alias.jira_name is not None
        return pick(alias.jira_name, _field_candidates(pool), "field", scope).value  # type: ignore[no-any-return]
    candidates = _field_candidates(pool)
    candidates += [
        Candidate(label=name, value=pool[ref.id], token=name)
        for name, ref in aliases.items()
        if ref.id is not None and ref.id in pool
    ]
    chosen = pick(wanted, candidates, "field", scope)
    return chosen.value  # type: ignore[no-any-return]


def unresolved_problem(exc: Unresolved) -> FieldProblem:
    return FieldProblem(exc.message, exc.fix, exc.details(), replacement=exc.replacement)


# --------------------------------------------------------------------------- coercion


def _labels(option: dict[str, Any]) -> tuple[str, ...]:
    return tuple(str(option[k]) for k in ("value", "name") if isinstance(option.get(k), str))


def _enabled_options(meta: FieldMeta) -> list[dict[str, Any]]:
    return [o for o in meta.allowed_values or [] if o.get("disabled") is not True]


def allowed_labels(meta: FieldMeta) -> list[str]:
    return [labels[0] for o in _enabled_options(meta) if (labels := _labels(o))]


def _pick_option(meta: FieldMeta, wanted: str) -> dict[str, str]:
    options = [
        Candidate(label=labels[0], value=o, names=labels[1:])
        for o in _enabled_options(meta)
        if (labels := _labels(o)) and isinstance(o.get("id"), str)
    ]
    try:
        chosen = pick(wanted, options, "value", f"for {meta.name}")
    except Unresolved as exc:
        allowed = [c.label for c in options[:MAX_LISTED_VALUES]]
        fix = (
            exc.fix
            if exc.kind == "ambiguous" or exc.suggestion
            else (
                f"Use one of the allowed values for {meta.name}: "
                f"{', '.join(repr(a) for a in allowed) or '(none exposed)'}."
            )
        )
        raise FieldProblem(
            exc.message,
            fix,
            {"field_id": meta.id, "allowed": allowed, **exc.details()},
            replacement=exc.replacement,
        ) from None
    return {"id": str(chosen.value["id"])}


def _unsupported(meta: FieldMeta) -> FieldProblem:
    return FieldProblem(
        f"{meta.shown} has a Jira type ({meta.schema.get('type') or 'unknown'}"
        f"{'/' + str(meta.schema['items']) if meta.schema.get('items') else ''}) that yojo cannot "
        "set safely.",
        f"Set {meta.name} in the Jira issue view, or leave it out of this command.",
        {"field_id": meta.id, "schema": meta.schema},
    )


def _missing_options(meta: FieldMeta) -> FieldProblem:
    return FieldProblem(
        f"Jira did not list the allowed values for {meta.shown}, so yojo cannot pick one safely.",
        f"Ask a Jira admin to show {meta.name} with its options on this screen, or leave it out.",
        {"field_id": meta.id},
        status="unverified",
    )


def _as_list(meta: FieldMeta, value: Any) -> list[str]:
    if isinstance(value, list):
        items = [str(v).strip() for v in value]
    elif isinstance(value, str):
        items = [part.strip() for part in value.split(",")]
    else:
        items = [str(value)]
    items = [i for i in items if i]
    if not items or len(items) > MAX_VALUE_ITEMS:
        raise FieldProblem(
            f"{meta.name} needs 1-{MAX_VALUE_ITEMS} values.",
            f"Pass {meta.name} as a comma-separated list, e.g. --set '{meta.name}=a,b'.",
            {"field_id": meta.id},
        )
    return items


def _as_text(meta: FieldMeta, value: Any) -> str:
    if isinstance(value, (bool, list)) or value is None:
        raise FieldProblem(
            f"{meta.name} needs a single text value.",
            f"Pass one value, e.g. --set '{meta.name}=...'.",
            {"field_id": meta.id},
        )
    text = str(value)
    if not text.strip():
        raise FieldProblem(
            f"{meta.name} is blank.",
            f"Give {meta.name} a non-blank value, or leave it out.",
            {"field_id": meta.id},
        )
    return text


def _number(meta: FieldMeta, value: Any) -> int | float:
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        number: int | float = value
    else:
        text = _as_text(meta, value).strip()
        try:
            number = int(text) if INT_RE.fullmatch(text) else float(text)
        except ValueError:
            raise FieldProblem(
                f"{meta.name} needs a number, not {sanitize_text(text, 40)!r}.",
                f"Pass a number, e.g. --set '{meta.name}=3'.",
                {"field_id": meta.id},
            ) from None
    if isinstance(number, float) and not math.isfinite(number):
        raise FieldProblem(
            f"{meta.name} needs a finite number.",
            f"Pass a number, e.g. --set '{meta.name}=3'.",
            {"field_id": meta.id},
        )
    return number


def _datetime(meta: FieldMeta, text: str) -> str:
    try:
        datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        raise FieldProblem(
            f"{meta.name} needs a date and time such as 2026-10-28T09:30:00.000+0000.",
            f"Pass {meta.name} in ISO 8601 form with a UTC offset.",
            {"field_id": meta.id},
        ) from None
    return text


def _issue_key(meta: FieldMeta, text: str) -> str:
    key = text.strip()
    if not ISSUE_KEY_RE.fullmatch(key):
        raise FieldProblem(
            f"{meta.name} needs an issue key such as PROJ-123, not {sanitize_text(text, 40)!r}.",
            f"Pass an issue key, e.g. --set '{meta.name}=PROJ-123'.",
            {"field_id": meta.id},
        )
    return key


def _user(meta: FieldMeta, text: str, users: UserLookup) -> dict[str, Any]:
    try:
        return users(text)
    except Unresolved as exc:
        exc.scope = f"for {meta.name}"
        raise FieldProblem(
            exc.message,
            exc.fix,
            {"field_id": meta.id, **exc.details()},
            replacement=exc.replacement,
        ) from None


def coerce(meta: FieldMeta, value: Any, users: UserLookup) -> Any:
    """Return the Jira REST value for ``value`` in ``meta``, or raise ``FieldProblem``."""
    if meta.operations is not None and "set" not in meta.operations:
        raise FieldProblem(
            f"{meta.shown} cannot be set on this screen.",
            f"Ask a Jira admin to allow setting {meta.name} here, or leave it out.",
            {"field_id": meta.id, "operations": meta.operations},
        )
    kind = meta.schema.get("type")
    custom = str(meta.schema.get("custom") or "")
    if kind == "array":
        items_kind = meta.schema.get("items")
        if items_kind not in ARRAY_ITEM_TYPES:
            raise _unsupported(meta)
        items = _as_list(meta, value)
        if items_kind == "string":
            if meta.id == "labels" and any(any(ch.isspace() for ch in i) for i in items):
                raise FieldProblem(
                    "Labels cannot contain spaces.",
                    "Use labels without spaces, e.g. --set 'Labels=ui,login-flow'.",
                    {"field_id": meta.id},
                )
            return items
        if items_kind == "user":
            return [_user(meta, i, users) for i in items]
        if meta.allowed_values is None:
            raise _missing_options(meta)
        return [_pick_option(meta, i) for i in items]
    if kind in OPTION_TYPES:
        if "cascadingselect" in custom:
            raise _unsupported(meta)
        if meta.allowed_values is None:
            raise _missing_options(meta)
        return _pick_option(meta, _as_text(meta, value).strip())
    if kind == "number":
        return _number(meta, value)
    if kind == "boolean":
        if isinstance(value, bool):
            return value
        if isinstance(value, str) and value.strip().casefold() in ("true", "false"):
            return value.strip().casefold() == "true"
        raise FieldProblem(
            f"{meta.name} needs true or false.",
            f"Pass --set '{meta.name}=true' or --set '{meta.name}=false'.",
            {"field_id": meta.id},
        )
    if kind == "string":
        if meta.allowed_values:
            raise _unsupported(meta)
        return _as_text(meta, value)
    if kind == "date":
        text = _as_text(meta, value).strip()
        if not DATE_RE.fullmatch(text):
            raise FieldProblem(
                f"{meta.name} needs a date such as 2026-10-28.",
                f"Pass {meta.name} as YYYY-MM-DD.",
                {"field_id": meta.id},
            )
        return text
    if kind == "datetime":
        return _datetime(meta, _as_text(meta, value).strip())
    if kind == "user":
        return _user(meta, _as_text(meta, value), users)
    if kind == "issuelink" or kind == "issue":
        return {"key": _issue_key(meta, _as_text(meta, value))}
    if kind == "any" and ("epic-link" in custom or "parent" in custom):
        return _issue_key(meta, _as_text(meta, value))
    raise _unsupported(meta)


# --------------------------------------------------------------------------- read back


def _comparable(value: Any) -> Any:
    if isinstance(value, dict):
        for key in ("id", "name", "key", "value"):
            if isinstance(value.get(key), str):
                return value[key]
        return None
    if isinstance(value, str):
        return value.replace("\r\n", "\n").rstrip()
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return float(value)
    return value


def _comparable_datetime(value: Any) -> Any:
    if not isinstance(value, str):
        return None
    text = value.replace("Z", "+00:00")
    if re.search(r"[+-]\d{4}$", text):
        text = text[:-2] + ":" + text[-2:]
    try:
        return datetime.fromisoformat(text)
    except ValueError:
        return None


def confirmed(meta: FieldMeta, sent: Any, current: Any) -> bool:
    """True when the value Jira now reports is the value that was sent."""
    if meta.schema.get("type") == "datetime":
        expected = _comparable_datetime(sent)
        return expected is not None and expected == _comparable_datetime(current)
    if isinstance(sent, list):
        if not isinstance(current, list):
            return False
        return sorted(map(str, map(_comparable, sent))) == sorted(
            map(str, map(_comparable, current))
        )
    if isinstance(sent, dict) and isinstance(current, dict):
        # A user is sent by name; Jira may answer with name and key.
        return any(_comparable(sent) == current.get(k) for k in ("id", "name", "key", "value"))
    return bool(_comparable(sent) == _comparable(current))


# --------------------------------------------------------------------------- display


def simplify(value: Any, depth: int = 0) -> Any:
    """Reduce a Jira field value to the plain words a person sees."""
    if value is None or isinstance(value, (bool, int, float)):
        return value
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        return [simplify(v, depth + 1) for v in value[:MAX_VALUE_ITEMS]]
    if isinstance(value, dict) and depth < 3:
        for key in ("displayName", "value", "name", "key"):
            if isinstance(value.get(key), str):
                return value[key]
        return {str(k): simplify(v, depth + 1) for k, v in list(value.items())[:20]}
    return sanitize_text(value, 200)
