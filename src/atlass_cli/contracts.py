"""Output envelope, error catalog, exit codes, and the offline command/schema registry."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import IntEnum
from typing import Any

SCHEMA_VERSION = 1

# `(?![\s\S])` is an absolute end in both Python and ECMAScript; `$` also matches before a
# trailing newline in Python, and `\Z` is not valid in ECMAScript JSON Schema patterns.
_END = r"(?![\s\S])"
ISSUE_KEY_PATTERN = r"^[A-Z][A-Z0-9_]*-[1-9][0-9]*" + _END
PROJECT_KEY_PATTERN = r"^[A-Z][A-Z0-9_]*" + _END
PAGE_ID_PATTERN = r"^[1-9][0-9]{0,19}" + _END
SPACE_KEY_PATTERN = r"^~?[A-Za-z0-9_.@-]{1,255}" + _END

MAX_SEARCH_LIMIT = 100
MAX_QUERY_CHARS = 4000


class ExitCode(IntEnum):
    OK = 0
    INPUT = 2
    AUTH = 3
    REQUIREMENT = 4
    REMOTE = 5
    TRANSPORT = 6
    PARTIAL = 7


ERROR_CODES: dict[str, ExitCode] = {
    "usage_error": ExitCode.INPUT,
    "input_invalid": ExitCode.INPUT,
    "config_not_found": ExitCode.INPUT,
    "config_invalid": ExitCode.INPUT,
    "service_not_configured": ExitCode.INPUT,
    "project_not_configured": ExitCode.INPUT,
    "transition_not_configured": ExitCode.INPUT,
    "issue_key_mismatch": ExitCode.INPUT,
    "file_invalid": ExitCode.INPUT,
    "attachment_too_large": ExitCode.INPUT,
    "auth_missing": ExitCode.AUTH,
    "auth_failed": ExitCode.AUTH,
    "forbidden": ExitCode.AUTH,
    "keyring_unavailable": ExitCode.AUTH,
    "login_failed": ExitCode.AUTH,
    "requirements_unmet": ExitCode.REQUIREMENT,
    "approval_required": ExitCode.REQUIREMENT,
    "config_drift": ExitCode.REQUIREMENT,
    "config_unverified": ExitCode.REQUIREMENT,
    "remote_validation": ExitCode.REMOTE,
    "remote_limit_exceeded": ExitCode.REMOTE,
    "not_found": ExitCode.REMOTE,
    "conflict": ExitCode.REMOTE,
    "version_conflict": ExitCode.REMOTE,
    "transport_error": ExitCode.TRANSPORT,
    "tls_error": ExitCode.TRANSPORT,
    "redirect_refused": ExitCode.TRANSPORT,
    "service_error": ExitCode.TRANSPORT,
    "rate_limited": ExitCode.TRANSPORT,
    "response_invalid": ExitCode.TRANSPORT,
    "metadata_incomplete": ExitCode.TRANSPORT,
    "outcome_unknown": ExitCode.TRANSPORT,
    "interrupted": ExitCode.TRANSPORT,
    "internal_error": ExitCode.TRANSPORT,
    "partial_transition": ExitCode.PARTIAL,
}


class AtlError(Exception):
    """A failure rendered as the JSON error envelope.

    ``write_outcome`` is set by the transport for write requests: ``not_sent``,
    ``rejected`` (server answered 4xx, nothing applied) or ``unknown``.
    """

    def __init__(
        self,
        code: str,
        message: str,
        fix: str,
        details: dict[str, Any] | None = None,
        *,
        write_outcome: str | None = None,
    ) -> None:
        if code not in ERROR_CODES:
            raise ValueError(f"unregistered error code: {code}")
        super().__init__(message)
        self.code = code
        self.message = message
        self.fix = fix
        self.details: dict[str, Any] = details or {}
        self.write_outcome = write_outcome

    @property
    def exit_code(self) -> ExitCode:
        return ERROR_CODES[self.code]

    def envelope(self) -> dict[str, Any]:
        return {
            "ok": False,
            "error": {
                "code": self.code,
                "message": self.message,
                "fix": self.fix,
                "details": self.details,
            },
        }


def success(data: Any) -> dict[str, Any]:
    return {"ok": True, "data": data}


# --------------------------------------------------------------------------- schemas


def _obj(
    properties: dict[str, Any],
    required: list[str] | None = None,
    *,
    extra: bool = False,
) -> dict[str, Any]:
    schema: dict[str, Any] = {
        "type": "object",
        "properties": properties,
        "additionalProperties": extra,
    }
    if required:
        schema["required"] = required
    return schema


def _arr(items: dict[str, Any]) -> dict[str, Any]:
    return {"type": "array", "items": items}


STR: dict[str, Any] = {"type": "string"}
NULLABLE_STR: dict[str, Any] = {"type": ["string", "null"]}
INT: dict[str, Any] = {"type": "integer"}
BOOL: dict[str, Any] = {"type": "boolean"}
ISSUE_KEY: dict[str, Any] = {"type": "string", "pattern": ISSUE_KEY_PATTERN}
PAGE_ID: dict[str, Any] = {"type": "string", "pattern": PAGE_ID_PATTERN}
INPUT_PATH: dict[str, Any] = {
    "type": "string",
    "minLength": 1,
    "description": "Path to a UTF-8 JSON file, or '-' for stdin.",
}
TEXT_PATH: dict[str, Any] = {
    "type": "string",
    "minLength": 1,
    "description": "Path to a plain UTF-8 text file (not JSON, max 1 MiB), or '-' for stdin.",
}
START: dict[str, Any] = {"type": "integer", "minimum": 0, "default": 0}
LIMIT: dict[str, Any] = {
    "type": "integer",
    "minimum": 1,
    "maximum": MAX_SEARCH_LIMIT,
    "default": 50,
}
QUERY: dict[str, Any] = {"type": "string", "minLength": 1, "maxLength": MAX_QUERY_CHARS}

CHECK_STATUSES = ["pass", "fail", "drift", "unverified", "not_applicable"]

CHECK = _obj(
    {
        "id": STR,
        "status": {"enum": CHECK_STATUSES},
        "message": STR,
        "fix": NULLABLE_STR,
        "details": {"type": "object"},
    },
    ["id", "status", "message", "fix"],
)

PLANNED_FIELD = _obj(
    {"friendly": STR, "id": STR, "name": STR},
    ["friendly", "id", "name"],
)

OPERATION_STATUSES = ["planned", "succeeded", "rejected", "unknown", "skipped", "not_attempted"]

OPERATION = _obj(
    {
        "op": {"enum": ["edit", "transition"]},
        "method": {"enum": ["PUT", "POST"]},
        "status": {"enum": OPERATION_STATUSES},
        "fields": _arr(PLANNED_FIELD),
        "transition_id": NULLABLE_STR,
        "reason": NULLABLE_STR,
    },
    ["op", "method", "status", "fields"],
)

CHECKLIST = _obj(
    {
        "key": STR,
        "to": STR,
        "from_status": STR,
        "transition_name": NULLABLE_STR,
        "ready": BOOL,
        "transition": {"oneOf": [{"type": "null"}, _obj({"id": STR, "name": STR}, ["id", "name"])]},
        "checks": _arr(CHECK),
        "plan": _obj(
            {"atomic": BOOL, "operations": _arr(OPERATION)},
            ["atomic", "operations"],
        ),
        "notice": STR,
    },
    ["key", "to", "from_status", "ready", "transition", "checks", "plan", "notice"],
)

ERROR_ENVELOPE: dict[str, Any] = _obj(
    {
        "ok": {"const": False},
        "error": _obj(
            {
                "code": {"enum": sorted(ERROR_CODES)},
                "message": STR,
                "fix": {"type": "string", "minLength": 1},
                "details": {"type": "object"},
            },
            ["code", "message", "fix", "details"],
        ),
    },
    ["ok", "error"],
)

EXIT_CODES_DOC: dict[str, str] = {
    "0": "success",
    "2": "input or configuration error",
    "3": "authentication or keyring error",
    "4": "unmet workflow requirement, approval required, or config drift/unverified",
    "5": "remote validation error, not found, or conflict",
    "6": "transport, TLS, or service failure, or unknown write outcome",
    "7": "confirmed partial write",
}


@dataclass(frozen=True)
class CommandSpec:
    path: str
    summary: str
    arguments: dict[str, Any]
    success: dict[str, Any]
    input: dict[str, Any] | None = None
    errors: list[str] = field(default_factory=list)
    needs_config: bool = True


STORY_CREATE_INPUT = _obj(
    {
        "project": {"type": "string", "pattern": PROJECT_KEY_PATTERN},
        "summary": {"type": "string", "minLength": 1, "maxLength": 255},
        "acceptance_criteria": {
            "type": "string",
            "maxLength": 32767,
            "description": "Caller-authored acceptance criteria. atl never writes this text.",
        },
        "feature_key": {
            "type": "string",
            "description": "Caller-chosen parent feature key. atl never chooses a feature.",
        },
        "description": {"type": "string", "maxLength": 32767},
        "fields": {
            "type": "object",
            "description": "Extra values keyed by friendly field names from atl.yaml.",
            "additionalProperties": {
                "oneOf": [
                    {"type": "string", "maxLength": 32767},
                    {"type": "array", "items": STR, "maxItems": 100},
                ]
            },
        },
    },
    ["project", "summary"],
)

FEATURE_CREATE_INPUT = _obj(
    {
        "project": {"type": "string", "pattern": PROJECT_KEY_PATTERN},
        "summary": {"type": "string", "minLength": 1, "maxLength": 255},
        "description": {"type": "string", "maxLength": 32767},
        "fields": STORY_CREATE_INPUT["properties"]["fields"],
        "human_confirmation": _obj(
            {
                "confirmed_by": {"type": "string", "minLength": 1, "maxLength": 200},
                "statement": {"type": "string", "minLength": 1, "maxLength": 2000},
            },
            ["confirmed_by", "statement"],
        ),
    },
    ["project", "summary"],
)

PAGE_CREATE_INPUT = _obj(
    {
        "space_key": {"type": "string", "pattern": SPACE_KEY_PATTERN},
        "title": {"type": "string", "minLength": 1, "maxLength": 255},
        "body_storage": {"type": "string", "maxLength": 5 * 1024 * 1024},
        "parent_id": PAGE_ID,
    },
    ["space_key", "title", "body_storage"],
)

PAGE_UPDATE_INPUT = _obj(
    {
        "expected_version": {"type": "integer", "minimum": 1},
        "title": {"type": "string", "minLength": 1, "maxLength": 255},
        "body_storage": {"type": "string", "maxLength": 5 * 1024 * 1024},
    },
    ["expected_version"],
)
PAGE_UPDATE_INPUT["anyOf"] = [{"required": ["title"]}, {"required": ["body_storage"]}]

ATTESTATION = {
    "oneOf": [
        {"type": "null"},
        _obj(
            {
                "confirmed_by": STR,
                "statement": STR,
                "identity_verified": {"const": False},
                "note": STR,
            },
            ["confirmed_by", "statement", "identity_verified", "note"],
        ),
    ]
}

COMMON_REMOTE_ERRORS = [
    "config_not_found",
    "config_invalid",
    "service_not_configured",
    "auth_missing",
    "auth_failed",
    "forbidden",
    "keyring_unavailable",
    "not_found",
    "transport_error",
    "tls_error",
    "redirect_refused",
    "service_error",
    "rate_limited",
    "response_invalid",
]

WRITE_ERRORS = ["remote_validation", "remote_limit_exceeded", "outcome_unknown"]

COMMANDS: list[CommandSpec] = [
    CommandSpec(
        path="commands",
        summary="List every command with a one-line summary. Offline.",
        arguments=_obj({}),
        success=_obj(
            {
                "version": STR,
                "schema_version": INT,
                "commands": _arr(
                    _obj(
                        {"path": STR, "summary": STR, "schema": STR}, ["path", "summary", "schema"]
                    )
                ),
                "exit_codes": {"type": "object"},
            },
            ["version", "schema_version", "commands", "exit_codes"],
        ),
        needs_config=False,
    ),
    CommandSpec(
        path="schema",
        summary="Print argument, input, success, and error JSON Schemas for a command. Offline.",
        arguments=_obj({"command": STR}, ["command"]),
        success=_obj(
            {
                "schema_version": INT,
                "command": STR,
                "summary": STR,
                "arguments": {"type": "object"},
                "input": {"type": ["object", "null"]},
                "success": {"type": "object"},
                "error": {"type": "object"},
                "error_codes": _arr(STR),
                "exit_codes": {"type": "object"},
            },
            [
                "schema_version",
                "command",
                "summary",
                "arguments",
                "input",
                "success",
                "error",
                "error_codes",
                "exit_codes",
            ],
        ),
        errors=["usage_error"],
        needs_config=False,
    ),
    CommandSpec(
        path="login",
        summary=(
            "Verify a PAT or username/password against the service and store it in the OS "
            "keyring. Secrets come from stdin or a no-echo prompt, never arguments."
        ),
        arguments=_obj(
            {
                "service": {"enum": ["jira", "confluence", "all"]},
                "auth": {"enum": ["pat", "basic"], "default": "pat"},
                "username": {"type": "string", "description": "Basic auth username (not secret)."},
                "token_stdin": BOOL,
                "password_stdin": BOOL,
            },
            ["service"],
        ),
        success=_obj(
            {
                "services": _arr(
                    _obj(
                        {
                            "service": {"enum": ["jira", "confluence"]},
                            "status": {"enum": ["stored", "failed", "not_attempted"]},
                            "auth": {"enum": ["pat", "basic"]},
                            "user": NULLABLE_STR,
                            "base_url": STR,
                        },
                        ["service", "status", "auth", "base_url"],
                        extra=True,
                    )
                )
            },
            ["services"],
        ),
        errors=[*COMMON_REMOTE_ERRORS, "usage_error", "login_failed"],
    ),
    CommandSpec(
        path="config check",
        summary=(
            "Compare atl.yaml with live Jira create, field, and transition metadata. Reports "
            "pass, drift, and unverified checks; read-only. --issue is used only for transition "
            "sources it matches; other sources fall back to search."
        ),
        arguments=_obj({"issue": ISSUE_KEY}),
        success=_obj(
            {
                "status": {"const": "pass"},
                "summary": {"type": "object"},
                "checks": _arr(CHECK),
            },
            ["status", "summary", "checks"],
        ),
        errors=[*COMMON_REMOTE_ERRORS, "config_drift", "config_unverified"],
    ),
    CommandSpec(
        path="jira story create",
        summary=(
            "Create a story after preflight against create.story.required and live create "
            "metadata. Caller supplies acceptance criteria and the chosen feature key."
        ),
        arguments=_obj({"input": INPUT_PATH}, ["input"]),
        input=STORY_CREATE_INPUT,
        success=_obj(
            {
                "key": ISSUE_KEY,
                "id": STR,
                "url": STR,
                "project": STR,
                "issue_type": STR,
                "checks": _arr(CHECK),
            },
            ["key", "id", "url", "project", "issue_type", "checks"],
        ),
        errors=[
            *COMMON_REMOTE_ERRORS,
            *WRITE_ERRORS,
            "input_invalid",
            "project_not_configured",
            "requirements_unmet",
            "metadata_incomplete",
        ],
    ),
    CommandSpec(
        path="jira requirements",
        summary=(
            "One-call checklist of configured and live requirements for moving an issue to a "
            "status, including the planned write sequence. Read-only."
        ),
        arguments=_obj({"key": ISSUE_KEY, "to": STR}, ["key", "to"]),
        success=CHECKLIST,
        errors=[
            *COMMON_REMOTE_ERRORS,
            "project_not_configured",
            "transition_not_configured",
            "issue_key_mismatch",
            "metadata_incomplete",
        ],
    ),
    CommandSpec(
        path="jira transition",
        summary=(
            "Transition an issue to a configured status after the requirements checklist passes. "
            "Optional pre_edit PUT is non-atomic with the transition POST."
        ),
        arguments=_obj({"key": ISSUE_KEY, "to": STR}, ["key", "to"]),
        success=_obj(
            {
                "key": STR,
                "from_status": STR,
                "to": STR,
                "transition_id": STR,
                "atomic": BOOL,
                "operations": _arr(OPERATION),
            },
            ["key", "from_status", "to", "transition_id", "atomic", "operations"],
        ),
        errors=[
            *COMMON_REMOTE_ERRORS,
            *WRITE_ERRORS,
            "project_not_configured",
            "transition_not_configured",
            "issue_key_mismatch",
            "metadata_incomplete",
            "requirements_unmet",
            "partial_transition",
        ],
    ),
    CommandSpec(
        path="jira feature find",
        summary=(
            "Rank configured feature-type issues for an issue or free text. Never selects or "
            "creates a feature."
        ),
        arguments=_obj(
            {
                "issue": ISSUE_KEY,
                "text": {"type": "string", "minLength": 1, "maxLength": 1000},
                "limit": {"type": "integer", "minimum": 1, "maximum": 25, "default": 10},
            },
            extra=False,
        )
        | {"oneOf": [{"required": ["issue"]}, {"required": ["text"]}]},
        success=_obj(
            {
                "mode": {"enum": ["issue", "text"]},
                "query": STR,
                "candidates": _arr(
                    _obj(
                        {
                            "key": STR,
                            "project": STR,
                            "summary": STR,
                            "status": NULLABLE_STR,
                            "score": INT,
                            "reasons": _arr(STR),
                        },
                        ["key", "project", "summary", "score", "reasons"],
                    )
                ),
                "candidate_pool_limit": {
                    "const": 100,
                    "description": "Most text matches (newest updated first) ranked locally. "
                    "Keys named in --text are always fetched and ranked.",
                },
                "truncated": {
                    "type": "boolean",
                    "description": "True when the text search may have matched more issues than "
                    "the candidate pool held. --limit trimming does not set this.",
                },
                "selection": {"const": "caller"},
            },
            ["mode", "query", "candidates", "candidate_pool_limit", "truncated", "selection"],
        ),
        errors=[*COMMON_REMOTE_ERRORS, "usage_error", "project_not_configured"],
    ),
    CommandSpec(
        path="jira feature create",
        summary=(
            "Create a feature under the project's feature_create_policy after preflight against "
            "create.feature.required and live create metadata. human_confirmed requires a "
            "caller-supplied attestation, which is not identity proof."
        ),
        arguments=_obj({"input": INPUT_PATH}, ["input"]),
        input=FEATURE_CREATE_INPUT,
        success=_obj(
            {
                "key": ISSUE_KEY,
                "id": STR,
                "url": STR,
                "project": STR,
                "issue_type": STR,
                "policy": {"enum": ["automatic", "human_confirmed"]},
                "attestation": ATTESTATION,
                "checks": _arr(CHECK),
            },
            ["key", "id", "url", "project", "issue_type", "policy", "attestation", "checks"],
        ),
        errors=[
            *COMMON_REMOTE_ERRORS,
            *WRITE_ERRORS,
            "input_invalid",
            "project_not_configured",
            "approval_required",
            "requirements_unmet",
            "metadata_incomplete",
        ],
    ),
    CommandSpec(
        path="jira attachment add",
        summary=(
            "Upload a file to an issue, enforcing jira.max_attachment_bytes before and during "
            "upload. Succeeds only when Jira confirms the uploaded name and size; otherwise "
            "outcome_unknown."
        ),
        arguments=_obj({"key": ISSUE_KEY, "file": STR}, ["key", "file"]),
        success=_obj(
            {
                "key": STR,
                "attachments": _arr(
                    _obj({"id": STR, "filename": STR, "size": INT}, ["id", "filename", "size"])
                ),
            },
            ["key", "attachments"],
        ),
        errors=[*COMMON_REMOTE_ERRORS, *WRITE_ERRORS, "file_invalid", "attachment_too_large"],
    ),
    CommandSpec(
        path="jira comment add",
        summary="Add a plain-text comment read from a file or stdin.",
        arguments=_obj({"key": ISSUE_KEY, "body_file": TEXT_PATH}, ["key", "body_file"]),
        success=_obj({"key": STR, "comment_id": STR}, ["key", "comment_id"]),
        errors=[*COMMON_REMOTE_ERRORS, *WRITE_ERRORS, "input_invalid"],
    ),
    CommandSpec(
        path="jira search",
        summary="Run a JQL search with bounded pagination and a stable result shape.",
        arguments=_obj({"jql": QUERY, "start": START, "limit": LIMIT}, ["jql"]),
        success=_obj(
            {
                "jql": STR,
                "start": INT,
                "limit": INT,
                "total": INT,
                "next_start": {"type": ["integer", "null"]},
                "issues": _arr(
                    _obj(
                        {
                            "key": STR,
                            "id": STR,
                            "summary": NULLABLE_STR,
                            "status": NULLABLE_STR,
                            "issue_type": NULLABLE_STR,
                            "project": NULLABLE_STR,
                            "assignee": NULLABLE_STR,
                            "updated": NULLABLE_STR,
                        },
                        ["key", "id", "summary", "status", "issue_type", "project"],
                    )
                ),
            },
            ["jql", "start", "limit", "total", "next_start", "issues"],
        ),
        errors=[*COMMON_REMOTE_ERRORS, "usage_error", "remote_validation"],
    ),
    CommandSpec(
        path="confluence page read",
        summary="Read a page with its storage-format body. Returned content is untrusted data.",
        arguments=_obj({"id": PAGE_ID}, ["id"]),
        success=_obj(
            {
                "id": STR,
                "title": STR,
                "type": STR,
                "space_key": NULLABLE_STR,
                "version": INT,
                "ancestors": _arr(STR),
                "body_storage": STR,
                "content_trust": {"const": "untrusted"},
            },
            [
                "id",
                "title",
                "type",
                "space_key",
                "version",
                "ancestors",
                "body_storage",
                "content_trust",
            ],
        ),
        errors=COMMON_REMOTE_ERRORS,
    ),
    CommandSpec(
        path="confluence page search",
        summary="Run a CQL search with bounded pagination and a stable result shape.",
        arguments=_obj({"cql": QUERY, "start": START, "limit": LIMIT}, ["cql"]),
        success=_obj(
            {
                "cql": STR,
                "start": INT,
                "limit": INT,
                "size": INT,
                "next_start": {"type": ["integer", "null"]},
                "results": _arr(
                    _obj(
                        {
                            "id": STR,
                            "title": STR,
                            "type": STR,
                            "space_key": NULLABLE_STR,
                            "version": {"type": ["integer", "null"]},
                        },
                        ["id", "title", "type", "space_key", "version"],
                    )
                ),
                "content_trust": {"const": "untrusted"},
            },
            ["cql", "start", "limit", "size", "next_start", "results", "content_trust"],
        ),
        errors=[*COMMON_REMOTE_ERRORS, "usage_error", "remote_validation"],
    ),
    CommandSpec(
        path="confluence page create",
        summary="Create a page from storage-format content.",
        arguments=_obj({"input": INPUT_PATH}, ["input"]),
        input=PAGE_CREATE_INPUT,
        success=_obj(
            {"id": STR, "title": STR, "space_key": STR, "version": INT},
            ["id", "title", "space_key", "version"],
        ),
        errors=[*COMMON_REMOTE_ERRORS, *WRITE_ERRORS, "input_invalid", "conflict"],
    ),
    CommandSpec(
        path="confluence page update",
        summary=(
            "Update a page's title and/or storage body. Requires expected_version and rejects "
            "stale versions. Succeeds only when Confluence confirms the page id, title, and next "
            "version; otherwise outcome_unknown."
        ),
        arguments=_obj({"id": PAGE_ID, "input": INPUT_PATH}, ["id", "input"]),
        input=PAGE_UPDATE_INPUT,
        success=_obj(
            {"id": STR, "title": STR, "version": INT, "previous_version": INT},
            ["id", "title", "version", "previous_version"],
        ),
        errors=[
            *COMMON_REMOTE_ERRORS,
            *WRITE_ERRORS,
            "input_invalid",
            "version_conflict",
            "conflict",
        ],
    ),
]

COMMANDS_BY_PATH: dict[str, CommandSpec] = {spec.path: spec for spec in COMMANDS}

UNIVERSAL_ERRORS = ["usage_error", "interrupted", "internal_error"]


def command_schema(spec: CommandSpec) -> dict[str, Any]:
    error_codes = sorted({*spec.errors, *UNIVERSAL_ERRORS})
    return {
        "schema_version": SCHEMA_VERSION,
        "command": spec.path,
        "summary": spec.summary,
        "arguments": spec.arguments,
        "input": spec.input,
        "success": _obj({"ok": {"const": True}, "data": spec.success}, ["ok", "data"]),
        "error": ERROR_ENVELOPE,
        "error_codes": error_codes,
        "exit_codes": EXIT_CODES_DOC,
    }
