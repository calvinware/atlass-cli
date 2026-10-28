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
DEFAULT_SEARCH_LIMIT = 20
MAX_QUERY_CHARS = 4000
MAX_TEXT_CHARS = 2000


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
    "config_legacy": ExitCode.INPUT,
    "service_not_configured": ExitCode.INPUT,
    "issue_key_mismatch": ExitCode.INPUT,
    "file_invalid": ExitCode.INPUT,
    "attachment_too_large": ExitCode.INPUT,
    "auth_missing": ExitCode.AUTH,
    "auth_failed": ExitCode.AUTH,
    "forbidden": ExitCode.AUTH,
    "keyring_unavailable": ExitCode.AUTH,
    "login_failed": ExitCode.AUTH,
    "requirements_unmet": ExitCode.REQUIREMENT,
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


class YojoError(Exception):
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
        # Checks behind a refused write; the command line turns their edits into a retry.
        self.checks: list[Any] = []

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
    "default": DEFAULT_SEARCH_LIMIT,
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

CHANGED_FIELD = _obj({"field": STR, "id": STR}, ["field", "id"])

OPERATION_STATUSES = ["planned", "succeeded", "rejected", "unknown", "skipped", "not_attempted"]

OPERATION = _obj(
    {
        "op": {"enum": ["edit", "transition"]},
        "method": {"enum": ["PUT", "POST"]},
        "status": {"enum": OPERATION_STATUSES},
        "fields": _arr(CHANGED_FIELD),
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
        "rule": NULLABLE_STR,
        "ready": BOOL,
        "transition": {"oneOf": [{"type": "null"}, _obj({"id": STR, "name": STR}, ["id", "name"])]},
        "checks": _arr(CHECK),
        "plan": _obj(
            {"atomic": BOOL, "operations": _arr(OPERATION)},
            ["atomic", "operations"],
        ),
        "notice": STR,
    },
    ["key", "to", "from_status", "rule", "ready", "transition", "checks", "plan", "notice"],
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
    "4": "unmet requirement (nothing was written), or config drift/unverified",
    "5": "remote validation error, not found, or conflict",
    "6": "transport, TLS, or service failure, or unknown write outcome",
    "7": "confirmed partial write",
}

GLOBAL_OPTIONS: dict[str, str] = {
    "--json": "Print the JSON envelope even on a terminal. Piped output is always JSON.",
    "--base-url URL": "Jira or Confluence HTTPS root for this call; works without yojo.yaml.",
    "--config PATH": "Use this yojo.yaml instead of searching the current directory upward.",
}


@dataclass(frozen=True)
class CommandSpec:
    path: str
    summary: str
    usage: str
    example: str
    arguments: dict[str, Any]
    success: dict[str, Any]
    input: dict[str, Any] | None = None
    errors: list[str] = field(default_factory=list)
    needs_config: bool = True


SET_ITEM: dict[str, Any] = {
    "type": "string",
    "pattern": "^[^=]+=",
    "description": (
        "NAME=VALUE, split at the first '='. NAME is the field name shown in Jira, a yojo.yaml "
        "alias, or a raw ID such as customfield_10002 (any case). VALUE is plain text: a number; "
        "an option, priority, resolution, version, or component label; a person's username, "
        "display name, or email; an issue key for link fields; or a comma-separated list for "
        "multi-value fields."
    ),
}
SETS: dict[str, Any] = {"type": "array", "items": SET_ITEM, "maxItems": 100}
FIELD_VALUE: dict[str, Any] = {
    "oneOf": [
        {"type": "string", "maxLength": 32767},
        {"type": "number"},
        {"type": "array", "items": STR, "maxItems": 100},
    ]
}
FIELDS_INPUT: dict[str, Any] = {
    "type": "object",
    "description": "Field values keyed like --set NAME; same value forms, lists as arrays.",
    "additionalProperties": FIELD_VALUE,
    "maxProperties": 100,
}
SUMMARY: dict[str, Any] = {"type": "string", "minLength": 1, "maxLength": 255}
LONG_TEXT: dict[str, Any] = {"type": "string", "maxLength": 32767}
URL: dict[str, Any] = {"type": "string", "description": "Link to open in a browser."}
FULL: dict[str, Any] = {
    "type": "boolean",
    "default": False,
    "description": "Return long text without trimming.",
}

