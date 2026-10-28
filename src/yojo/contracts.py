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
FRIENDLY_NAME_PATTERN = r"^[a-z][a-z0-9_]{0,63}" + _END
FIELD_ID_PATTERN = r"^(customfield_[0-9]{1,10}|[a-z][A-Za-z]{0,63})" + _END
SHA256_PATTERN = r"^[0-9a-f]{64}" + _END
PAGE_ID_PATTERN = r"^[1-9][0-9]{0,19}" + _END
SPACE_KEY_PATTERN = r"^~?[A-Za-z0-9_.@-]{1,255}" + _END
BITBUCKET_PROJECT_PATTERN = r"^~?[A-Za-z0-9_][A-Za-z0-9_.@-]{0,127}" + _END
REPO_PATTERN = (
    r"^~?[A-Za-z0-9_][A-Za-z0-9_.@-]{0,127}/(?!\.{1,2}" + _END + r")[A-Za-z0-9_.-]{1,128}" + _END
)

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
    "file_conflict": ExitCode.INPUT,
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
    "--base-url URL": (
        "Jira, Confluence, or Bitbucket HTTPS root for this call; works without yojo.yaml."
    ),
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
        "alias, or a raw ID such as customfield_10002 (any case). VALUE is plain text: a number "
        "or true/false for a boolean field; "
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
        BOOL,
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

