"""Field resolution and documented value serializers, validated against live Jira metadata."""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

from atlass_cli.config import FieldRef
from atlass_cli.contracts import ISSUE_KEY_PATTERN

ISSUE_KEY_RE = re.compile(ISSUE_KEY_PATTERN)
MAX_LISTED_VALUES = 25


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


class FieldProblem(Exception):
    def __init__(self, message: str, fix: str, details: dict[str, Any] | None = None) -> None:
        super().__init__(message)
        self.message = message
        self.fix = fix
        self.details = details or {}


def resolve(ref: FieldRef, pool: dict[str, FieldMeta]) -> FieldMeta | None:
    """Return the one live field matching ``ref``; raise if the name is ambiguous."""
    if ref.id is not None:
        return pool.get(ref.id)
    matches = [meta for meta in pool.values() if meta.name == ref.jira_name]
    if len(matches) > 1:
        ids = sorted(meta.id for meta in matches)
        raise FieldProblem(
            f"Field name {ref.jira_name!r} matches {len(matches)} Jira fields: {', '.join(ids)}.",
            f"Replace jira_name with an explicit id (one of {', '.join(ids)}) for field "
            f"{ref.friendly!r} in atl.yaml.",
            {"friendly": ref.friendly, "candidates": ids},
        )
    return matches[0] if matches else None


def _option_label(option: dict[str, Any], key: str) -> str | None:
    value = option.get(key)
    return value if isinstance(value, str) else None


def _enabled_options(meta: FieldMeta) -> list[dict[str, Any]]:
    return [o for o in meta.allowed_values or [] if o.get("disabled") is not True]


def _pick(meta: FieldMeta, ref: FieldRef, wanted: str, label_key: str) -> dict[str, str]:
    options = _enabled_options(meta)
    matches = [o for o in options if _option_label(o, label_key) == wanted]
    if len(matches) != 1 or not isinstance(matches[0].get("id"), str):
        allowed = [label for o in options if (label := _option_label(o, label_key))]
        raise FieldProblem(
            f"{wanted!r} is not an allowed value for {meta.name!r} ({meta.id}).",
            f"Use one of the allowed values for {ref.friendly!r}: "
            f"{', '.join(allowed[:MAX_LISTED_VALUES]) or '(none exposed)'}.",
            {"friendly": ref.friendly, "field_id": meta.id, "allowed": allowed[:MAX_LISTED_VALUES]},
        )
    return {"id": matches[0]["id"]}


def _wrong_schema(meta: FieldRef | Any, field: FieldMeta, expected: str) -> FieldProblem:
    return FieldProblem(
        f"Field {field.name!r} ({field.id}) has schema {field.schema or '{}'}, which does not "
        f"support encoding {meta.encoding!r}.",
        f"Change the encoding of {meta.friendly!r} in atl.yaml to match a {expected} field, or "
        "map it to a different Jira field.",
        {"friendly": meta.friendly, "field_id": field.id, "schema": field.schema},
    )


def serialize(ref: FieldRef, meta: FieldMeta, value: Any, *, require_set: bool) -> Any:
    """Return the Jira REST payload for ``value`` or raise ``FieldProblem``."""
    if require_set and (meta.operations is None or "set" not in meta.operations):
        raise FieldProblem(
            f"Field {meta.name!r} ({meta.id}) does not allow the 'set' operation here.",
            f"Ask a Jira admin to make {meta.name!r} editable on this issue type, or remove it "
            "from auto_set.",
            {"friendly": ref.friendly, "field_id": meta.id, "operations": meta.operations},
        )
    if meta.operations is not None and "set" not in meta.operations:
        raise FieldProblem(
            f"Field {meta.name!r} ({meta.id}) cannot be set on this screen.",
            f"Ask a Jira admin to allow setting {meta.name!r}, or stop supplying {ref.friendly!r}.",
            {"friendly": ref.friendly, "field_id": meta.id, "operations": meta.operations},
        )

    schema_type = meta.schema.get("type")
    encoding = ref.encoding

    if encoding == "multi_option":
        if schema_type != "array" or meta.schema.get("items") != "option":
            raise _wrong_schema(ref, meta, "multi-select option")
        if not isinstance(value, list) or not value or not all(isinstance(v, str) for v in value):
            raise FieldProblem(
                f"{ref.friendly!r} needs a non-empty list of option values.",
                f"Supply {ref.friendly!r} as a list of strings.",
                {"friendly": ref.friendly},
            )
        return [_pick(meta, ref, v, "value") for v in value]

    if not isinstance(value, str) or not value.strip():
        raise FieldProblem(
            f"{ref.friendly!r} needs a non-empty string value.",
            f"Supply a non-empty string for {ref.friendly!r}.",
            {"friendly": ref.friendly},
        )

    if encoding == "text":
        if schema_type != "string" or meta.allowed_values:
            raise _wrong_schema(ref, meta, "free-text string")
        return value
    if encoding == "issue_key_string":
        if schema_type not in ("string", "any") or meta.allowed_values:
            raise _wrong_schema(ref, meta, "issue-key string")
        _require_key(ref, value)
        return value
    if encoding == "issue_key_object":
        if schema_type not in ("issuelink", "issue"):
            raise _wrong_schema(ref, meta, "issue link")
        _require_key(ref, value)
        return {"key": value}
    if encoding == "single_option":
        if schema_type != "option" or meta.allowed_values is None:
            raise _option_unavailable(ref, meta, "single-select option")
        return _pick(meta, ref, value, "value")
    if encoding == "resolution_option":
        if schema_type != "resolution" or meta.allowed_values is None:
            raise _option_unavailable(ref, meta, "resolution")
        return _pick(meta, ref, value, "name")
    raise FieldProblem(
        f"Unsupported encoding {encoding!r}.",
        "Use a supported encoding; see docs/configuration.md.",
        {"friendly": ref.friendly},
    )


def _option_unavailable(ref: FieldRef, meta: FieldMeta, expected: str) -> FieldProblem:
    if meta.schema.get("type") in ("option", "resolution"):
        return FieldProblem(
            f"Jira did not expose allowed values for {meta.name!r} ({meta.id}); atl cannot "
            "establish a safe encoding.",
            f"Ask a Jira admin to expose {meta.name!r} on this screen with its options, or "
            f"remove {ref.friendly!r} from the request.",
            {"friendly": ref.friendly, "field_id": meta.id},
        )
    return _wrong_schema(ref, meta, expected)


def _require_key(ref: FieldRef, value: str) -> None:
    if not ISSUE_KEY_RE.fullmatch(value):
        raise FieldProblem(
            f"{value!r} is not a Jira issue key.",
            f"Supply {ref.friendly!r} as an issue key such as ABC-123.",
            {"friendly": ref.friendly},
        )