ISSUE_CREATE_INPUT = _obj(
    {
        "project": {"type": "string", "pattern": PROJECT_KEY_PATTERN},
        "type": {"type": "string", "minLength": 1, "maxLength": 255},
        "summary": SUMMARY,
        "description": LONG_TEXT,
        "fields": FIELDS_INPUT,
    }
)
ISSUE_UPDATE_INPUT = _obj({"summary": SUMMARY, "description": LONG_TEXT, "fields": FIELDS_INPUT})
TRANSITION_INPUT = _obj({"fields": FIELDS_INPUT})

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

COMMON_REMOTE_ERRORS = [
    "config_not_found",
    "config_invalid",
    "config_legacy",
    "service_not_configured",
    "input_invalid",
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

WRITE_ERRORS = [
    "requirements_unmet",
    "metadata_incomplete",
    "remote_validation",
    "remote_limit_exceeded",
    "outcome_unknown",
]

ISSUE_WRITE_SUCCESS = {
    "key": ISSUE_KEY,
    "url": URL,
    "status": NULLABLE_STR,
    "changed": _arr(CHANGED_FIELD),
}

COMMANDS: list[CommandSpec] = [
    CommandSpec(
        path="commands",
        summary="List every command with its signature and an example. Offline.",
        usage="yojo commands",
        example="yojo --json commands",
        arguments=_obj({}),
        success=_obj(
            {
                "version": STR,
                "schema_version": INT,
                "commands": _arr(
                    _obj(
                        {"path": STR, "summary": STR, "usage": STR, "example": STR},
                        ["path", "summary", "usage", "example"],
                    )
                ),
                "global_options": {"type": "object"},
                "exit_codes": {"type": "object"},
            },
            ["version", "schema_version", "commands", "global_options", "exit_codes"],
        ),
        needs_config=False,
    ),
    CommandSpec(
        path="schema",
        summary="Print argument, input, success, and error JSON Schemas for a command. Offline.",
        usage="yojo schema COMMAND...",
        example="yojo --json schema 'jira issue create'",
        arguments=_obj({"command": STR}, ["command"]),
        success=_obj(
            {
                "schema_version": INT,
                "command": STR,
                "summary": STR,
                "usage": STR,
                "example": STR,
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
                "usage",
                "example",
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
            "Check a personal access token (or username and password) against the service and "
            "store it in the OS keyring for that URL. Secrets come from stdin or a hidden prompt, "
            "never arguments."
        ),
        usage=(
            "yojo login --service jira|confluence|all [--base-url URL] [--auth pat|basic] "
            "[--username NAME] [--token-stdin | --password-stdin]"
        ),
        example="yojo login --service jira --base-url https://jira.example.test/jira",
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
            "Compare yojo.yaml aliases, create rules, and transition rules with live Jira "
            "fields, projects, and statuses. Read-only."
        ),
        usage="yojo config check [--config PATH]",
        example="yojo --json config check",
        arguments=_obj({}),
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
        path="jira issue create",
        summary=(
            "Create an issue of any type. Checks the project's live create screen and any "
            "yojo.yaml rules first, and refuses with every problem listed if anything is "
            "missing; nothing is written then."
        ),
        usage=(
            "yojo jira issue create PROJECT --type TYPE --summary TEXT [--description TEXT] "
            "[--set 'NAME=VALUE']... [--input FILE|-]"
        ),
        example=(
            "yojo --json jira issue create PROJ --type Bug --summary 'Login fails on Safari' "
            "--set 'Story Points=3' --set priority=High"
        ),
        arguments=_obj(
            {
                "project": {"type": "string", "pattern": PROJECT_KEY_PATTERN},
                "type": {"type": "string", "description": "Issue type name, any case."},
                "summary": SUMMARY,
                "description": LONG_TEXT,
                "set": SETS,
                "input": INPUT_PATH,
            }
        ),
        input=ISSUE_CREATE_INPUT,
        success=_obj(
            {**ISSUE_WRITE_SUCCESS, "issue_type": STR, "project": STR},
            ["key", "url", "status", "changed", "issue_type", "project"],
        ),
        errors=[*COMMON_REMOTE_ERRORS, *WRITE_ERRORS],
    ),
    CommandSpec(
        path="jira issue get",
        summary=(
            "Read an issue's key, summary, status, type, assignee, and link. --fields adds more "
            "fields by name; long text is trimmed unless --full."
        ),
        usage="yojo jira issue get KEY [--fields 'NAME,NAME'] [--full]",
        example="yojo --json jira issue get PROJ-123 --fields 'Story Points,description'",
        arguments=_obj(
            {
                "key": ISSUE_KEY,
                "fields": {
                    "type": "string",
                    "description": "Comma-separated field names, aliases, or IDs.",
                },
                "full": FULL,
            },
            ["key"],
        ),
        success=_obj(
            {
                "key": ISSUE_KEY,
                "url": URL,
                "summary": NULLABLE_STR,
                "status": NULLABLE_STR,
                "issue_type": NULLABLE_STR,
                "project": NULLABLE_STR,
                "assignee": NULLABLE_STR,
                "updated": NULLABLE_STR,
                "fields": _arr(
                    _obj(
                        {"field": STR, "id": STR, "value": {}, "truncated": BOOL},
                        ["field", "id", "value", "truncated"],
                    )
                ),
                "more": NULLABLE_STR,
                "content_trust": {"const": "untrusted"},
            },
            [
                "key",
                "url",
                "summary",
                "status",
                "issue_type",
                "project",
                "assignee",
                "updated",
                "fields",
                "more",
                "content_trust",
            ],
        ),
        errors=[*COMMON_REMOTE_ERRORS, "metadata_incomplete"],
    ),
    CommandSpec(
        path="jira issue update",
        summary=(
            "Change fields on an issue. Checks the live edit screen first, refuses with every "
            "problem listed, then reads the issue back to confirm the new values."
        ),
        usage=(
            "yojo jira issue update KEY [--summary TEXT] [--description TEXT] "
            "[--set 'NAME=VALUE']... [--input FILE|-]"
        ),
        example=(
            "yojo --json jira issue update PROJ-123 --set 'Story Points=5' "
            "--set assignee=example-user"
        ),
        arguments=_obj(
            {
                "key": ISSUE_KEY,
                "summary": SUMMARY,
                "description": LONG_TEXT,
                "set": SETS,
                "input": INPUT_PATH,
            },
            ["key"],
        ),
        input=ISSUE_UPDATE_INPUT,
        success=_obj(ISSUE_WRITE_SUCCESS, ["key", "url", "status", "changed"]),
        errors=[*COMMON_REMOTE_ERRORS, *WRITE_ERRORS],
    ),
    CommandSpec(
        path="jira issue link",
        summary=(
            "Link two issues, for example `PROJ-1 blocks PROJ-2`. --type takes the link name or "
            "either direction's wording; the link is read back to confirm it."
        ),
        usage="yojo jira issue link KEY OTHER_KEY --type 'LINK TYPE'",
        example="yojo --json jira issue link PROJ-1 PROJ-2 --type blocks",
        arguments=_obj(
            {
                "key": ISSUE_KEY,
                "other": ISSUE_KEY,
                "type": {
                    "type": "string",
                    "description": "Link type name or wording, e.g. Blocks, blocks, is blocked by.",
                },
            },
            ["key", "other", "type"],
        ),
        success=_obj(
            {"key": ISSUE_KEY, "url": URL, "relation": STR, "other": ISSUE_KEY, "link_type": STR},
            ["key", "url", "relation", "other", "link_type"],
        ),
        errors=[*COMMON_REMOTE_ERRORS, *WRITE_ERRORS],
    ),
    CommandSpec(
        path="jira requirements",
        summary=(
            "Optional look-before-you-leap: every check `jira transition` would run for moving "
            "an issue to a status, plus the planned writes. Read-only."
        ),
        usage="yojo jira requirements KEY --to STATUS [--set 'NAME=VALUE']...",
        example="yojo --json jira requirements PROJ-123 --to Done",
        arguments=_obj({"key": ISSUE_KEY, "to": STR, "set": SETS}, ["key", "to"]),
        success=CHECKLIST,
        errors=[*COMMON_REMOTE_ERRORS, "issue_key_mismatch", "metadata_incomplete"],
    ),
    CommandSpec(
        path="jira transition",
        summary=(
            "Move an issue to a status (any case), setting transition-screen fields with --set. "
            "Checks every rule and required field first and refuses with all problems listed. "
            "A configured pre_edit runs a separate edit first and is not atomic."
        ),
        usage="yojo jira transition KEY --to STATUS [--set 'NAME=VALUE']... [--input FILE|-]",
        example="yojo --json jira transition PROJ-123 --to Done --set resolution=Fixed",
        arguments=_obj(
            {"key": ISSUE_KEY, "to": STR, "set": SETS, "input": INPUT_PATH}, ["key", "to"]
        ),
        input=TRANSITION_INPUT,
        success=_obj(
            {
                "key": ISSUE_KEY,
                "url": URL,
                "from_status": STR,
                "status": STR,
                "transition_id": STR,
                "changed": _arr(CHANGED_FIELD),
                "atomic": BOOL,
                "operations": _arr(OPERATION),
            },
            [
                "key",
                "url",
                "from_status",
                "status",
                "transition_id",
                "changed",
                "atomic",
                "operations",
            ],
        ),
        errors=[*COMMON_REMOTE_ERRORS, *WRITE_ERRORS, "issue_key_mismatch", "partial_transition"],
    ),
    CommandSpec(
        path="jira attachment add",
        summary=(
            "Upload a file to an issue after checking permission and the size limit. Succeeds "
            "only when Jira confirms the uploaded name and size; otherwise outcome_unknown."
        ),
        usage="yojo jira attachment add KEY --file PATH",
        example="yojo --json jira attachment add PROJ-123 --file results.xml",
        arguments=_obj({"key": ISSUE_KEY, "file": STR}, ["key", "file"]),
        success=_obj(
            {
                "key": STR,
                "url": URL,
                "attachments": _arr(
                    _obj({"id": STR, "filename": STR, "size": INT}, ["id", "filename", "size"])
                ),
            },
            ["key", "url", "attachments"],
        ),
        errors=[
            *COMMON_REMOTE_ERRORS,
            *WRITE_ERRORS,
            "file_invalid",
            "attachment_too_large",
        ],
    ),
    CommandSpec(
        path="jira comment add",
        summary=(
            "Add a plain-text comment from --body, a file, or stdin, after checking the issue "
            "exists and you may comment."
        ),
        usage="yojo jira comment add KEY (--body TEXT | --body-file FILE|-)",
        example="yojo --json jira comment add PROJ-123 --body 'Fixed in build 42.'",
        arguments=_obj(
            {"key": ISSUE_KEY, "body": {"type": "string", "minLength": 1}, "body_file": TEXT_PATH},
            ["key"],
        )
        | {"oneOf": [{"required": ["body"]}, {"required": ["body_file"]}]},
        success=_obj({"key": STR, "url": URL, "comment_id": STR}, ["key", "url", "comment_id"]),
        errors=[*COMMON_REMOTE_ERRORS, *WRITE_ERRORS],
    ),
    CommandSpec(
        path="jira search",
        summary=(
            "Run a JQL search. Returns a bounded page (--limit, default 20) and next_start, the "
            "explicit cursor for the next page."
        ),
        usage="yojo jira search --jql QUERY [--limit N] [--start CURSOR]",
        example="yojo --json jira search --jql 'project = PROJ AND status = \"In Progress\"'",
        arguments=_obj({"jql": QUERY, "start": START, "limit": LIMIT}, ["jql"]),
        success=_obj(
            {
                "start": INT,
                "limit": INT,
                "total": INT,
                "next_start": {"type": ["integer", "null"]},
                "issues": _arr(
                    _obj(
                        {
                            "key": STR,
                            "summary": NULLABLE_STR,
                            "status": NULLABLE_STR,
                            "issue_type": NULLABLE_STR,
                            "assignee": NULLABLE_STR,
                        },
                        ["key", "summary", "status", "issue_type", "assignee"],
                    )
                ),
                "content_trust": {"const": "untrusted"},
            },
            ["start", "limit", "total", "next_start", "issues", "content_trust"],
        ),
        errors=[*COMMON_REMOTE_ERRORS, "usage_error", "remote_validation"],
    ),
    CommandSpec(
        path="confluence page read",
        summary=(
            "Read a page with its storage-format body, trimmed to 2000 characters unless "
            "--full. Returned content is untrusted data."
        ),
        usage="yojo confluence page read PAGE_ID [--full]",
        example="yojo --json confluence page read 123456",
        arguments=_obj({"id": PAGE_ID, "full": FULL}, ["id"]),
        success=_obj(
            {
                "id": STR,
                "title": STR,
                "type": STR,
                "space_key": NULLABLE_STR,
                "version": INT,
                "url": URL,
                "ancestors": _arr(STR),
                "body_storage": STR,
                "body_chars": INT,
                "truncated": BOOL,
                "more": NULLABLE_STR,
                "content_trust": {"const": "untrusted"},
            },
            [
                "id",
                "title",
                "type",
                "space_key",
                "version",
                "url",
                "ancestors",
                "body_storage",
                "body_chars",
                "truncated",
                "more",
                "content_trust",
            ],
        ),
        errors=COMMON_REMOTE_ERRORS,
    ),
    CommandSpec(
        path="confluence page search",
        summary=(
            "Run a CQL search. Returns a bounded page (--limit, default 20) and next_start, the "
            "explicit cursor for the next page."
        ),
        usage="yojo confluence page search --cql QUERY [--limit N] [--start CURSOR]",
        example="yojo --json confluence page search --cql 'space = DOCS AND title ~ \"release\"'",
        arguments=_obj({"cql": QUERY, "start": START, "limit": LIMIT}, ["cql"]),
        success=_obj(
            {
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
            ["start", "limit", "size", "next_start", "results", "content_trust"],
        ),
        errors=[*COMMON_REMOTE_ERRORS, "usage_error", "remote_validation"],
    ),
    CommandSpec(
        path="confluence page create",
        summary="Create a page from storage-format content given as JSON input.",
        usage="yojo confluence page create --input FILE|-",
        example="yojo --json confluence page create --input page.json",
        arguments=_obj({"input": INPUT_PATH}, ["input"]),
        input=PAGE_CREATE_INPUT,
        success=_obj(
            {"id": STR, "title": STR, "space_key": STR, "version": INT, "url": URL},
            ["id", "title", "space_key", "version", "url"],
        ),
        errors=[*COMMON_REMOTE_ERRORS, *WRITE_ERRORS, "conflict"],
    ),
    CommandSpec(
        path="confluence page update",
        summary=(
            "Update a page's title and/or storage body. Requires expected_version and rejects "
            "stale versions. Succeeds only when Confluence confirms the page id, title, and next "
            "version; otherwise outcome_unknown."
        ),
        usage="yojo confluence page update PAGE_ID --input FILE|-",
        example="yojo --json confluence page update 123456 --input page.json",
        arguments=_obj({"id": PAGE_ID, "input": INPUT_PATH}, ["id", "input"]),
        input=PAGE_UPDATE_INPUT,
        success=_obj(
            {"id": STR, "title": STR, "version": INT, "previous_version": INT, "url": URL},
            ["id", "title", "version", "previous_version", "url"],
        ),
        errors=[*COMMON_REMOTE_ERRORS, *WRITE_ERRORS, "version_conflict", "conflict"],
    ),
]

COMMANDS_BY_PATH: dict[str, CommandSpec] = {spec.path: spec for spec in COMMANDS}

UNIVERSAL_ERRORS = ["usage_error", "interrupted", "internal_error"]


def command_listing(spec: CommandSpec) -> dict[str, str]:
    return {
        "path": spec.path,
        "summary": spec.summary,
        "usage": spec.usage,
        "example": spec.example,
    }


def command_schema(spec: CommandSpec) -> dict[str, Any]:
    error_codes = sorted({*spec.errors, *UNIVERSAL_ERRORS})
    return {
        "schema_version": SCHEMA_VERSION,
        "command": spec.path,
        "summary": spec.summary,
        "usage": spec.usage,
        "example": spec.example,
        "arguments": spec.arguments,
        "input": spec.input,
        "success": _obj({"ok": {"const": True}, "data": spec.success}, ["ok", "data"]),
        "error": ERROR_ENVELOPE,
        "error_codes": error_codes,
        "exit_codes": EXIT_CODES_DOC,
    }