# The `yojo init --input` policy: the version 1 yojo.yaml `jira.fields` and `jira.projects` shapes.
NAME: dict[str, Any] = {"type": "string", "minLength": 1, "maxLength": 255}
NAMES: dict[str, Any] = {"type": "array", "items": NAME, "minItems": 1}
FIELD_REF: dict[str, Any] = _obj(
    {"jira_name": NAME, "id": {"type": "string", "pattern": FIELD_ID_PATTERN}}
) | {"oneOf": [{"required": ["jira_name"]}, {"required": ["id"]}]}
ALIASES: dict[str, Any] = {
    "type": "object",
    "description": "Alias (lowercase snake_case) to {jira_name: NAME} or {id: FIELD_ID}.",
    "propertyNames": {"pattern": FRIENDLY_NAME_PATTERN},
    "additionalProperties": FIELD_REF,
}
TRANSITION_RULE: dict[str, Any] = _obj(
    {
        "from": NAMES,
        "to": NAME,
        "pre_edit": BOOL,
        "auto_set": {
            "type": "object",
            "additionalProperties": {"oneOf": [NAME, NAMES]},
        },
        "require": _arr(
            {
                "oneOf": [
                    _obj(
                        {"kind": {"const": "attachment_filename"}, "pattern": NAME},
                        ["kind", "pattern"],
                    ),
                    _obj({"kind": {"const": "field_set"}, "field": NAME}, ["kind", "field"]),
                ]
            }
        ),
    },
    ["from", "to"],
)
POLICY_PROJECT: dict[str, Any] = _obj(
    {
        "key": {"type": "string", "pattern": PROJECT_KEY_PATTERN},
        "fields": ALIASES,
        "create": {
            "type": "object",
            "description": "Issue type name to {required: [FIELD, ...]}.",
            "additionalProperties": _obj({"required": _arr(NAME)}, ["required"]),
        },
        "transitions": {
            "type": "object",
            "description": "Rule name (lowercase snake_case) to a transition rule.",
            "propertyNames": {"pattern": FRIENDLY_NAME_PATTERN},
            "additionalProperties": TRANSITION_RULE,
        },
    },
    ["key"],
)
POLICY_INPUT: dict[str, Any] = _obj(
    {"jira": _obj({"fields": ALIASES, "projects": _arr(POLICY_PROJECT)})}, ["jira"]
)
SERVICE_NAMES = ["jira", "confluence", "bitbucket"]
AGENT_TARGETS = ["agents", "claude", "cursor", "copilot", "gemini"]
SHA256: dict[str, Any] = {"type": "string", "pattern": SHA256_PATTERN}
FILE_ERRORS = ["file_invalid", "file_conflict"]
INIT_ERRORS = [
    "usage_error",
    "input_invalid",
    "config_invalid",
    "config_legacy",
    *FILE_ERRORS,
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
    "metadata_incomplete",
]

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
            "never arguments. Bitbucket takes a token only; `all` means Jira and Confluence."
        ),
        usage=(
            "yojo login --service jira|confluence|bitbucket|all [--base-url URL] "
            "[--auth pat|basic] [--username NAME] [--ca-bundle PEM] "
            "[--token-stdin | --password-stdin]"
        ),
        example="yojo --json login --service jira --base-url https://jira.example.test/jira",
        arguments=_obj(
            {
                "service": {"enum": ["jira", "confluence", "bitbucket", "all"]},
                "auth": {"enum": ["pat", "basic"], "default": "pat"},
                "username": {"type": "string", "description": "Basic auth username (not secret)."},
                "ca_bundle": {
                    "type": "string",
                    "description": "Readable PEM CA bundle for login; relative to cwd.",
                },
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
                            "service": {"enum": ["jira", "confluence", "bitbucket"]},
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
            "fields, projects, and statuses, and check that a configured Bitbucket answers and "
            "accepts the stored token. Read-only."
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
        path="init",
        summary=(
            "Draft a version 1 yojo.yaml from live Jira, Confluence, and Bitbucket metadata, "
            "using the logins already stored for each URL. Shows a preview by default; --save "
            "creates the file only if it is absent, and --replace needs the current file's "
            "sha256. Team rules come only from --policy-input (or --input); nothing is "
            "inferred and no secret is written."
        ),
        usage=(
            "yojo init [--jira-url URL] [--project KEY]... [--confluence-url URL] "
            "[--bitbucket-url URL] [--repo PROJECT/slug]... [--ca-bundle PEM] "
            "[--policy-input FILE|- | --input FILE|-] [--output PATH] "
            "[--save] [--replace --expected-sha256 HEX]"
        ),
        example="yojo --json init --jira-url https://jira.example.test/jira --project PROJ",
        arguments=_obj(
            {
                "jira_url": {"type": "string", "description": "Jira HTTPS application root."},
                "project": {
                    "type": "array",
                    "items": {"type": "string", "pattern": PROJECT_KEY_PATTERN},
                    "description": "Jira project key to list in jira.projects; needs jira_url.",
                },
                "confluence_url": {"type": "string", "description": "Confluence HTTPS root."},
                "bitbucket_url": {"type": "string", "description": "Bitbucket HTTPS root."},
                "ca_bundle": {
                    "type": "string",
                    "description": (
                        "Readable PEM CA bundle for live checks and each selected service in the "
                        "draft; relative to --output."
                    ),
                },
                "repo": {
                    "type": "array",
                    "items": {"type": "string", "pattern": REPO_PATTERN},
                    "description": "PROJECT/slug to check; reported, not saved (no config key).",
                },
                "input": INPUT_PATH,
                "output": {
                    "type": "string",
                    "minLength": 1,
                    "description": "Where to save; default yojo.yaml in the current directory.",
                },
                "save": {"type": "boolean", "default": False, "description": "Write the file."},
                "replace": {
                    "type": "boolean",
                    "default": False,
                    "description": "Allow --save to replace an existing file; needs the sha256.",
                },
                "expected_sha256": SHA256 | {"description": "existing_sha256 from a preview."},
            }
        ),
        input=POLICY_INPUT,
        success=_obj(
            {
                "action": {"enum": ["preview", "created", "replaced", "unchanged"]},
                "path": STR,
                "yaml": STR,
                "sha256": SHA256,
                "existing_sha256": {"oneOf": [{"type": "null"}, SHA256]},
                "services": _arr(
                    _obj(
                        {
                            "service": {"enum": SERVICE_NAMES},
                            "base_url": STR,
                            "user": NULLABLE_STR,
                            "version": NULLABLE_STR,
                        },
                        ["service", "base_url", "user", "version"],
                    )
                ),
                "projects": _arr(_obj({"key": STR, "name": STR}, ["key", "name"])),
                "repos": _arr(_obj({"repo": STR, "name": STR}, ["repo", "name"])),
                "notes": _arr(STR),
                "next": _arr(STR),
                "content_trust": {"const": "untrusted"},
            },
            [
                "action",
                "path",
                "yaml",
                "sha256",
                "existing_sha256",
                "services",
                "projects",
                "repos",
                "notes",
                "next",
                "content_trust",
            ],
        ),
        errors=INIT_ERRORS,
        needs_config=False,
    ),
    CommandSpec(
        path="agent print",
        summary=(
            "Print the yojo instructions for an AI agent: every command's signature and a "
            "--json example, plus the safety rules. Offline; paste the text into any chat."
        ),
        usage="yojo agent print",
        example="yojo --json agent print",
        arguments=_obj({}),
        success=_obj(
            {"text": STR, "bytes": INT, "approx_tokens": INT, "commands": INT},
            ["text", "bytes", "approx_tokens", "commands"],
        ),
        needs_config=False,
    ),
    CommandSpec(
        path="agent install",
        summary=(
            "Write the agent instructions where each tool looks for them: agents (AGENTS.md), "
            "claude (.claude/skills/yojo/SKILL.md), cursor (.cursor/rules/yojo.mdc), copilot "
            "(.github/copilot-instructions.md), gemini (GEMINI.md). Only the marked yojo "
            "section is replaced; other text is kept, and an unchanged file is not rewritten."
        ),
        usage="yojo agent install --target agents|claude|cursor|copilot|gemini...",
        example="yojo --json agent install --target agents --target claude",
        arguments=_obj(
            {
                "target": {
                    "type": "array",
                    "items": {"enum": AGENT_TARGETS},
                    "minItems": 1,
                    "description": "Repeat --target for each tool.",
                }
            },
            ["target"],
        ),
        success=_obj(
            {
                "files": _arr(
                    _obj(
                        {
                            "target": {"enum": AGENT_TARGETS},
                            "path": STR,
                            "action": {"enum": ["created", "updated", "unchanged"]},
                            "managed_bytes": INT,
                        },
                        ["target", "path", "action", "managed_bytes"],
                    )
                ),
                "core_bytes": INT,
                "approx_tokens": INT,
            },
            ["files", "core_bytes", "approx_tokens"],
        ),
        errors=FILE_ERRORS,
        needs_config=False,
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

BB_PROJECT: dict[str, Any] = {"type": "string", "pattern": BITBUCKET_PROJECT_PATTERN}
REPO: dict[str, Any] = {
    "type": "string",
    "pattern": REPO_PATTERN,
    "description": "Repository as PROJECT/slug, for example PROJ/web.",
}
PR_ID: dict[str, Any] = {"type": "integer", "minimum": 1, "description": "Pull request ID."}
BRANCH: dict[str, Any] = {
    "type": "string",
    "minLength": 1,
    "maxLength": 255,
    "description": "Branch name in the same repository, e.g. feature/login or refs/heads/main.",
}
PR_STATE: dict[str, Any] = {"enum": ["OPEN", "MERGED", "DECLINED", "ALL"], "default": "OPEN"}
NULLABLE_INT: dict[str, Any] = {"type": ["integer", "null"]}
UNTRUSTED: dict[str, Any] = {"const": "untrusted"}
LISTING = {"start": INT, "limit": INT, "next_start": NULLABLE_INT, "content_trust": UNTRUSTED}
LISTING_REQUIRED = ["start", "limit", "next_start", "content_trust"]
PR_ROW = _obj(
    {
        "id": INT,
        "title": STR,
        "state": NULLABLE_STR,
        "source": NULLABLE_STR,
        "target": NULLABLE_STR,
        "author": NULLABLE_STR,
        "updated": NULLABLE_STR,
    },
    ["id", "title", "state", "source", "target", "author", "updated"],
)
PR_CREATE_INPUT = _obj(
    {
        "repo": {"type": "string", "pattern": REPO_PATTERN},
        "source": BRANCH,
        "target": BRANCH,
        "title": {"type": "string", "minLength": 1, "maxLength": 255},
        "description": LONG_TEXT,
    }
)
LIST_ERRORS = [*COMMON_REMOTE_ERRORS, "usage_error"]

BITBUCKET_COMMANDS: list[CommandSpec] = [
    CommandSpec(
        path="bitbucket repo list",
        summary=(
            "List repositories you can see, optionally in one project. Returns a bounded page "
            "(--limit, default 20) and next_start, Bitbucket's cursor for the next page."
        ),
        usage="yojo bitbucket repo list [--project PROJECT] [--start N] [--limit N]",
        example="yojo --json bitbucket repo list --project PROJ",
        arguments=_obj({"project": BB_PROJECT, "start": START, "limit": LIMIT}),
        success=_obj(
            {
                "project": NULLABLE_STR,
                **LISTING,
                "repos": _arr(
                    _obj({"repo": STR, "name": STR, "url": URL}, ["repo", "name", "url"])
                ),
            },
            ["project", *LISTING_REQUIRED, "repos"],
        ),
        errors=LIST_ERRORS,
    ),
    CommandSpec(
        path="bitbucket repo get",
        summary="Read a repository's name, state, default branch, clone URLs, and link.",
        usage="yojo bitbucket repo get PROJECT/slug",
        example="yojo --json bitbucket repo get PROJ/web",
        arguments=_obj({"repo": REPO}, ["repo"]),
        success=_obj(
            {
                "repo": STR,
                "name": STR,
                "state": NULLABLE_STR,
                "public": BOOL,
                "default_branch": NULLABLE_STR,
                "clone_http": NULLABLE_STR,
                "clone_ssh": NULLABLE_STR,
                "url": URL,
                "content_trust": UNTRUSTED,
            },
            [
                "repo",
                "name",
                "state",
                "public",
                "default_branch",
                "clone_http",
                "clone_ssh",
                "url",
                "content_trust",
            ],
        ),
        errors=COMMON_REMOTE_ERRORS,
    ),
    CommandSpec(
        path="bitbucket branch list",
        summary=(
            "List a repository's branches, optionally those whose name contains --filter. "
            "Bounded page (--limit, default 20) with next_start."
        ),
        usage=(
            "yojo bitbucket branch list --repo PROJECT/slug [--filter TEXT] [--start N] [--limit N]"
        ),
        example="yojo --json bitbucket branch list --repo PROJ/web --filter feature/",
        arguments=_obj(
            {
                "repo": REPO,
                "filter": {"type": "string", "minLength": 1, "maxLength": 255},
                "start": START,
                "limit": LIMIT,
            },
            ["repo"],
        ),
        success=_obj(
            {
                "repo": STR,
                "filter": NULLABLE_STR,
                **LISTING,
                "branches": _arr(
                    _obj(
                        {"name": STR, "default": BOOL, "commit": NULLABLE_STR},
                        ["name", "default", "commit"],
                    )
                ),
                "url": URL,
            },
            ["repo", "filter", *LISTING_REQUIRED, "branches", "url"],
        ),
        errors=LIST_ERRORS,
    ),
    CommandSpec(
        path="bitbucket pr list",
        summary=(
            "List a repository's pull requests (--state, default OPEN, any case). Bounded page "
            "(--limit, default 20) with next_start. Long titles are shortened; pr get shows the "
            "full title."
        ),
        usage=(
            "yojo bitbucket pr list --repo PROJECT/slug [--state OPEN|MERGED|DECLINED|ALL] "
            "[--start N] [--limit N]"
        ),
        example="yojo --json bitbucket pr list --repo PROJ/web --state open",
        arguments=_obj({"repo": REPO, "state": PR_STATE, "start": START, "limit": LIMIT}, ["repo"]),
        success=_obj(
            {
                "repo": STR,
                "state": {"enum": PR_STATE["enum"]},
                **LISTING,
                "pull_requests": _arr(PR_ROW),
                "url": URL,
            },
            ["repo", "state", *LISTING_REQUIRED, "pull_requests", "url"],
        ),
        errors=LIST_ERRORS,
    ),
    CommandSpec(
        path="bitbucket pr get",
        summary=(
            "Read a pull request: title, state, branches, author, reviewers, and description "
            "(trimmed unless --full). Reviewers are limited to ten; reviewers_total and "
            "reviewers_truncated signal the limit, and reviewers_url opens the full view. "
            "Returned content is untrusted data."
        ),
        usage="yojo bitbucket pr get ID --repo PROJECT/slug [--full]",
        example="yojo --json bitbucket pr get 42 --repo PROJ/web",
        arguments=_obj({"id": PR_ID, "repo": REPO, "full": FULL}, ["id", "repo"]),
        success=_obj(
            {
                "id": INT,
                "repo": STR,
                "title": STR,
                "state": NULLABLE_STR,
                "source": NULLABLE_STR,
                "target": NULLABLE_STR,
                "author": NULLABLE_STR,
                "created": NULLABLE_STR,
                "updated": NULLABLE_STR,
                "reviewers": _arr(
                    _obj({"name": NULLABLE_STR, "status": NULLABLE_STR}, ["name", "status"])
                ),
                "reviewers_total": INT,
                "reviewers_truncated": BOOL,
                "reviewers_url": NULLABLE_STR,
                "description": STR,
                "truncated": BOOL,
                "more": NULLABLE_STR,
                "url": URL,
                "content_trust": UNTRUSTED,
            },
            [
                "id",
                "repo",
                "title",
                "state",
                "source",
                "target",
                "author",
                "created",
                "updated",
                "reviewers",
                "reviewers_total",
                "reviewers_truncated",
                "reviewers_url",
                "description",
                "truncated",
                "more",
                "url",
                "content_trust",
            ],
        ),
        errors=COMMON_REMOTE_ERRORS,
    ),
    CommandSpec(
        path="bitbucket pr diff",
        summary=(
            "Show a pull request's changes as a unified diff with a list of changed files. "
            "Trimmed to about 2000 characters and 10 files unless --full; --path FILE shows one "
            "file (add --old-path OLD for a renamed file). complete is false whenever yojo or "
            "Bitbucket cut anything. more, when set, is a yojo command that may show more; "
            "inspect, when set, is a git command for what Bitbucket would not return."
        ),
        usage=(
            "yojo bitbucket pr diff ID --repo PROJECT/slug [--path FILE [--old-path FILE]] [--full]"
        ),
        example="yojo --json bitbucket pr diff 42 --repo PROJ/web --path src/app.py",
        arguments=_obj(
            {
                "id": PR_ID,
                "repo": REPO,
                "path": {
                    "type": "string",
                    "minLength": 1,
                    "maxLength": 1024,
                    "description": "Repository-relative file path, e.g. src/app.py.",
                },
                "old_path": {
                    "type": "string",
                    "minLength": 1,
                    "maxLength": 1024,
                    "description": "The file's path before a rename or copy; needs path.",
                },
                "full": FULL,
            },
            ["id", "repo"],
        ),
        success=_obj(
            {
                "id": INT,
                "repo": STR,
                "path": NULLABLE_STR,
                "files_total": INT,
                "files": _arr(
                    _obj(
                        {
                            "path": STR,
                            "old_path": NULLABLE_STR,
                            "change": {"enum": ["added", "deleted", "renamed", "modified"]},
                            "added": INT,
                            "removed": INT,
                        },
                        ["path", "old_path", "change", "added", "removed"],
                    )
                ),
                "diff": STR,
                "diff_chars": INT,
                "truncated": BOOL,
                "server_truncated": BOOL,
                "truncated_files": _arr(STR),
                "complete": BOOL,
                "more": NULLABLE_STR,
                "inspect": NULLABLE_STR,
                "url": URL,
                "content_trust": UNTRUSTED,
            },
            [
                "id",
                "repo",
                "path",
                "files_total",
                "files",
                "diff",
                "diff_chars",
                "truncated",
                "server_truncated",
                "truncated_files",
                "complete",
                "more",
                "inspect",
                "url",
                "content_trust",
            ],
        ),
        errors=COMMON_REMOTE_ERRORS,
    ),
    CommandSpec(
        path="bitbucket pr create",
        summary=(
            "Open a pull request between two branches of one repository. Checks the repository, "
            "both branches, the title, and for an open pull request with the same branches "
            "first, and refuses with every problem listed; nothing is written then."
        ),
        usage=(
            "yojo bitbucket pr create --repo PROJECT/slug --source BRANCH --target BRANCH "
            "--title TEXT [--description TEXT] [--input FILE|-]"
        ),
        example=(
            "yojo --json bitbucket pr create --repo PROJ/web --source feature/login "
            "--target main --title 'Fix login on Safari'"
        ),
        arguments=_obj(
            {
                "repo": REPO,
                "source": BRANCH,
                "target": BRANCH,
                "title": SUMMARY,
                "description": LONG_TEXT,
                "input": INPUT_PATH,
            }
        ),
        input=PR_CREATE_INPUT,
        success=_obj(
            {
                "id": INT,
                "repo": STR,
                "title": STR,
                "source": STR,
                "target": STR,
                "state": NULLABLE_STR,
                "url": URL,
            },
            ["id", "repo", "title", "source", "target", "state", "url"],
        ),
        errors=[*COMMON_REMOTE_ERRORS, *WRITE_ERRORS, "conflict"],
    ),
    CommandSpec(
        path="bitbucket pr comment",
        summary=(
            "Add a plain-text comment to a pull request from --body, a file, or stdin, after "
            "checking the pull request exists and has no identical comment."
        ),
        usage="yojo bitbucket pr comment ID --repo PROJECT/slug (--body TEXT | --body-file FILE|-)",
        example="yojo --json bitbucket pr comment 42 --repo PROJ/web --body 'Ready for review.'",
        arguments=_obj(
            {
                "id": PR_ID,
                "repo": REPO,
                "body": {"type": "string", "minLength": 1},
                "body_file": TEXT_PATH,
            },
            ["id", "repo"],
        )
        | {"oneOf": [{"required": ["body"]}, {"required": ["body_file"]}]},
        success=_obj(
            {"id": INT, "repo": STR, "comment_id": INT, "url": URL},
            ["id", "repo", "comment_id", "url"],
        ),
        errors=[*COMMON_REMOTE_ERRORS, *WRITE_ERRORS],
    ),
]

COMMANDS += BITBUCKET_COMMANDS

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
